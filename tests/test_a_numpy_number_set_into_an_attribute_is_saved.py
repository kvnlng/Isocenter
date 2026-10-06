"""A numpy number set into an attribute is the Python number it equals, in
the graph and in the store (#926).

`inst.set_attr("0018,1150", np.int64(7))` -- what indexing an array or
`pixel_array.max()` hands back -- made the next `save(sync=True)` raise
`TypeError: Object of type int64 is not JSON serializable`, and every later
save and export of the session with it, because the instance stays unsaved
and the save is one transaction. `np.float64` and `np.str_` saved only
because they subclass `float` and `str`.

The owner ruled where it becomes a Python number (Q1 A):

- in `set_attr` (`isocenter.entities._python_value`), so the graph holds
  one representation and a live export reads what a reopened one reads;
- again in the store, `isocenter.persistence`'s encoder and its private
  tier, for a value written around `set_attr` (`attributes[...] =`), which
  the entity does not see.

Every expected value here is a literal, never "what the twin does at run
time". The private tier stores text (no VR was recorded for a value set by
hand), which is why a private `7` reopens as `'7'`, as a Python `int` does.
"""
import os
import sqlite3
import json

import numpy as np
import pydicom
import pytest
from pydicom.multival import MultiValue
from pydicom.valuerep import DSfloat, IS

from isocenter.entities import DicomItem, _python_value
from isocenter.persistence import isocenter_json_object_hook
from isocenter.session import DicomSession

from support.ct_small_files import study_uid, write_ct

STANDARD, PRIVATE = "0018,1150", "0011,1001"
OUTER, INNER = "0008,1140", "0040,a730"
SEQ = "0400,0500"
PID = "PAT-926"
UID = f"{study_uid('926')}.1.1"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _open(tmp_path):
    """One CT, ingested and saved, with an item nested two deep."""
    write_ct(tmp_path / "in" / "1.dcm", PID, "926")
    session = DicomSession(str(tmp_path / "s.db"))
    assert session.ingest(str(tmp_path / "in")).ingested == 1
    (inst,) = _instances(session)
    outer, inner = DicomItem(), DicomItem()
    inst.add_sequence_item(OUTER, outer)
    outer.add_sequence_item(INNER, inner)
    session.save(sync=True)
    return session, inst, inner


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _place(inst, inner, where):
    """(the item, the tag) a test writes to."""
    return {"standard": (inst, STANDARD), "nested": (inner, STANDARD),
            "private": (inst, PRIVATE)}[where]


def _reopened(tmp_path, where):
    with DicomSession(str(tmp_path / "s.db")) as session:
        (inst,) = _instances(session)
        inner = inst.sequences[OUTER].items[0].sequences[INNER].items[0]
        item, tag = _place(inst, inner, where)
        return item.attributes[tag]


def _typed(value):
    """`value` with the type of every atom, so `7 == 7.0 == True` cannot
    pass for one another."""
    if isinstance(value, list):
        return [_typed(v) for v in value]
    return (type(value), value)


def test_a_numpy_integer_set_through_set_attr_is_saved_as_an_int(tmp_path):
    session, inst, _inner = _open(tmp_path)
    with session:
        inst.set_attr(STANDARD, np.int64(7))
        assert type(inst.attributes[STANDARD]) is int
        assert inst.attributes[STANDARD] == 7
        # Red on main: `TypeError: Object of type int64 is not JSON
        # serializable`.
        session.save(sync=True)
        assert not inst.has_unsaved_changes
    assert _typed(_reopened(tmp_path, "standard")) == (int, 7)


VALUES = [
    # value, what the graph holds, what a private tag reopens as
    pytest.param(lambda: np.uint16(7), (int, 7), "7", id="uint16"),
    pytest.param(lambda: np.float32(1.5), (float, 1.5), "1.5", id="float32"),
    pytest.param(lambda: np.bool_(True), (bool, True), "True", id="bool_"),
    pytest.param(lambda: np.str_("x"), (str, "x"), "x", id="str_"),
    pytest.param(lambda: np.array(7), (int, 7), "7", id="0-d-array"),
    pytest.param(lambda: [np.int64(1), np.int64(2)],
                 [(int, 1), (int, 2)], ["1", "2"], id="list"),
    pytest.param(lambda: (np.int64(1), np.int64(2)),
                 [(int, 1), (int, 2)], ["1", "2"], id="tuple"),
    pytest.param(lambda: [np.float32(0.5), 2, np.array(3)],
                 [(float, 0.5), (int, 2), (int, 3)], ["0.5", "2", "3"],
                 id="mixed-list"),
]


@pytest.mark.parametrize("where", ["standard", "nested", "private"])
@pytest.mark.parametrize("make, held, private_text", VALUES)
def test_each_numpy_number_becomes_its_python_twin_and_is_saved(
        tmp_path, make, held, private_text, where):
    session, inst, inner = _open(tmp_path)
    item, tag = _place(inst, inner, where)
    with session:
        item.set_attr(tag, make())
        assert _typed(item.attributes[tag]) == held
        # Red on main for every standard and nested cell but `str_`.
        session.save(sync=True)
        assert not inst.has_unsaved_changes
    stored = _reopened(tmp_path, where)
    if where == "private":
        assert stored == private_text
    else:
        assert _typed(stored) == held


AROUND = [
    pytest.param("standard", lambda: np.int64(7), (int, 7), id="standard"),
    pytest.param("nested", lambda: np.float32(1.5), (float, 1.5), id="nested"),
    pytest.param("standard", lambda: [np.int64(1), [np.uint8(2)]],
                 [(int, 1), [(int, 2)]], id="inside-a-list"),
    pytest.param("standard", lambda: np.array(7), (int, 7), id="0-d-array"),
]


@pytest.mark.parametrize("where, make, stored", AROUND)
def test_a_numpy_number_written_around_the_entity_is_saved(
        tmp_path, where, make, stored):
    """`attributes[...] =` is not seen by the entity, so the store's
    encoder converts too. Kills the conversion at `set_attr` only."""
    session, inst, inner = _open(tmp_path)
    item, tag = _place(inst, inner, where)
    with session:
        item.attributes[tag] = make()
        inst.mark_modified()
        # Red on main: the same `TypeError`.
        session.save(sync=True)
    assert _typed(_reopened(tmp_path, where)) == stored


@pytest.mark.parametrize("make, text", [
    (lambda: np.int64(7), "7"),
    (lambda: [np.int64(1)], ["1"]),
    # `str(np.float32(0.1))` is `'0.1'`; the float it equals is
    # 0.10000000149011612, which is what `set_attr` would have stored.
    (lambda: np.float32(0.1), "0.10000000149011612"),
], ids=["int64", "list", "float32"])
def test_a_private_numpy_number_written_around_the_entity_is_stored_as_its_twins_text(
        tmp_path, make, text):
    """The private tier never reaches `json`: it stores `str()` of each
    atom, so it converts for itself. Pins the third caller."""
    session, inst, _inner = _open(tmp_path)
    with session:
        inst.attributes[PRIVATE] = make()
        inst.mark_modified()
        session.save(sync=True)
    assert _reopened(tmp_path, "private") == text


def test_a_lock_over_a_numpy_value_written_around_the_entity_is_stored(tmp_path):
    """`lock_identities(persist=True)` writes through `update_attributes`,
    the encoder's other caller. Red on main: json's `TypeError`, after
    the token was embedded in memory."""
    session, inst, _inner = _open(tmp_path)
    db = str(tmp_path / "s.db")
    with session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        inst.attributes[STANDARD] = np.int64(7)
        session.lock_identities(PID, persist=True)
        with sqlite3.connect(db) as conn:
            ((uid, attrs),) = conn.execute(
                "SELECT sop_instance_uid, attributes_json FROM instances").fetchall()
        stored = json.loads(attrs, object_hook=isocenter_json_object_hook)
        assert uid == UID
        assert SEQ in stored["__sequences__"]
        assert _typed(stored[STANDARD]) == (int, 7)


def _element(folder, tag):
    """(VR, value bytes) of `tag` in the one file under `folder`."""
    (path,) = [os.path.join(r, f) for r, _d, fs in os.walk(folder)
               for f in fs if f.endswith(".dcm")]
    raw = pydicom.dcmread(path).get_item(tag)
    return str(raw.VR), bytes(raw.value)


def _accounting(db):
    with sqlite3.connect(db) as conn:
        return sorted(conn.execute(
            "SELECT action_type, details FROM audit_log "
            "WHERE action_type IN ('WARNING', 'DATA_LOSS', 'ERROR')").fetchall())


def test_a_live_export_writes_what_a_reopened_export_writes(tmp_path):
    """The reason the graph holds the Python number. On main the save
    raised; and a numpy object left in the graph exported differently from
    the number it reopens as: dropped under `US or SS` and under a private
    tag, and written as a 19-character DS with no row. The compressed
    export is the one that names its VRs on the wire."""
    session, inst, _inner = _open(tmp_path)
    db = str(tmp_path / "s.db")
    with session:
        inst.set_attr("0028,0106", np.int64(7))
        inst.set_attr(PRIVATE, np.int64(7))
        inst.set_attr("0028,1050", np.float32(0.1))
        assert session.export(str(tmp_path / "live"), use_compression=True).written == 1
    live_rows = _accounting(db)
    with DicomSession(db) as reopened:
        assert reopened.export(str(tmp_path / "again"), use_compression=True).written == 1
    both_rows = _accounting(db)

    expected = {0x00280106: ("SS", b"\x07\x00"), 0x00111001: ("LO", b"7 "),
                0x00281050: ("DS", b"0.10000000149012")}
    for tag, written in expected.items():
        assert _element(tmp_path / "live", tag) == written
        assert _element(tmp_path / "again", tag) == written
    # Each export said the same things: the second run's rows are the
    # first run's again.
    assert live_rows and both_rows == sorted(live_rows * 2)
    assert [d for a, d in live_rows if a == "DATA_LOSS"] == []


def test_a_numpy_descriptor_edit_under_resident_pixels_is_stored_as_an_int(tmp_path):
    """`Instance.set_attr` judges a descriptor edit against the resident
    array before it writes. Rows 4 over a 4x4 array reads as it read, so
    it is a plain write, of the `int`."""
    session, inst, _inner = _open(tmp_path)
    with session:
        pixels = np.zeros((4, 4), dtype=np.uint16)
        inst.set_pixel_data(pixels)
        inst.set_attr("0028,0010", np.int64(4))
        assert _typed(inst.attributes["0028,0010"]) == (int, 4)
        assert inst.pixel_array is pixels
        session.save(sync=True)
    with DicomSession(str(tmp_path / "s.db")) as reopened:
        (stored,) = _instances(reopened)
        assert _typed(stored.attributes["0028,0010"]) == (int, 4)


CONTROLS = [
    pytest.param(lambda: DSfloat("1.50"), id="DSfloat"),
    pytest.param(lambda: IS("7"), id="IS"),
    pytest.param(lambda: MultiValue(int, [1, 2]), id="MultiValue"),
    pytest.param(lambda: b"\x01\x02", id="bytes"),
    pytest.param(lambda: [1, 2], id="list"),
    pytest.param(lambda: (1, 2), id="tuple"),
    pytest.param(lambda: [[np.int64(1)]], id="list-of-lists"),
]


@pytest.mark.parametrize("make", CONTROLS)
def test_a_value_with_no_numpy_member_is_stored_as_the_object_given(make):
    """`set_attr` copies nothing it does not have to. Kills a helper that
    rebuilds every list, converts by `float()`, or unwraps a `MultiValue`.
    A list is scanned one level deep, which is what `set_attr` is charged
    for; a nested list's members are the store's to convert."""
    item, value = DicomItem(), make()
    item.set_attr(STANDARD, value)
    assert item.attributes[STANDARD] is value
    assert _python_value(value) is value


NOT_NUMBERS = [
    pytest.param(lambda: np.array([7]), "ndarray", id="1-d-array"),
    pytest.param(lambda: np.datetime64("2020-01-02"), "datetime64", id="datetime64"),
    pytest.param(lambda: np.complex64(1j), "complex64", id="complex64"),
]


@pytest.mark.parametrize("make, type_name", NOT_NUMBERS)
def test_a_numpy_value_that_is_not_a_number_the_store_holds_is_left_as_given(
        tmp_path, make, type_name):
    """`.item()` of a datetime64 is a `date`, of a complex64 a `complex`:
    neither is a value the store holds, so neither is converted, and the
    save refuses it by the name the caller gave it (#775). A 1-d array
    could mean numbers or bytes; the caller says which. Kills "convert
    whatever `.item()` returns" and "listify arrays"."""
    session, inst, _inner = _open(tmp_path)
    value = make()
    with session:
        inst.set_attr(STANDARD, value)
        assert inst.attributes[STANDARD] is value
        with pytest.raises(TypeError) as refused:
            session.save(sync=True)
        assert f"holds a {type_name} at {STANDARD}," in str(refused.value)
        assert inst.has_unsaved_changes

"""A value the store cannot hold is refused by the save, whole, by name (#775).

`set_attr` takes any object. The store's JSON does not: a `set`, a `date`,
a `Decimal`, a pydicom `PersonName`, a numpy array of one or more
dimensions or an arbitrary object made `save(sync=True)`, `export()`,
`compact()` and `lock_identities(persist=True)` raise `TypeError: Object of
type X is not JSON serializable`, which names a type and neither the
instance nor the tag, for every later save of the session too. On a
private tag the same value *saved*, as `str(value)`: `'{1, 2}'`,
`'[1 2 3]'`, `'<object object at 0x...>'`.

The owner ruled (Q2 A, Q3 A): the class stays `TypeError`; the message
names the instance, the tag and the type, and never the value; nothing is
saved and no audit row is written; a private tag follows the same rule.
`isocenter.persistence` does the naming, and `isocenter.session`'s doors
let it through.

Every message is asserted whole against a literal.
"""
import logging
import os
import sqlite3
from datetime import date
from decimal import Decimal

import numpy as np
import pydicom
import pytest
from pydicom.valuerep import DSfloat, IS, PersonName

from isocenter.entities import DicomItem
from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession

from support.ct_small_files import study_uid, write_ct

STANDARD, PRIVATE = "0018,1150", "0011,1001"
OUTER, INNER = "0008,1140", "0040,a730"
NESTED = f"{OUTER}[0] > {INNER}[0] > {STANDARD}"
SEQ = "0400,0500"
PID = "PAT-775"
UID = f"{study_uid('7751')}.1.1"
UID_2 = f"{study_uid('7752')}.1.1"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _message(uid, type_name, path, private=False):
    """The refusal, whole. `private` for a private tag at the top level
    of an instance, which the private-attribute table holds under a
    narrower rule than the JSON a standard tag is stored as."""
    rule = ("A private attribute at the top level of an instance is None, a "
            "str, bytes, a bool, an int, a float, a pydicom DS or IS value, "
            "or one list of those holding no bytes and no list."
            if private else
            "An attribute's value is None, a str, bytes, a bool, an int, a "
            "float, a pydicom DS or IS value, or a list of those.")
    return (f"save: instance {uid} holds a {type_name} at {path}, which the "
            f"store cannot hold. {rule} Nothing was saved. Set a value of "
            "one of those types and save again.")


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _open(tmp_path, files=1):
    """`files` CTs, ingested and saved; the first holds an item nested two
    deep. Returns the session, the first instance and that item."""
    for n in range(1, files + 1):
        write_ct(tmp_path / "in" / f"{n}.dcm", PID, f"775{n}")
    session = DicomSession(str(tmp_path / "s.db"))
    assert session.ingest(str(tmp_path / "in")).ingested == files
    inst = next(i for i in _instances(session) if i.sop_instance_uid == UID)
    outer, inner = DicomItem(), DicomItem()
    inst.add_sequence_item(OUTER, outer)
    outer.add_sequence_item(INNER, inner)
    session.save(sync=True)
    return session, inst, inner


def _stored(db):
    with sqlite3.connect(db) as conn:
        return dict(conn.execute(
            "SELECT sop_instance_uid, attributes_json FROM instances"))


def _rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT action_type FROM audit_log WHERE action_type IN "
            "('ERROR', 'WARNING', 'DATA_LOSS')").fetchall()


VALUES = [
    pytest.param(lambda: {1, 2}, "set", id="set"),
    pytest.param(lambda: date(2020, 1, 2), "date", id="date"),
    pytest.param(lambda: Decimal("1.5"), "Decimal", id="Decimal"),
    pytest.param(lambda: PersonName("SECRETNAME^X"), "PersonName", id="PersonName"),
    pytest.param(lambda: np.array([1, 2, 3]), "ndarray", id="ndarray"),
    pytest.param(lambda: object(), "object", id="object"),
    pytest.param(lambda: [1, [2, bytearray(b"SECRETNAME")]], "bytearray",
                 id="inside-a-list"),
]


@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "nested"])
@pytest.mark.parametrize("make, type_name", VALUES)
def test_the_save_names_the_instance_the_tag_and_the_type(
        tmp_path, make, type_name, nested):
    session, inst, inner = _open(tmp_path)
    db = str(tmp_path / "s.db")
    before = _stored(db)
    with session:
        (inner if nested else inst).set_attr(STANDARD, make())
        with pytest.raises(TypeError) as refused:
            session.save(sync=True)
        # Red on main: "Object of type set is not JSON serializable".
        assert str(refused.value) == _message(
            UID, type_name, NESTED if nested else STANDARD)
        assert "SECRETNAME" not in str(refused.value)
        assert inst.has_unsaved_changes
        assert _stored(db) == before
    # Nothing was lost and nothing was written, so there is no row.
    assert _rows(db) == []


def test_one_bad_value_saves_nothing_and_the_session_recovers_when_it_is_fixed(
        tmp_path):
    """All or nothing: the second instance's good edit is not stored
    either, both stay unsaved, and once the value is one the store holds
    the same save writes both."""
    session, inst, _inner = _open(tmp_path, files=2)
    db = str(tmp_path / "s.db")
    other = next(i for i in _instances(session) if i.sop_instance_uid == UID_2)
    before = _stored(db)
    with session:
        inst.set_attr(STANDARD, {1, 2})
        other.set_attr("0008,103e", "A GOOD EDIT")
        with pytest.raises(TypeError):
            session.save(sync=True)
        assert inst.has_unsaved_changes and other.has_unsaved_changes
        assert _stored(db) == before
        # An unrelated edit does not get the session past it.
        other.set_attr("0008,103e", "ANOTHER GOOD EDIT")
        with pytest.raises(TypeError) as again:
            session.save(sync=True)
        assert str(again.value) == _message(UID, "set", STANDARD)

        inst.set_attr(STANDARD, [1, 2])
        session.save(sync=True)
        assert not inst.has_unsaved_changes and not other.has_unsaved_changes
    with DicomSession(db) as reopened:
        held = {i.sop_instance_uid: i for i in _instances(reopened)}
        assert held[UID].attributes[STANDARD] == [1, 2]
        assert held[UID_2].attributes["0008,103e"] == "ANOTHER GOOD EDIT"


@pytest.mark.parametrize("door", [
    pytest.param(lambda s, out: s.export(out), id="export-dicom"),
    pytest.param(lambda s, out: s.export(out, format="wfdb"), id="export-wfdb"),
    pytest.param(lambda s, out: s.compact(), id="compact"),
])
def test_the_doors_that_begin_with_a_save_raise_it_before_any_file_is_exported(
        tmp_path, door):
    session, inst, _inner = _open(tmp_path)
    out = str(tmp_path / "out")
    with session:
        inst.set_attr(STANDARD, {1, 2})
        with pytest.raises(TypeError) as refused:
            door(session, out)
        assert str(refused.value) == _message(UID, "set", STANDARD)
        assert not os.path.exists(out)


PRIVATE_REFUSED = [
    pytest.param(lambda: date(2020, 1, 2), "date", id="date"),
    pytest.param(lambda: {1, 2}, "set", id="set"),
    pytest.param(lambda: np.array([1, 2, 3]), "ndarray", id="ndarray"),
    pytest.param(lambda: object(), "object", id="object"),
    pytest.param(lambda: PersonName("SECRETNAME^X"), "PersonName", id="PersonName"),
    pytest.param(lambda: ["a", date(2020, 1, 2)], "date", id="inside-a-list"),
    # The two values the JSON rule's sentence would have been false for.
    pytest.param(lambda: [b"a", b"b"], "bytes", id="list-of-bytes"),
    pytest.param(lambda: [1, [2]], "list", id="list-in-a-list"),
]


@pytest.mark.parametrize("make, type_name", PRIVATE_REFUSED)
def test_a_private_tag_follows_the_same_rule(tmp_path, make, type_name):
    """Red on main: the save returned, and the value reopened as its
    `str()` -- `'2020-01-02'`, `'{1, 2}'`, `'[1 2 3]'`,
    `'<object object at 0x...>'`."""
    session, inst, _inner = _open(tmp_path)
    db = str(tmp_path / "s.db")
    before = _stored(db)
    with session:
        inst.set_attr(PRIVATE, make())
        with pytest.raises(TypeError) as refused:
            session.save(sync=True)
        assert str(refused.value) == _message(UID, type_name, PRIVATE,
                                              private=True)
        assert inst.has_unsaved_changes
        assert _stored(db) == before
    with DicomSession(db) as reopened:
        (stored,) = _instances(reopened)
        assert PRIVATE not in stored.attributes


UID_SIBLING = f"{UID}.2"


@pytest.mark.parametrize("bad, good", [(UID, UID_SIBLING), (UID_SIBLING, UID)],
                         ids=["first-of-the-series", "second-of-the-series"])
def test_a_private_tier_refusal_names_the_instance_that_holds_the_value(
        tmp_path, bad, good):
    """Two instances of **one series** with unsaved changes, one holding
    the value. A series' instances are written as one batch: the private
    tier writes after every `instances` row of the batch is in, and
    finds its instance by position in it. Whichever of the two holds the
    value is the one named. Kills "name the batch's first instance";
    two instances in two series would not, each being a batch of one."""
    write_ct(tmp_path / "in" / "1.dcm", PID, "7751")
    sibling = pydicom.dcmread(str(tmp_path / "in" / "1.dcm"))
    sibling.SOPInstanceUID = UID_SIBLING
    sibling.file_meta.MediaStorageSOPInstanceUID = UID_SIBLING
    sibling.save_as(str(tmp_path / "in" / "2.dcm"))
    session = DicomSession(str(tmp_path / "s.db"))
    assert session.ingest(str(tmp_path / "in")).ingested == 2
    session.save(sync=True)
    (series,) = [se for p in session.store.patients for st in p.studies
                 for se in st.series]
    held = {i.sop_instance_uid: i for i in series.instances}
    assert set(held) == {UID, UID_SIBLING}
    with session:
        held[good].set_attr("0008,103e", "A GOOD EDIT")
        held[good].set_attr(PRIVATE, "A GOOD PRIVATE VALUE")
        held[bad].set_attr(PRIVATE, {1, 2})
        with pytest.raises(TypeError) as refused:
            session.save(sync=True)
        assert str(refused.value) == _message(bad, "set", PRIVATE, private=True)
        held[bad].set_attr(PRIVATE, 7)


@pytest.mark.parametrize("tag, private", [(STANDARD, False), (PRIVATE, True)],
                         ids=["standard", "private"])
def test_a_refused_save_stores_no_row_and_moves_no_revision(
        tmp_path, tag, private):
    """What "nothing was saved" means, measured: no `instances` row and no
    private-attribute row changes, no audit row is written, and neither
    instance's revision or persisted revision moves -- with another
    instance holding unsaved pixels, whose frame the save's prepass may
    already have appended to the sidecar by the time the refusal comes.
    That frame is not a stored row; the next save reuses it. The
    sidecar's size is deliberately not asserted."""
    session, inst, _inner = _open(tmp_path, files=2)
    db = str(tmp_path / "s.db")
    other = next(i for i in _instances(session) if i.sop_instance_uid == UID_2)

    def vertical():
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT * FROM instance_attributes ORDER BY 1, 2, 3").fetchall()

    with session:
        other.set_pixel_data(other.get_pixel_data() + np.int16(1))
        inst.set_attr(tag, {1, 2})
        rows, private_rows = _stored(db), vertical()
        revisions = [(i._revision, i._persisted_revision) for i in (inst, other)]
        with pytest.raises(TypeError) as refused:
            session.save(sync=True)
        assert str(refused.value) == _message(UID, "set", tag, private=private)
        assert _stored(db) == rows and vertical() == private_rows
        assert [(i._revision, i._persisted_revision)
                for i in (inst, other)] == revisions
        assert inst.has_unsaved_changes and other.has_unsaved_changes
        assert _rows(db) == []
        # The same save, once the value is one the store holds.
        inst.set_attr(tag, 7)
        session.save(sync=True)
        assert not inst.has_unsaved_changes and not other.has_unsaved_changes


@pytest.mark.parametrize("make, text", [
    (lambda: 7, "7"), (lambda: 1.5, "1.5"), (lambda: "x", "x"),
    (lambda: None, None), (lambda: True, "True"),
    (lambda: DSfloat("1.50"), "1.50"), (lambda: IS("7"), "7"),
    (lambda: [1, "a", None], ["1", "a", None]),
], ids=["int", "float", "str", "None", "bool", "DSfloat", "IS", "list"])
def test_the_private_values_ingest_produces_still_save(tmp_path, make, text):
    session, inst, _inner = _open(tmp_path)
    with session:
        inst.set_attr(PRIVATE, make())
        session.save(sync=True)
        assert not inst.has_unsaved_changes
    with DicomSession(str(tmp_path / "s.db")) as reopened:
        (stored,) = _instances(reopened)
        assert stored.attributes[PRIVATE] == text


def test_a_background_save_logs_the_named_refusal_once(tmp_path, caplog):
    """`save()` returns before the write; its failure is the worker's
    log line, which now carries the instance and the tag."""
    session, inst, _inner = _open(tmp_path)
    with session:
        inst.set_attr(STANDARD, {1, 2})
        with caplog.at_level(logging.ERROR, logger="isocenter"):
            caplog.clear()
            session.save()
            session.persistence_manager.flush()
        failed = [r.getMessage() for r in caplog.records
                  if r.getMessage().startswith("Background save failed:")]
        assert failed == [
            "Background save failed: TypeError: " + _message(UID, "set", STANDARD)]
        assert inst.has_unsaved_changes
        inst.set_attr(STANDARD, 7)


def test_a_lock_over_such_a_value_raises_the_named_refusal_after_the_embed(tmp_path):
    """`lock_identities(persist=True)` writes through `update_attributes`.
    The token is embedded in memory before the write, as it is when the
    write fails for sqlite's reasons (#599): the instance holds it,
    unsaved, and the store does not."""
    session, inst, _inner = _open(tmp_path)
    db = str(tmp_path / "s.db")
    with session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        inst.attributes[STANDARD] = {1, 2}
        with pytest.raises(TypeError) as refused:
            session.lock_identities(PID, persist=True)
        assert str(refused.value) == _message(UID, "set", STANDARD)
        assert SEQ in inst.sequences and inst.has_unsaved_changes
        assert SEQ not in _stored(db)[UID]
        inst.attributes[STANDARD] = 7


def test_a_type_error_of_another_origin_is_not_renamed(tmp_path, monkeypatch):
    """The naming is a diagnosis: it walks the instance for a value the
    store cannot hold, and when it finds none the exception that was
    raised leaves unchanged. Kills "every `TypeError` becomes ours"."""
    session, inst, _inner = _open(tmp_path)
    with session:
        inst.set_attr("0008,103e", "A GOOD EDIT")
        real = SqliteStore._serialize_item

        def broken(self, item):
            raise TypeError("of another origin")

        monkeypatch.setattr(SqliteStore, "_serialize_item", broken)
        with pytest.raises(TypeError) as raised:
            session.save(sync=True)
        assert str(raised.value) == "of another origin"
        monkeypatch.setattr(SqliteStore, "_serialize_item", real)
        session.save(sync=True)

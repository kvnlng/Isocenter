"""The exporter writes a value as the Python value it equals (#938, #979, #940, #939).

`set_attr` gives the graph one representation of a number (#926), and the
store gives a reopened graph another chance at it: a numpy number comes
back as its Python twin and a `tuple` as a `list`. A value written around
the entity (`attributes[tag] = value`), or held by a hand-built graph, got
neither, and the exporter's readers each asked a different type question
of it. Measured on `main` at fd359eb3, one store exported two ways:

- `np.float32(0.1)` in a DS went out live as 19 characters with no row and
  PASS, and after a reopen as 16 characters with #723's `WARNING` (#979);
- `np.bool_(True)` under `US or SS`, and any numpy number under a private
  tag, was dropped live and written after a reopen (#938);
- `set_attr(tag, (1, 2))` lost the element under IS and DS and failed the
  whole file under `US or SS` and FD, and the reopened store wrote the
  list (#940);
- a `Decimal` in an IS was written `1.5`, unrounded, with no row (#940);
- a number in a standard text VR failed the whole file (#939).

The owner's rulings of 2026-10-08: the exporter treats a value as the
Python value it equals (a numpy number its twin, a `tuple` the `list`, a
finite `Decimal` the `float`); and a number under a standard text VR loses
that element with one `DATA_LOSS` row, as PN and UI already did.

The property is the cell table: each cell exports the same element, the
same file bytes, the same rows and the same grade live, as its Python
twin, through `set_attr`, and after a save and a reopen. Every cell is
pinned to literal bytes first, because two exports that are equally wrong
are equal. Elements are read as written (`get_item`), never parsed.
"""
import datetime
import hashlib
import io
import os
import shutil
import sqlite3
import struct
from decimal import Decimal

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.multival import MultiValue
from pydicom.uid import UID, ExplicitVRLittleEndian
from pydicom.valuerep import DSdecimal, DSfloat, IS

from isocenter.entities import DicomItem
from isocenter.io_handlers import DicomExporter, ExportError, _export_value
from isocenter.session import DicomSession

IS_TAG, DS_TAG, US_SS_TAG, FD_TAG = "0018,1150", "0028,1050", "0028,0106", "0018,9087"
LO_TAG, PRIVATE = "0008,1090", "0011,1001"
#: The sequence a nested cell's item sits in.
SEQ = "0008,1140"

#: One standard tag per text VR the gate covers (#939). None is stamped
#: from an owner, and CT_small carries none of them but Patient's Age.
TEXT_TAGS = {
    "LO": LO_TAG, "SH": "0008,0094", "CS": "0008,0061", "DA": "0018,1012",
    "TM": "0018,0027", "DT": "0008,0106", "AE": "0040,0001", "AS": "0010,1010",
    "LT": "0008,0108", "ST": "0008,0081", "UT": "0008,030e", "UC": "0008,0119",
    "UR": "0008,010e", "PN": "0008,009c", "UI": "0008,0062",
}


@pytest.fixture(autouse=True)
def _two_workers(monkeypatch):
    """`session.export()` always hands its instances to the recycling
    process pool, on both interpreters, so every `_run` below crosses a
    pickle. Two workers start faster than one per core."""
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")


def _instance(session):
    (patient,) = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _target(inst, nested):
    if not nested:
        return inst
    return inst.sequences[SEQ].items[0]


def _numbers(tag):
    return tuple(int(part, 16) for part in tag.split(","))


class _Result(dict):
    """A dict that prints as its verdict, so a failed assertion does not
    print the instance and the file."""

    def __repr__(self):
        return (f"<export written={self.get('written')!r} "
                f"grade={self.get('grade')} rows={self.get('rows')}>")


def _run(folder, edit, *, reopen=False):
    """One fresh store: ingest CT_small, `edit(instance, item, session)`,
    export compressed (so the file names its VRs).

    `item` is an empty item nested in `SEQ`, saved before the edit. With
    `reopen`, the store is saved and reopened between the edit and the
    export. Returns a dict: `written` (the count, or the `ExportError`),
    `file` (the one file's bytes, or None), `ds`, `rows` (every ERROR,
    WARNING and DATA_LOSS row as `(action, scope, details)`), `grade`.
    """
    folder.mkdir(parents=True)
    (folder / "in").mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(folder / "in" / "a.dcm"))
    db = str(folder / "s.db")
    out = folder / "out"
    result = _Result()
    session = DicomSession(db)
    try:
        assert session.ingest(str(folder / "in")).ingested == 1
        inst = _instance(session)
        inst.add_sequence_item(SEQ, DicomItem())
        session.save(sync=True)
        result["edit"] = edit(inst, _target(inst, True), session)
        if reopen:
            session.save(sync=True)
            session.close()
            session = DicomSession(db)
        try:
            result["written"] = session.export(
                str(out), use_compression=True, show_progress=False).written
        except ExportError as exc:
            result["written"] = exc
        session.generate_report(str(folder / "report.md"))
        result["dirty"] = _instance(session).has_unsaved_changes
        result["instance"] = _instance(session)
    finally:
        session.close()
    text = (folder / "report.md").read_text(encoding="utf-8")
    result["grade"] = [g for g in ("PASS", "REVIEW_REQUIRED", "FAIL")
                       if f"**{g}**" in text]
    with sqlite3.connect(db) as conn:
        result["rows"] = [
            (action, scope, (details or "").replace(str(folder), "<D>"))
            for action, scope, details in conn.execute(
                "SELECT action_type, loss_scope, details FROM audit_log WHERE "
                "action_type IN ('ERROR', 'WARNING', 'DATA_LOSS') ORDER BY rowid")]
    files = [os.path.join(r, f) for r, _d, fs in os.walk(str(out))
             for f in fs if f.endswith(".dcm")]
    result["file"], result["ds"] = None, None
    if files:
        (path,) = files
        with open(path, "rb") as fh:
            result["file"] = fh.read()
        result["ds"] = pydicom.dcmread(io.BytesIO(result["file"]))
    return result


def _element(result, tag, nested=False):
    """`(VR, value bytes)` of `tag` as written, or None when it is absent."""
    ds = result["ds"]
    assert ds is not None, result["written"]
    if nested:
        ds = ds[_numbers(SEQ)][0]
    raw = ds.get_item(_numbers(tag))
    if raw is None:
        return None
    return str(ds[_numbers(tag)].VR), bytes(raw.value)


def _same(result):
    """What two routes must agree on: the count, the bytes, every row,
    the grade."""
    written = result["written"]
    return (written if isinstance(written, int) else repr(written),
            hashlib.sha256(result["file"] or b"").hexdigest(),
            result["rows"], result["grade"])


def _around(tag, value, nested=False, mark=False):
    def edit(inst, item, _session):
        (item if nested else inst).attributes[tag] = value
        if mark:
            inst.mark_modified()
    return edit


def _through_set_attr(tag, value, nested=False):
    def edit(inst, item, _session):
        (item if nested else inst).set_attr(tag, value)
    return edit


_ROUNDED_IS = "cannot be written in an Integer String, and was rounded: "
_ROUNDED_DS = "cannot be written exactly, and was rounded to fit: "
_PASS, _REVIEW = ["PASS"], ["REVIEW_REQUIRED"]


def _not_text(kind, vr, tag=None):
    tag = tag or TEXT_TAGS[vr]
    return ("DATA_LOSS", f"Tag {tag} not exported (data loss): TypeError: "
                         f"a {kind} is not text, and {vr} holds text")


def _cell(name, tag, make, twin, element, rows=(), grade=_PASS, nested=False):
    return pytest.param(tag, nested, make, twin, element, list(rows), grade,
                        id=name)


# (tag, nested, the value, its Python twin, the element as written, the
# rows, the grade). The first 21 are the cells of #938's table that
# differed between a live export and its twin's at fd359eb3.
CELLS = [
    # `US or SS`: dropped live with "the value fits no numeric arm".
    _cell("us-ss/bool_", US_SS_TAG, lambda: np.bool_(True), lambda: True,
          ("SS", b"\x01\x00")),
    _cell("us-ss/0-d", US_SS_TAG, lambda: np.array(7), lambda: 7,
          ("SS", b"\x07\x00")),
    # DS: 19 characters live, no row, PASS (#979).
    _cell("ds/float32", DS_TAG, lambda: np.float32(0.1),
          lambda: 0.10000000149011612, ("DS", b"0.10000000149012"),
          [("WARNING", f"Tag {DS_TAG} (DS): a float longer than DS's 16 "
                       f"characters {_ROUNDED_DS}0.10000000149011612 as "
                       f"'0.10000000149012'.")], _REVIEW),
    # LO: two raw 8-byte integers live, no row, PASS; its twin failed the
    # file. Both are #939's one lost element now.
    _cell("lo/list-int64", LO_TAG, lambda: [np.int64(1), np.int64(2)],
          lambda: [1, 2], None, [_not_text("int", "LO")]),
    # LO scalars: the file failed both ways, and the ERROR row named
    # `numpy.int64` live and `int` for the twin.
    _cell("lo/int64", LO_TAG, lambda: np.int64(7), lambda: 7, None,
          [_not_text("int", "LO")]),
    _cell("lo/uint8", LO_TAG, lambda: np.uint8(200), lambda: 200, None,
          [_not_text("int", "LO")]),
    _cell("lo/float32-0.1", LO_TAG, lambda: np.float32(0.1),
          lambda: 0.10000000149011612, None, [_not_text("float", "LO")]),
    _cell("lo/float64", LO_TAG, lambda: np.float64(0.1), lambda: 0.1, None,
          [_not_text("float", "LO")]),
    _cell("lo/float32-1.5", LO_TAG, lambda: np.float32(1.5), lambda: 1.5, None,
          [_not_text("float", "LO")]),
    _cell("lo/bool_", LO_TAG, lambda: np.bool_(True), lambda: True, None,
          [_not_text("bool", "LO")]),
    _cell("lo/0-d", LO_TAG, lambda: np.array(7), lambda: 7, None,
          [_not_text("int", "LO")]),
    # Private, no recorded VR: dropped live under a PRIVATE row and
    # REVIEW_REQUIRED; the twin was written.
    _cell("private/int64", PRIVATE, lambda: np.int64(7), lambda: 7,
          ("LO", b"7 ")),
    _cell("private/uint8", PRIVATE, lambda: np.uint8(200), lambda: 200,
          ("LO", b"200 ")),
    _cell("private/float32-0.1", PRIVATE, lambda: np.float32(0.1),
          lambda: 0.10000000149011612, ("LO", b"0.10000000149011612 ")),
    _cell("private/float32-1.5", PRIVATE, lambda: np.float32(1.5), lambda: 1.5,
          ("LO", b"1.5 ")),
    _cell("private/bool_", PRIVATE, lambda: np.bool_(True), lambda: True,
          ("LO", b"True")),
    _cell("private/0-d", PRIVATE, lambda: np.array(7), lambda: 7, ("LO", b"7 ")),
    _cell("private/list-int64", PRIVATE, lambda: [np.int64(1), np.int64(2)],
          lambda: [1, 2], ("LO", b"1\\2 ")),
    # IS: the same bytes and grade; the row quoted numpy's repr.
    _cell("is/float32-0.1", IS_TAG, lambda: np.float32(0.1),
          lambda: 0.10000000149011612, ("IS", b"0 "),
          [("WARNING", f"Tag {IS_TAG} (IS): a number that is not an integer "
                       f"{_ROUNDED_IS}0.10000000149011612 as '0'.")], _REVIEW),
    _cell("is/float64", IS_TAG, lambda: np.float64(0.1), lambda: 0.1,
          ("IS", b"0 "),
          [("WARNING", f"Tag {IS_TAG} (IS): a number that is not an integer "
                       f"{_ROUNDED_IS}0.1 as '0'.")], _REVIEW),
    _cell("is/float32-1.5", IS_TAG, lambda: np.float32(1.5), lambda: 1.5,
          ("IS", b"2 "),
          [("WARNING", f"Tag {IS_TAG} (IS): a number that is not an integer "
                       f"{_ROUNDED_IS}1.5 as '2'.")], _REVIEW),
    # #940's tuple. Dropped under IS and DS, the whole file failed under
    # `US or SS` and FD, live and through `set_attr`; the reopened store
    # wrote the list.
    _cell("is/tuple", IS_TAG, lambda: (1, 2), lambda: [1, 2], ("IS", b"1\\2 ")),
    _cell("ds/tuple", DS_TAG, lambda: (1, 2), lambda: [1, 2],
          ("DS", b"1.0\\2.0 ")),
    _cell("us-ss/tuple", US_SS_TAG, lambda: (1, 2), lambda: [1, 2],
          ("SS", b"\x01\x00\x02\x00")),
    _cell("fd/tuple", FD_TAG, lambda: (1, 2), lambda: [1, 2],
          ("FD", struct.pack("<2d", 1.0, 2.0))),
    _cell("is/tuple-int64", IS_TAG, lambda: (np.int64(1), np.int64(2)),
          lambda: [1, 2], ("IS", b"1\\2 ")),
    _cell("nested-is/tuple", IS_TAG, lambda: (1, 2), lambda: [1, 2],
          ("IS", b"1\\2 "), nested=True),
    _cell("nested-ds/tuple", DS_TAG, lambda: (1, 2), lambda: [1, 2],
          ("DS", b"1.0\\2.0 "), nested=True),
    # Inside a sequence item: `_merge` runs once per item.
    _cell("nested-is/float32", IS_TAG, lambda: np.float32(1.5), lambda: 1.5,
          ("IS", b"2 "),
          [("WARNING", f"Tag ({SEQ}) > {IS_TAG} (IS): a number that is not an "
                       f"integer {_ROUNDED_IS}1.5 as '2'.")], _REVIEW,
          nested=True),
    _cell("nested-lo/int64", LO_TAG, lambda: np.int64(7), lambda: 7, None,
          [_not_text("int", "LO")], nested=True),
    # Controls: cells that agreed before, and still do.
    _cell("control/is-int64", IS_TAG, lambda: np.int64(7), lambda: 7,
          ("IS", b"7 ")),
    _cell("control/ds-list", DS_TAG, lambda: [1, 2], lambda: [1, 2],
          ("DS", b"1.0\\2.0 ")),
    _cell("control/fd-float32", FD_TAG, lambda: np.float32(0.1),
          lambda: 0.10000000149011612,
          ("FD", struct.pack("<d", 0.10000000149011612))),
    # Written before for the wrong reason: `_is_value_that_fits` rebuilt a
    # list because it rounded something, where `(1, 2)` was dropped.
    _cell("control/is-tuple-of-floats", IS_TAG, lambda: (1.5, 2.5),
          lambda: [1.5, 2.5], ("IS", b"2\\2 "),
          [("WARNING", f"Tag {IS_TAG} (IS): a number that is not an integer "
                       f"{_ROUNDED_IS}1.5 as '2', 2.5 as '2'.")], _REVIEW),
    _cell("control/lo-tuple-of-text", LO_TAG, lambda: ("A", "B"),
          lambda: ["A", "B"], ("LO", b"A\\B ")),
    _cell("control/private-tuple", PRIVATE, lambda: (1, 2), lambda: [1, 2],
          ("LO", b"1\\2 ")),
]


def _assert_cell(result, tag, nested, element, rows, grade):
    """The literal half: what the cell writes, says and grades."""
    assert result["written"] == 1, result["written"]
    assert _element(result, tag, nested) == element
    said = [(action, details) for action, _scope, details in result["rows"]]
    assert len(said) == len(rows), said
    for (action, details), (want_action, want_details) in zip(said, rows):
        assert action == want_action, said
        # The parent prefixes the instance; the sentence is whole.
        assert details.endswith(want_details), (details, want_details)
    assert result["grade"] == grade


@pytest.mark.parametrize("tag, nested, make, twin, element, rows, grade", CELLS)
def test_a_cell_exports_one_way_by_every_route(
        tmp_path, tag, nested, make, twin, element, rows, grade):
    """Live, as the Python twin, through `set_attr`, and saved and
    reopened: one element, one file, one set of rows, one grade."""
    live = _run(tmp_path / "live", _around(tag, make(), nested))
    _assert_cell(live, tag, nested, element, rows, grade)
    routes = {
        "twin": _around(tag, twin(), nested),
        "set_attr": _through_set_attr(tag, make(), nested),
    }
    for name, edit in routes.items():
        other = _run(tmp_path / name, edit)
        assert _element(other, tag, nested) == element, name
        assert _same(other) == _same(live), name
    reopened = _run(tmp_path / "reopened",
                    _around(tag, make(), nested, mark=True), reopen=True)
    assert _element(reopened, tag, nested) == element
    assert _same(reopened) == _same(live)


@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "nested"])
def test_a_numpy_float_in_a_ds_is_fitted_live_as_it_is_after_a_reopen(
        tmp_path, nested):
    """#979. The live export wrote `0.10000000149011612`, 19 characters,
    with no row and PASS; the same store reopened wrote 16 characters with
    #723's `WARNING` and REVIEW_REQUIRED."""
    where = f"({SEQ}) > " if nested else ""
    sentence = (f"Tag {where}{DS_TAG} (DS): a float longer than DS's 16 "
                f"characters {_ROUNDED_DS}0.10000000149011612 as "
                f"'0.10000000149012'.")
    for name, reopen in (("live", False), ("reopened", True)):
        result = _run(tmp_path / name,
                      _around(DS_TAG, np.float32(0.1), nested, mark=True),
                      reopen=reopen)
        assert result["written"] == 1
        assert _element(result, DS_TAG, nested) == ("DS", b"0.10000000149012")
        ((action, _scope, details),) = result["rows"]
        assert action == "WARNING" and details.endswith(sentence), details
        assert result["grade"] == _REVIEW


@pytest.mark.parametrize("tag, element", [
    (IS_TAG, ("IS", b"1\\2 ")),
    (DS_TAG, ("DS", b"1.0\\2.0 ")),
    (US_SS_TAG, ("SS", b"\x01\x00\x02\x00")),
    (FD_TAG, ("FD", struct.pack("<2d", 1.0, 2.0))),
], ids=["IS", "DS", "US-or-SS", "FD"])
def test_a_tuple_set_through_set_attr_is_written_as_the_list(
        tmp_path, tag, element):
    """#940. `set_attr(tag, (1, 2))` stores the tuple as given (#926 pins
    that) and the save writes a list, so one call exported two ways: the
    element lost under IS and DS and the whole file under `US or SS` and
    FD live, the list after a reopen. The graph still holds the tuple:
    the exporter converts what it writes, never what the graph holds."""
    value = (1, 2)
    result = _run(tmp_path / "live", _through_set_attr(tag, value))
    assert result["written"] == 1, result["written"]
    assert _element(result, tag) == element
    assert result["rows"] == []
    assert result["grade"] == _PASS
    assert result["instance"].attributes[tag] is value


@pytest.mark.parametrize("tag, make, twin, element, fragment, grade", [
    pytest.param(IS_TAG, lambda: Decimal("1.5"), lambda: 1.5, ("IS", b"2 "),
                 f"{_ROUNDED_IS}1.5 as '2'.", _REVIEW, id="IS"),
    pytest.param(IS_TAG, lambda: [Decimal("1.5"), Decimal("2")],
                 lambda: [1.5, 2.0], ("IS", b"2\\2 "),
                 f"{_ROUNDED_IS}1.5 as '2'.", _REVIEW, id="IS-list"),
    pytest.param(IS_TAG, lambda: (Decimal("1.5"), np.int64(2)),
                 lambda: [1.5, 2], ("IS", b"2\\2 "),
                 f"{_ROUNDED_IS}1.5 as '2'.", _REVIEW, id="IS-tuple-with-numpy"),
    pytest.param(DS_TAG, lambda: Decimal("0.1234567890123456"),
                 lambda: 0.1234567890123456, ("DS", b"0.12345678901235"),
                 f"{_ROUNDED_DS}0.1234567890123456 as '0.12345678901235'.",
                 _REVIEW, id="DS-18-characters"),
    pytest.param(DS_TAG, lambda: Decimal("1.5"), lambda: 1.5, ("DS", b"1.5 "),
                 None, _PASS, id="DS-short"),
    pytest.param(PRIVATE, lambda: Decimal("1.5"), lambda: 1.5, ("LO", b"1.5 "),
                 None, _PASS, id="private"),
    pytest.param(PRIVATE, lambda: Decimal("7"), lambda: 7.0, ("LO", b"7.0 "),
                 None, _PASS, id="private-whole"),
    pytest.param(FD_TAG, lambda: Decimal("1.5"), lambda: 1.5,
                 ("FD", struct.pack("<d", 1.5)), None, _PASS, id="FD"),
])
def test_a_decimal_is_written_as_the_float_it_equals(
        tmp_path, tag, make, twin, element, fragment, grade):
    """#940, ruling Q3 A. A `Decimal` reaches the exporter around the
    entity (the save refuses one by name, #775). In an IS it was written
    `1.5`, in a DS as its 18 characters, both with no row and PASS; under
    a private tag it was dropped. It is the `float` it equals now: rounded
    with #897's row, fitted with #723's, written `LO` as the float's
    text."""
    live = _run(tmp_path / "live", _around(tag, make()))
    assert live["written"] == 1
    assert _element(live, tag) == element
    if fragment is None:
        assert live["rows"] == []
    else:
        ((action, _scope, details),) = live["rows"]
        assert action == "WARNING" and details.endswith(fragment), details
    assert live["grade"] == grade
    as_float = _run(tmp_path / "twin", _around(tag, twin()))
    assert _same(as_float) == _same(live)


def test_a_decimal_set_through_set_attr_is_still_refused_at_the_save(tmp_path):
    """The exporter's answer is for a `Decimal` the store never met. One
    set through the entity is refused by name at the export's leading
    save, as before (#775)."""
    (tmp_path / "in").mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(tmp_path / "in" / "a.dcm"))
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        inst = _instance(session)
        inst.set_attr(IS_TAG, Decimal("1.5"))
        with pytest.raises(TypeError) as raised:
            session.export(str(out), use_compression=True, show_progress=False)
        # So the session can close: the value the save refuses is taken out.
        inst.set_attr(IS_TAG, 1)
    message = str(raised.value)
    assert "Decimal" in message and IS_TAG in message, message
    assert not list(out.rglob("*.dcm"))


@pytest.mark.parametrize("make, kind", [
    (lambda: Decimal("sNaN"), "Decimal"),
    (lambda: Decimal("NaN"), "Decimal"),
    (lambda: Decimal("Infinity"), "Decimal"),
    (lambda: [Decimal("1.5"), Decimal("-Infinity")], "list"),
], ids=["sNaN", "NaN", "Infinity", "list-with-Infinity"])
def test_a_decimal_that_is_no_number_costs_its_element_not_the_file(
        tmp_path, make, kind):
    """`float(Decimal("sNaN"))` raises, and the conversion runs outside
    the per-element `try`. A non-finite `Decimal` is left a `Decimal`,
    and under a private tag that is one PRIVATE loss row."""
    result = _run(tmp_path / "live", _around(PRIVATE, make()))
    assert result["written"] == 1, result["written"]
    assert _element(result, PRIVATE) is None
    ((action, scope, details),) = result["rows"]
    assert (action, scope) == ("DATA_LOSS", "PRIVATE")
    assert details.endswith(f"Tag {PRIVATE} not exported (data loss): "
                            f"ValueError: no VR fits a {kind} value"), details


def _merged(attrs, vrs=None):
    """`_merge` alone, written Explicit VR to memory and read back raw."""
    ds, losses, corrections, warnings = Dataset(), [], [], []
    DicomExporter._merge(ds, attrs, losses, vrs, corrections=corrections,
                         warnings=warnings)
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=False)
    buffer.seek(0)
    back = pydicom.dcmread(buffer, force=True)

    def raw(tag):
        item = back.get_item(_numbers(tag))
        return None if item is None else (str(back[_numbers(tag)].VR),
                                          bytes(item.value))
    return raw, losses, corrections + warnings


def test_a_source_decimal_string_is_not_the_callers_decimal():
    """pydicom's `DSdecimal` is a `Decimal` subclass carrying the file's
    own text. It is a source value, never converted: an `isinstance` test
    would make it a bare float and fit its 19 characters to 16."""
    source = DSdecimal("0.30000000000000004")
    assert source.original_string == "0.30000000000000004"
    raw, losses, notes = _merged({DS_TAG: source})
    assert raw(DS_TAG) == ("DS", b"0.30000000000000004 ")
    assert losses == [] and notes == []
    assert _export_value(source) is source
    # The caller's own, for contrast: fitted, and said.
    raw, losses, notes = _merged({DS_TAG: Decimal("0.30000000000000004")})
    assert raw(DS_TAG) == ("DS", b"0.30000000000000")
    assert losses == [] and len(notes) == 1


_NUMBERS_AS_TEXT = [
    pytest.param(lambda: 7, "int", id="int"),
    pytest.param(lambda: 1.5, "float", id="float"),
    pytest.param(lambda: True, "bool", id="bool"),
    pytest.param(lambda: [1, "SECRETNAME"], "int", id="list"),
]


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
@pytest.mark.parametrize("make, kind", _NUMBERS_AS_TEXT)
def test_a_number_under_a_text_vr_loses_that_element_and_the_file_is_written(
        tmp_path, make, kind, reopen):
    """#939, ruling Q1 A. `set_attr("0008,1090", 7)` failed the whole
    file: pydicom accepts the number at `add_new` and raises in its text
    writer, past the per-element `try`. PN and UI lost the element with
    pydicom's own sentence. All fifteen text VRs now say one sentence,
    naming the tag, the type and the VR and never the value, and every
    other element is written as a control export writes it."""
    def edit(inst, _item, _session):
        for tag in TEXT_TAGS.values():
            inst.set_attr(tag, make())

    control = _run(tmp_path / "control", lambda inst, item, session: None)
    result = _run(tmp_path / "out", edit, reopen=reopen)
    assert result["written"] == 1, result["written"]
    # `keys()` and `get_item`, so nothing is parsed: the bytes as written.
    lost = {pydicom.tag.Tag(_numbers(tag)) for tag in TEXT_TAGS.values()}
    kept = set(control["ds"].keys()) - lost
    assert len(kept) > 100 and set(result["ds"].keys()) == kept
    for tag in sorted(kept):
        assert (result["ds"].get_item(tag).value
                == control["ds"].get_item(tag).value), tag
    said = sorted((action, scope, details.split("Tag ", 1)[1])
                  for action, scope, details in result["rows"])
    assert said == sorted(
        ("DATA_LOSS", "STANDARD",
         f"{tag} not exported (data loss): TypeError: a {kind} is not text, "
         f"and {vr} holds text") for vr, tag in TEXT_TAGS.items())
    assert not any("SECRETNAME" in details for _a, _s, details in result["rows"])
    assert result["grade"] == _PASS


def test_the_issues_own_call_exports_the_file_without_the_model_name(tmp_path):
    """#939 as filed: one `set_attr`, one `export()`."""
    result = _run(tmp_path / "out", _through_set_attr(LO_TAG, 7))
    assert result["written"] == 1, result["written"]
    assert _element(result, LO_TAG) is None
    ((action, scope, details),) = result["rows"]
    assert (action, scope) == ("DATA_LOSS", "STANDARD")
    assert details.endswith(_not_text("int", "LO")[1]), details
    assert result["grade"] == _PASS


@pytest.mark.parametrize("vr, tag", sorted(TEXT_TAGS.items()))
@pytest.mark.parametrize("make, kind", [
    (lambda: np.array([7]), "ndarray"),
    (lambda: np.datetime64("2020-01-02"), "datetime64"),
    (lambda: np.ma.masked, "MaskedConstant"),
    (lambda: np.complex64(1j), "complex64"),
    (lambda: 1j, "complex"),
    (lambda: Decimal("NaN"), "Decimal"),
    (lambda: ["A", np.int64(7)], "int"),
    (lambda: MultiValue(float, [7.5]), "float"),
], ids=["ndarray", "datetime64", "masked", "complex64", "complex",
        "Decimal-NaN", "list-ending-in-int64", "MultiValue-of-float"])
def test_each_text_vr_refuses_each_kind_of_number_by_its_type(vr, tag, make, kind):
    """The gate, VR by VR, on `_merge` alone: a number of any kind, a
    numpy value #926 leaves unconverted and a non-finite `Decimal` are
    one loss row naming the type. No value is quoted."""
    raw, losses, notes = _merged({tag: make()})
    assert raw(tag) is None
    assert losses == [("STANDARD", f"Tag {tag} not exported (data loss): "
                                   f"TypeError: a {kind} is not text, and "
                                   f"{vr} holds text")]
    assert notes == []


@pytest.mark.parametrize("tag, make, element", [
    pytest.param(LO_TAG, lambda: "7", ("LO", b"7 "), id="LO-text-of-a-number"),
    pytest.param(LO_TAG, lambda: ["A", "B"], ("LO", b"A\\B "), id="LO-list"),
    pytest.param(LO_TAG, lambda: b"AB", ("LO", b"AB"), id="LO-bytes"),
    pytest.param(LO_TAG, lambda: np.str_("x"), ("LO", b"x "), id="LO-numpy-str"),
    pytest.param(LO_TAG, lambda: np.bytes_(b"AB"), ("LO", b"AB"),
                 id="LO-numpy-bytes"),
    pytest.param(LO_TAG, lambda: MultiValue(str, ["A", "B"]), ("LO", b"A\\B "),
                 id="LO-MultiValue"),
    pytest.param(LO_TAG, lambda: None, ("LO", b""), id="LO-None"),
    pytest.param(LO_TAG, lambda: "", ("LO", b""), id="LO-empty"),
    pytest.param(TEXT_TAGS["DA"], lambda: datetime.date(2020, 1, 2),
                 ("DA", b"20200102"), id="DA-date"),
    pytest.param(TEXT_TAGS["DA"], lambda: "not a date", ("DA", b"not a date"),
                 id="DA-text-that-is-no-date"),
    pytest.param(TEXT_TAGS["PN"], lambda: "Doe^Jane", ("PN", b"Doe^Jane"),
                 id="PN-text"),
    pytest.param(TEXT_TAGS["PN"],
                 lambda: pydicom.valuerep.PersonName("Doe^Jane"),
                 ("PN", b"Doe^Jane"), id="PN-PersonName"),
    pytest.param(TEXT_TAGS["UI"], lambda: UID("1.2.3"), ("UI", b"1.2.3\x00"),
                 id="UI-UID"),
    pytest.param(TEXT_TAGS["LO"], lambda: "x" * 70, ("LO", b"x" * 70),
                 id="LO-over-64-characters"),
])
def test_text_under_a_text_vr_is_written_as_it_was(tag, make, element):
    """The gate refuses numbers, not everything that is not a `str`:
    bytes, numpy text, a `MultiValue`, None, a `date` in a DA, an LO past
    its 64 characters and a DA that names no date are written as before."""
    raw, losses, notes = _merged({tag: make()})
    assert raw(tag) == element
    assert losses == [] and notes == []


@pytest.mark.parametrize("tag, make, element, fragment", [
    pytest.param(IS_TAG, lambda: np.array([7]), None,
                 "TypeError: only 0-dimensional arrays can be converted to "
                 "Python scalars", id="IS-1-d"),
    pytest.param(IS_TAG, lambda: np.datetime64("2020-01-02"), None,
                 "TypeError: int() argument must be a string, a bytes-like "
                 "object or a real number, not 'datetime.date'",
                 id="IS-datetime64"),
    pytest.param(IS_TAG, lambda: np.ma.masked, None,
                 "MaskError: Cannot convert masked element to a Python int.",
                 id="IS-masked"),
    pytest.param(PRIVATE, lambda: np.array([7]), None,
                 "ValueError: no VR fits a ndarray value", id="private-1-d"),
    pytest.param(PRIVATE, lambda: np.datetime64("2020-01-02"), None,
                 "ValueError: no VR fits a datetime64 value",
                 id="private-datetime64"),
    pytest.param(PRIVATE, lambda: np.ma.masked, None,
                 "ValueError: no VR fits a MaskedConstant value",
                 id="private-masked"),
    pytest.param(PRIVATE, lambda: [[np.int64(1)]], None,
                 "ValueError: no VR fits a list value", id="private-two-deep"),
])
def test_a_numpy_value_that_is_not_a_number_is_still_not_converted(
        tag, make, element, fragment):
    """#926's rule holds at the exporter: the dtype's kind decides, never
    what `.item()` returns. A 1-d array, a `datetime64` and a masked
    value export as they did, which here is one loss row each; and the
    conversion is one level deep, as `set_attr`'s is."""
    raw, losses, notes = _merged({tag: make()})
    assert raw(tag) == element
    ((_scope, loss),) = losses
    assert loss == f"Tag {tag} not exported (data loss): {fragment}"
    assert notes == []


def test_the_serializer_converts_what_it_writes_and_not_the_graph(
        tmp_path, monkeypatch):
    """`write_tree()` under threads is the one door where the worker holds
    the live graph (`session.export()` pickles it to a pool), and the door
    a store-less graph takes. The conversion rebinds a local: a value
    written back into `attributes` would be an edit no revision recorded.
    The file is the one the Python twins write."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    held = {IS_TAG: np.int64(7), DS_TAG: (1, 2), PRIVATE: Decimal("1.5"),
            US_SS_TAG: [np.int64(1), np.int64(2)], FD_TAG: np.float32(1.5)}
    twins = {IS_TAG: 7, DS_TAG: [1, 2], PRIVATE: 1.5, US_SS_TAG: [1, 2],
             FD_TAG: 1.5}
    nested = (np.int64(1), Decimal("2"))
    (tmp_path / "in").mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(tmp_path / "in" / "a.dcm"))
    written = {}
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        (patient,) = session.store.patients
        inst = _instance(session)
        item = DicomItem()
        inst.add_sequence_item(SEQ, item)
        session.save(sync=True)
        for name, values, inner in (("live", held, nested), ("twin", twins, [1, 2.0])):
            inst.attributes.update(values)
            item.attributes[IS_TAG] = inner
            revision = inst._revision
            DicomExporter.write_tree(patient, str(tmp_path / name),
                                     compression="j2k", show_progress=False)
            for tag, value in values.items():
                assert inst.attributes[tag] is value, (name, tag)
            assert item.attributes[IS_TAG] is inner
            assert inst._revision == revision
            (path,) = (tmp_path / name).rglob("*.dcm")
            written[name] = path.read_bytes()
        # So the session can close: the save refuses a `Decimal` by name.
        inst.attributes.update(twins)
    ds = pydicom.dcmread(io.BytesIO(written["live"]))
    assert bytes(ds.get_item(_numbers(IS_TAG)).value) == b"7 "
    assert bytes(ds.get_item(_numbers(DS_TAG)).value) == b"1.0\\2.0 "
    assert bytes(ds.get_item(_numbers(PRIVATE)).value) == b"1.5 "
    assert bytes(ds.get_item(_numbers(US_SS_TAG)).value) == b"\x01\x00\x02\x00"
    assert bytes(ds.get_item(_numbers(FD_TAG)).value) == struct.pack("<d", 1.5)
    assert bytes(ds[_numbers(SEQ)][0].get_item(_numbers(IS_TAG)).value) == b"1\\2 "
    assert written["live"] == written["twin"]


def test_merge_leaves_the_mapping_it_reads_as_it_was():
    """The same, on `_merge` alone: every value is the object it was."""
    attrs = {IS_TAG: np.int64(7), DS_TAG: (1, 2), PRIVATE: Decimal("1.5"),
             US_SS_TAG: [np.int64(1), np.int64(2)], LO_TAG: np.int64(7),
             FD_TAG: [Decimal("1.5")]}
    before = dict(attrs)
    ds, losses = Dataset(), []
    DicomExporter._merge(ds, attrs, losses)
    assert list(attrs) == list(before)
    for tag, value in before.items():
        assert attrs[tag] is value, tag
    assert len(losses) == 1 and ds[_numbers(IS_TAG)].value == 7


def test_a_numpy_value_on_an_owner_is_the_stamp_its_twin_is(tmp_path):
    """The three owner stamps go through the same `_merge`. A numpy number
    assigned to a `Patient`'s name is the Python number's loss row, word
    for word."""
    def assign(value):
        def edit(_inst, _item, session):
            (patient,) = session.store.patients
            patient.patient_name = value
        return edit

    live = _run(tmp_path / "live", assign(np.int64(7)))
    twin = _run(tmp_path / "twin", assign(7))
    assert live["written"] == 1, live["written"]
    ((action, scope, details),) = live["rows"]
    assert (action, scope) == ("DATA_LOSS", "STANDARD")
    assert details.endswith(_not_text("int", "PN", "0010,0010")[1]), details
    assert _same(twin) == _same(live)


def _typed(value):
    """`value` with the type of every atom, so `7 == 7.0 == True` and
    `(1, 2) != [1, 2]` cannot pass for one another."""
    if type(value) in (list, tuple):
        return (type(value), [_typed(member) for member in value])
    return (type(value), value)


@pytest.mark.parametrize("make, written", [
    (lambda: np.int64(7), 7),
    (lambda: np.uint8(200), 200),
    (lambda: np.bool_(True), True),
    (lambda: np.float32(1.5), 1.5),
    (lambda: np.float64(0.1), 0.1),
    (lambda: np.array(7), 7),
    (lambda: [np.int64(1), "A", 2.5], [1, "A", 2.5]),
    (lambda: (np.int64(1), np.int64(2)), [1, 2]),
    (lambda: (1, 2), [1, 2]),
    (lambda: (), []),
    (lambda: ("A", "B"), ["A", "B"]),
    (lambda: Decimal("1.5"), 1.5),
    (lambda: Decimal("7"), 7.0),
    (lambda: [Decimal("1.5"), 2, "A"], [1.5, 2, "A"]),
    (lambda: (Decimal("1.5"), np.int64(2)), [1.5, 2]),
    (lambda: [Decimal("1.5"), np.float32(1.5)], [1.5, 1.5]),
    (lambda: [1, 2.5, "A", True, None], [1, 2.5, "A", True, None]),
    # Every plain type and one numpy member: a list is plain only when
    # all of its members are, not when it holds one of each.
    (lambda: [1, 2.5, "A", True, None, np.int64(7)], [1, 2.5, "A", True, None, 7]),
    (lambda: [1, 2.5, "A", True, None, Decimal("1.5")],
     [1, 2.5, "A", True, None, 1.5]),
], ids=lambda value: None)
def test_the_value_the_writer_is_handed(make, written):
    """`_export_value`, by type as well as by value."""
    assert _typed(_export_value(make())) == _typed(written)


#: What `_export_value` hands back as the object it was given: a source's
#: values, bytes, and everything #926 says is not a number.
_UNTOUCHED = [
    7, 1.5, True, None, "CT", b"AB", bytearray(b"AB"), memoryview(b"AB"),
    DSfloat("1.50"), IS("7"), DSdecimal("1.50"), UID("1.2.3"),
    pydicom.tag.BaseTag(0x00100010), datetime.date(2020, 1, 2),
    pydicom.valuerep.PersonName("Doe^Jane"),
    [1, 2], ["A", "B"], [1, "A", 2.5, None, True], [], [[np.int64(1)]],
    [DSfloat("1.5"), DSfloat("2.5")], [IS("1"), IS("2")], [UID("1.2"), UID("1.3")],
    [DSdecimal("1.5"), 2], [b"A", b"B"],
    MultiValue(int, [1, 2]), MultiValue(DSfloat, ["1.5", "2.5"]),
    MultiValue(str, ["A", "B"]),
    Decimal("sNaN"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
    np.array([7]), np.array([1, 2, 3]), np.array([[1, 2], [3, 4]]),
    np.datetime64("2020-01-02"), np.timedelta64(5, "ns"), np.complex64(1j),
    np.ma.masked, np.ma.array(7, mask=True), np.ma.array([1, 2], mask=[0, 1]),
    np.str_("x"), np.bytes_(b"x"), 1j, object(), {"a": 1}, {1, 2},
    float("nan"), float("inf"), 10 ** 400,
]


@pytest.mark.parametrize("value", _UNTOUCHED, ids=lambda value: type(value).__name__)
def test_what_is_not_a_callers_number_tuple_or_decimal_is_handed_back_itself(value):
    """The same object: `original_string` rides on it, a `MultiValue` is
    never scanned, and a plain list (what a reopened store hands back for
    65,536 LUT entries) is never rebuilt."""
    assert _export_value(value) is value


@pytest.mark.parametrize("value", _UNTOUCHED + [
    [Decimal("sNaN"), Decimal("1.5")], (Decimal("Infinity"),),
    [np.ma.masked, np.int64(1)], (np.datetime64("2020-01-02"), 1),
    [object(), Decimal("1")], [None, np.array([1, 2])],
    (np.array([1, 2]), Decimal("NaN")), [float("nan"), Decimal("2")],
    [10 ** 400, np.float64("inf")], [Decimal(10) ** 400],
    MultiValue(int, []), [{}], [[Decimal("1")]], ((1, 2), (3, 4)),
], ids=lambda value: type(value).__name__)
def test_the_conversion_never_raises(value):
    """It runs outside the per-element `try`, where a raise costs the
    file. Every type the store was found to hold, everything #926 leaves
    alone, and the `Decimal`s `float()` refuses."""
    _export_value(value)
    # And through `_merge`, under a standard numeric VR, a text VR and a
    # private tag: an element may be lost; nothing is raised.
    for tag in (IS_TAG, LO_TAG, PRIVATE):
        DicomExporter._merge(Dataset(), {tag: value}, [])


def test_a_decimal_past_a_floats_range_is_the_float_python_makes_of_it():
    """`Decimal(10) ** 400` is finite and `float()` of it is `inf`, with
    no error. The conversion is `float()`, so that is what the writer is
    handed, and an IS or a DS then loses the element with the row any
    non-finite float gets. Pinned as measured, not as a design choice:
    the ruling's words are "the float it equals", and no float equals
    this one."""
    huge = Decimal(10) ** 400
    assert _typed(_export_value(huge)) == (float, float("inf"))
    for tag, words in ((IS_TAG, "inf has no Integer String spelling"),
                       (DS_TAG, "inf has no Decimal String spelling")):
        raw, losses, notes = _merged({tag: huge})
        assert raw(tag) is None
        ((_scope, loss),) = losses
        assert loss == f"Tag {tag} not exported (data loss): ValueError: {words}"


def test_a_whole_decimal_of_sixteen_digits_is_written_exactly_and_noted():
    """pydicom wrote it `1234567890123456.0`, 18 characters. As the float
    it takes #723's exact arm: the integer spelling, an INFO note, no
    row."""
    ds, losses, corrections, warnings = Dataset(), [], [], []
    DicomExporter._merge(ds, {DS_TAG: Decimal("1234567890123456")}, losses,
                         corrections=corrections, warnings=warnings)
    assert losses == [] and warnings == []
    assert corrections == [
        f"Tag {DS_TAG} (DS): a float longer than DS's 16 characters was "
        f"written in its integer spelling, the same number: "
        f"1234567890123456.0 as '1234567890123456'."]
    raw, _losses, _notes = _merged({DS_TAG: Decimal("1234567890123456")})
    assert raw(DS_TAG) == ("DS", b"1234567890123456")


def test_a_source_value_beside_a_callers_decimal_is_still_itself():
    """One list, two kinds: the caller's `Decimal` becomes a float and the
    source's `DSdecimal` beside it keeps its text."""
    source = DSdecimal("2.50")
    converted = _export_value([Decimal("1.5"), source])
    assert _typed(converted[:1]) == _typed([1.5])
    assert converted[1] is source and source.original_string == "2.50"
    raw, losses, notes = _merged({DS_TAG: [Decimal("1.5"), source]})
    assert raw(DS_TAG) == ("DS", b"1.5\\2.50")
    assert losses == [] and notes == []


def test_a_large_plain_list_is_not_rebuilt():
    big = list(range(65536))
    assert _export_value(big) is big
    multi = MultiValue(int, big)
    assert _export_value(multi) is multi
    # One numpy member anywhere in it is still found.
    big.append(np.int64(7))
    converted = _export_value(big)
    assert converted is not big and type(converted[-1]) is int
    assert type(big[-1]) is np.int64

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
import collections
import datetime
import enum
import fractions
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
from isocenter import io_handlers
from isocenter.io_handlers import DicomExporter, ExportError, _export_value
from isocenter.session import DicomSession

_Pair = collections.namedtuple("_Pair", "first second")


class _TupleSubclass(tuple):
    pass


class _ListSubclass(list):
    pass

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


def _run(folder, edit, *, reopen=False, source=None):
    """One fresh store: ingest CT_small, `edit(instance, item, session)`,
    export compressed (so the file names its VRs). `source(dataset)`,
    when given, edits the input file before it is ingested.

    `item` is an empty item nested in `SEQ`, saved before the edit. With
    `reopen`, the store is saved and reopened between the edit and the
    export. Returns a dict: `written` (the count, or the `ExportError`),
    `file` (the one file's bytes, or None), `ds`, `rows` (every ERROR,
    WARNING and DATA_LOSS row as `(action, scope, details)`), `grade`.
    """
    folder.mkdir(parents=True)
    (folder / "in").mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(folder / "in" / "a.dcm"))
    if source is not None:
        given = pydicom.dcmread(str(folder / "in" / "a.dcm"))
        source(given)
        given.save_as(str(folder / "in" / "a.dcm"))
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
def test_a_decimal_that_is_no_number_under_a_private_tag_costs_its_element(
        tmp_path, make, kind):
    """`float(Decimal("sNaN"))` raises, and the conversion runs outside
    the per-element `try`. A non-finite `Decimal` is left a `Decimal`,
    and under a private tag that is one PRIVATE loss row. Under a private
    tag only: `Decimal("sNaN")` under FD still fails the whole file, as it
    did before this change (pydicom packs it at `dcmwrite`; #986's
    class)."""
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
    (lambda: True, "bool"),
    (lambda: ["A", False], "bool"),
    (lambda: fractions.Fraction(1, 2), "Fraction"),
    (lambda: _ListSubclass(["A", 7]), "int"),
], ids=["ndarray", "datetime64", "masked", "complex64", "complex",
        "Decimal-NaN", "list-ending-in-int64", "MultiValue-of-float",
        "bool", "list-ending-in-bool", "Fraction", "list-subclass"])
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
    # A subclass of `tuple` or `list` is not the container the store
    # reads it as, and is left alone as #926's converter leaves it
    # (`type(...) in (list, tuple)`): it exports as it did. `isinstance`
    # in any of the three tests would change that.
    _Pair(1, 2), _Pair(np.int64(1), 2), _TupleSubclass((1, 2)),
    _ListSubclass([Decimal("1.5")]), _ListSubclass([np.int64(1)]),
    _ListSubclass([1, 2]),
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


@pytest.mark.parametrize("make", [
    lambda: Decimal(10) ** 400, lambda: -(Decimal(10) ** 400),
    lambda: Decimal("1e400"), lambda: Decimal("1.8e308"),
], ids=["10**400", "-10**400", "1e400", "1.8e308"])
def test_a_decimal_past_a_floats_range_stays_the_decimal(make):
    """`Decimal(10) ** 400` is finite and `float()` of it is `inf`, with
    no error: no float equals it, so it is not converted (owner ruling on
    #938, 2026-10-08) and takes the answer it had before. Alone and in a
    list, where the member beside it is still converted."""
    huge = make()
    assert _export_value(huge) is huge
    beside = _export_value([huge, Decimal("1.5")])
    assert beside[0] is huge and _typed(beside[1]) == (float, 1.5)


def test_a_decimal_past_a_floats_range_exports_as_it_did(tmp_path):
    """Nothing new is written silently. Under a private tag it is dropped
    with a PRIVATE row and the run is REVIEW_REQUIRED, where the float
    `inf` would be written `LO inf` with no row. In an IS and a DS it is
    written `inf`, as it was before `_export_value` existed; that is
    pydicom's own spelling of the `Decimal`, not a conversion made here."""
    huge = Decimal("1e400")
    private = _run(tmp_path / "private", _around(PRIVATE, huge))
    assert private["written"] == 1, private["written"]
    assert _element(private, PRIVATE) is None
    ((action, scope, details),) = private["rows"]
    assert (action, scope) == ("DATA_LOSS", "PRIVATE")
    assert details.endswith(f"Tag {PRIVATE} not exported (data loss): "
                            f"ValueError: no VR fits a Decimal value"), details
    assert private["grade"] == _REVIEW
    for tag, vr in ((IS_TAG, "IS"), (DS_TAG, "DS")):
        result = _run(tmp_path / vr, _around(tag, huge))
        assert result["written"] == 1, result["written"]
        # Read raw: pydicom raises converting an IS that says `inf`.
        raw = result["ds"].get_item(_numbers(tag))
        assert (str(raw.VR), bytes(raw.value)) == (vr, b"inf "), raw
        assert result["rows"] == [] and result["grade"] == _PASS


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


def _under_a_recorded_vr(value, vr):
    """`_merge` of one private value whose VR the source recorded."""
    ds, losses, revrs = Dataset(), [], []
    DicomExporter._merge(ds, {PRIVATE: value}, losses, vrs={PRIVATE: vr},
                         revrs=revrs)
    elem = ds.get(_numbers(PRIVATE))
    written = None if elem is None else (str(elem.VR), _typed(
        [str(v) for v in elem.value] if isinstance(elem.value, MultiValue)
        else str(elem.value)))
    return written, losses, [(r.recorded, r.written) for r in revrs]


@pytest.mark.parametrize("live, twin, written, revr", [
    # Names: the gate in front of a recorded VR (`_value_fits_vr`, which
    # since #951 reads a backslash under PN as the delimiter) refuses a
    # tuple outright. It is asked about the list, so the recorded PN is
    # kept live as it is after a reopen.
    (lambda: ("A^B", "C^D"), lambda: ["A^B", "C^D"], ("PN", ["A^B", "C^D"]), []),
    (lambda: ("A^B\\C^D",), lambda: ["A^B\\C^D"], ("PN", "A^B\\C^D"), []),
    # A number under a recorded PN is that gate's to refuse, before the
    # text gate is reached: written as text under LO with the re-VR
    # sentence, live and as its twin alike. Not #939's row: that is for a
    # standard element.
    (lambda: np.int64(7), lambda: 7, ("LO", "7"), [("PN", "LO")]),
    (lambda: ("A^B", np.int64(7)), lambda: ["A^B", 7],
     ("LO", ["A^B", "7"]), [("PN", "LO")]),
], ids=["names", "one-backslash-member", "number", "name-and-number"])
def test_a_recorded_private_person_name_is_one_answer_live_and_as_its_twin(
        live, twin, written, revr):
    """Where #951's Person Name arm meets the tuple and the number."""
    expected = (written[0], _typed(written[1]))
    assert _under_a_recorded_vr(live(), "PN") == (expected, [], revr)
    assert _under_a_recorded_vr(twin(), "PN") == (expected, [], revr)


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


def test_a_reopened_stores_multi_valued_ds_and_is_take_the_fast_path():
    """A reopened store hands back every multi-valued DS and IS as a list
    of `DSfloat` or `IS`. Neither is ever converted, so both exact types
    are plain: without them each such list paid the type pass and both
    scans, about 6 ms per 65,536 members. Removing them changes no output,
    which is why the set is read here as well as the result."""
    assert {DSfloat, IS} <= io_handlers._PLAIN_TYPES
    assert Decimal not in io_handlers._PLAIN_TYPES
    assert DSdecimal not in io_handlers._PLAIN_TYPES
    for big in ([DSfloat("1.50")] * 65536, [IS("7")] * 65536,
                [DSfloat("1.50"), 2, IS("7"), "A", None]):
        assert _export_value(big) is big
    # A caller's `Decimal` beside them is still found.
    mixed = [DSfloat("1.50"), IS("7"), Decimal("2.5")]
    converted = _export_value(mixed)
    assert converted[0] is mixed[0] and converted[1] is mixed[1]
    assert _typed(converted[2]) == (float, 2.5)


# -- A value pydicom built from a file is never refused as a number ----------

_FROM_A_FILE = [
    pytest.param(lambda: DSfloat("072730.5"), b"072730.5", id="DSfloat"),
    pytest.param(lambda: IS("20040119"), b"20040119", id="IS"),
    pytest.param(lambda: DSdecimal("072730.50"), b"072730.50 ", id="DSdecimal"),
    pytest.param(lambda: MultiValue(DSfloat, ["1.5", "2.5"]), b"1.5\\2.5 ",
                 id="MultiValue-of-DSfloat"),
    pytest.param(lambda: [IS("1"), IS("2")], b"1\\2 ", id="list-of-IS"),
    pytest.param(lambda: [DSfloat("1.5"), "A"], b"1.5\\A ", id="DSfloat-and-text"),
]


@pytest.mark.parametrize("vr", ["DA", "DT", "TM"])
@pytest.mark.parametrize("make, written", _FROM_A_FILE)
def test_a_files_own_number_under_a_date_or_time_tag_is_written_as_the_file_wrote_it(
        vr, make, written):
    """A file that wrote Acquisition Time under `DS` hands ingest a
    `DSfloat`: a number to `numbers.Number`, and the file's own text to
    pydicom's DA, DT and TM writers, which write `original_string`. The
    gate refuses a caller's Python or numpy number by its exact type, so
    a value pydicom built is not its to refuse (review of #1009: at
    eb11c289 these eighteen were lost with `a DSfloat is not text`)."""
    raw, losses, notes = _merged({TEXT_TAGS[vr]: make()})
    assert raw(TEXT_TAGS[vr]) == (vr, written)
    assert losses == [] and notes == []


@pytest.mark.parametrize("value", [
    DSfloat("1.5"), IS("7"), DSdecimal("1.50"), MultiValue(DSfloat, ["1.5"]), [IS("1"), "A"], "A", b"A", None,
    datetime.date(2020, 1, 2), [[1]], [(1, 2)],
], ids=lambda value: type(value).__name__)
@pytest.mark.parametrize("vr", sorted(TEXT_TAGS))
def test_the_gate_lets_by_everything_that_is_not_a_callers_number(vr, value):
    """Under every text VR, not only the three whose writer takes it:
    under LO, SH, CS and the rest a file's own `IS` fails the whole file
    at `dcmwrite`, as it did before the gate existed, and the gate does
    not turn that into a lost element. Two levels down is not looked at
    either (`[[1]]`): the conversion and the gate are one level deep."""
    assert io_handlers._refuse_a_number_as_text(vr, value) is None


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_source_file_with_a_time_written_as_a_decimal_string_exports_it(
        tmp_path, reopen):
    """End to end, no caller's value anywhere: CT_small with Acquisition
    Time `(0008,0032)` written under the explicit VR `DS` and Content Date
    `(0008,0023)` under `IS`, ingested and exported untouched."""
    def source(given):
        given[0x00080032] = pydicom.DataElement(0x00080032, "DS", "072730.5")
        given[0x00080023] = pydicom.DataElement(0x00080023, "IS", "20040119")

    result = _run(tmp_path / "out", lambda inst, item, session: None,
                  reopen=reopen, source=source)
    assert result["written"] == 1, result["written"]
    assert _element(result, "0008,0032") == ("TM", b"072730.5")
    assert _element(result, "0008,0023") == ("DA", b"20040119")
    assert result["rows"] == [] and result["grade"] == _PASS


# -- One tag under AT stays one tag -------------------------------------------

AT_TAG = "0028,0009"
_ONE = b"\x18\x00\x63\x10"
_OTHER = b"\x18\x00\x64\x10"


class _IntSubclass(int):
    pass


class _Group(enum.IntEnum):
    ACQUISITION = 0x0018


@pytest.mark.parametrize("make, written", [
    pytest.param(lambda: (0x0018, 0x1063), _ONE, id="2-tuple"),
    pytest.param(lambda: (np.uint16(0x0018), np.uint16(0x1063)), _ONE,
                 id="2-tuple-of-numpy"),
    pytest.param(lambda: pydicom.tag.BaseTag(0x00181063), _ONE, id="BaseTag"),
    pytest.param(lambda: 0x00181063, _ONE, id="int"),
    pytest.param(lambda: np.uint32(0x00181063), _ONE, id="numpy-int"),
    pytest.param(lambda: [(0x0018, 0x1063), (0x0018, 0x1064)], _ONE + _OTHER,
                 id="list-of-2-tuples"),
    pytest.param(lambda: ((0x0018, 0x1063), (0x0018, 0x1064)), _ONE + _OTHER,
                 id="tuple-of-2-tuples"),
    pytest.param(lambda: [pydicom.tag.BaseTag(0x00181063),
                          pydicom.tag.BaseTag(0x00181064)], _ONE + _OTHER,
                 id="list-of-BaseTag"),
    # A list of two ints is two tags, `(0000,0018)` and `(0000,1063)`: it
    # always was, and it is what a reopened store holds for the 2-tuple.
    pytest.param(lambda: [0x0018, 0x1063],
                 b"\x00\x00\x18\x00\x00\x00\x63\x10", id="list-of-two-ints"),
    # Only the 2-tuple is one tag. A longer tuple of ints names none, and
    # is the list it equals, as under every other VR.
    pytest.param(lambda: (0x0018, 0x1063, 0x1064),
                 b"\x00\x00\x18\x00\x00\x00\x63\x10\x00\x00\x64\x10",
                 id="3-tuple"),
    # Delta review of #1009, finding 4: whatever pydicom reads as one tag
    # stays one tag. Its `Tag` takes a pair of two `int`s or two `str`s.
    pytest.param(lambda: ("0018", "1063"), _ONE, id="pair-of-hex-strings"),
    pytest.param(lambda: (_IntSubclass(0x0018), 0x1063), _ONE,
                 id="pair-with-an-int-subclass"),
    pytest.param(lambda: (_Group.ACQUISITION, 0x1063), _ONE,
                 id="pair-with-an-IntEnum"),
    pytest.param(lambda: (True, 7), b"\x01\x00\x07\x00", id="pair-with-a-bool"),
    # A pair of floats names no tag; it is the list it equals, which
    # pydicom has always written as two group-0000 tags.
    pytest.param(lambda: (1.0, 7), b"\x00\x00\x01\x00\x00\x00\x07\x00",
                 id="pair-with-a-float"),
])
def test_under_at_a_two_tuple_is_one_tag(make, written):
    """`(0x0018, 0x1063)` is pydicom's ordinary spelling of one tag, and
    under `AT` it stays the tuple it is (owner ruling on #1009, finding 2):
    as the list it equals it would be the two tags `(0000,0018)` and
    `(0000,1063)`, with no row. Everything else under AT is as elsewhere."""
    raw, losses, notes = _merged({AT_TAG: make()})
    assert raw(AT_TAG) == ("AT", written)
    assert losses == [] and notes == []


def test_a_two_tuple_under_a_private_tag_recorded_as_at_is_as_it_was():
    """Recorded `AT`, the gate in front of a recorded VR still refuses the
    tuple, as on `main`: text under `LO`, with the re-VR sentence."""
    assert _under_a_recorded_vr((0x0018, 0x1063), "AT") == (
        ("LO", _typed(["24", "4195"])), [], [("AT", "LO")])


@pytest.mark.parametrize("route", ["around", "set_attr"])
def test_one_tag_set_as_a_two_tuple_exports_as_one_tag(tmp_path, route):
    """Through `export()`, live, by both doors. After a save and a reopen
    the store holds `[24, 4195]` and the file carries two tags: the
    store's own spelling of a tuple, measured in the PR for #938 and not
    changed here."""
    edit = (_around if route == "around" else _through_set_attr)(
        AT_TAG, (0x0018, 0x1063))
    result = _run(tmp_path / "live", edit)
    assert result["written"] == 1, result["written"]
    assert _element(result, AT_TAG) == ("AT", _ONE)
    assert result["rows"] == [] and result["grade"] == _PASS


# -- An array of text under a text VR is text ---------------------------------

@pytest.mark.parametrize("tag, make, written", [
    ("0008,0008", lambda: np.array(["ORIGINAL", "PRIMARY"]),
     ("CS", b"ORIGINAL\\PRIMARY")),
    ("0008,1090", lambda: np.array(["A", "B"]), ("LO", b"A\\B ")),
    ("0008,1090", lambda: np.array(["A", "B"], dtype=object), ("LO", b"A\\B ")),
    ("0008,1090", lambda: np.array([b"A", b"B"]), ("LO", b"A\\B ")),
    ("0008,1090", lambda: [np.array(["A", "B"])], ("LO", b"A\\B ")),
    ("0008,0012", lambda: np.array(["20200101"]), ("DA", b"20200101")),
], ids=["cs-U", "lo-U", "lo-object", "lo-S", "lo-in-a-list", "da-U"])
def test_a_numpy_array_of_text_under_a_text_vr_is_written_as_it_was(
        tag, make, written):
    """Delta review of #1009, finding 5. The gate refused every
    `np.ndarray`, and `np.array(["ORIGINAL", "PRIMARY"])` under CS, which
    was always written, was lost under `a ndarray is not text`. Literal
    bytes, as measured on `main` at ebcebbe8."""
    raw, losses, notes = _merged({tag: make()})
    assert raw(tag) == written
    assert losses == [] and notes == []


@pytest.mark.parametrize("make", [
    lambda: np.array([7]), lambda: np.array([1.5, 2.5]),
    lambda: np.array(["A", 7], dtype=object), lambda: np.array([7, 8], dtype=object),
    lambda: np.array([True]),
], ids=["int", "float", "object-mixed", "object-ints", "bool"])
def test_a_numpy_array_holding_a_number_under_a_text_vr_is_still_refused(make):
    """One number in it and it is not an array of text."""
    raw, losses, notes = _merged({LO_TAG: make()})
    assert raw(LO_TAG) is None
    ((scope, detail),) = losses
    assert scope == "STANDARD"
    assert detail.endswith("TypeError: a ndarray is not text, and LO holds text")


# -- A file's own binary number under a text tag is a plain number ------------

@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_files_own_us_ss_or_fd_under_a_text_tag_loses_that_element(
        tmp_path, reopen):
    """Owner ruling of 2026-10-09 on the delta review of #1009 (finding
    3). pydicom builds a plain `int` or `float` for US, SS and FD, and
    after ingest nothing in the graph tells a file that stated `US 42`
    for an LO tag from `set_attr(tag, 42)`: so the gate takes it, the one
    element is lost under #939's row and the file is written, where the
    whole file failed before the gate. pydicom's own `IS` and `DSfloat`
    are the other half (#1017)."""
    cells = (("0008,1090", "US", 42, "int", "LO"),
             ("0018,5100", "SS", -3, "int", "CS"),
             ("0008,0070", "FD", 4.5, "float", "LO"))

    def source(given):
        for tag, vr, value, _kind, _text in cells:
            given[_numbers(tag)] = pydicom.DataElement(_numbers(tag), vr, value)

    held = {}

    def look(inst, item, session):
        for tag, _vr, value, _kind, _text in cells:
            held[tag] = (type(inst.attributes[tag]), inst.attributes[tag],
                         inst.attribute_vrs.get(tag))

    result = _run(tmp_path / "out", look, reopen=reopen, source=source)
    # What the graph holds: the plain number and no record of the file's VR.
    assert held == {tag: (type(value), value, None)
                    for tag, _vr, value, _kind, _text in cells}
    assert result["written"] == 1, result["written"]
    for tag, _vr, _value, _kind, _text in cells:
        assert _element(result, tag) is None
    assert sorted((action, scope, details.split("Tag ", 1)[1])
                  for action, scope, details in result["rows"]) == sorted(
        ("DATA_LOSS", "STANDARD",
         f"{tag} not exported (data loss): TypeError: a {kind} is not text, "
         f"and {text} holds text") for tag, _vr, _value, kind, text in cells)
    assert result["grade"] == _PASS


# -- An owner's stamp that cannot be written fails the file -------------------

def _owner(session, kind):
    (patient,) = session.store.patients
    study = patient.studies[0]
    return {"patient": patient, "study": study, "series": study.series[0]}[kind]


# The three fields the store keys a row by. A value there that is not a
# `str` is refused by the save `session.export()` begins with (#949),
# before any file is planned, as a SOP Instance UID is (#721): owner
# ruling of 2026-10-09 on #1015 against #1009. `DicomExporter.write_tree()`
# has no save, so there the stamp's own failure is what a caller meets.
_STORE_KEYS = {"patient_id", "study_instance_uid", "series_instance_uid"}
_SAVE_REFUSAL = "hold a key that is not a str"


def _refused_by_the_save(raised, rows):
    """The save's named `ValueError`, and nothing written about it."""
    assert type(raised) is ValueError, raised
    assert str(raised).startswith("save: "), raised
    assert _SAVE_REFUSAL in str(raised), raised
    assert rows == [], rows


def _export_with_an_owner_field(folder, kind, field, value, after_a_pass,
                                door="export"):
    """Ingest CT_small, optionally `anonymize(audit())`, assign
    `owner.field = value`, export through `door`: `session.export()`, or
    `DicomExporter.write_tree()` for `"tree"`. Returns what the door
    raised (or None), every exported file's bytes, the source's and the
    pass's values of the field, and the ERROR / DATA_LOSS rows."""
    folder.mkdir(parents=True)
    (folder / "in").mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(folder / "in" / "a.dcm"))
    db, out = str(folder / "s.db"), folder / "out"
    raised = None
    session = DicomSession(db)
    try:
        session.ingest(str(folder / "in"))
        before = [getattr(_owner(session, kind), field)]
        if after_a_pass:
            session.anonymize(session.audit())
            before.append(getattr(_owner(session, kind), field))
        owner = _owner(session, kind)
        setattr(owner, field, value)
        try:
            if door == "tree":
                DicomExporter.write_tree(_owner(session, "patient"), str(out),
                                         show_progress=False)
            else:
                session.export(str(out), use_compression=True,
                               show_progress=False)
        except Exception as exc:  # the keys raise before any worker runs
            raised = exc
        # So the session can close on a value the store holds.
        setattr(owner, field, before[-1])
    finally:
        session.close()
    with sqlite3.connect(db) as conn:
        rows = [(action, details or "") for action, details in conn.execute(
            "SELECT action_type, details FROM audit_log WHERE action_type "
            "IN ('ERROR', 'DATA_LOSS') ORDER BY rowid")]
    files = [path.read_bytes() for path in out.rglob("*") if path.is_file()]
    return raised, files, before, rows


@pytest.mark.parametrize("after_a_pass", [False, True],
                         ids=["no-pass", "after-audit-and-anonymize"])
@pytest.mark.parametrize("kind, field, tag", [
    ("patient", "patient_name", "0010,0010"),
    ("patient", "patient_id", "0010,0020"),
    ("study", "study_instance_uid", "0020,000d"),
    ("series", "series_instance_uid", "0020,000e"),
])
@pytest.mark.parametrize("make", [lambda: 7, lambda: np.int64(7)],
                         ids=["int", "numpy-int"])
def test_an_owners_value_that_cannot_be_stamped_fails_the_file(
        tmp_path, kind, field, tag, make, after_a_pass):
    """Owner ruling on #1009, finding 3. The exporter stamps each of
    these over the instance's own copy. When the stamp cannot be written
    the copy underneath it must not go out in its place: at eb11c289
    `patient.patient_id = 7` wrote the file with the source's Patient ID
    under a row saying the tag was not exported, and a Patient's Name did
    the same on `main`. The file fails; nothing is on disk; no row says
    "not exported" for a tag whose value was written.

    Patient's Name fails the file at `session.export()`. The three keys
    never reach a worker there: the export's leading save refuses a key
    that is not a `str` (#949), with no file and no row. The stamp's own
    failure for a key is pinned through `write_tree()`, in
    `test_an_owners_key_that_cannot_be_stamped_fails_the_file_at_write_tree`."""
    raised, files, before, rows = _export_with_an_owner_field(
        tmp_path / "out", kind, field, make(), after_a_pass)
    assert raised is not None
    assert files == []
    assert not [details for action, details in rows
                if action == "DATA_LOSS" and tag in details], rows
    if field in _STORE_KEYS:
        _refused_by_the_save(raised, rows)
    else:
        assert isinstance(raised, ExportError), raised
        (failure,) = [details for action, details in rows if action == "ERROR"]
        assert tag in failure, failure
        for value in before:
            assert str(value) not in failure, failure


@pytest.mark.parametrize("after_a_pass", [False, True],
                         ids=["no-pass", "after-audit-and-anonymize"])
@pytest.mark.parametrize("kind, field, tag, make", [
    ("patient", "patient_id", "0010,0020", lambda: 7),
    ("patient", "patient_id", "0010,0020", lambda: np.int64(7)),
    ("study", "study_instance_uid", "0020,000d", lambda: b"\xff"),
    ("series", "series_instance_uid", "0020,000e", lambda: b"\xff"),
], ids=["patient-id-int", "patient-id-numpy", "study-uid-bytes",
        "series-uid-bytes"])
def test_an_owners_key_that_cannot_be_stamped_fails_the_file_at_write_tree(
        tmp_path, monkeypatch, kind, field, tag, make, after_a_pass):
    """`_merge_stamp`'s failure for the three keys, through the door that
    reaches it. `write_tree()` runs no save, so nothing refuses the key
    before the worker: the stamp cannot be written, the file fails, and
    the instance's own copy (the source's, or the pass's) is not on disk
    in its place. Until the ruling of 2026-10-09 these cells were asserted
    through `session.export()`, where #949's save now speaks first."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    raised, files, before, rows = _export_with_an_owner_field(
        tmp_path / "out", kind, field, make(), after_a_pass, door="tree")
    assert type(raised) is RuntimeError, raised
    assert f"the owner's value for {tag} cannot be written" in str(raised)
    assert files == []
    assert rows == []
    for value in before:
        assert str(value) not in str(raised), raised


def test_every_refused_stamp_fails_the_file_copy_or_no_copy():
    """Owner ruling of 2026-10-09, and the delta review of #1009 (finding
    1): one rule for every owner stamp. The refusal does not ask whether
    the instance holds a copy of the tag: with none, `Patient(7, ...)`
    over a hand-built instance wrote a file with no Patient ID at all,
    where `main` failed it."""
    # No copy underneath.
    with pytest.raises(ValueError) as refused:
        DicomExporter._merge_stamp(Dataset(), {"0010,0020": 7})
    assert "0010,0020" in str(refused.value)
    # The instance's copy written, the stamp refused.
    ds = Dataset()
    DicomExporter._merge(ds, {"0010,0010": "SOURCE^NAME", "0010,0020": "ID"}, [])
    with pytest.raises(ValueError) as refused:
        DicomExporter._merge_stamp(ds, {"0010,0010": 7, "0010,0020": "NEW"})
    assert "0010,0010" in str(refused.value)
    assert "SOURCE" not in str(refused.value)
    # A value neither level can write: the stamp is refused all the same.
    ds, losses = Dataset(), []
    DicomExporter._merge(ds, {"0010,0010": 7}, losses)
    assert "PatientName" not in ds and len(losses) == 1
    with pytest.raises(ValueError):
        DicomExporter._merge_stamp(ds, {"0010,0010": 7})
    # And a stamp that can be written is written.
    ds = Dataset()
    DicomExporter._merge(ds, {"0010,0020": "ID"}, [])
    DicomExporter._merge_stamp(ds, {"0010,0020": "NEW"})
    assert ds.PatientID == "NEW"


def _hand_built(patient_id, name):
    """A graph built through `DicomBuilder`: its instance holds no copy
    of Patient ID or Patient's Name, as no hand-built instance does."""
    from isocenter.builders import DicomBuilder
    builder = DicomBuilder.start_patient(patient_id, name)
    instance = (builder.add_study("1.2.826.0.2.999", datetime.date(2023, 1, 2))
                .add_series("1.2.826.0.3.999", "OT", 3)
                .add_instance("1.2.826.0.1.999.1", "1.2.840.10008.5.1.4.1.1.7", 1))
    instance.set_pixel_data(np.arange(64, dtype=np.uint16).reshape(8, 8))
    instance.end_instance().end_series().end_study()
    patient = builder.build()
    (inst,) = patient.studies[0].series[0].instances
    assert "0010,0020" not in inst.attributes
    assert "0010,0010" not in inst.attributes
    return patient


@pytest.mark.parametrize("patient_id, name", [
    (7, "Doe^Jane"), (1.5, "Doe^Jane"), (np.int64(7), "Doe^Jane"),
    ("PAT", 7), ("PAT", np.int64(7)),
], ids=["id-int", "id-float", "id-numpy", "name-int", "name-numpy"])
def test_a_hand_built_graph_with_an_unwritable_owner_value_fails_the_file(
        tmp_path, monkeypatch, patient_id, name):
    """The builder route, by both doors. `start_patient(7, ...)` is the
    likeliest way to meet this: an integer MRN. The file fails; nothing
    is on disk. For Patient ID that is `main`'s answer; a hand-built
    `patient_name = 7` was a file without the name under one row on
    `main`, and is a failed file now, by the ruling.

    At `session.export()` a Patient ID that is not a `str` is refused by
    the save first (#949; owner ruling of 2026-10-09): the named
    `ValueError`, no file, no row. `write_tree()` above is where the
    stamp's failure is asserted for it."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    out = tmp_path / "tree"
    with pytest.raises(RuntimeError):
        DicomExporter.write_tree(_hand_built(patient_id, name), str(out),
                                 show_progress=False)
    assert not [p for p in out.rglob("*") if p.is_file()]

    db, exported = str(tmp_path / "s.db"), tmp_path / "exported"
    session = DicomSession(db)
    try:
        session.store.patients.append(_hand_built(patient_id, name))
        with pytest.raises(Exception) as refused:
            session.export(str(exported), use_compression=False,
                           show_progress=False)
    finally:
        session.store.patients.clear()
        session.close()
    assert not [p for p in exported.rglob("*") if p.is_file()]
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT action_type, details FROM audit_log WHERE "
                            "action_type IN ('ERROR', 'DATA_LOSS')").fetchall()
    if not isinstance(patient_id, str):
        _refused_by_the_save(refused.value, rows)
        return
    assert isinstance(refused.value, ExportError), refused.value
    assert [action for action, _details in rows] == ["ERROR"], rows
    assert "cannot be written" in rows[0][1], rows


def test_a_hand_built_graph_the_exporter_can_stamp_is_written(tmp_path):
    """The control for the test above."""
    out = tmp_path / "tree"
    DicomExporter.write_tree(_hand_built("PAT", "Doe^Jane"), str(out),
                             show_progress=False)
    (path,) = [p for p in out.rglob("*.dcm")]
    written = pydicom.dcmread(str(path))
    assert written.PatientID == "PAT" and str(written.PatientName) == "Doe^Jane"


def test_a_file_name_refusal_speaks_before_a_refused_stamp(tmp_path):
    """Two reasons to fail one file: its SOP Instance UID cannot name a
    file (GHSA-2rc2-r9r5-x7hm, #1024) and its Patient's stamp cannot be
    written. The file-name refusal is raised first in the worker, before
    any dataset is built, so it is the one the row carries: one ERROR
    row, one failure, nothing on disk.

    The unwritable stamp is a Patient's Name of 7, not a Patient ID of 7
    as it was: a Patient ID that is not a `str` is refused by the export's
    leading save (#949), so neither refusal this test orders would be
    reached. The name is not a key and the save holds it."""
    folder = tmp_path / "both"
    folder.mkdir()
    (folder / "in").mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(folder / "in" / "a.dcm"))
    db, out = str(folder / "s.db"), folder / "out"
    session = DicomSession(db)
    try:
        session.ingest(str(folder / "in"))
        inst, patient = _instance(session), _owner(session, "patient")
        uid, name = inst.sop_instance_uid, patient.patient_name
        inst.sop_instance_uid = "../elsewhere"
        patient.patient_name = 7
        with pytest.raises(ExportError):
            session.export(str(out), use_compression=True, show_progress=False)
        # So the session can close on values the store holds.
        inst.sop_instance_uid, patient.patient_name = uid, name
    finally:
        session.close()
    with sqlite3.connect(db) as conn:
        errors = [details for (details,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'ERROR'")]
    assert len(errors) == 1, errors
    assert "SOP Instance UID (0008,0018) cannot name a file" in errors[0]
    assert "cannot be written" not in errors[0]
    assert [p for p in tmp_path.rglob("*.dcm") if "in" not in p.parts] == []


@pytest.mark.parametrize("after_a_pass", [False, True],
                         ids=["no-pass", "after-audit-and-anonymize"])
@pytest.mark.parametrize("kind, field, tag", [
    ("study", "study_instance_uid", "0020,000d"),
    ("series", "series_instance_uid", "0020,000e"),
])
def test_an_owners_uid_that_cannot_be_stamped_fails_the_file(
        tmp_path, kind, field, tag, after_a_pass):
    """The study and series stamps are reachable (delta review, finding
    2): a number never gets as far as a worker, but `b"\\xff"` does. On
    `main` the file went out carrying the instance's own UID, the
    source's or the pass's, under a row saying the tag was not exported.

    At `session.export()` it no longer gets as far as a worker either:
    the leading save refuses a Study or Series Instance UID that is not a
    `str` (#949; owner ruling of 2026-10-09), with no file and no row.
    The stamp's failure, which is what a caller of `write_tree()` meets,
    is in `test_an_owners_key_that_cannot_be_stamped_fails_the_file_at_write_tree`."""
    raised, files, before, rows = _export_with_an_owner_field(
        tmp_path / "out", kind, field, b"\xff", after_a_pass)
    assert files == []
    _refused_by_the_save(raised, rows)
    for value in before:
        assert str(value) not in str(raised), raised


def test_a_study_date_that_names_no_date_is_stamped_as_its_text(tmp_path):
    """Study Date is the one stamp a number can be written under: the
    stamp is `format_study_date()`'s text, so `study.study_date = 7`
    writes `7`, as it did before. The stamp is written; the source's date
    is not in the file. Not a date, and not this change's to refuse."""
    raised, files, before, rows = _export_with_an_owner_field(
        tmp_path / "out", "study", "study_date", 7, False)
    assert raised is None and len(files) == 1
    written = pydicom.dcmread(io.BytesIO(files[0]))
    assert bytes(written.get_item(0x00080020).value) == b"7 "
    assert rows == []


# -- A numpy scalar under a binary VR fails the file, as its twin does --------

_BINARY_TAGS = {"OB": "0042,0011", "OW": "0028,1201", "OF": "0064,0009",
                "OD": "0070,150d", "OL": "0066,0040", "OV": "0072,0081"}


def test_each_binary_tag_is_the_vr_it_is_listed_under():
    """(0072,0083) is `UV` and (0008,3002) is `UI`; the table is checked."""
    for vr, tag in _BINARY_TAGS.items():
        assert pydicom.datadict.dictionary_VR(_numbers(tag)) == vr, (vr, tag)
_NUMPY_SCALARS = {
    "uint8": lambda: np.uint8(200), "uint16": lambda: np.uint16(7),
    "int32": lambda: np.int32(7), "int64": lambda: np.int64(7),
    "float32": lambda: np.float32(1.5), "float64": lambda: np.float64(1.5),
    "bool": lambda: np.bool_(True), "0-d": lambda: np.array(7),
}


@pytest.mark.parametrize("vr", sorted(_BINARY_TAGS))
@pytest.mark.parametrize("kind", sorted(_NUMPY_SCALARS))
def test_a_numpy_scalar_under_a_binary_vr_fails_the_file_as_its_twin_does(
        vr, kind):
    """Owner ruling of 2026-10-09 on the review of #1009 (finding 4): fail
    everywhere. Before the conversion a live numpy scalar under OB, OD,
    OF, OL, OV or OW was written as the bytes of its own buffer
    (`np.uint8(200)` as `c8 00`, under every one of the six), where its
    Python twin and the reopened store failed the file. It is its twin
    now, and the twin's answer is pydicom's at `dcmwrite`. What a number
    under a binary VR should export as is #1018."""
    tag, make = _BINARY_TAGS[vr], _NUMPY_SCALARS[kind]
    twin = make().item()
    assert type(twin) in (int, float, bool)
    with pytest.raises(TypeError, match="bytes-like object is required"):
        _merged({tag: twin})
    with pytest.raises(TypeError, match="bytes-like object is required"):
        _merged({tag: make()})


@pytest.mark.parametrize("vr", sorted(_BINARY_TAGS))
def test_bytes_and_a_1_d_array_under_a_binary_vr_are_written_as_they_were(vr):
    """The control: what a binary VR does hold is not converted."""
    tag = _BINARY_TAGS[vr]
    raw, losses, notes = _merged({tag: b"ABCDEFGH"})
    assert raw(tag) == (vr, b"ABCDEFGH") and losses == [] and notes == []
    raw, losses, notes = _merged({tag: np.array([7], dtype=np.int64)})
    assert raw(tag) == (vr, b"\x07" + b"\x00" * 7) and losses == []

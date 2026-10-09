"""Two values in an element the `series` row is built from are held as the
source wrote them, and the file is ingested (#985, owner ruling Q5 A,
2026-10-08).

PS3.5 6.4 delimits the values of a character-string element with a
backslash, and pydicom reads an element holding one as a `MultiValue`.
`isocenter.io_handlers.ingest_worker` took five elements as pydicom gave
them -- Manufacturer, Manufacturer's Model Name, Device Serial Number,
Modality and Series Number -- and handed them to the parent as the fields
of `Equipment` and `Series`. Measured on `main` at fd359eb3, on 3.12.14
and 3.14.7t: the `save(sync=True)` that ends `ingest()` then raised

    sqlite3.ProgrammingError: Error binding parameter N: type 'MultiValue'
    is not supported

so **nothing from the folder was stored**, an unchanged file beside it
included, `save()` and `export()` raised the same for the rest of the
session, and no audit row said why. It is #747's cause on fields that are
not linkage keys, so #747's refusal did not see them.

Now the four text fields hold the source's own text, values joined by the
backslash that delimited them (`_source_text`, as `_pn_text` does for a
Person Name, #937), and Series Number, which is a number, reads 0 as an
unreadable one already does (#870). Nothing is linked by any of the five,
and no file is written from them (#869): the instance's own elements are
held by `populate_attrs` as before and exported as the source wrote them.

**The assertions on an exported file read the element raw** (`get_item`,
bytes), as `test_a_multi_valued_person_name_is_held_as_written.py` does.
"""
import os
import sqlite3

import pydicom
import pytest

from isocenter import Session
from isocenter import io_handlers

from support.ct_small_files import row_counts, write_ct

IGNORE = pydicom.config.IGNORE

#: keyword, the two-valued text, the `meta` key the worker hands on, what
#: that key holds now, and the element's bytes in the source and the export
#: (padded to even length as the writer pads).
FIELDS = [
    ("Manufacturer", "ACME\\Imaging", "man", "ACME\\Imaging", b"ACME\\Imaging"),
    ("ManufacturerModelName", "X\\Y", "model", "X\\Y", b"X\\Y "),
    ("DeviceSerialNumber", "1\\2", "dev_sn", "1\\2", b"1\\2 "),
    ("Modality", "CT\\MR", "modality", "CT\\MR", b"CT\\MR "),
    ("SeriesNumber", "1\\2", "series_num", 0, b"1\\2 "),
]
IDS = [field[0] for field in FIELDS]


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _two_valued(tmp_path, keyword, text, name="a.dcm"):
    """CT_small with `keyword` holding `text`, past pydicom's VM check."""
    path = write_ct(tmp_path / "in" / name, "PID-985", 985)
    ds = pydicom.dcmread(path)
    tag = pydicom.datadict.tag_for_keyword(keyword)
    ds[tag] = pydicom.DataElement(tag, pydicom.datadict.dictionary_VR(tag),
                                  text, validation_mode=IGNORE)
    ds.save_as(path)
    return path


def _series(session, patient_id="PID-985"):
    [patient] = [p for p in session.store.patients if p.patient_id == patient_id]
    [series] = [se for st in patient.studies for se in st.series]
    return series


def _field(series, key):
    return {"man": lambda: series.equipment.manufacturer,
            "model": lambda: series.equipment.model_name,
            "dev_sn": lambda: series.equipment.device_serial_number,
            "modality": lambda: series.modality,
            "series_num": lambda: series.series_number}[key]()


def _rows(session, *kinds):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        rows = conn.execute("SELECT action_type, details FROM audit_log").fetchall()
    return [row for row in rows if row[0] in kinds]


def _exported(folder):
    return {name: pydicom.dcmread(os.path.join(root, name))
            for root, _, names in os.walk(folder)
            for name in names if name.endswith(".dcm")}


# ---------------------------------------------------------------------------
# What the worker hands the parent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keyword, text, key, held, _wire", FIELDS, ids=IDS)
def test_the_worker_hands_on_one_value_the_store_can_hold(
        tmp_path, keyword, text, key, held, _wire):
    """On main each was pydicom's `MultiValue`, which sqlite cannot bind."""
    result = io_handlers.ingest_worker(_two_valued(tmp_path, keyword, text))
    meta, error = result[0], result[-1]
    assert error is None
    assert meta[key] == held
    assert type(meta[key]) is type(held)


@pytest.mark.parametrize("keyword, text, key, held", [
    ("Manufacturer", "ACME", "man", "ACME"),
    ("ManufacturerModelName", "X", "model", "X"),
    ("DeviceSerialNumber", "12", "dev_sn", "12"),
    ("Modality", "MR", "modality", "MR"),
    ("SeriesNumber", "7", "series_num", 7),
], ids=IDS)
def test_one_value_is_handed_on_as_before(tmp_path, keyword, text, key, held):
    """Control, green on main: a single value is what pydicom read."""
    result = io_handlers.ingest_worker(_two_valued(tmp_path, keyword, text))
    meta = result[0]
    assert result[-1] is None
    assert meta[key] == held


def test_an_absent_element_is_handed_on_as_before(tmp_path):
    """Control, green on main: the defaults are untouched."""
    path = write_ct(tmp_path / "in" / "a.dcm", "PID-985", 985)
    ds = pydicom.dcmread(path)
    for keyword in IDS:
        if keyword in ds:
            del ds[keyword]
    ds.save_as(path)
    meta = io_handlers.ingest_worker(path)[0]
    assert (meta["man"], meta["model"], meta["dev_sn"]) == ("", "", "")
    assert (meta["modality"], meta["series_num"]) == ("OT", 0)


@pytest.mark.parametrize("value", [None, 7, 1.5, b"x", ["A", "B"], ""],
                         ids=["none", "int", "float", "bytes", "list", "empty"])
def test_anything_that_is_not_a_multivalue_is_returned_as_given(value):
    """`_source_text` is not `str()`: only a `MultiValue` is joined.
    Returning `str(value)` otherwise turns an absent value into `'None'`
    in a field the store holds, and that variant passed every other test
    in this file."""
    assert io_handlers._source_text(value) is value


def test_three_values_and_an_empty_one_are_joined_as_written(tmp_path):
    """The join is the file's own text: every value, an empty one too."""
    path = _two_valued(tmp_path, "Manufacturer", "A\\\\C")
    meta = io_handlers.ingest_worker(path)[0]
    assert meta["man"] == "A\\\\C"


# ---------------------------------------------------------------------------
# The folder is ingested, saved and reopened
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keyword, text, key, held, _wire", FIELDS, ids=IDS)
def test_the_folder_is_ingested_and_the_store_holds_both_files(
        tmp_path, keyword, text, key, held, _wire):
    """On main `ingest()` raised `sqlite3.ProgrammingError` and the store
    held nothing, the unchanged file of another patient included."""
    _two_valued(tmp_path, keyword, text)
    write_ct(tmp_path / "in" / "b.dcm", "GOOD", 986)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert summary.ingested == 2
        assert summary.failures == []
        assert _field(_series(session), key) == held
        assert type(_field(_series(session), key)) is type(held)
        session.save(sync=True)
        assert _rows(session, "ERROR", "WARNING", "DATA_LOSS") == []
    assert row_counts(db) == (2, 2, 2, 2)
    with Session(str(db)) as reopened:
        assert _field(_series(reopened), key) == held
        assert type(_field(_series(reopened), key)) is type(held)


# ---------------------------------------------------------------------------
# What the export writes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keyword, text, key, held, wire", FIELDS, ids=IDS)
def test_an_export_with_no_pass_writes_the_sources_values(
        tmp_path, keyword, text, key, held, wire):
    """The file's own element is held by `populate_attrs` and written with
    the source's values; the pixels are written; no row."""
    source = pydicom.dcmread(_two_valued(tmp_path, keyword, text))
    tag = pydicom.datadict.tag_for_keyword(keyword)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        result = session.export(str(tmp_path / "out"), use_compression=False,
                                show_progress=False)
        assert result.failures == []
        [ds] = _exported(tmp_path / "out").values()
        assert bytes(ds.get_item(tag).value) == wire
        assert ds[tag].VM == 2
        assert ds.PixelData == source.PixelData
        assert _rows(session, "ERROR", "WARNING", "DATA_LOSS") == []


@pytest.mark.parametrize("keyword, text, folder", [
    ("Manufacturer", "ACME\\Imaging", "Series_1_CT_"),
    ("Modality", "CT\\MR", "Series_1_OT_"),
    ("SeriesNumber", "1\\2", "Series_0_CT_"),
], ids=["control", "Modality", "SeriesNumber"])
def test_the_series_folder_names_one_modality_and_one_number(
        tmp_path, keyword, text, folder):
    """The folder is named from the instance's own elements (#869), and a
    name takes one Modality and one number: two Modalities read as `OT`
    and two Series Numbers as 0, the names an absent one gets. Pinned so a
    change to either is seen; the file inside carries both values (above)."""
    _two_valued(tmp_path, keyword, text)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
    [series_dir] = [name for _, dirs, _ in os.walk(tmp_path / "out")
                    for name in dirs if name.startswith("Series_")]
    assert series_dir.startswith(folder), series_dir


def test_a_machine_rule_reads_the_joined_text(tmp_path):
    """`create_config()` and the knowledge-base match read the equipment's
    fields as text (`.lower()`, `==`, `in`); a `MultiValue` has no
    `.lower()`. A rule matches the joined text, or a part of it under
    CTP's containment match, as it would any one value."""
    from isocenter.session import _match_ctp_rule

    _two_valued(tmp_path, "Manufacturer", "ACME\\Imaging")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        [equipment] = session.store.get_unique_equipment()
        assert equipment.manufacturer == "ACME\\Imaging"
        assert type(equipment.manufacturer) is str
        session.create_config(str(tmp_path / "c.yaml"))
        assert (tmp_path / "c.yaml").exists()
        model = equipment.model_name
        for manufacturer in ("acme\\imaging", "ACME"):
            matched = _match_ctp_rule(
                equipment, [{"manufacturer": manufacturer, "model_name": model}])
            assert matched is not None, manufacturer
        assert _match_ctp_rule(
            equipment, [{"manufacturer": "Other", "model_name": model}]) is None

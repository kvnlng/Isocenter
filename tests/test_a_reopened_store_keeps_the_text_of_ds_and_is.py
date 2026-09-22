"""A saved and reopened store exports DS and IS values as the source spelled them (#662).

`DSfloat` and `IS` are `float` and `int` subclasses, and `json` writes a
`float` or `int` subclass as a bare number **without consulting the
encoder's `default()`**, so `attributes_json` kept `5.0` and dropped
`'5.000000'`. The in-session export kept the text (the graph held the
pydicom value), so one graph exported two ways depending on whether a
save had happened: `'5.000000'` -> `'5.0'`, `'+7'` -> `'7.0'`, IS
`'0005'` -> `'5'`, and a 16-character DS `'1234567890123456'` ->
`'1234567890123456.0'`, eighteen characters, two past what DS may hold,
written without a warning.

The store now keeps a DS or IS atom as tagged text and hydrates it as the
pydicom value ingest produced. Every comparison here is of the **raw
element bytes** on the wire (`get_item(tag).value` on a fresh `dcmread`),
never of parsed numbers, which is the one comparison the defect passes.

Every UID is literal, so a failure reproduces.
"""
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.valuerep import DSfloat, IS

from isocenter.session import DicomSession

SOP = "1.2.826.0.1.662.1"
STUDY = "1.2.826.0.1.662.2"
SERIES = "1.2.826.0.1.662.3"

#: The 16-character DS whose rendering as a Python float is 18.
SIXTEEN = "1234567890123456"


def _source(folder: Path, extra=()) -> Path:
    """One Explicit VR LE CT, 4x4 uint16, whose DS/IS text is not Python's."""
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    meta.MediaStorageSOPInstanceUID = SOP
    meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = meta.MediaStorageSOPClassUID, SOP
    ds.PatientID, ds.PatientName = "P662", "Doe^John"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = STUDY, SERIES
    ds.Modality, ds.StudyDate = "CT", "20200101"
    ds.SliceThickness = "5.000000"                       # 0018,0050
    ds.KVP = "120.000000000000"                          # 0018,0060
    ds.PixelSpacing = ["0.500000", "0.500000"]           # 0028,0030
    ds.ImagePositionPatient = ["-125.000000", "1.5E-05", "+7"]
    ds.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
    ds.SliceLocation = SIXTEEN                           # 0020,1041
    ds.InstanceNumber = "0005"                           # 0020,0013 IS
    ds.SeriesNumber = " 12"                              # 0020,0011 IS
    item = Dataset()
    item.SliceThickness = "2.50000"
    item.ReferencedFrameNumber = "01"                    # 0008,1160 IS
    ds.ReferencedImageSequence = Sequence([item])
    ds.add_new(0x00090010, "LO", "ACME")
    private_item = Dataset()
    private_item.add_new(0x00090010, "LO", "ACME")
    private_item.add_new(0x00091001, "DS", SIXTEEN)
    ds.add_new(0x00091010, "SQ", Sequence([private_item]))
    for tag, vr, value in extra:
        ds.add_new(tag, vr, value)
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(16, dtype=np.uint16).tobytes()
    path = folder / "src.dcm"
    ds.save_as(str(path), enforce_file_format=True)
    return path


def _raw_ds_is(ds, prefix=""):
    """Every DS/IS element's raw bytes, keyed by its path, at every depth."""
    out = {}
    for tag in ds.keys():
        raw = ds.get_item(tag)
        key = prefix + f"{tag.group:04x},{tag.element:04x}"
        vr = ds[tag].VR
        if vr == "SQ":
            for index, item in enumerate(ds[tag].value):
                out.update(_raw_ds_is(item, f"{key}[{index}]>"))
        elif vr in ("DS", "IS"):
            value = raw.value
            out[key] = value if isinstance(value, bytes) else None
    return out


def _only(folder: Path) -> Path:
    (path,) = list(folder.rglob("*.dcm"))
    return path


def _ingest_save_close(tmp_path, src_dir):
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(src_dir))
        session.export(str(tmp_path / "live"), use_compression=False)
        session.save(sync=True)
    return db


def _instance(session):
    (patient,) = session.store.patients
    return patient.studies[0].series[0].instances[0]


def test_a_reopened_store_exports_ds_and_is_as_the_source_spelled_them(tmp_path):
    """Live, reopened and source agree byte for byte on every DS/IS element,
    at the root and one level down. Killing mutations: the tag written in a
    `default()` arm, which `json` never calls for a `float` subclass; DS
    tagged and IS not; the pre-walk not recursing into `__sequences__`; a
    `MultiValue` whose atoms are not walked."""
    src = _source(tmp_path / "src")
    db = _ingest_save_close(tmp_path, tmp_path / "src")
    with DicomSession(db) as session:
        session.export(str(tmp_path / "reopened"), use_compression=False)

    source = _raw_ds_is(pydicom.dcmread(str(src)))
    live = _raw_ds_is(pydicom.dcmread(str(_only(tmp_path / "live"))))
    reopened = _raw_ds_is(pydicom.dcmread(str(_only(tmp_path / "reopened"))))

    # Every element the export writes is compared below; these, the ones
    # the fixture was built to carry, must be among them.
    for key in ("0018,0050", "0018,0060", "0028,0030", "0020,0032",
                "0020,0037", "0020,1041", "0020,0013", "0020,0011",
                "0008,1140[0]>0018,0050", "0008,1140[0]>0008,1160"):
        assert key in live, key
    assert reopened == live
    for key, value in live.items():
        assert value == source[key], key


def test_a_sixteen_character_ds_is_still_sixteen_characters_after_a_reopen(tmp_path):
    """The validity half of #662: the reopened export wrote
    `1234567890123456.0`, two characters past DS's sixteen."""
    _source(tmp_path / "src")
    db = _ingest_save_close(tmp_path, tmp_path / "src")
    with DicomSession(db) as session:
        session.export(str(tmp_path / "reopened"), use_compression=False)

    ds = pydicom.dcmread(str(_only(tmp_path / "reopened")))
    raw = ds.get_item(0x00201041).value
    assert raw.strip() == SIXTEEN.encode()
    assert len(raw.strip()) <= 16


def _assert_ingest_types(inst):
    thickness = inst.attributes["0018,0050"]
    assert isinstance(thickness, DSfloat)
    assert str(thickness) == "5.000000"
    assert thickness + 1 == 6.0
    number = inst.attributes["0020,0013"]
    assert isinstance(number, IS)
    assert str(number) == "0005"
    assert number == 5
    spacing = inst.attributes["0028,0030"]
    assert [type(v) for v in spacing] == [DSfloat, DSfloat]
    assert [str(v) for v in spacing] == ["0.500000", "0.500000"]
    (nested,) = inst.sequences["0008,1140"].items
    assert str(nested.attributes["0008,1160"]) == "01"


def test_a_reloaded_ds_is_the_value_ingest_gave(tmp_path):
    """Reopened by both hydration routes -- `load_all` (a new session) and
    `load_patient` -- a DS is a `DSfloat` printing its source text and
    doing arithmetic, an IS an `IS`. Killing mutations: the hook returning
    the raw `str` (arithmetic breaks); returning `float(data)` (the text is
    lost again); the hook missing at either `json.loads` site."""
    _source(tmp_path / "src")
    db = _ingest_save_close(tmp_path, tmp_path / "src")
    with DicomSession(db) as session:
        _assert_ingest_types(_instance(session))
        patient = session.store_backend.load_patient("P662")
    _assert_ingest_types(patient.studies[0].series[0].instances[0])


def test_the_identity_lock_write_keeps_the_text_too(tmp_path):
    """`update_attributes` -- the `lock_identities(persist=True)` path --
    is the second place `attributes_json` is written. Killing mutation:
    the fix at `save_all`'s dump site only; this rewrite then puts the bare
    numbers back over the text ingest stored."""
    _source(tmp_path / "src")
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "src"))
        session.save(sync=True)
        session.store_backend.update_attributes([_instance(session)])
    with DicomSession(db) as session:
        session.export(str(tmp_path / "reopened"), use_compression=False)

    ds = pydicom.dcmread(str(_only(tmp_path / "reopened")))
    assert ds.get_item(0x00180050).value.strip() == b"5.000000"
    assert ds.get_item(0x00200013).value.strip() == b"0005"


def test_a_private_ds_in_a_sequence_keeps_its_vr_after_a_reopen(tmp_path):
    """A private DS inside a sequence rides `attributes_json`. Reloaded as a
    float whose `str()` is 18 characters, `_value_fits_vr` refused `DS` and
    the explicit export wrote it `LO` with a re-VR `WARNING` row. Killing
    mutation: the pre-walk not reaching nested items."""
    _source(tmp_path / "src")
    db = _ingest_save_close(tmp_path, tmp_path / "src")
    with DicomSession(db) as session:
        session.export(str(tmp_path / "reopened"), use_compression=True)

    ds = pydicom.dcmread(str(_only(tmp_path / "reopened")))
    (item,) = ds[0x00091010].value
    # The raw bytes first: `item[tag]` converts the element in place.
    assert item.get_item(0x00091001).value.strip() == SIXTEEN.encode()
    assert item[0x00091001].VR == "DS"
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT details FROM audit_log "
                            "WHERE action_type = 'WARNING'").fetchall()
    assert rows == []


def test_a_store_saved_before_this_keeps_its_numbers(tmp_path):
    """A 0.9.x store holds the bare number; the text is gone and nothing
    may invent one. It loads as a `float` and exports Python's rendering,
    the stated limit. Killing mutation: a hook or migration that
    fabricates a spelling for an old store."""
    _source(tmp_path / "src")
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "src"))
        session.save(sync=True)
    # The session is closed before the write: it holds handles on two
    # threads, and a write under an open session races them.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE instances SET attributes_json = "
                     "json_set(attributes_json, '$.\"0018,0050\"', 5.0)")
    with DicomSession(db) as session:
        value = _instance(session).attributes["0018,0050"]
        assert type(value) is float and value == 5.0
        session.export(str(tmp_path / "reopened"), use_compression=False)

    ds = pydicom.dcmread(str(_only(tmp_path / "reopened")))
    assert ds.get_item(0x00180050).value.strip() == b"5.0"


def test_a_source_text_pydicom_warns_about_reloads_without_a_warning(tmp_path):
    """An IS of 13 characters: pydicom 3.0.2 accepts it at ingest under its
    default `WARN` ("exceeds the maximum length of 12") and warns again for
    `IS(text)` under that mode. The reload reads under `IGNORE`: ingest
    already took the text, and a reopen neither refuses nor warns a second
    time. (A DS is no probe here: `DSfloat` warns under no mode, and raises
    only under `RAISE`.) Killing mutation: the hook constructing without
    `validation_mode=IGNORE`."""
    long_is = "1234567890123"
    _source(tmp_path / "src", extra=[(0x00200012, "IS", long_is)])
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "src"))
        session.save(sync=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with DicomSession(db) as session:
            value = _instance(session).attributes["0020,0012"]
    assert str(value) == long_is
    assert [str(w.message) for w in caught
            if issubclass(w.category, UserWarning)
            and "for VR IS" in str(w.message)] == []

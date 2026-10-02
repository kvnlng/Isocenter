"""An Instance Number the file lacks is not invented, and an infinite IS does not refuse the file (#870).

Two defects, measured on `main` at 7579d4df.

**The fabricated 0.** `Instance.__post_init__` sets `0020,0013` from
`instance_number`, which ingest reads as 0 for a file with none (#810), and
`populate_attrs` writes the file's element over it only when the file has
one. So a file with no Instance Number was exported with `(0020,0013) '0'`.
Both hydration sites (`load_all` and `load_patient`) construct the
`Instance` the same way and then `attributes.update()` the stored JSON,
which keeps the 0 when the JSON lacks the key: fixing ingest alone still
exported 0 after a reopen.

**The refusal.** pydicom reads an IS of `inf`, `-inf` or `1e400` as a float
it cannot make an int of, and its `OverflowError` escapes `ds.get()` and
`ds[tag]` in its default reading mode, so the file was refused at ingest.
The owner's ruling covers every IS element, Series Number included: the
file is ingested, and the element that cannot be written is dropped at
export with one `DATA_LOSS` row, as an IS of `ab12cd` already was.

Assertions read the written element (`get_item`), never a parsed number.
"""
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataelem import RawDataElement
from pydicom.tag import Tag

from isocenter.io_handlers import ingest_worker
from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession

PATIENT = "1CT1"


def _source(src, edit=None):
    """CT_small.dcm (Explicit VR LE) under `src`, edited by `edit(ds)`."""
    src.mkdir(parents=True, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    if edit is not None:
        edit(ds)
    path = src / "a.dcm"
    ds.save_as(str(path), enforce_file_format=True)
    return path


def _drop_instance_number(ds):
    del ds.InstanceNumber


def _raw_is(tag, text):
    """An edit that writes the IS element `tag` as exactly `text`, past pydicom."""
    data = text.encode()
    if len(data) % 2:
        data += b" "

    def edit(ds):
        ds._dict[Tag(tag)] = RawDataElement(
            Tag(tag), "IS", len(data), data, 0, False, True)
    return edit


def _export(tmp_path, *, reopen=False):
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"
    with DicomSession(db) as s:
        summary = s.ingest(str(tmp_path / "src"))
        if reopen:
            s.save(sync=True)
        else:
            s.export(str(out), use_compression=False)
            losses = s.store_backend.get_audit_losses()
    if reopen:
        with DicomSession(db) as s:
            s.export(str(out), use_compression=False)
            losses = s.store_backend.get_audit_losses()
    (written,) = list(out.rglob("*.dcm"))
    return summary, pydicom.dcmread(str(written)), losses


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_file_without_instance_number_exports_without_one(tmp_path, reopen):
    _source(tmp_path / "src", _drop_instance_number)
    _summary, ds, _losses = _export(tmp_path, reopen=reopen)
    assert 0x00200013 not in ds


def test_load_patient_does_not_invent_it(tmp_path):
    _source(tmp_path / "src", _drop_instance_number)
    db = str(tmp_path / "s.db")
    with DicomSession(db) as s:
        s.ingest(str(tmp_path / "src"))
        s.save(sync=True)
    store = SqliteStore(db)
    try:
        patient = store.load_patient(PATIENT)
        (inst,) = patient.studies[0].series[0].instances
        assert "0020,0013" not in inst.attributes
        # The column still takes 0, as #810 ruled.
        assert inst.instance_number == 0
    finally:
        store.stop()


def test_the_store_column_still_reads_zero(tmp_path):
    _source(tmp_path / "src", _drop_instance_number)
    with DicomSession(str(tmp_path / "s.db")) as s:
        s.ingest(str(tmp_path / "src"))
        s.save(sync=True)
    with sqlite3.connect(tmp_path / "s.db") as conn:
        assert conn.execute(
            "SELECT instance_number FROM instances").fetchall() == [(0,)]


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_file_with_instance_number_zero_keeps_it(tmp_path, reopen):
    _source(tmp_path / "src", _raw_is(0x00200013, "0"))
    _summary, ds, _losses = _export(tmp_path, reopen=reopen)
    assert ds.get_item(0x00200013).value == b"0 "


def test_a_zero_length_instance_number_stays_zero_length(tmp_path):
    _source(tmp_path / "src", _raw_is(0x00200013, ""))
    _summary, ds, _losses = _export(tmp_path)
    element = ds.get_item(0x00200013)
    assert element is not None
    assert element.value in (b"", "", None)


@pytest.mark.parametrize("tag", [0x00200013, 0x00200011, 0x00200012],
                         ids=["instance-number", "series-number",
                              "acquisition-number"])
@pytest.mark.parametrize("text", ["inf", "-inf", "1e400"])
@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_an_infinite_is_is_ingested_and_its_element_dropped(tmp_path, tag, text, reopen):
    _source(tmp_path / "src", _raw_is(tag, text))
    summary, ds, losses = _export(tmp_path, reopen=reopen)

    assert summary.failures == []
    assert ds.pixel_array.shape == (128, 128)
    tag_text = f"{tag >> 16:04x},{tag & 0xFFFF:04x}"
    rows = [row for row in losses if f"Tag {tag_text} not exported" in row[2]]
    assert len(rows) == 1, losses
    assert rows[0][3] == "STANDARD"
    assert tag not in ds


@pytest.mark.parametrize("text", ["inf", "-inf", "1e400"])
def test_an_infinite_number_of_frames_over_pixels_is_refused_by_name(tmp_path, text):
    """NumberOfFrames is the one IS element the file cannot be read without.

    The frame count divides Pixel Data into frames, so an image whose
    count reads as infinite has no layout to decode; the file is refused,
    as it was. What changed (review of #900, F3) is the reason: it named
    `OverflowError: cannot convert float infinity to integer` and no tag.
    """
    path = _source(tmp_path / "src", _raw_is(0x00280008, text))
    _meta, inst, *_rest, error = ingest_worker(str(path))
    assert inst is None
    assert error.startswith("ValueError: NumberOfFrames (0028,0008) reads as "
                            "infinite"), error


def test_an_infinite_number_of_frames_with_no_pixels_is_ingested(tmp_path):
    """Without Pixel Data the count lays nothing out: an IS like the rest."""
    def edit(ds):
        del ds.PixelData
        _raw_is(0x00280008, "inf")(ds)

    _source(tmp_path / "src", edit)
    with DicomSession(str(tmp_path / "s.db")) as s:
        summary = s.ingest(str(tmp_path / "src"))
    assert summary.failures == []


@pytest.mark.parametrize("text", ["inf", "1e400"])
def test_the_worker_reads_an_infinite_instance_number_as_zero(tmp_path, text):
    path = _source(tmp_path / "src", _raw_is(0x00200013, text))
    _meta, inst, *_rest, error = ingest_worker(str(path))
    assert error is None, error
    assert inst.instance_number == 0
    # The element is held as the file's text, which cannot be written as
    # an IS; the export drops it with its row (above).
    assert str(inst.attributes["0020,0013"]).strip() == text


def test_an_infinite_is_from_an_implicit_vr_source_is_ingested(tmp_path):
    """Under Implicit VR the raw element states no VR, so the IS is the
    dictionary's; the file is ingested all the same."""
    src = tmp_path / "src"
    src.mkdir()
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    marker = "000000097531"
    ds.AcquisitionNumber = marker
    ds.file_meta.TransferSyntaxUID = pydicom.uid.ImplicitVRLittleEndian
    path = src / "a.dcm"
    ds.save_as(str(path), enforce_file_format=True, implicit_vr=True,
               little_endian=True)
    data = path.read_bytes()
    assert data.count(marker.encode()) == 1
    path.write_bytes(data.replace(marker.encode(), b"inf".ljust(12)))
    assert pydicom.dcmread(str(path)).get_item(0x00200012).VR is None
    _meta, inst, *_rest, error = ingest_worker(str(path))
    assert error is None, error
    assert inst.attributes["0020,0012"] == "inf"


def test_an_infinite_series_number_reads_as_zero(tmp_path):
    path = _source(tmp_path / "src", _raw_is(0x00200011, "inf"))
    meta, inst, *_rest, error = ingest_worker(str(path))
    assert error is None, error
    assert meta["series_num"] == 0


def test_an_unparseable_instance_number_is_dropped_with_a_row(tmp_path):
    _source(tmp_path / "src", _raw_is(0x00200013, "ab12cd"))
    summary, ds, losses = _export(tmp_path)
    assert summary.failures == []
    assert ds.pixel_array.shape == (128, 128)
    rows = [row for row in losses if "Tag 0020,0013 not exported" in row[2]]
    assert len(rows) == 1, losses
    assert 0x00200013 not in ds

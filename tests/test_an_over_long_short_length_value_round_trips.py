"""A value too long for its VR's 2-byte Explicit VR length is said, and our own export re-ingests it (#692).

An Explicit VR element of a VR with a 2-byte length (US, SS, UL, SL, FL, FD,
DS, IS, AT, the short text VRs) holds at most 65535 bytes. Implicit VR has a
4-byte length for every element, so a long value -- a Real World Value LUT
Data `(0040,9212)` FD list from an Implicit VR source, or a long list a
caller sets -- is legal there. pydicom writes such an element as `UN` under
an Explicit VR syntax (PS3.5 6.2.2), which is conformant. Measured on `main`
at 7579d4df:

- the compressed export, the default, wrote it `UN` with no row and no note
  (pydicom's warning fires in a spawned worker and never reaches the
  caller);
- re-ingesting that export dropped it: pydicom does not decode a `UN` of
  known VR above 0xFFFF bytes, and `populate_attrs`' `UN` size gate took it,
  with a `DATA_LOSS` row. This library could not re-ingest its own output.

The owner's rulings (Q4, Q5): an INFO note at export, and ingest decodes a
standard `UN` under its dictionary VR, so the round trip holds.
"""
import logging
import shutil

import numpy as np
import pydicom
import pytest
from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.filebase import DicomBytesIO
from pydicom.filewriter import write_dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

import isocenter.io_handlers as io_handlers
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
RWV = 0x00409212           # Real World Value LUT Data, FD 1-n
RWV_TEXT = "0040,9212"
VALUES = [float(i) / 4 for i in range(9000)]    # 72000 bytes


def _source(folder, *, syntax=ImplicitVRLittleEndian, extra=None):
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.692.1"
    meta.TransferSyntaxUID = syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = SC, meta.MediaStorageSOPInstanceUID
    ds.PatientID, ds.PatientName = "P692", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.692.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.692.3"
    ds.Modality, ds.StudyDate, ds.StudyTime = "OT", "20200101", "120000"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(16, dtype=np.uint16).tobytes()
    if extra is not None:
        extra(ds)
    ds.save_as(str(folder / "a.dcm"), enforce_file_format=True,
               implicit_vr=syntax == ImplicitVRLittleEndian, little_endian=True)


def _with_rwv(ds):
    ds.add_new(RWV, "FD", VALUES)


def _export(src, db, out, *, compress, mutate=None):
    with DicomSession(str(db)) as s:
        s.ingest(str(src))
        if mutate is not None:
            (p,) = s.store.patients
            mutate(p.studies[0].series[0].instances[0])
        s.export(str(out), use_compression=compress, show_progress=False)
        losses = s.store_backend.get_audit_losses()
        rows = s.store_backend.get_audit_errors()
    (written,) = list(out.rglob("*.dcm"))
    return written, losses, rows


def _notes(caplog):
    return [r.getMessage() for r in caplog.records if "#692" in r.getMessage()]


def test_an_implicit_source_fd_list_round_trips_through_our_own_compressed_export(tmp_path):
    _source(tmp_path / "src", extra=_with_rwv)
    first, _losses, _rows = _export(tmp_path / "src", tmp_path / "a.db",
                                    tmp_path / "out1", compress=True)
    assert pydicom.dcmread(str(first)).get_item(RWV).VR == "UN"

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    second, losses, _rows = _export(tmp_path / "again", tmp_path / "b.db",
                                    tmp_path / "out2", compress=False)

    assert not [r for r in losses if RWV_TEXT in r[2]], losses
    ds = pydicom.dcmread(str(second))
    assert ds[RWV].VR == "FD"
    assert list(ds[RWV].value) == VALUES


def test_the_relabel_to_un_is_said(tmp_path, caplog):
    _source(tmp_path / "src", extra=_with_rwv)
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _written, _losses, rows = _export(tmp_path / "src", tmp_path / "a.db",
                                          tmp_path / "out", compress=True)
    notes = _notes(caplog)
    assert len(notes) == 1, notes
    assert RWV_TEXT in notes[0] and "UN" in notes[0] and "72000" in notes[0]
    assert not [r for r in rows if "#692" in r[2]], rows


def test_an_implicit_export_writes_it_under_its_own_vr(tmp_path, caplog):
    _source(tmp_path / "src", extra=_with_rwv)
    with caplog.at_level(logging.INFO, logger="isocenter"):
        written, _losses, _rows = _export(tmp_path / "src", tmp_path / "a.db",
                                          tmp_path / "out", compress=False)
    ds = pydicom.dcmread(str(written))
    assert str(ds.file_meta.TransferSyntaxUID) == ImplicitVRLittleEndian
    assert ds[RWV].VR == "FD"
    assert _notes(caplog) == []


def test_a_short_value_is_not_relabelled(tmp_path, caplog):
    _source(tmp_path / "src", extra=lambda ds: ds.add_new(RWV, "FD", VALUES[:100]))
    with caplog.at_level(logging.INFO, logger="isocenter"):
        written, _losses, _rows = _export(tmp_path / "src", tmp_path / "a.db",
                                          tmp_path / "out", compress=True)
    assert pydicom.dcmread(str(written)).get_item(RWV).VR == "FD"
    assert _notes(caplog) == []


def test_a_private_un_over_the_threshold_is_still_dropped(tmp_path):
    """The decode is for standard tags: a private `UN` has no dictionary VR
    to decode under, and keeps the size gate."""
    def extra(ds):
        ds.add_new(0x00090010, "LO", "ACME")
        ds.add_new(0x00091001, "UN", bytes(70000))

    _source(tmp_path / "src", syntax=ExplicitVRLittleEndian, extra=extra)
    with DicomSession(str(tmp_path / "a.db")) as s:
        s.ingest(str(tmp_path / "src"))
        (p,) = s.store.patients
        attrs = p.studies[0].series[0].instances[0].attributes
        losses = s.store_backend.get_audit_losses()
    assert "0009,1001" not in attrs
    assert [r for r in losses if "0009,1001" in r[2]], losses


def test_a_caller_us_list_is_said_and_round_trips(tmp_path, caplog):
    values = list(range(40000))
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src")
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True,
            mutate=lambda i: i.set_attr("0018,1310", values))
    notes = _notes(caplog)
    assert len(notes) == 1 and "0018,1310" in notes[0], notes

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    second, losses, _rows = _export(tmp_path / "again", tmp_path / "b.db",
                                    tmp_path / "out2", compress=False)
    assert not [r for r in losses if "0018,1310" in r[2]], losses
    assert list(pydicom.dcmread(str(second))[0x00181310].value) == values


def _explicit_bytes(ds):
    buffer = DicomBytesIO()
    buffer.is_little_endian = True
    buffer.is_implicit_VR = False
    write_dataset(buffer, ds)
    return buffer.getvalue()


@pytest.mark.parametrize("vr,value", [
    ("FD", VALUES),
    ("US", list(range(40000))),
    ("SL", [-i for i in range(20000)]),
    ("LO", ["x" * 60] * 1200),
])
def test_our_relabel_writes_the_bytes_pydicom_would(vr, value):
    """Nothing in the golden cohort exercises the relabel, so this is the
    check that it encodes as pydicom itself does: same tag, VR, length and
    value bytes."""
    tag = {"FD": RWV, "US": 0x00181310, "SL": 0x00189219,
           "LO": 0x00081030}[vr]
    theirs = Dataset()
    theirs.add(DataElement(tag, vr, value))
    ours = Dataset()
    ours.add(DataElement(tag, vr, value))
    notes = []
    io_handlers._relabel_long_short_length_values(ours, notes)
    assert ours[tag].VR == "UN"
    assert len(notes) == 1
    with pytest.warns(UserWarning, match="64 kByte"):
        expected = _explicit_bytes(theirs)
    assert _explicit_bytes(ours) == expected

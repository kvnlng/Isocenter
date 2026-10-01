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


#: Noise, so a lossless J2K stream of it stays above 65535 bytes.
NOISE = np.random.default_rng(692).integers(
    -32768, 32768, size=(256, 256), dtype=np.int16)


@pytest.mark.parametrize("compress", [True, False])
def test_pixel_data_over_the_threshold_is_never_relabelled(tmp_path, caplog, compress):
    """Pixel Data's VR is a 4-byte-length VR whatever it is spelled as.

    Under compression `_compress_j2k` assigns the stream to a dataset with
    no Pixel Data element, so pydicom gives it the dictionary's `OB or OW`,
    a spelling outside `EXPLICIT_VR_LENGTH_32`, and pydicom settles it only
    inside `dcmwrite`. Read as "has a 2-byte length", a stream over 65535
    bytes was relabelled `UN` and written with an undefined length, a file
    pydicom cannot read back. Found by the fingerprint retake: 72 files,
    every compressed export whose stream passed 64 KiB. The relabel now
    weighs only the VRs pydicom names as 2-byte (`EXPLICIT_VR_LENGTH_16`).
    Noise, so the stream stays that long.
    """
    def big(ds):
        ds.Rows = ds.Columns = 256
        ds.PixelRepresentation = 1
        ds.PixelData = NOISE.tobytes()

    _source(tmp_path / "src", syntax=ExplicitVRLittleEndian, extra=big)
    with caplog.at_level(logging.INFO, logger="isocenter"):
        written, _losses, _rows = _export(tmp_path / "src", tmp_path / "a.db",
                                          tmp_path / "out", compress=compress)
    ds = pydicom.dcmread(str(written))
    assert ds["PixelData"].VR in ("OB", "OW")
    assert len(ds.PixelData) > 65535
    assert np.array_equal(ds.pixel_array, NOISE)
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


def test_a_caller_lut_data_list_under_an_ambiguous_vr_round_trips(tmp_path, caplog):
    """LUT Data (0028,3006) is `US or OW` in the dictionary.

    Its long `US` list is written `UN`; re-ingest read nothing under an
    ambiguous dictionary VR, so the `UN` size gate dropped it with a
    `DATA_LOSS` row (review of #900, F2, the spec's repro row 2). It is now
    read on its `US` arm, and the note says so.
    """
    values = [(i * 7) % 65536 for i in range(40000)]
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src")
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True,
            mutate=lambda i: (i.set_attr("0028,3002", [0, 0, 16]),
                              i.set_attr("0028,3006", values)))
    notes = _notes(caplog)
    assert len(notes) == 1 and "0028,3006" in notes[0], notes
    assert "reads them back under US" in notes[0]

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    second, losses, _rows = _export(tmp_path / "again", tmp_path / "b.db",
                                    tmp_path / "out2", compress=False)
    assert not [r for r in losses if "0028,3006" in r[2]], losses
    # The words, whichever arm pydicom reads the Implicit VR file under (a
    # 16-bit LUT Descriptor reads it `OW`, as bytes).
    read = pydicom.dcmread(str(second))[0x00283006].value
    words = (np.frombuffer(read, "<u2").tolist()
             if isinstance(read, bytes) else list(read))
    assert words == values


def test_a_caller_private_list_note_says_it_is_dropped_on_re_ingest(tmp_path, caplog):
    """A private tag has no dictionary VR: the narrowed promise, pinned.

    The note says what happens -- a private `UN` over 65534 bytes is
    dropped at ingest with a `DATA_LOSS` row -- and it is.
    """
    def private(i):
        i.set_attr("0009,0010", "ACME")
        # No recorded VR: the fallback writes the list as `LO` text,
        # about 230 KB of it.
        i.set_attr("0009,1001", list(range(40000)))

    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src")
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True, mutate=private)
    notes = _notes(caplog)
    assert len(notes) == 1 and "0009,1001" in notes[0], notes
    assert "reads them back" not in notes[0]
    assert "drops a private UN over 65534 bytes at ingest" in notes[0]

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    _second, losses, _rows = _export(tmp_path / "again", tmp_path / "b.db",
                                     tmp_path / "out2", compress=False)
    assert [r for r in losses if "0009,1001" in r[2]], losses


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

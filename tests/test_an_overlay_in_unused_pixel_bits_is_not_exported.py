"""An overlay stored in Pixel Data's unused high bits is not exported, and says so (#755).

The issue's premise was that a file declaring an overlay in Pixel Data
(group 60xx with OverlayBitsAllocated above 1 and no Overlay Data
`60xx,3000`) keeps those bits in the samples above BitsStored, and that
an export removing the `60xx` descriptors would leave them hidden under
PASS. Measured on `main` at 7579d4df (pydicom 3.0.2), that is not what
happens: ingest decodes through pydicom's `as_array`, whose
`correct_unused_bits` default clears every bit above BitsStored, so the
bits never reach the store or any export. Under a policy that keeps
`60xx` the export carried OverlayBitPosition pointing at bits that were
gone, with no row.

So this file pins two things (owner ruling Q1-B):

* **today's behaviour, by a library default**, which is the
  correct-by-accident shape: no exported sample holds a bit at or above
  BitsStored, read from the **raw** `PixelData` buffer (`pixel_array`
  would mask again and pass whatever the file holds). The source fixture
  is asserted to hold the 550 overlay bits first, so the test cannot
  pass on a fixture that never had them. Three encodings: native
  unsigned, native signed, RLE Lossless.
* **one STANDARD `DATA_LOSS` row at ingest** naming the group, its bit
  and BitsStored, which does not move the grade (STANDARD rows are not
  graded).

A JPEG 2000 stream whose own precision is 16 is the other case: the
decode reads the stream's width, the bits become sample values, the
export widens BitsStored, and the existing precision `WARNING` grades the
run `REVIEW_REQUIRED`. It gets no overlay row, because nothing was
cleared.
"""
import glob
import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (ExplicitVRLittleEndian, JPEG2000Lossless,
                         RLELossless, generate_uid)

from isocenter.session import DicomSession

ROWS = COLS = 64
BITS_STORED = 12
OVERLAY_BIT = 12
#: rows 10..19, cols 5..59.
OVERLAY_PIXELS = 10 * 55
ROW_TEXT = (
    "Overlay group 6000 is declared in Pixel Data at bit 12, at or above "
    "BitsStored 12: the decode reads BitsStored bits, so the stored pixels "
    "and every export carry no overlay bits.")


def _dataset(signed=False, bit=OVERLAY_BIT):
    arr = (np.arange(ROWS * COLS, dtype=np.uint16).reshape(ROWS, COLS) % 2048)
    overlay = np.zeros((ROWS, COLS), dtype=bool)
    overlay[10:20, 5:60] = True
    arr = arr | (overlay.astype(np.uint16) << bit)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = "Doe^Jane"
    ds.PatientID = "MRN755"
    ds.StudyDate = "20200101"
    ds.Modality = "OT"
    ds.Rows, ds.Columns = ROWS, COLS
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = BITS_STORED
    ds.HighBit = BITS_STORED - 1
    ds.PixelRepresentation = 1 if signed else 0
    ds.add_new(0x60000010, "US", ROWS)
    ds.add_new(0x60000011, "US", COLS)
    ds.add_new(0x60000040, "CS", "G")
    ds.add_new(0x60000050, "SS", [1, 1])
    ds.add_new(0x60000100, "US", 16)
    ds.add_new(0x60000102, "US", bit)
    return ds, arr


def _write(tmp_path, encoding):
    src = tmp_path / "input"
    src.mkdir()
    path = str(src / "overlay.dcm")
    ds, arr = _dataset(signed=(encoding == "native_signed"))
    if encoding == "j2k_precision_16":
        stream = imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K",
                                           reversible=True)
        ds.PixelData = encapsulate([stream])
        ds["PixelData"].VR = "OB"
        ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
    else:
        ds.PixelData = arr.tobytes()
        if encoding == "rle":
            ds.compress(RLELossless, encoding_plugin="pydicom")
    ds.save_as(path, enforce_file_format=True)
    return str(src), arr


def _bits_at_or_above(raw, bits_stored):
    return int(((raw.astype(np.uint32) >> bits_stored) != 0).sum())


def _pipeline(tmp_path, encoding):
    src, arr = _write(tmp_path, encoding)
    assert int(((arr >> OVERLAY_BIT) & 1).sum()) == OVERLAY_PIXELS, (
        "precondition: the source fixture must hold the overlay bits")
    db = str(tmp_path / "s.db")
    out = str(tmp_path / "out")
    report = str(tmp_path / "report.md")
    with DicomSession(persistence_file=db) as session:
        session.ingest(src)
        session.anonymize(session.audit())
        session.export(out, use_compression=False)
        session.generate_report(report)
    (exported,) = glob.glob(os.path.join(out, "**", "*.dcm"), recursive=True)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT action_type, details, loss_scope FROM audit_log "
            "WHERE action_type IN ('DATA_LOSS', 'WARNING')").fetchall()
    with open(report, encoding="utf-8") as fh:
        (grade,) = [line for line in fh.read().splitlines()
                    if "Validation Status" in line]
    return pydicom.dcmread(exported), rows, grade


@pytest.mark.parametrize("encoding", ["native", "native_signed", "rle"])
def test_the_overlay_bits_never_reach_an_export_and_the_loss_is_a_row(
        tmp_path, encoding):
    exported, rows, grade = _pipeline(tmp_path, encoding)

    raw = np.frombuffer(exported.PixelData, dtype="<u2")
    if encoding == "native_signed":
        # Signed samples are sign-extended over the unused bits; read
        # the BitsStored-wide value back and compare the container.
        signed = raw.view("<i2").astype(np.int32)
        low = -(1 << (BITS_STORED - 1))
        high = (1 << (BITS_STORED - 1)) - 1
        assert int(((signed < low) | (signed > high)).sum()) == 0
    else:
        assert _bits_at_or_above(raw, BITS_STORED) == 0
    assert exported.BitsStored == BITS_STORED

    assert rows.count(("DATA_LOSS", ROW_TEXT, "STANDARD")) == 1, rows
    assert grade == "| **Validation Status** | **PASS** |", grade


def test_a_stream_wider_than_bits_stored_keeps_the_bits_and_is_reviewed(tmp_path):
    exported, rows, grade = _pipeline(tmp_path, "j2k_precision_16")

    raw = np.frombuffer(exported.PixelData, dtype="<u2")
    assert int(((raw >> OVERLAY_BIT) & 1).sum()) == OVERLAY_PIXELS
    assert exported.BitsStored == 16
    assert grade == "| **Validation Status** | **REVIEW_REQUIRED** |", grade
    assert any(action == "WARNING" and "precision is 16" in details
               for action, details, _scope in rows), rows
    assert not any("Overlay group" in details for _a, details, _s in rows), rows


def test_an_overlay_below_bits_stored_is_not_called_lost(tmp_path):
    """The position test is `>= BitsStored`: bit 11 is a stored bit."""
    from isocenter.io_handlers import _in_pixel_overlays
    ds, _ = _dataset(bit=BITS_STORED - 1)
    assert _in_pixel_overlays(ds) is None
    ds, _ = _dataset(bit=BITS_STORED)
    assert _in_pixel_overlays(ds) == [("6000", BITS_STORED)]
    # A second group, at bit 13, is named beside the first.
    ds.add_new(0x60020100, "US", 16)
    ds.add_new(0x60020102, "US", 13)
    assert _in_pixel_overlays(ds) == [("6000", BITS_STORED), ("6002", 13)]
    # Overlay Data present: the overlay is its own element, not Pixel Data's.
    ds.add_new(0x60003000, "OW", b"\x00" * (ROWS * COLS // 8))
    ds.add_new(0x60023000, "OW", b"\x00" * (ROWS * COLS // 8))
    assert _in_pixel_overlays(ds) is None


def test_two_overlays_are_named_in_one_row():
    from isocenter.io_handlers import _in_pixel_overlay_words
    assert _in_pixel_overlay_words(
        {"overlays": [("6000", 12), ("6002", 13)], "bits_stored": 12}) == (
        "Overlay groups 6000 at bit 12 and 6002 at bit 13 are declared in "
        "Pixel Data, at or above BitsStored 12: the decode reads BitsStored "
        "bits, so the stored pixels and every export carry no overlay bits.")

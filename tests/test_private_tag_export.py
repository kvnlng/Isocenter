"""Private tags reach the graph and then vanish at export (#118).

`remove_private_tags=False` is the caller saying "keep the vendor block".
Ingest honours it -- the odd-group tags are in the object graph and in the
`instance_attributes` table -- and then `DicomExporter._merge` drops every
one of them on the way out, because `dictionary_VR` raises for a tag the
standard dictionary does not know and the `except` arm only logged.

So the flag appeared to work right up until the exported file was read
back. The audit report listed the tags as retained; the file did not have
them.
"""
import glob
import logging
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import DicomExporter
from isocenter.session import DicomSession

# A value longer than LO's 64-character limit, so the fallback cannot
# simply reach for LO and hope.
LONG_VALUE = "acquisition-profile-" + ("x" * 80)


def _write_src(folder, private=None):
    """A minimal single-instance study carrying a private block."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"

    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')        # Private Creator
    ds.add_new(0x00091001, 'LO', 'acquisition-v7')
    ds.add_new(0x00091002, 'LT', LONG_VALUE)

    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    path = os.path.join(folder, "one.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _private(ds):
    """`{"gggg,eeee": value}` for every odd-group tag in `ds`."""
    return {f"{el.tag.group:04x},{el.tag.element:04x}": el.value
            for el in ds if el.tag.group % 2 == 1}


def _roundtrip(tmp_path, anonymize=None):
    """Ingest a private-block study, optionally anonymize, export, re-read.

    `anonymize` is None (export straight through) or the value to give
    `remove_private_tags` before running the privacy pipeline.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))
    out = tmp_path / "out"

    session = DicomSession(persistence_file=str(tmp_path / "priv.db"))
    try:
        session.ingest(str(src))
        if anonymize is not None:
            session.configuration.remove_private_tags = anonymize
            session.audit()
            session.anonymize()
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"
    return pydicom.dcmread(written[0])


def test_a_private_tag_survives_export(tmp_path):
    """The bug in one line: ingested, held, exported without it."""
    assert _private(_roundtrip(tmp_path)).get("0009,1001") == "acquisition-v7"


def test_the_private_creator_survives_export(tmp_path):
    """Without (gggg,0010) the block is an anonymous byte range.

    A private tag whose Private Creator was dropped cannot be attributed
    to a vendor, which makes the value that *was* kept unreadable.
    """
    assert _private(_roundtrip(tmp_path)).get("0009,0010") == "ACME_HEADER"


def test_a_value_too_long_for_LO_survives_export(tmp_path):
    """The fallback VR has to be chosen from the value, not assumed.

    `LO` caps at 64 characters, so a fallback that always reached for it
    would round-trip the short tag in the test above and quietly mangle
    this one.
    """
    assert _private(_roundtrip(tmp_path)).get("0009,1002") == LONG_VALUE


def test_removing_private_tags_still_removes_them(tmp_path):
    """The other half of the flag, which was never broken -- and must not
    become broken by teaching the exporter to write these."""
    kept = _private(_roundtrip(tmp_path, anonymize=True))
    assert "0009,1001" not in kept, kept


def test_keeping_private_tags_keeps_them_through_anonymization(tmp_path):
    """#118 as reported: the flag says keep, the file says otherwise."""
    kept = _private(_roundtrip(tmp_path, anonymize=False))
    assert kept.get("0009,1001") == "acquisition-v7", kept


def test_a_binary_private_value_is_written_as_UN(tmp_path):
    """PS3.5 A.1: an unknown private value of raw bytes is `UN`.

    `UN` cannot hold a `str` -- pydicom raises at write time, not at
    `add_new` -- so this is the only branch it is right for.
    """
    ds = pydicom.Dataset()
    DicomExporter._merge(ds, {"0009,1003": b"\x01\x02\x03\x04"})

    assert ds[0x00091003].VR == "UN"
    assert ds[0x00091003].value == b"\x01\x02\x03\x04"


def test_an_unwritable_element_is_named_as_data_loss(caplog):
    """The fallback is not total, and the residue must not read as routine.

    `_merge` runs inside `_export_instance_worker`, which may be a
    subprocess, so there is no store handle to write an audit entry
    against (#126). The log line is all there is; it has to say that
    something was lost.
    """
    with caplog.at_level(logging.WARNING):
        DicomExporter._merge(pydicom.Dataset(), {"0009,1004": object()})

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("0009,1004" in m for m in msgs), msgs
    assert any("not exported" in m.lower() for m in msgs), msgs

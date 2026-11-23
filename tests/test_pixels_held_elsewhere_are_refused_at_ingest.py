"""An image whose pixels are held elsewhere is refused at ingest (#1097).

A file with no pixel element that names a Pixel Data Provider URL
(0028,7FE0) at the top level, or is under a JPIP Referenced transfer
syntax, holds an image this library never sees. Measured on 3e4ee91a,
3.12.14 and 3.14.7t: under Explicit VR Little Endian it was ingested and
exported with the URL unchanged, graded PASS, so the source UIDs a JPIP
URL's query carries left in the URL and no redaction zone was applied to
the pixels it names; under JPIP Referenced it was ingested, and every
export of it failed.

Now ingest refuses both shapes, in a sentence that never quotes the URL
(owner ruling 2026-11-22, Q13a; `### Breaking`: such files are in
`IngestSummary.failures`). An instance of the first shape already in a
store, ingested by 1.0.x, fails its export with one `ERROR` row in the
same sentence (Q13b). A pixel-bearing file with a URL is not this
refusal's (its export drops the URL with its own row), a URL inside a
sequence item is not looked for (owner ruling 2026-10-12), and a file
with no pixel element and no URL (an SR, a PDF) is ingested and exported.

Names `isocenter.io_handlers` and `isocenter.entities`.
"""
import os
import sqlite3

import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid

from isocenter.io_handlers import ingest_worker
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
EXPLICIT_LE = "1.2.840.10008.1.2.1"
JPIP = "1.2.840.10008.1.2.4.94"
#: A provider URL carrying a study's UID in its query, as a JPIP server's
#: does. The test asserts this text reaches no row and no failure.
URL = "https://pacs.example.org/jpip?study=1.2.826.0.1.3680043.9.1097.77"
TAIL = (", which this library does not fetch, so its pixels cannot be read, "
        "redacted or exported.")
URL_WORDS = ("Its pixels are held elsewhere: the file has no pixel element "
             "and names a Pixel Data Provider URL (0028,7FE0)" + TAIL)
JPIP_WORDS = ("Its pixels are held elsewhere: the file has no pixel element "
              "and is under JPIP Referenced (1.2.840.10008.1.2.4.94)" + TAIL)
BOTH_WORDS = ("Its pixels are held elsewhere: the file has no pixel element "
              "and names a Pixel Data Provider URL (0028,7FE0) and is under "
              "JPIP Referenced (1.2.840.10008.1.2.4.94)" + TAIL)


def _file(folder, *, syntax=EXPLICIT_LE, url=True, pixels=False,
          nested_url=False, modality="CT"):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = syntax
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "P1097", "DOE^JANE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID, ds.SOPClassUID = meta.MediaStorageSOPInstanceUID, SC
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = modality, 1, 1
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    if url:
        ds.add_new(0x00287FE0, "UR", URL)
    if pixels:
        ds.PixelData = bytes(16)
    if nested_url:
        item = Dataset()
        item.ReferencedSOPInstanceUID = generate_uid()
        item.add_new(0x00287FE0, "UR", URL)
        ds.ReferencedImageSequence = Sequence([item])
    os.makedirs(str(folder), exist_ok=True)
    path = os.path.join(str(folder), "one.dcm")
    # Written Explicit VR Little Endian whatever the meta says: that is
    # how pydicom reads a dataset under JPIP Referenced.
    pydicom.dcmwrite(path, ds, implicit_vr=False, little_endian=True,
                     enforce_file_format=True)
    return path


def _rows(db):
    with sqlite3.connect(str(db)) as conn:
        return conn.execute(
            "SELECT action_type, details FROM audit_log").fetchall()


@pytest.mark.parametrize("syntax, url, words", [
    (EXPLICIT_LE, True, URL_WORDS), (JPIP, False, JPIP_WORDS),
    (JPIP, True, BOTH_WORDS)], ids=["url", "jpip", "both"])
def test_ingest_refuses_each_shape_in_its_sentence(tmp_path, syntax, url,
                                                   words):
    path = _file(tmp_path / "src", syntax=syntax, url=url)
    assert ingest_worker(path)[-1] == f"ValueError: {words}"

    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        summary = session.ingest(str(tmp_path / "src"))
        assert summary.ingested == 0
        assert len(summary.failures) == 1
        assert words in summary.failures[0][1]
        assert not session.store.patients
    for _action, details in _rows(db):
        assert "pacs.example.org" not in (details or "")
        assert "9.1097.77" not in (details or "")
    assert "pacs.example.org" not in summary.failures[0][1]


def test_a_file_with_pixels_and_a_url_is_not_refused(tmp_path):
    path = _file(tmp_path / "src", pixels=True)
    assert ingest_worker(path)[-1] is None


def test_a_url_inside_a_sequence_item_is_not_looked_for(tmp_path):
    """Owner ruling 2026-10-12: the top level only."""
    path = _file(tmp_path / "src", url=False, nested_url=True, modality="SR")
    assert ingest_worker(path)[-1] is None


def test_a_file_with_no_pixels_and_no_url_is_ingested_and_exported(tmp_path):
    _file(tmp_path / "src", url=False, modality="SR")
    with DicomSession(str(tmp_path / "s.db")) as session:
        assert session.ingest(str(tmp_path / "src")).ingested == 1
        summary = session.export(str(tmp_path / "out"), show_progress=False)
        assert summary.written == 1, summary.failures


def test_an_instance_already_in_a_store_fails_its_export(tmp_path):
    """Q13b: an instance a 1.0.x ingest admitted, here given its URL on the
    graph after a pixel-less ingest, as such a store holds it."""
    _file(tmp_path / "src", url=False, modality="OT")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        assert session.ingest(str(tmp_path / "src")).ingested == 1
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        inst.set_attr("0028,7fe0", URL)
        summary = session.export(str(tmp_path / "out"), show_progress=False)
        assert summary.written == 0
        assert len(summary.failures) == 1
    errors = [d for a, d in _rows(db) if a == "ERROR"]
    assert len(errors) == 1, errors
    assert URL_WORDS in errors[0], errors
    assert all("pacs.example.org" not in (d or "") for _a, d in _rows(db))
    assert not [f for _r, _d, fs in os.walk(tmp_path / "out") for f in fs
                if f.endswith(".dcm")]


def test_an_instance_with_pixels_and_a_url_still_exports(tmp_path):
    """The export check sits after the pixel read: a pixel-bearing
    instance holding a URL is not refused there."""
    path = _file(tmp_path / "src", url=False, pixels=True, modality="OT")
    assert ingest_worker(path)[-1] is None
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "src"))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        inst.set_attr("0028,7fe0", URL)
        summary = session.export(str(tmp_path / "out"), show_progress=False)
        assert summary.written == 1, summary.failures

"""Private tags with a binary VR never reach the graph, silently (#125).

`populate_attrs` skips every element whose VR is in `BINARY_VRS`. For
standard tags that is right: the pixel and waveform blobs belong in the
sidecar, and holding them in `attributes` would undo the memory scaling
the design depends on.

For a *private* tag it is a loss with nothing behind it. A vendor block
routinely carries `OB` elements, and they are gone before
`remove_private_tags=False` is ever consulted -- so the flag cannot keep
what it promises to keep, and nothing said so.

Whether those bytes should be *kept* is a real design question and is
still open. That they are dropped in silence is not; #36 settled the
pattern for exactly this shape -- warn, and write a `DATA_LOSS` audit
entry, because the log line alone is not a compliance trail.
"""
import os
import sqlite3

import numpy as np
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.session import DicomSession

PRIVATE_BINARY = "0009,1002"
PRIVATE_TEXT = "0009,1001"


def _write_src(folder, with_private_binary=True):
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

    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')      # Private Creator
    ds.add_new(0x00091001, 'LO', 'acquisition-v7')   # survives ingest
    if with_private_binary:
        ds.add_new(0x00091002, 'OB', b'\x01\x02\x03\x04')

    # A standard binary blob, which is *not* lost -- it goes to the
    # sidecar -- and so must not be reported.
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)
    return ds.SOPInstanceUID


def _ingest(tmp_path, with_private_binary=True):
    src = tmp_path / "src"
    src.mkdir()
    uid = _write_src(str(src), with_private_binary)

    session = DicomSession(persistence_file=str(tmp_path / "pb.db"))
    try:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        attrs = dict(inst.attributes)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()
    return uid, attrs, rows


def test_the_dropped_private_binary_tag_is_recorded_in_the_audit_log(tmp_path):
    uid, _attrs, rows = _ingest(tmp_path)

    assert len(rows) == 1, rows
    entity_uid, details = rows[0]
    assert entity_uid == uid
    assert PRIVATE_BINARY in details


def test_the_report_names_the_vr_so_the_loss_can_be_acted_on(tmp_path):
    """"A tag was dropped" is not actionable; the VR says what it was."""
    _uid, _attrs, rows = _ingest(tmp_path)
    assert "OB" in rows[0][1], rows[0][1]


def test_the_tag_is_still_dropped(tmp_path):
    """This reports the loss; it does not change it.

    Keeping the bytes is the open half of #125 and a genuine design
    decision -- an arbitrary private `OB` can be megabytes, which is what
    `BINARY_VRS` exists to keep out of resident memory.
    """
    _uid, attrs, _rows = _ingest(tmp_path)
    assert PRIVATE_BINARY not in attrs
    assert attrs[PRIVATE_TEXT] == "acquisition-v7"


def test_a_file_with_no_private_binary_reports_nothing(tmp_path):
    """PixelData is skipped too and is not lost -- it goes to the sidecar.

    Reporting it would put a DATA_LOSS entry in the record of every
    image ever ingested, which is how a compliance trail becomes noise.
    """
    _uid, _attrs, rows = _ingest(tmp_path, with_private_binary=False)
    assert rows == []


def test_a_private_binary_tag_inside_a_sequence_is_reported_too(tmp_path):
    """Vendor blocks nest. A loss one level down is still a loss.

    `populate_attrs` recurses through `process_sequence`, so the
    accumulator has to recurse with it -- otherwise the report covers
    only the top level and reads as "nothing else was dropped".
    """
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src), with_private_binary=False)

    import pydicom
    path = os.path.join(str(src), "one.dcm")
    ds = pydicom.dcmread(path)
    item = Dataset()
    item.add_new(0x00090010, 'LO', 'ACME_HEADER')
    item.add_new(0x00091003, 'OB', b'\xaa\xbb')
    ds.AnatomicRegionSequence = Sequence([item])
    ds.save_as(path, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "nest.db"))
    try:
        session.ingest(str(src))
        db_path = session.store_backend.db_path
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action_type='DATA_LOSS'").fetchall()

    assert any("0009,1003" in d for (d,) in rows), rows


def test_a_standard_binary_element_is_not_reported(tmp_path):
    """The scope line, pinned deliberately rather than left to a gate.

    Overlay Data `(6000,3000)` and the palette LUTs are `OW`, are skipped
    by the same rule, and are not written to the sidecar -- so they are
    genuinely dropped, and this does not report them.

    That is a narrower line than "report every loss", and the reason is
    that these are two different things. `populate_attrs` documents
    skipping overlays as a design choice, and dropping them costs
    fidelity but cannot leak: nothing writes an element that is not in
    the graph, and overlay planes are a classic burned-in PHI vector. A
    private binary tag was never such a choice -- it is collateral from a
    rule aimed at pixels, and it makes `remove_private_tags=False` a
    promise the code cannot keep. Reporting the second is closing a gap;
    reporting the first is a separate call, tracked separately.
    """
    import pydicom

    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src), with_private_binary=False)
    path = os.path.join(str(src), "one.dcm")
    ds = pydicom.dcmread(path)
    ds.add_new(0x60000010, 'US', 4)                    # OverlayRows
    ds.add_new(0x60003000, 'OW', b'\x01\x02\x03\x04')  # OverlayData
    ds.save_as(path, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "ovl.db"))
    try:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert "6000,0010" in inst.attributes, "precondition: the overlay was read"
        assert "6000,3000" not in inst.attributes, "precondition: its data was dropped"
        db_path = session.store_backend.db_path
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT 1 FROM audit_log WHERE action_type='DATA_LOSS'").fetchall()

    assert rows == []

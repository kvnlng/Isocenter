"""Binary-VR elements that reach the graph via nothing, silently (#125, #137).

`populate_attrs` skips every element whose VR is in `BINARY_VRS`. For
`PixelData` and `WaveformData` that is right and lossless: both are
extracted and written to the sidecar before this runs, and holding them
in `attributes` would undo the memory scaling the design depends on.

For everything else with a binary VR there is nothing behind the skip.
A private vendor block routinely carries `OB` elements, and they are
gone before `remove_private_tags=False` is ever consulted -- so the flag
cannot keep what it promises to keep (#125). Overlay Data and the
palette color LUTs are `OW`, standard, and equally unrouted; the
exported file keeps their `US` descriptors and so declares a plane it
does not carry (#137).

Whether those bytes should be *kept* is a real design question and is
still open. That they are dropped in silence is not; #36 settled the
pattern for exactly this shape -- warn, and write a `DATA_LOSS` audit
entry, because the log line alone is not a compliance trail.

The boundary that needs pinning is therefore "binary VR and routed
nowhere", not "binary VR" and not "odd group": widening it naively puts
a DATA_LOSS entry in the record of every image and every waveform ever
ingested.
"""
import os
import sqlite3

import numpy as np
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import LOSS_SCOPE_PRIVATE, LOSS_SCOPE_STANDARD
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
            "SELECT entity_uid, details, loss_scope FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()
    return uid, attrs, rows


def test_the_dropped_private_binary_tag_is_recorded_in_the_audit_log(tmp_path):
    uid, _attrs, rows = _ingest(tmp_path)

    assert len(rows) == 1, rows
    entity_uid, details, _scope = rows[0]
    assert entity_uid == uid
    assert PRIVATE_BINARY in details


def test_the_private_loss_is_recorded_as_private_scope(tmp_path):
    """The grade is decided here, not downstream (#146).

    `validation_status` flips to REVIEW_REQUIRED on a private loss and
    not on a standard one, and the only place that still knows which
    this was is the emitter. Deriving it later would mean parsing the
    tag back out of the `details` prose, which three differently-shaped
    emitters write and one of them does not name a tag at all.
    """
    _uid, _attrs, rows = _ingest(tmp_path)

    assert rows[0][2] == LOSS_SCOPE_PRIVATE, rows


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


def _ingest_with(tmp_path, name, mutate):
    """Write the base file, apply `mutate`, ingest, return DATA_LOSS rows."""
    import pydicom

    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src), with_private_binary=False)
    path = os.path.join(str(src), "one.dcm")
    ds = pydicom.dcmread(path)
    mutate(ds)
    ds.save_as(path, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
    try:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        attrs = dict(inst.attributes)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details, loss_scope FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()
    return attrs, rows


def test_overlay_data_is_reported(tmp_path):
    """Overlay Data `(6000,3000)` is `OW`, is skipped for its VR, and --
    unlike pixels and waveforms -- is routed nowhere. It is simply gone
    (#137).

    The exported file keeps the overlay *descriptors*, which are `US`
    and reach the graph, so it declares a plane it does not carry. That
    is the shape 0.8.1 removed from the WFDB header: an artefact
    asserting something it does not contain.
    """
    def add_overlay(ds):
        ds.add_new(0x60000010, 'US', 4)                    # OverlayRows
        ds.add_new(0x60003000, 'OW', b'\x01\x02\x03\x04')  # OverlayData

    attrs, rows = _ingest_with(tmp_path, "ovl", add_overlay)

    assert "6000,0010" in attrs, "precondition: the descriptors were read"
    assert "6000,3000" not in attrs, "precondition: the data was dropped"
    assert any("6000,3000" in d for d, _s in rows), rows
    assert any("OW" in d for d, _s in rows), rows


def test_overlay_data_is_recorded_as_standard_scope(tmp_path):
    """Reported, and deliberately not graded (#146).

    An overlay is dropped on ordinary images, by the thousand, with no
    vendor intent behind it. If this scope ever became PRIVATE the grade
    would read REVIEW_REQUIRED for most real cohorts and stop meaning
    anything -- the same failure mode #146 opened against a bare
    `DATA_LOSS: 3`.
    """
    def add_overlay(ds):
        ds.add_new(0x60000010, 'US', 4)
        ds.add_new(0x60003000, 'OW', b'\x01\x02\x03\x04')

    _attrs, rows = _ingest_with(tmp_path, "ovlscope", add_overlay)

    assert [s for _d, s in rows] == [LOSS_SCOPE_STANDARD], rows


def test_a_standard_loss_is_not_described_as_a_private_one(tmp_path):
    """The entry and its scope have to agree.

    Both losses ride one loop, which said "Private tag" for all of them
    until the group started deciding the grade -- so the report would
    have shown `Private tag 6000,3000` on a row scoped STANDARD and
    invited the reader to distrust whichever half they checked second.
    """
    def add_overlay(ds):
        ds.add_new(0x60003000, 'OW', b'\x01\x02\x03\x04')

    _attrs, rows = _ingest_with(tmp_path, "ovlprose", add_overlay)

    assert not any("Private" in d for d, _s in rows), rows


def test_palette_lut_data_is_reported(tmp_path):
    """`(0028,1201)` Red Palette Color LUT Data is the same case in an
    even group that is not an overlay, so it pins the rule rather than
    the one tag it was found through."""
    def add_lut(ds):
        ds.add_new(0x00281201, 'OW', b'\xbb' * 16)

    _attrs, rows = _ingest_with(tmp_path, "lut", add_lut)

    assert any("0028,1201" in d for d, _s in rows), rows
    assert [s for _d, s in rows] == [LOSS_SCOPE_STANDARD], rows


def test_waveform_data_is_not_reported(tmp_path):
    """The false positive the gate has to avoid.

    `(5400,1010)` Waveform Data is `OW` and *is* skipped by the VR rule
    -- but `ingest_worker` has already pulled it out and written it to
    the sidecar, exactly as it does for `PixelData`. Reporting it would
    put a `DATA_LOSS` entry in the record of every waveform ever
    ingested, which is how a compliance trail becomes noise.

    This is why #137's "widening the gate is one line" is not true: the
    rule is "binary VR and routed nowhere", not "binary VR".
    """
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    def add_waveform(ds):
        item = Dataset()
        item.NumberOfWaveformChannels = 1
        item.NumberOfWaveformSamples = 4
        item.SamplingFrequency = 100
        item.WaveformBitsAllocated = 16
        item.WaveformSampleInterpretation = "SS"
        item.add_new(0x54001010, 'OW', b'\x00\x01' * 4)
        ds.WaveformSequence = Sequence([item])

    _attrs, rows = _ingest_with(tmp_path, "wave", add_waveform)

    assert not any("5400,1010" in d for d, _s in rows), rows


def test_pixel_data_is_not_reported(tmp_path):
    """The other routed blob. Excluded above the VR check by its group,
    so it never reaches the gate -- pinned so that stays true."""
    _attrs, rows = _ingest_with(tmp_path, "pix", lambda ds: None)

    assert not any("7fe0,0010" in d for d, _s in rows), rows

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

# --- Group 7fe0: skipped by group, before the VR check ever runs (#169) ---
#
# `populate_attrs` took group `7fe0` out above the gate, so nothing in it
# could ever reach `dropped`. The group has six assigned members and the
# blanket skip was right for some of them and silent about the rest.
# Deciding which is which per element -- and, for one tag, per depth --
# is what #169 is:
#
#   (7fe0,0001) Extended Offset Table         OV     not data -> not a loss
#   (7fe0,0002) Extended Offset Table Lengths OV     not data -> not a loss
#   (7fe0,0003) Encapsulated PD Total Length  UV     not data -> not a loss
#   (7fe0,0008) Float Pixel Data              OF     routed at the top level
#   (7fe0,0009) Double Float Pixel Data       OD     routed at the top level
#   (7fe0,0010) Pixel Data                    OB/OW  routed at the top level
#   ... any of the last three in a sequence item     lost -> reported
#
# The three routed elements are routed by two different mechanisms.
# (7fe0,0010) goes to the sidecar at ingest. The float pair is not
# ingested at all: `_export_instance_worker` re-reads it through
# `get_pixel_data()` and writes it back under its own tag (#170, #193),
# which is weaker -- it needs the source file to still be there -- but it
# does reach the exported file, and a `DATA_LOSS` row saying otherwise
# would be false. The first three are not data at all: they are byte
# offsets into an encapsulated fragment stream that ingest decodes and
# export re-writes uncompressed, so they describe a layout the exported
# file does not have. Reporting them put two spurious rows on every
# encapsulated instance carrying an Extended Offset Table (#194).


def _float_src(tag, vr, dtype, keep_pixels=False):
    """Return a mutator that puts one float pixel element on the file."""
    def mutate(ds):
        if not keep_pixels:
            del ds.PixelData
        ds.add_new(tag, vr, np.arange(4, dtype=dtype).tobytes())
    return mutate


def test_float_pixel_data_is_not_reported_at_the_top_level(tmp_path):
    """(7fe0,0008) reaches the exported file, so it is not a loss (#193).

    It is not ingested -- `attributes` never holds it, and there is no
    sidecar blob for it -- but the export re-reads the source and writes
    it back under (7fe0,0008). `reporting.py`'s section 3 promises the
    elements it lists "were present in the source and are **not** in the
    exported data", so a row here would say something untrue about a
    file whose payload survived. That is the defect #194 opened against
    the first cut of this fix, and it applies to this tag for exactly
    the same reason.
    """
    attrs, rows = _ingest_with(tmp_path, "fpx",
                               _float_src(0x7FE00008, 'OF', np.float32))

    assert "7fe0,0008" not in attrs, "precondition: it does not reach the graph"
    assert rows == [], rows


def test_double_float_pixel_data_is_not_reported_either(tmp_path):
    """(7fe0,0009) is the same shape one VR over, and takes the same
    route out -- pinned so the rule reads as the pair and not as the one
    tag it was found through."""
    _attrs, rows = _ingest_with(tmp_path, "dfpx",
                                _float_src(0x7FE00009, 'OD', np.float64))

    assert rows == [], rows


def test_float_pixel_data_beside_pixel_data_is_reported():
    """The exemption is conditional, because the carrying is.

    PS3.5 A.1 makes Pixel Data and Float Pixel Data mutually exclusive,
    but malformed input exists. When both are present the sidecar wins:
    `ingest_worker` writes (7fe0,0010) to it, `get_pixel_data()` returns
    that array through the loader, and the float half is carried by
    nothing at all. Exempting it unconditionally would have restored the
    silence #169 exists to end, in the one case where the bytes really
    do vanish.

    Driven through `populate_attrs` rather than a fixture file, because
    `session.ingest()` cannot reach it: pydicom refuses to decode a
    dataset carrying two pixel elements, so `ingest_worker` returns
    "Decompression Failed" and the instance never enters the store --
    loud, and a different outcome from the one being pinned here. The
    guard covers `populate_attrs`' own callers, which include the
    fixture generators in `scripts/`, and it costs one boolean.
    """
    from pydicom.dataset import Dataset

    from isocenter.entities import DicomItem
    from isocenter.io_handlers import populate_attrs

    ds = Dataset()
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = 8
    ds.add_new(0x7FE00010, 'OW', b'\x01\x02\x03\x04')
    ds.add_new(0x7FE00008, 'OF', np.arange(4, dtype=np.float32).tobytes())

    dropped = []
    populate_attrs(ds, DicomItem(), dropped)

    assert dropped == [("7fe0,0008", "OF")], dropped


def test_the_extended_offset_table_is_not_reported(tmp_path):
    """(7fe0,0001) and (7fe0,0002) are an index, not data (#194).

    They are byte offsets and lengths into the encapsulated Pixel Data
    fragment stream. Isocenter decodes the pixels at ingest and re-writes
    them uncompressed, so the fragment layout the table describes does
    not exist in the exported file -- the table cannot be carried, and
    its absence loses nothing recoverable, because the pixels it indexes
    round-trip exactly.

    Reporting them cost two `DATA_LOSS` rows and two warnings on every
    instance stored with one. The Extended Offset Table is the mechanism
    DICOM added for large multi-frame objects, so that is precisely
    where a spurious per-instance pair is least welcome -- the noise
    failure the gate's own comment warns about, reached from the other
    side.
    """
    def add_eot(ds):
        ds.add_new(0x7FE00001, 'OV', b'\x00' * 8)
        ds.add_new(0x7FE00002, 'OV', b'\x10' * 8)

    attrs, rows = _ingest_with(tmp_path, "eot", add_eot)

    assert "7fe0,0001" not in attrs, "precondition: still skipped"
    assert rows == [], rows


def test_the_encapsulated_total_length_is_not_reported(tmp_path):
    """(7fe0,0003) is the same index by another name, and the tag that
    made the emitter's prose wrong.

    It is `UV`, a 64-bit unsigned integer -- not a binary VR at all --
    so it never reaches the VR gate, and the row it used to get read
    "binary-VR elements are not held in the object graph", which is not
    the reason it was skipped. Exempting the three non-binary members of
    the group is what makes that clause true of everything still
    reported (#194).
    """
    def add_total_length(ds):
        ds.add_new(0x7FE00003, 'UV', 4096)

    _attrs, rows = _ingest_with(tmp_path, "eotlen", add_total_length)

    assert rows == [], rows


def test_nothing_reported_from_this_group_misstates_its_own_reason(tmp_path):
    """The emitter has one reason clause; everything reaching it must fit.

    `import_files` words a standard loss as "binary-VR elements are not
    held in the object graph". That is a claim about the element, and
    the group-level append is the one path that can reach the emitter
    with an element the VR gate never saw. Whatever survives the
    exemptions has to be a binary VR for the sentence to be true.

    The fixture carries a private `OB` alongside the three index tags so
    the assertion has a non-empty list to filter. Run against the three
    on their own it would pass over no rows at all -- true, and true of
    a build where the emitter had stopped running.
    """
    def add_everything(ds):
        ds.add_new(0x7FE00001, 'OV', b'\x00' * 8)
        ds.add_new(0x7FE00002, 'OV', b'\x10' * 8)
        ds.add_new(0x7FE00003, 'UV', 4096)
        ds.add_new(0x00090010, 'LO', 'ACME_HEADER')
        ds.add_new(0x00091002, 'OB', b'\x01\x02\x03\x04')

    _attrs, rows = _ingest_with(tmp_path, "prose", add_everything)

    assert rows, "precondition: the emitter ran and wrote something"
    assert any("0009,1002" in d for d, _s in rows), rows
    assert not any("OV" in d or "UV" in d for d, _s in rows), rows


def test_an_encapsulated_instance_keeps_its_pixels_and_reports_nothing(
        tmp_path):
    """The claim under the Extended Offset Table exemption, on the input
    that makes it (#194).

    "Not a loss" rests on the pixels the table indexes coming through
    intact. An uncompressed fixture cannot show that -- it has no
    fragment stream for the offsets to describe -- so this one is RLE
    encapsulated, two frames, with an Extended Offset Table built to
    PS3.5 A.4: per-frame byte offsets and lengths relative to the first
    fragment item tag, plus (7fe0,0003) for the stream's total length.

    Ingest decodes to raw and the export re-writes uncompressed, so the
    layout the table describes genuinely does not exist on the way out.
    The table is gone from the exported file and the pixels are
    identical -- which is the whole argument for not filing a row about
    it.
    """
    import struct

    import pydicom
    from pydicom.uid import RLELossless

    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src), with_private_binary=False)
    path = os.path.join(str(src), "one.dcm")

    ds = pydicom.dcmread(path)
    # 8 columns, not the base fixture's 4: `_export_instance_worker`
    # reads a 3-D array whose last axis is 3 or 4 as interleaved
    # samples, so a 2-frame 4x4 image comes back out as a 2x4 RGBA one.
    # Pre-existing and unrelated to the exemption under test; sidestep
    # it rather than assert around it.
    ds.Rows = ds.Columns = 8
    source = np.arange(2 * 8 * 8, dtype=np.uint8).reshape(2, 8, 8)
    ds.NumberOfFrames = 2
    ds.PixelData = source.tobytes()
    ds.compress(RLELossless)

    fragments = list(pydicom.encaps.generate_fragmented_frames(ds.PixelData))
    offsets, lengths, pos = [], [], 0
    for frag in fragments:
        blob = b"".join(frag)
        offsets.append(pos)
        lengths.append(len(blob))
        pos += 8 + len(blob)          # 8 = the item tag and length header
    ds.add_new(0x7FE00001, 'OV', struct.pack(f"<{len(offsets)}Q", *offsets))
    ds.add_new(0x7FE00002, 'OV', struct.pack(f"<{len(lengths)}Q", *lengths))
    ds.add_new(0x7FE00003, 'UV', pos)
    ds.save_as(path, enforce_file_format=True)

    out = tmp_path / "out"
    session = DicomSession(persistence_file=str(tmp_path / "eot.db"))
    try:
        session.ingest(str(src))
        session.export(str(out), format="dicom", use_compression=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()

    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, written
    exported = pydicom.dcmread(written[0])

    assert rows == [], rows
    assert (0x7FE0, 0x0001) not in exported, "the table cannot survive"
    assert np.array_equal(exported.pixel_array, source), (
        "the pixels it indexed must, or the exemption is wrong")


def test_pixel_data_inside_a_sequence_item_is_reported(tmp_path):
    """The same tag, routed at one depth and nowhere at the other (#169).

    An Icon Image Sequence item carries its own (7fe0,0010). The sidecar
    holds one pixel blob per instance today, so the nested one is routed
    nowhere, while the item's eight `0028,xxxx` descriptors reach the
    graph normally and are exported. The result declares a 2x2 8-bit
    icon and carries no bytes for it, with Pixel Data Type 1 in the Icon
    Image Macro (PS3.3 C.7.6.1.1.6). That is #160's shape at a second
    site. Carrying it is #183.

    The exemption is therefore per-depth, not per-tag: this file has a
    top-level (7fe0,0010) too, and reporting *that* would file a loss on
    every image ever ingested.
    """
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    def add_icon(ds):
        item = Dataset()
        item.Rows = item.Columns = 2
        item.BitsAllocated = item.BitsStored = 8
        item.HighBit = 7
        item.SamplesPerPixel = 1
        item.PhotometricInterpretation = "MONOCHROME2"
        item.PixelRepresentation = 0
        item.PlanarConfiguration = 0
        item.add_new(0x7FE00010, 'OW', b'\x01\x02\x03\x04')
        ds.IconImageSequence = Sequence([item])

    _attrs, rows = _ingest_with(tmp_path, "icon", add_icon)

    assert len(rows) == 1, rows
    assert "7fe0,0010" in rows[0][0], rows
    assert rows[0][1] == LOSS_SCOPE_STANDARD, rows


def test_the_top_level_pixel_data_of_that_same_file_is_still_not_reported(
        tmp_path):
    """The false positive the depth rule has to avoid.

    One row, not two: the instance's own pixels went to the sidecar.
    Widening the group exemption into a blanket one would put a
    `DATA_LOSS` entry in the record of every image ever ingested, which
    is how a compliance trail becomes noise.
    """
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    def add_icon(ds):
        item = Dataset()
        item.Rows = item.Columns = 2
        item.add_new(0x7FE00010, 'OW', b'\x01\x02\x03\x04')
        ds.IconImageSequence = Sequence([item])

    _attrs, rows = _ingest_with(tmp_path, "icontop", add_icon)

    assert len(rows) == 1, rows


def test_float_pixel_data_inside_a_sequence_item_is_reported(tmp_path):
    """The float exemption is depth-sensitive for the same reason.

    `get_pixel_data()` re-reads the *top level*, so a float element one
    level down is carried by nothing. Unreachable from a conformant
    file, and pinned anyway: the exemption's condition is the depth, and
    a rule that only happens to be right at the depth it was tested is
    the shape #169 started from.
    """
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    def add_nested_float(ds):
        item = Dataset()
        item.Rows = item.Columns = 2
        item.add_new(0x7FE00008, 'OF', np.arange(4, dtype=np.float32).tobytes())
        ds.AnatomicRegionSequence = Sequence([item])

    _attrs, rows = _ingest_with(tmp_path, "nestfloat", add_nested_float)

    assert any("7fe0,0008" in d for d, _s in rows), rows

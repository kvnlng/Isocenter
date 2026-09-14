"""Pixel data inside a sequence item is carried by the store (#183).

An Icon Image Sequence item carries its own (7fe0,0010), and until now
nothing carried it anywhere: the item's `0028,xxxx` descriptors reached the
graph and were exported while the bytes did not, producing a file that
declares a 2x2 icon and holds nothing for it. Pixel Data is Type 1 in the
Icon Image Macro (PS3.3 C.7.6.1.1.6), so that file is nonconformant, and it
is #160's shape at a second site. #169 made the drop *audible*; this makes
it stop happening.

Three properties this module exists to hold down, in descending order of how
badly a regression would hurt:

1. **No icon ships that could show what redaction removed.** An icon is a
   downsampled copy of a frame, and *nothing* in this pipeline scans or
   redacts one: every pixel consumer reads `instance.get_pixel_data()`,
   which is the top-level frame and only that. So carrying icon bytes out of
   a session that redacted could re-export a thumbnail of exactly what
   redaction removed. For every icon but the carrier's own, the gate is
   store-wide rather than per-instance, because an icon under Referenced
   Image Sequence is a thumbnail of a *different* SOP instance (PS3.3
   C.7.6.16), and redaction calls `regenerate_uid()` -- so "look up the
   referenced instance and ask if it was redacted" returns nothing for
   precisely the instances that were. That lookup fails open, which is the
   worst available answer. The carrier's own depth-1 icon thumbnails the
   carrier, so since #542 it goes only when that instance is redacted, and
   every drop is a graded `SIGNAL` loss.

2. **Position is the only identity a sequence item has.** The blob's key is
   a path recorded at ingest and resolved at export, and everything in
   between can change the sequence. An item *removed* makes the path resolve
   to None; an earlier sibling removed makes it resolve to the **wrong
   item**. Silent wrong bytes is this repo's worst failure class.

3. **Carried or reported, never both and never neither.** A decode that
   fails must still file its `DATA_LOSS` row, and bytes that are in the
   store must stop filing one -- reporting a loss that did not happen is
   #194's defect, and section 3 of the compliance report is headed "present
   in the source and not in the exported data".
"""

import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRLittleEndian, JPEGBaseline8Bit,
                         JPEGExtended12Bit, RLELossless, generate_uid)

from isocenter import io_handlers
from isocenter.io_handlers import (DicomExporter, LOSS_SCOPE_SIGNAL,
                                   LOSS_SCOPE_STANDARD,
                                   _CARRIABLE_TRANSFER_SYNTAXES)
from isocenter.blob_kind import serialize_blob_kind
from isocenter.session import DicomSession

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"

#: The icon's own bytes, distinct from anything the top-level frame holds
#: so a mix-up cannot pass a byte comparison.
ICON_BYTES = bytes([11, 22, 33, 44])

#: Icon Image Sequence and Referenced Image Sequence, in the lowercase-hex
#: spelling the graph and the blob kind both use.
ICON_SEQ = "0088,0200"
REF_IMAGE_SEQ = "0008,1140"


def _icon_item(payload=ICON_BYTES, rows=2, cols=2, samples=1,
               photometric="MONOCHROME2", planar=None, bits=8,
               encapsulated=False):
    """One Icon Image Sequence item, complete enough to decode.

    "Complete enough" is the whole difference between this and the
    bare-descriptor icon in `tests/test_private_binary_ingest.py`, whose
    missing BitsAllocated makes it undecodable and so keeps its loss row.

    `encapsulated=True` marks the element undefined-length, which is how
    an encapsulated (fragmented) payload is written under a compressed
    transfer syntax; the payload is then the output of `encapsulate`.
    """
    item = Dataset()
    item.Rows, item.Columns = rows, cols
    item.BitsAllocated = item.BitsStored = bits
    item.HighBit = bits - 1
    item.SamplesPerPixel = samples
    item.PhotometricInterpretation = photometric
    item.PixelRepresentation = 0
    if planar is not None:
        item.PlanarConfiguration = planar
    item.add_new(0x7FE00010, 'OW' if bits > 8 else 'OB', payload)
    if encapsulated:
        item["PixelData"].is_undefined_length = True
    return item


#: The lossy icon's flat colour, chosen so YBR and RGB triples are far
#: apart: `(220, 40, 90)` read as YBR_FULL shows `(169, 255, 65)`.
LOSSY_ICON_RGB = (220, 40, 90)
LOSSY_ICON_SIZE = 8


def _jpeg_icon_item():
    """A real JPEG Baseline icon declared `YBR_FULL_422`, via Pillow.

    Pillow's `subsampling=1` is 4:2:2, which is what the declared label
    says. Built here rather than skipped: Pillow is an `install_requires`,
    so no plugin is needed to encode or to decode it (#372 corrected the
    #183 spec's "no lossy fixture can be built in this venv").
    """
    from io import BytesIO
    from PIL import Image
    from pydicom.encaps import encapsulate

    buf = BytesIO()
    Image.fromarray(np.full((LOSSY_ICON_SIZE, LOSSY_ICON_SIZE, 3),
                            LOSSY_ICON_RGB, dtype=np.uint8), "RGB").save(
        buf, format="JPEG", subsampling=1, quality=95)
    return _icon_item(payload=encapsulate([buf.getvalue()]),
                      rows=LOSSY_ICON_SIZE, cols=LOSSY_ICON_SIZE, samples=3,
                      photometric="YBR_FULL_422", planar=0, encapsulated=True)


def _write_src(folder, icons=(), referenced_icons=(), serial="SN-1",
               transfer_syntax=ExplicitVRLittleEndian, top_level_pixels=True,
               patient_id="PAT1"):
    """A CT instance carrying icons at depth 1 and/or depth 2.

    `icons` go under Icon Image Sequence directly. `referenced_icons` go
    under Referenced Image Sequence items -- the depth-2 shape, and the one
    whose thumbnail is of a *different* SOP instance.

    `top_level_pixels=False` is for the lossy-transfer-syntax fixture:
    under JPEG Baseline the top level would need a real encoded frame of
    its own, and a raw one would fail the *whole* ingest at the top-level
    decode and never reach the nested candidate at all. Keeping the top
    level pixel-free makes the nested decode the only one under test.

    `patient_id` puts an instance under a second patient, for the tests
    that hold the foreign-icon gate to the store rather than the export's
    `patient_ids`.
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = transfer_syntax

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = patient_id, "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    ds.DeviceSerialNumber = serial
    ds.Manufacturer, ds.ManufacturerModelName = "ACME", "SCAN9000"
    # `IODValidator` refuses to write a CT Image that is missing these, so
    # without them every export in this module fails before it reaches the
    # subject under test -- with an `ERROR` row about geometry rather than a
    # visible assertion about icons.
    ds.SliceThickness, ds.KVP = "1.0", "120"
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]

    if top_level_pixels:
        ds.Rows = ds.Columns = 4
        ds.BitsAllocated = ds.BitsStored = 8
        ds.HighBit = 7
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelRepresentation = 0
        ds.PixelData = np.arange(16, dtype=np.uint8).tobytes()
    else:
        # A conformant-enough non-image shape: no Image Pixel Module at all,
        # so nothing asks pydicom to decode the top level.
        ds.Modality = "SR"
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.11"
        meta.MediaStorageSOPClassUID = ds.SOPClassUID

    if icons:
        ds.IconImageSequence = Sequence(list(icons))
    if referenced_icons:
        items = []
        for icon in referenced_icons:
            ref = Dataset()
            ref.ReferencedSOPClassUID = CT_IMAGE
            ref.ReferencedSOPInstanceUID = generate_uid()
            ref.IconImageSequence = Sequence([icon])
            items.append(ref)
        ds.ReferencedImageSequence = Sequence(items)

    path = os.path.join(folder, "one.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _ingest(tmp_path, name, **kwargs):
    """Write a source, ingest it, save, close. Returns (db, src_dir)."""
    src = tmp_path / f"src_{name}"
    src.mkdir()
    _write_src(str(src), **kwargs)
    db = str(tmp_path / f"{name}.db")

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        session.save()
    finally:
        session.close()
    return db, str(src)


def _exported(out_dir):
    """The one written .dcm, read back."""
    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out_dir))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, written
    return pydicom.dcmread(written[0])


def _data_loss_rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT details, loss_scope FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()


def _blob_kinds(db):
    with sqlite3.connect(db) as conn:
        return [k for k, in conn.execute("SELECT kind FROM instance_blobs")]


#: `[y1, y2, x1, x2]`, the shape `redact_by_machine` documents. Covers the
#: whole 4x4 top-level frame, so the redaction is unmistakable.
WHOLE_FRAME = [0, 4, 0, 4]


def _export(db, out):
    """Reopen the store and export."""
    session = DicomSession(persistence_file=db)
    try:
        session.export(str(out), format="dicom", use_compression=False)
    finally:
        session.close()


# --- 1. The bytes survive the store -------------------------------------

def test_a_nested_icon_survives_a_close_and_reopen(tmp_path):
    """Ingest, save, close, reopen, export -- the icon's bytes come back.

    The reopen is the point. Carriage that only works while the session is
    still open is carriage by the pydicom Dataset that ingest happened to
    still be holding, not by the store.
    """
    db, _src = _ingest(tmp_path, "reopen", icons=[_icon_item()])
    out = tmp_path / "out"
    _export(db, out)

    exported = _exported(out)
    assert "IconImageSequence" in exported
    icon = exported.IconImageSequence[0]
    assert icon.PixelData == ICON_BYTES
    assert not [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d]


def test_the_source_file_can_go_away(tmp_path):
    """The whole point of carrying the bytes rather than re-reading them.

    Float pixel data was carried by the *source file* until #327, and the
    failure mode that motivated moving it was exactly this: move the file,
    delete it, or reopen the session on another machine, and the bytes are
    gone. An icon held only in a pydicom Dataset has the same problem.
    """
    db, src = _ingest(tmp_path, "gone", icons=[_icon_item()])
    os.remove(os.path.join(src, "one.dcm"))

    out = tmp_path / "out"
    _export(db, out)

    assert _exported(out).IconImageSequence[0].PixelData == ICON_BYTES


def test_a_depth_two_icon_round_trips(tmp_path):
    """`0008,1140/3/0088,0200/0/7fe0,0010` -- the path loop, exercised.

    Depth 1 alone would leave the loop unexercised, and a rule that is only
    right at the depth it was tested is the shape #169 started from. Four
    Referenced Image Sequence items so the ordinal under test is not 0:
    an index that is always zero cannot tell a working path walk from one
    that ignores the index entirely.
    """
    payloads = [bytes([i, i + 1, i + 2, i + 3]) for i in (1, 5, 9, 13)]
    db, _src = _ingest(
        tmp_path, "depth2",
        referenced_icons=[_icon_item(p) for p in payloads])

    kinds = _blob_kinds(db)
    assert serialize_blob_kind(
        "pixels", ((REF_IMAGE_SEQ, 3), (ICON_SEQ, 0)), "7fe0,0010") in kinds

    out = tmp_path / "out"
    _export(db, out)
    exported = _exported(out)
    for index, payload in enumerate(payloads):
        icon = exported.ReferencedImageSequence[index].IconImageSequence[0]
        assert icon.PixelData == payload, index


def test_a_planar_colour_icon_is_stored_and_declared_interleaved(tmp_path):
    """PlanarConfiguration must be corrected on the item, as at the top level.

    pydicom de-planarises on read, so the bytes the sidecar holds are
    interleaved whatever the source declared. `ingest_worker` already forces
    the top-level (0028,0006) to 0 for that reason. Leaving a nested 1 in
    place would export interleaved bytes under a planar declaration -- a
    colour icon read as garbage by a conformant reader, which is worse than
    the drop this change replaces.
    """
    planar = np.array([[[1, 2, 3], [4, 5, 6]],
                       [[7, 8, 9], [10, 11, 12]]], dtype=np.uint8)
    # Planar on the wire: all reds, then all greens, then all blues.
    wire = planar.transpose(2, 0, 1).tobytes()

    db, _src = _ingest(
        tmp_path, "planar",
        icons=[_icon_item(wire, samples=3, photometric="RGB", planar=1)])

    out = tmp_path / "out"
    _export(db, out)
    icon = _exported(out).IconImageSequence[0]

    assert icon.PlanarConfiguration == 0
    assert icon.PixelData == planar.tobytes()


def test_compaction_preserves_a_nested_blob(tmp_path):
    """A loader left on a pre-compaction offset reads the wrong bytes.

    `compact_sidecar`'s uid_map is pixels-only *and keyed by UID alone*, so
    it cannot carry a second pixel payload for one instance. Nested refs are
    repointed from the blob table for the same reason waveform loaders are.
    The failure this catches is silent wrong bytes, which only a byte
    comparison after a compaction can see.
    """
    db, _src = _ingest(tmp_path, "compact", icons=[_icon_item()])

    session = DicomSession(persistence_file=db)
    try:
        session.compact()
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        key = (((ICON_SEQ, 0),), "7fe0,0010")
        assert key in inst._nested_pixel_refs, inst._nested_pixel_refs
        session.export(str(tmp_path / "out"), format="dicom",
                       use_compression=False)
    finally:
        session.close()

    assert _exported(tmp_path / "out").IconImageSequence[0].PixelData \
        == ICON_BYTES


def test_a_redacted_instance_keeps_its_nested_row_under_the_new_uid(tmp_path):
    """`instance_blobs` is keyed by UID and `regenerate_uid()` changes it.

    This went wrong once already for the top-level blob -- see
    `tests/test_redaction_identity.py` -- and a row left under the retired
    UID is an orphan only `compact()` notices. The nested rows follow for
    free only because `save_all` re-emits them from
    `inst.sop_instance_uid` on every save; a design that wrote them once at
    ingest and never again would strand them here.
    """
    db, _src = _ingest(tmp_path, "reduid", icons=[_icon_item()])

    session = DicomSession(persistence_file=db)
    try:
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        old_uid = inst.sop_instance_uid
        session.redact_by_machine("SN-1", WHOLE_FRAME)
        new_uid = session.store.patients[0].studies[0].series[0]\
            .instances[0].sop_instance_uid
        session.save()
    finally:
        session.close()

    assert new_uid != old_uid
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT instance_uid, kind FROM instance_blobs").fetchall()

    nested = [(u, k) for u, k in rows if k.startswith("pixels:")]
    assert nested == [(new_uid, "pixels:0088,0200/0/7fe0,0010")], rows


# --- 2. The de-identification gate ---------------------------------------

def test_a_redacted_store_exports_no_icon_item_at_all(tmp_path):
    """Not descriptors without bytes -- the whole item goes (#183 Q2/Q10).

    Nothing scans or redacts an icon, so the drop is what protects the
    export and carrying the bytes would remove that protection silently.
    Leaving the descriptors behind would reintroduce the Type 1 violation
    this change exists to fix, at a fourth site; #160 settled the identical
    question for discarded multiplex groups and chose exactly this.
    """
    db, _src = _ingest(tmp_path, "redacted", icons=[_icon_item()])

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.configuration.rules = [
            {"serial_number": "SN-1", "redaction_zones": [WHOLE_FRAME]}]
        session.redact()
        session.export(str(out), format="dicom", use_compression=False)
    finally:
        session.close()

    exported = _exported(out)
    assert "IconImageSequence" not in exported
    assert ICON_BYTES not in exported.PixelData
    rows = [d for d, s in _data_loss_rows(db)
            if "7fe0,0010" in d and "redact" in d.lower()]
    assert len(rows) == 1, _data_loss_rows(db)
    # SIGNAL since #542: an icon dropped because pixels are redacted is
    # acquired content that was in the source and is not in the export,
    # and it grades. It was STANDARD under #183, so the run read PASS.
    assert all(s == LOSS_SCOPE_SIGNAL
               for d, s in _data_loss_rows(db) if "redact" in d.lower())


def test_the_gate_fires_on_the_attestation_with_no_configured_zones(tmp_path):
    """`ctx.redaction_zones` alone is not the gate, and this proves it.

    `_redaction_zones_for` looks the zones up **at export time**, from the
    **current** configuration, keyed on the series' device serial number.
    `RedactionService` does not consult that -- it redacts whatever `rois`
    its caller passed. So the zones list is empty at export while the pixels
    are redacted whenever the rule was edited, the serial changed, the
    service was driven directly, or the series has no equipment at all. In
    each of those the top-level frame ships zeroed and, without this half of
    the gate, the icon ships intact.

    The instance itself carries the answer: `_ISOCENTER_REDACTION_HASH` is
    written on every path that actually modified pixels.
    """
    db, _src = _ingest(tmp_path, "attest", icons=[_icon_item()])

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        # Redacted through the direct `rois` door, so nothing is configured
        # and `_redaction_zones_for` returns [] at export.
        session.redact_by_machine("SN-1", WHOLE_FRAME)
        # `redact_by_machine` restores the original rules in its `finally`,
        # so nothing is configured by the time the export looks.
        assert not session.configuration.rules
        session.export(str(out), format="dicom", use_compression=False)
    finally:
        session.close()

    assert "IconImageSequence" not in _exported(out)


def test_an_unredacted_instance_loses_its_icon_to_a_redaction_elsewhere(
        tmp_path):
    """The store-wide half, and the reason a per-instance gate cannot work.

    An icon under Referenced Image Sequence is a thumbnail of the SOP
    instance being *referenced* (PS3.3 C.7.6.16), not of the one carrying
    it. If the referenced image was redacted and this one was not, both
    halves of a per-instance condition pass and the export ships a thumbnail
    of exactly what redaction removed, one file over.

    It cannot be resolved by following the reference either: redaction calls
    `regenerate_uid()`, so `ReferencedSOPInstanceUID` names a UID that is no
    longer in the store, and the lookup returns nothing for precisely the
    instances that were redacted. It fails *open*.

    So the condition is store-wide, and this test is what says so: the
    instance carrying the icon is never redacted, and its icon is dropped
    anyway because a different instance in the same store was.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src), referenced_icons=[_icon_item()], serial="SN-CARRIER")
    # A second, unrelated instance in its own series -- the one that gets
    # redacted. Different device serial, so no configured rule can reach the
    # carrier even if one existed.
    src2 = tmp_path / "src2"
    src2.mkdir()
    _write_src(str(src2), serial="SN-OTHER")
    os.replace(os.path.join(str(src2), "one.dcm"),
               os.path.join(str(src), "two.dcm"))

    db = str(tmp_path / "storewide.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        carrier = None
        other = None
        for patient in session.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for inst in series.instances:
                        if inst.sequences.get(REF_IMAGE_SEQ):
                            carrier = inst
                        else:
                            other = inst
        assert carrier is not None and other is not None

        # Redacted through `redact_by_machine`, which restores the
        # (empty) original rules in its `finally` -- so at export time
        # `_redaction_zones_for` returns [] for BOTH instances and the
        # attestation on `other` is the only thing the gate can see.
        session.redact_by_machine("SN-OTHER", WHOLE_FRAME)
        assert "_ISOCENTER_REDACTION_HASH" in other.attributes
        assert "_ISOCENTER_REDACTION_HASH" not in carrier.attributes
        assert not session.configuration.rules

        session.export(str(out), format="dicom", use_compression=False)
    finally:
        session.close()

    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 2, written
    for path in written:
        ds = pydicom.dcmread(path)
        for ref in ds.get("ReferencedImageSequence", []):
            assert "IconImageSequence" not in ref, path


def test_the_gate_removes_the_icon_item_not_its_referencing_parent(tmp_path):
    """Drop the icon's item; the Referenced Image Sequence item stays.

    The item at the full path goes. Its parent carries
    `ReferencedSOPInstanceUID`, which is a reference and not a thumbnail --
    removing it would delete information the icon was merely attached to.
    """
    db, _src = _ingest(tmp_path, "parent", referenced_icons=[_icon_item()])

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.redact_by_machine("SN-1", WHOLE_FRAME)
        session.export(str(out), format="dicom", use_compression=False)
    finally:
        session.close()

    exported = _exported(out)
    assert "ReferencedImageSequence" in exported
    assert len(exported.ReferencedImageSequence) == 1
    assert "ReferencedSOPInstanceUID" in exported.ReferencedImageSequence[0]
    assert "IconImageSequence" not in exported.ReferencedImageSequence[0]


def test_removing_one_icon_item_does_not_shift_the_next_one_out_of_reach(
        tmp_path):
    """Two icons under ONE parent sequence, both dropped by the gate.

    Removing item 0 slides item 1 into index 0. A loop that resolved each
    path as it removed would then find `0088,0200/1` resolving to None,
    file a "gone" row for it, and leave item 1 in the file -- descriptors
    with no bytes, which is the Type 1 violation the gate exists to avoid.
    So every path is resolved and every outcome decided before anything is
    removed, and the removals are then done by object identity rather than
    by index.

    Two icons under one sequence is the only shape that can see this. One
    icon each under two *different* parents cannot: removing from one
    sequence shifts nothing in the other, so an interleaved loop passes.
    This test was written that way first and passed against a deliberately
    interleaved implementation, which is why it is spelled out here.

    Icon Image Sequence is VM 1 in the standard, so a two-item one is
    nonconformant -- but the sequence walk is generic, and this is the
    cheapest shape that puts two carried blobs under one parent. The
    conformant version of the same hazard is two Referenced Image Sequence
    items each holding an icon, where the *outer* sequence is the one that
    shifts; that is a deeper path to the same off-by-one.
    """
    db, _src = _ingest(
        tmp_path, "shift",
        icons=[_icon_item(b"\x01\x02\x03\x04"),
               _icon_item(b"\x05\x06\x07\x08")])

    # Both blobs are in the store under their own paths before the export
    # has a chance to lose one.
    assert sorted(_blob_kinds(db)) == [
        "pixels",
        "pixels:0088,0200/0/7fe0,0010",
        "pixels:0088,0200/1/7fe0,0010",
    ]

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.redact_by_machine("SN-1", WHOLE_FRAME)
        session.export(str(out), format="dicom", use_compression=False)
    finally:
        session.close()

    # Both items gone, so the emptied sequence is gone too. An interleaved
    # implementation leaves the second item behind, descriptors and all.
    exported = _exported(out)
    assert "IconImageSequence" not in exported


# --- 2b. Two tiers: an own icon per instance, every other one store-wide --
#
# #183 dropped every nested icon from every file once anything redacted,
# or once *any* rule carried zones -- even a rule matching no scanner --
# and filed each drop `STANDARD`, so the run graded PASS (#542, measured
# on ac33641: redact one of three series, all three own icons and a
# referenced icon gone, PASS).
#
# The owner's ruling (Q1) splits the gate along the one line the path
# draws without following any reference. An **own** icon -- the carrier's
# depth-1 Icon Image Sequence item -- thumbnails the carrier itself
# (PS3.3 C.7.6.1.1.6), so it can only show what *this* instance's
# redaction removed: it goes iff this instance carries the attestation or
# has zones applied at export. **Every other** nested icon may thumbnail a
# different, redacted SOP whose UID redaction regenerated, so it keeps a
# store-wide gate -- narrowed to an attestation anywhere or a zones rule
# that matches a series in the store. Both drops are `SIGNAL` (Q8).
#
# The export batch always runs in processes (#185), whatever the mode; the
# mode axis on the tests below reaches the redaction pass that writes the
# attestation and pickles it back, which is the half of the own-icon gate a
# worker has to see.

MODES = ["threads", "processes"]


@pytest.fixture
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


def _multi_src(tmp_path, specs):
    """One source folder holding one instance per `(serial, kwargs)`."""
    src = tmp_path / "src"
    src.mkdir()
    for serial, kwargs in specs:
        scratch = tmp_path / f"tmp_{serial}"
        scratch.mkdir()
        _write_src(str(scratch), serial=serial, **kwargs)
        os.replace(os.path.join(str(scratch), "one.dcm"),
                   os.path.join(str(src), f"{serial}.dcm"))
    return str(src)


def _by_serial(session):
    return {series.equipment.device_serial_number: instance
            for patient in session.store.patients
            for study in patient.studies
            for series in study.series
            for instance in series.instances}


def _exported_by_serial(out_dir):
    found = {}
    for root, _dirs, files in os.walk(str(out_dir)):
        for name in files:
            if name.endswith(".dcm"):
                ds = pydicom.dcmread(os.path.join(root, name))
                found[str(ds.DeviceSerialNumber)] = ds
    return found


def _icon_loss_rows(db):
    """`(entity_uid, loss_scope, details)` for every icon pixel loss row."""
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT entity_uid, loss_scope, details FROM audit_log "
            "WHERE action_type='DATA_LOSS' AND details LIKE '%7fe0,0010%'"
        ).fetchall()


def _ref_icons(ds):
    return [item for item in ds.get("ReferencedImageSequence", [])
            if "IconImageSequence" in item]


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_an_unredacted_instances_own_icon_survives_a_redaction_elsewhere(
        tmp_path, mode):
    """r1: redact SN-1; SN-2's own icon is a thumbnail of SN-2, and stays."""
    src = _multi_src(tmp_path, [("SN-1", dict(icons=[_icon_item()])),
                                ("SN-2", dict(icons=[_icon_item()]))])
    db = str(tmp_path / "own.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        session.redact_by_machine("SN-1", WHOLE_FRAME)
        redacted_uid = _by_serial(session)["SN-1"].sop_instance_uid
        session.export(str(out), format="dicom", use_compression=False,
                       show_progress=False)
        session.store_backend.flush_audit_queue()
    finally:
        session.close()

    exported = _exported_by_serial(out)
    assert set(exported) == {"SN-1", "SN-2"}, exported
    assert "IconImageSequence" not in exported["SN-1"]
    assert "IconImageSequence" in exported["SN-2"], (
        "an unredacted instance lost its own icon to a redaction elsewhere")
    assert exported["SN-2"].IconImageSequence[0].PixelData == ICON_BYTES

    rows = _icon_loss_rows(db)
    assert [(uid, scope) for uid, scope, _d in rows] == [
        (redacted_uid, LOSS_SCOPE_SIGNAL)], rows
    assert "this instance's pixel data is redacted" in rows[0][2], rows


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_export_time_zones_drop_that_instances_own_icon(tmp_path, mode):
    """r3: a rule for SN-2, no `redact()`, no attestation -- zones at export.

    The export redacts SN-2's frame itself, so its own icon is a thumbnail
    of pixels this export removes. SN-1 has no rule and keeps its icon.
    """
    src = _multi_src(tmp_path, [("SN-1", dict(icons=[_icon_item()])),
                                ("SN-2", dict(icons=[_icon_item()]))])
    db = str(tmp_path / "zones.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        session.configuration.rules = [
            {"serial_number": "SN-2", "redaction_zones": [WHOLE_FRAME]}]
        assert not any("_ISOCENTER_REDACTION_HASH" in i.attributes
                       for i in _by_serial(session).values())
        session.export(str(out), format="dicom", use_compression=False,
                       show_progress=False)
        zoned_uid = _by_serial(session)["SN-2"].sop_instance_uid
        session.store_backend.flush_audit_queue()
    finally:
        session.close()

    exported = _exported_by_serial(out)
    assert "IconImageSequence" not in exported["SN-2"]
    assert exported["SN-1"].IconImageSequence[0].PixelData == ICON_BYTES
    assert [(uid, scope) for uid, scope, _d in _icon_loss_rows(db)] == [
        (zoned_uid, LOSS_SCOPE_SIGNAL)], _icon_loss_rows(db)


def test_a_zones_rule_matching_no_series_drops_no_icon(tmp_path):
    """r2: a rule for a scanner nobody has is not a redaction in effect."""
    src = _multi_src(tmp_path, [
        ("SN-1", dict(icons=[_icon_item()])),
        ("SN-2", dict(icons=[_icon_item()])),
        ("SN-4", dict(referenced_icons=[_icon_item()]))])
    db = str(tmp_path / "nobody.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        session.configuration.rules = [
            {"serial_number": "SN-NOBODY", "redaction_zones": [WHOLE_FRAME]}]
        session.export(str(out), format="dicom", use_compression=False,
                       show_progress=False)
        session.store_backend.flush_audit_queue()
        report = tmp_path / "report.md"
        session.generate_report(str(report))
    finally:
        session.close()

    exported = _exported_by_serial(out)
    assert exported["SN-1"].IconImageSequence[0].PixelData == ICON_BYTES
    assert exported["SN-2"].IconImageSequence[0].PixelData == ICON_BYTES
    assert len(_ref_icons(exported["SN-4"])) == 1, exported["SN-4"]
    assert _icon_loss_rows(db) == []
    status = [line for line in report.read_text(encoding="utf-8").splitlines()
              if "Validation Status" in line]
    assert status == ["| **Validation Status** | **PASS** |"], status


def _carrier_and_other(tmp_path):
    """SN-CARRIER holds a referenced icon; SN-OTHER has no icon at all."""
    return _multi_src(tmp_path, [
        ("SN-CARRIER", dict(referenced_icons=[_icon_item()])),
        ("SN-OTHER", dict())])


def test_a_foreign_icon_drop_is_signal(tmp_path):
    """The store-wide tier grades too (Q8), on the carrier's own UID.

    `test_an_unredacted_instance_loses_its_icon_to_a_redaction_elsewhere`
    pins *that* the foreign icon goes; this pins how the loss is scoped,
    rather than editing that pin.
    """
    src = _carrier_and_other(tmp_path)
    db = str(tmp_path / "foreign.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        carrier_uid = _by_serial(session)["SN-CARRIER"].sop_instance_uid
        session.redact_by_machine("SN-OTHER", WHOLE_FRAME)
        session.export(str(out), format="dicom", use_compression=False,
                       show_progress=False)
        session.store_backend.flush_audit_queue()
    finally:
        session.close()

    assert _ref_icons(_exported_by_serial(out)["SN-CARRIER"]) == []
    rows = _icon_loss_rows(db)
    assert [(uid, scope) for uid, scope, _d in rows] == [
        (carrier_uid, LOSS_SCOPE_SIGNAL)], rows
    assert "may be a thumbnail of a redacted instance" in rows[0][2], rows


def test_a_matched_zones_rule_drops_a_foreign_icon_with_no_attestation(
        tmp_path):
    """The belt half: zones configured for a series that exists, not yet run."""
    src = _carrier_and_other(tmp_path)
    db = str(tmp_path / "matched.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        session.configuration.rules = [
            {"serial_number": "SN-OTHER", "redaction_zones": [WHOLE_FRAME]}]
        session.export(str(out), format="dicom", use_compression=False,
                       show_progress=False)
    finally:
        session.close()

    assert _ref_icons(_exported_by_serial(out)["SN-CARRIER"]) == []


@pytest.mark.parametrize("how", ["attestation", "zones"])
def test_a_subset_does_not_turn_the_foreign_gate_off(tmp_path, how):
    """r4 with a referenced-icon carrier: the gate is over the store.

    A subset that keeps only the carrier excludes the redacted (or zoned)
    series, and the carrier's referenced icon may still be its thumbnail.
    """
    src = _carrier_and_other(tmp_path)
    db = str(tmp_path / f"subset_{how}.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        if how == "attestation":
            session.redact_by_machine("SN-OTHER", WHOLE_FRAME)
        else:
            session.configuration.rules = [
                {"serial_number": "SN-OTHER",
                 "redaction_zones": [WHOLE_FRAME]}]
        carrier_uid = _by_serial(session)["SN-CARRIER"].sop_instance_uid
        session.export(str(out), format="dicom", use_compression=False,
                       show_progress=False, subset=[carrier_uid])
    finally:
        session.close()

    exported = _exported_by_serial(out)
    assert set(exported) == {"SN-CARRIER"}, exported
    assert _ref_icons(exported["SN-CARRIER"]) == []


@pytest.mark.parametrize("how", ["attestation", "zones"])
def test_patient_ids_do_not_turn_the_foreign_gate_off(tmp_path, how):
    """The same gate with the redacted series under a **second** patient.

    The subset test above narrows inside one patient, so a gate computed
    over the export's `patient_ids` still sees the redaction there. Here
    `patient_ids` leaves the redacted (or zoned) patient out entirely, and
    the carrier's referenced icon may still be a thumbnail of it.
    """
    src = _multi_src(tmp_path, [
        ("SN-CARRIER", dict(referenced_icons=[_icon_item()])),
        ("SN-OTHER", dict(patient_id="PAT2"))])
    db = str(tmp_path / f"patients_{how}.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        assert sorted(p.patient_id for p in session.store.patients) == [
            "PAT1", "PAT2"]
        if how == "attestation":
            session.redact_by_machine("SN-OTHER", WHOLE_FRAME)
        else:
            session.configuration.rules = [
                {"serial_number": "SN-OTHER",
                 "redaction_zones": [WHOLE_FRAME]}]
        session.export(str(out), format="dicom", use_compression=False,
                       show_progress=False, patient_ids=["PAT1"])
    finally:
        session.close()

    exported = _exported_by_serial(out)
    assert set(exported) == {"SN-CARRIER"}, exported
    assert _ref_icons(exported["SN-CARRIER"]) == []


def test_an_icon_drop_grades_review_required(tmp_path):
    """r1's report: the drop is a graded loss in section 3.1, by name."""
    src = _multi_src(tmp_path, [("SN-1", dict(icons=[_icon_item()])),
                                ("SN-2", dict(icons=[_icon_item()]))])
    db = str(tmp_path / "grade.db")
    report = tmp_path / "report.md"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        session.redact_by_machine("SN-1", WHOLE_FRAME)
        session.export(str(tmp_path / "out"), format="dicom",
                       use_compression=False, show_progress=False)
        session.generate_report(str(report))
    finally:
        session.close()

    text = report.read_text(encoding="utf-8")
    status = [line for line in text.splitlines()
              if "Validation Status" in line]
    assert status == ["| **Validation Status** | **REVIEW_REQUIRED** |"], text
    section_5 = text.split("## 5. Validation & Verification", 1)[1]
    assert "1 graded data loss(es) in section 3.1" in section_5, section_5


@pytest.mark.parametrize("path, own", [
    ((("0088,0200", 0),), True),
    ((("0088,0200", 1),), True),
    ((("0008,1140", 0), ("0088,0200", 0)), False),
    ((("0088,0200", 0), ("0088,0200", 0)), False),
    ((("0040,a730", 0),), False),
    ((("0009,1001", 0),), False),
])
def test_only_a_depth_one_icon_image_sequence_item_is_the_carriers_own(
        path, own):
    """The classifier reads the path alone and never follows a reference."""
    assert io_handlers._is_own_icon_path(path) is own


# --- 3. Carried or reported, never both and never neither ----------------

def test_a_shifted_index_refuses_rather_than_writing_the_wrong_icon(tmp_path):
    """Position is the only identity a sequence item has.

    The path is recorded at ingest and resolved at export. Remove an earlier
    sibling in between and the path still *resolves* -- to the wrong item --
    so the icon would be written into a neighbour's descriptors, silently.
    A `DATA_LOSS` row is the honest outcome; a wrong icon is not.

    The check is the reshape the loader already performs against the
    resolved item's own descriptors, which is why the two icons here have
    different geometry: equal-geometry items are indistinguishable by it,
    and this test is about the detectable half.
    """
    db, _src = _ingest(
        tmp_path, "shift",
        referenced_icons=[_icon_item(bytes(range(16)), rows=4, cols=4),
                          _icon_item(b"\x09\x0a\x0b\x0c", rows=2, cols=2)])

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        # Remove Referenced Image Sequence item 0 after ingest. The 2x2
        # icon's path now names index 0, which holds the 4x4 icon's item.
        del inst.sequences[REF_IMAGE_SEQ].items[0]
        session.export(str(out), format="dicom", use_compression=False)
    finally:
        session.close()

    exported = _exported(out)
    assert len(exported.ReferencedImageSequence) == 1
    survivor = exported.ReferencedImageSequence[0]
    assert "IconImageSequence" not in survivor, (
        "an icon whose path resolved to a different item must not be written")
    assert [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d], \
        _data_loss_rows(db)
    # Not a redaction drop, so still STANDARD after #542.
    assert {s for d, s in _data_loss_rows(db) if "7fe0,0010" in d} == {
        LOSS_SCOPE_STANDARD}, _data_loss_rows(db)


def test_an_item_removed_outright_files_a_loss_row_and_writes_nothing(
        tmp_path):
    """`resolve -> None` means "this item is gone", never "use the root".

    Writing an icon's pixels onto the instance would fabricate a top-level
    element that was never in the file, which is #57's defect exactly.
    """
    db, _src = _ingest(tmp_path, "removed", icons=[_icon_item()])

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        del inst.sequences[ICON_SEQ]
        session.export(str(out), format="dicom", use_compression=False)
    finally:
        session.close()

    exported = _exported(out)
    assert "IconImageSequence" not in exported
    assert exported.PixelData == np.arange(16, dtype=np.uint8).tobytes(), (
        "the icon's bytes must not be fabricated onto the instance")
    assert [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d], \
        _data_loss_rows(db)
    # #542 made the *redaction* drop SIGNAL and nothing else: an item that
    # is simply gone is a routine standard-group loss and does not grade.
    assert [s for d, s in _data_loss_rows(db) if "7fe0,0010" in d] == [
        LOSS_SCOPE_STANDARD], _data_loss_rows(db)


def test_an_rle_encapsulated_icon_is_decoded_and_written_raw(tmp_path):
    """Decode at ingest, store raw, write raw -- one rule for both depths.

    Verbatim carriage of the element value is wrong and must not be
    implemented: the export writes Implicit VR Little Endian, so 90 bytes of
    encapsulated fragments would produce an icon no reader can decode, under
    a transfer syntax that says there are no fragments. The top-level path
    already decodes at ingest for exactly this reason.
    """
    src = tmp_path / "src"
    src.mkdir()
    ds = pydicom.dcmread(_write_src(str(src), icons=[_icon_item()]))

    # The icon first, while the borrowed `file_meta` still says
    # ExplicitVRLittleEndian: pydicom refuses to compress a dataset whose
    # transfer syntax is already encapsulated, and `ds.compress` rewrites
    # the very `file_meta` the icon has to borrow.
    icon = ds.IconImageSequence[0]
    icon.file_meta = FileMetaDataset()
    icon.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    icon.compress(RLELossless)
    del icon.file_meta

    ds.compress(RLELossless)
    ds.save_as(os.path.join(str(src), "one.dcm"), enforce_file_format=True)

    db = str(tmp_path / "rle.db")
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        session.save()
    finally:
        session.close()

    out = tmp_path / "out"
    _export(db, out)
    exported = _exported(out)

    assert exported.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2"
    assert exported.IconImageSequence[0].PixelData == ICON_BYTES


def test_a_lossy_icon_is_carried_and_relabelled(tmp_path):
    """A JPEG Baseline icon declared `YBR_FULL_422` is carried, as RGB.

    This test was `test_a_lossy_source_keeps_its_loss_row_and_carries_
    nothing` until #372, and its reason for refusing was "a correctness
    claim with no measurement behind it": a lossy-JPEG icon decodes to
    RGB from a declared `YBR_FULL_422`, so carrying it means rewriting
    the item's Photometric Interpretation to match the bytes. That is
    now measured, and the rewrite is what `_decode_nested_pixels` does
    from the decoder's own meta -- the same helper the top level uses.
    JPEG Baseline and JPEG 2000 are the two lossy syntaxes measured
    through a nested item and the two added to the allow-list; the rest
    stay out as unmeasured, not as unsafe.

    Killed by deleting the nested conditional write (the item exports
    `YBR_FULL_422` over RGB bytes) and by removing JPEG Baseline from
    `_CARRIABLE_TRANSFER_SYNTAXES` (the loss row returns).

    The fixture carries no top-level Pixel Data on purpose; see
    `_write_src`.
    """
    db, _src = _ingest(tmp_path, "lossy", icons=[_jpeg_icon_item()],
                       transfer_syntax=JPEGBaseline8Bit,
                       top_level_pixels=False)

    assert [k for k in _blob_kinds(db) if k.startswith("pixels:")], \
        _blob_kinds(db)
    assert not [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d], \
        _data_loss_rows(db)

    out = tmp_path / "out"
    _export(db, out)
    exported = _exported(out)
    icon = exported.IconImageSequence[0]
    assert icon.PhotometricInterpretation == "RGB", (
        "the icon exports declaring %r over the RGB bytes pydicom decoded "
        "it to (#372)" % icon.PhotometricInterpretation)
    raw = icon.PixelData
    assert len(raw) == LOSSY_ICON_SIZE * LOSSY_ICON_SIZE * 3, len(raw)
    assert all(abs(a - e) <= 4 for a, e in zip(raw[:3], LOSSY_ICON_RGB)), (
        "the icon's first triple is %r, not %r" % (tuple(raw[:3]), LOSSY_ICON_RGB))
    # And as a reader would see it, borrowing the file's transfer syntax
    # the way the ingest decode does.
    icon.file_meta = exported.file_meta
    px = icon.pixel_array[0, 0]
    assert all(abs(int(a) - e) <= 4 for a, e in zip(px, LOSSY_ICON_RGB)), tuple(px)


def test_a_jpeg_extended_icon_is_carried_and_relabelled(tmp_path):
    """N1: a JPEG Extended (.4.51) icon is carried, as RGB (#387).

    Until #387 this file declared `.4.51` to witness the gate: the icon was
    refused only because the allow-list did not name the syntax. It was
    measured instead, through a sequence item: pydicom's Pillow plugin
    decodes an 8-bit baseline stream under a `.4.51` label and labels it
    RGB, exactly as it does under `.4.50` (the test above), so the item is
    relabelled from the decoder's meta. Admission is not a decode claim, it
    only stops the gate refusing what the decoder can read; a true 12-bit
    SOF1 icon, which Pillow refuses, decodes through the imagecodecs
    fallback since #604 when it is monochrome (the test below).

    Killed by removing JPEG Extended from `_CARRIABLE_TRANSFER_SYNTAXES`
    (the loss row returns and the export carries no Pixel Data).
    """
    db, _src = _ingest(tmp_path, "extended", icons=[_jpeg_icon_item()],
                       transfer_syntax=JPEGExtended12Bit,
                       top_level_pixels=False)

    assert [k for k in _blob_kinds(db) if k.startswith("pixels:")], \
        _blob_kinds(db)
    assert not [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d], \
        _data_loss_rows(db)

    out = tmp_path / "out"
    _export(db, out)
    icon = _exported(out).IconImageSequence[0]
    assert icon.PhotometricInterpretation == "RGB", \
        icon.PhotometricInterpretation
    raw = icon.PixelData
    assert len(raw) == LOSSY_ICON_SIZE * LOSSY_ICON_SIZE * 3, len(raw)
    assert all(abs(a - e) <= 4 for a, e in zip(raw[:3], LOSSY_ICON_RGB)), (
        "the icon's first triple is %r, not %r" % (tuple(raw[:3]),
                                                   LOSSY_ICON_RGB))


@pytest.mark.parametrize("cut", [False, True], ids=["whole", "cut-short"])
def test_a_12_bit_jpeg_extended_icon_is_carried_whole_and_refused_cut_short(
        tmp_path, cut):
    """#604 and review of #606 (M-r2-1), at the icon door.

    A 12-bit monochrome SOF1 icon, which Pillow refuses, is decoded by the
    imagecodecs fallback and carried. The same stream cut short -- which
    libjpeg-turbo would fill with mid-grey, 2048, and return -- is refused
    by the EOI check, and the icon keeps the loss row a decode failure
    earns.
    """
    from pydicom.encaps import encapsulate
    yy, xx = np.mgrid[0:64, 0:64]
    icon = ((yy * 61 + xx * 37) % 4096).astype(np.uint16)
    stream = imagecodecs.jpeg8_encode(icon, level=95, bitspersample=12)
    if cut:
        # Past the headers and into the scan, so libjpeg-turbo has rows
        # to fill: a cut inside the headers is refused by the codec itself.
        stream = stream[:int(len(stream) * 0.9)]
    item = _icon_item(payload=encapsulate([stream]), rows=64, cols=64,
                      bits=16, encapsulated=True)
    item.BitsStored, item.HighBit = 12, 11
    db, _src = _ingest(tmp_path, "sof1", icons=[item],
                       transfer_syntax=JPEGExtended12Bit,
                       top_level_pixels=False)
    carried = [k for k in _blob_kinds(db) if k.startswith("pixels:")]
    lost = [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d]
    if cut:
        assert not carried and lost, (_blob_kinds(db), _data_loss_rows(db))
    else:
        assert carried and not lost, (_blob_kinds(db), _data_loss_rows(db))


#: A 4x4 icon and its colour twin, channels distinct so a plane swap shows.
NEAR_ICON_MONO = np.array([[10, 60, 110, 160], [20, 70, 120, 170],
                           [30, 80, 130, 180], [40, 90, 140, 250]],
                          dtype=np.uint8)
NEAR_ICON_RGB = np.stack([NEAR_ICON_MONO, 255 - NEAR_ICON_MONO,
                          NEAR_ICON_MONO // 2], axis=-1)
#: What a NEAR=2 JPEG-LS decode of each returns, captured once from
#: `imagecodecs.jpegls_decode` (2026.8.16) and checked in. Near-lossless
#: means each sample may differ from its source by up to NEAR, so the
#: source alone is not the answer; this is.
NEAR_ICON_DECODED = {
    "MONOCHROME2": [
        [10, 60, 110, 161],
        [20, 70, 121, 171],
        [31, 82, 129, 181],
        [38, 92, 138, 252],
    ],
    "RGB": [
        [[10, 245, 5], [60, 195, 30], [109, 145, 55], [158, 95, 79]],
        [[20, 235, 10], [70, 184, 35], [121, 137, 61], [168, 85, 84]],
        [[31, 223, 16], [79, 174, 42], [132, 125, 64], [179, 73, 90]],
        [[39, 214, 18], [89, 167, 47], [139, 114, 72], [250, 5, 127]],
    ],
}


@pytest.mark.parametrize("photometric", ["MONOCHROME2", "RGB"])
def test_a_jpeg_ls_near_lossless_icon_is_carried(tmp_path, photometric):
    """N2: a JPEG-LS Near-Lossless (.4.81) icon is carried (#387).

    Measured through a sequence item: pydicom has no JPEG-LS plugin here,
    so the icon decodes through the imagecodecs fallback (#416), within
    the stream's NEAR bound, and keeps its label. Until #387 the gate
    refused it before any decode.

    Killed by removing JPEG-LS Near-Lossless from
    `_CARRIABLE_TRANSFER_SYNTAXES` (the loss row returns).
    """
    import imagecodecs
    from pydicom.encaps import encapsulate
    from pydicom.uid import JPEGLSNearLossless

    source = NEAR_ICON_RGB if photometric == "RGB" else NEAR_ICON_MONO
    samples = 3 if photometric == "RGB" else 1
    icon = _icon_item(
        payload=encapsulate([imagecodecs.jpegls_encode(source, level=2)]),
        rows=4, cols=4, samples=samples, photometric=photometric,
        planar=0 if samples == 3 else None, encapsulated=True)
    db, _src = _ingest(tmp_path, "near", icons=[icon],
                       transfer_syntax=JPEGLSNearLossless,
                       top_level_pixels=False)

    assert not [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d], \
        _data_loss_rows(db)
    out = tmp_path / "out"
    _export(db, out)
    exported = _exported(out).IconImageSequence[0]
    assert exported.PhotometricInterpretation == photometric
    got = np.frombuffer(exported.PixelData, dtype=np.uint8).reshape(
        source.shape)
    assert got.tolist() == NEAR_ICON_DECODED[photometric]
    assert int(np.abs(got.astype(int) - source.astype(int)).max()) <= 2


def test_a_ybr_full_jpeg_ls_icon_is_carried_as_rgb(tmp_path):
    """N3: an 8-bit `YBR_FULL` JPEG-LS icon is carried, relabelled RGB.

    The top level's #448 conversion at nested depth, through the same
    `_decode_pixels`: the fallback converts and returns `RGB`, and
    `_decode_nested_pixels` relabels the item from it. Without the
    JPEG-LS row the icon is refused and files its loss row.
    """
    import imagecodecs
    from pydicom.encaps import encapsulate
    from pydicom.pixels import convert_color_space
    from pydicom.uid import JPEGLSNearLossless

    ybr = convert_color_space(NEAR_ICON_RGB, "RGB", "YBR_FULL")
    icon = _icon_item(
        payload=encapsulate([imagecodecs.jpegls_encode(ybr, level=2)]),
        rows=4, cols=4, samples=3, photometric="YBR_FULL", planar=0,
        encapsulated=True)
    db, _src = _ingest(tmp_path, "near_ybr", icons=[icon],
                       transfer_syntax=JPEGLSNearLossless,
                       top_level_pixels=False)

    assert not [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d], \
        _data_loss_rows(db)
    out = tmp_path / "out"
    _export(db, out)
    exported = _exported(out).IconImageSequence[0]
    assert exported.PhotometricInterpretation == "RGB"
    got = np.frombuffer(exported.PixelData, dtype=np.uint8).reshape(4, 4, 3)
    # NEAR 2 on the YBR samples, then a colour conversion: a few levels.
    assert int(np.abs(got.astype(int) - NEAR_ICON_RGB.astype(int)).max()) <= 6


@pytest.mark.parametrize("ts", ["1.2.840.10008.1.2.4.201",
                                "1.2.840.10008.1.2.4.202",
                                "1.2.840.10008.1.2.4.203"],
                         ids=[".201", ".202", ".203"])
def test_an_htj2k_icon_on_a_pixel_less_instance_is_carried(tmp_path, ts):
    """N6: an HTJ2K icon is carried, under all three HTJ2K syntaxes (#459).

    Before, every one dropped with the generic loss row: `.201` and
    `.202` were admitted by the gate and then failed to decode (pydicom
    has no plugin here), and `.203` was not admitted. Both are gone:
    the fallback decodes HTJ2K through `jpeg2k_decode`, and `.203` is
    admitted, measured here. The exported item holds the source's samples.

    Killed by `.203` leaving `_CARRIABLE_TRANSFER_SYNTAXES` (its loss row
    returns) and by HTJ2K leaving `_IMAGECODECS_FALLBACK_SYNTAXES` (all
    three rows return).
    """
    import imagecodecs
    from pydicom.encaps import encapsulate

    source = (np.arange(16, dtype=np.int64) * 16).astype(
        np.uint8).reshape(4, 4)
    icon = _icon_item(
        payload=encapsulate([imagecodecs.htj2k_encode(source,
                                                      reversible=True)]),
        rows=4, cols=4, encapsulated=True)
    db, _src = _ingest(tmp_path, "htj2k", icons=[icon], transfer_syntax=ts,
                       top_level_pixels=False)

    assert not [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d], \
        _data_loss_rows(db)
    out = tmp_path / "out"
    _export(db, out)
    exported = _exported(out).IconImageSequence[0]
    assert exported.PhotometricInterpretation == "MONOCHROME2"
    got = np.frombuffer(exported.PixelData, dtype=np.uint8).reshape(4, 4)
    assert got.tolist() == source.tolist()


def test_an_icon_whose_header_pydicom_rejects_is_not_carried(tmp_path):
    """N5: an icon gets pydicom's header validation too (#453, attack A20c).

    A JPEG Lossless icon with BitsStored absent. pydicom has no JPEG
    Lossless plugin here, so it never validated, and the imagecodecs
    fallback carried the icon with BitsStored `None` -- the top level's
    #453 cell, one depth down. `_decode_pixels` now validates before the
    fallback, on the sequence item itself, whose `file_meta` is borrowed
    from the enclosing dataset. The icon files its loss row, and the
    instance still ingests: an icon is not a reason to lose it.
    """
    import imagecodecs
    from pydicom.encaps import encapsulate
    from pydicom.uid import JPEGLosslessSV1

    source = (np.arange(16, dtype=np.int64) * 4000).astype(
        np.uint16).reshape(4, 4)
    icon = _icon_item(
        payload=encapsulate([imagecodecs.ljpeg_encode(source)]),
        rows=4, cols=4, bits=16, encapsulated=True)
    del icon.BitsStored
    db, _src = _ingest(tmp_path, "no_bits_stored", icons=[icon],
                       transfer_syntax=JPEGLosslessSV1,
                       top_level_pixels=False)

    assert [d for d, _s in _data_loss_rows(db) if "7fe0,0010" in d], \
        _data_loss_rows(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM instances").fetchone() \
            == (1,)
    assert not [k for k in _blob_kinds(db) if k.startswith("pixels:")], \
        _blob_kinds(db)


def test_the_gate_refuses_a_syntax_it_does_not_name(monkeypatch):
    """N4: the allow-list gate refuses, in behaviour, and only it does.

    Added by review of #391, when deleting the gate's three lines left
    this file green: every icon a fixture could build under an unlisted
    syntax failed to decode anyway and landed in `dropped` from the
    `except` arm, gate or no gate. Its witness was a JPEG Extended icon,
    the one unlisted syntax the decoder reads -- and #387 measured and
    listed `.4.51`, so that witness is gone and no listed-or-not syntax
    is left that the decoder takes. So the gate is exercised directly:
    the allow-list narrowed to exclude JPEG Baseline, whose icon the
    decoder does read (the control, unpatched, carries it).

    In-process on purpose. A Session hands ingest to its own process
    pool, which a monkeypatch of the module global does not reach.

    Killed by deleting the gate: the narrowed call carries the icon.
    """
    from isocenter import io_handlers

    def decode(allow_list):
        monkeypatch.setattr(io_handlers, "_CARRIABLE_TRANSFER_SYNTAXES",
                            allow_list)
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = JPEGBaseline8Bit
        dropped = []
        carried = io_handlers._decode_nested_pixels(
            ds, [((), "7fe0,0010", "OB", _jpeg_icon_item())], dropped,
            None, offset_tables=[])
        return carried, dropped

    carried, dropped = decode(_CARRIABLE_TRANSFER_SYNTAXES)
    assert (len(carried), dropped) == (1, []), dropped

    carried, dropped = decode(
        _CARRIABLE_TRANSFER_SYNTAXES - {str(JPEGBaseline8Bit)})
    assert carried == []
    assert dropped == [("7fe0,0010", "OB")]


def test_the_carriable_transfer_syntaxes_are_the_uids_pydicom_names(tmp_path):
    """The allow-list is UID strings, so it needs checking against pydicom.

    Written as strings rather than `pydicom.uid` names because the names are
    not stable -- a draft of #183's spec cited
    `JPEGLossyCompressedPixelTransferSyntaxes`, which does not exist in
    pydicom 3.0.2 -- but a typo in a UID would silently refuse every file of
    that syntax, which reads exactly like "this codec is not supported".

    The list is no longer lossless-only. JPEG Baseline and JPEG 2000 are
    in it since #372, and JPEG Extended and JPEG-LS Near-Lossless since
    #387, each because its behaviour through a nested item was measured
    (N1, N2) and the label is corrected from the decoder's meta. HTJ2K
    (.4.203) joined `.4.201` and `.4.202` in #459, once the fallback
    decoded all three (N6) -- until then an allow-list's unmeasured side
    was, rightly, its refusing side.

    An exact set, not a membership check, so a syntax added without a
    test that names it is red here too.
    """
    from pydicom import uid

    names = ("ImplicitVRLittleEndian", "ExplicitVRLittleEndian",
             "DeflatedExplicitVRLittleEndian", "ExplicitVRBigEndian",
             "RLELossless", "JPEGLossless", "JPEGLosslessSV1",
             "JPEGLSLossless", "JPEG2000Lossless", "HTJ2KLossless",
             "HTJ2KLosslessRPCL", "JPEGBaseline8Bit", "JPEG2000",
             "JPEGExtended12Bit", "JPEGLSNearLossless", "HTJ2K")
    assert _CARRIABLE_TRANSFER_SYNTAXES == frozenset(
        str(getattr(uid, name)) for name in names)


# --- 4. Both export paths ------------------------------------------------

def test_write_tree_carries_the_icon_too(tmp_path):
    """`session.export()` and `DicomExporter.write_tree()` agree.

    A post-pass on one path only is the divergence `tests/test_api_coherence
    .py` exists to catch, and it catches it by comparing trees rather than
    contents -- so an icon written by one path and not the other would slip
    past it. Asserted here directly.
    """
    db, _src = _ingest(tmp_path, "writetree", icons=[_icon_item()])

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        patient = session.store.patients[0]
        DicomExporter.write_tree(patient, str(out),
                                 studies=patient.studies)
    finally:
        session.close()

    assert _exported(out).IconImageSequence[0].PixelData == ICON_BYTES


def test_write_tree_honours_the_redaction_attestation(tmp_path):
    """The serializer skips the pipeline's gates; this is not one of them.

    `write_tree` applies no burned-in scan, no subset filter and no
    redaction zones -- but dropping an icon out of a graph that carries a
    redaction attestation is a property of carrying icon bytes at all, not a
    pipeline step. The configuration half of the condition is structurally
    unavailable here (there is no session), so the attestation half is what
    this path can see, and it must see it.
    """
    db, _src = _ingest(tmp_path, "writetreeredact", icons=[_icon_item()])

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.redact_by_machine("SN-1", WHOLE_FRAME)
        patient = session.store.patients[0]
        DicomExporter.write_tree(patient, str(out),
                                 studies=patient.studies)
    finally:
        session.close()

    assert "IconImageSequence" not in _exported(out)


def test_write_tree_keeps_an_unattested_instances_own_icon(tmp_path):
    """The serializer's own-icon rule is the same per-instance rule.

    One patient, two series, one redacted. `write_tree` has no session and
    no zones, so the attestation on each instance is the whole own-icon
    gate there; the unredacted instance's thumbnail is of itself.
    """
    src = _multi_src(tmp_path, [("SN-1", dict(icons=[_icon_item()])),
                                ("SN-2", dict(icons=[_icon_item()]))])
    db = str(tmp_path / "wt.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        assert len(session.store.patients) == 1
        session.redact_by_machine("SN-1", WHOLE_FRAME)
        patient = session.store.patients[0]
        DicomExporter.write_tree(patient, str(out), studies=patient.studies)
    finally:
        session.close()

    exported = _exported_by_serial(out)
    assert "IconImageSequence" not in exported["SN-1"]
    assert exported["SN-2"].IconImageSequence[0].PixelData == ICON_BYTES


def test_write_tree_drops_a_referenced_icon_under_a_redaction_elsewhere(
        tmp_path):
    """The serializer's store-wide tier: attestation anywhere in the tree.

    `test_write_tree_honours_the_redaction_attestation` uses the carrier's
    own icon, which the worker decides from that instance's attestation, so
    it stays green with `write_tree`'s foreign flag wired off. This one is
    a referenced icon on an unredacted carrier, beside a redacted series
    under the same patient: only the foreign flag can drop it.
    """
    src = _carrier_and_other(tmp_path)
    db = str(tmp_path / "wtforeign.db")
    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        assert len(session.store.patients) == 1
        carrier_uid = _by_serial(session)["SN-CARRIER"].sop_instance_uid
        session.redact_by_machine("SN-OTHER", WHOLE_FRAME)
        patient = session.store.patients[0]
        DicomExporter.write_tree(patient, str(out), studies=patient.studies,
                                 show_progress=False,
                                 store_backend=session.store_backend)
        session.store_backend.flush_audit_queue()
    finally:
        session.close()

    exported = _exported_by_serial(out)
    assert set(exported) == {"SN-CARRIER", "SN-OTHER"}, exported
    assert _ref_icons(exported["SN-CARRIER"]) == []
    assert [(uid, scope) for uid, scope, _d in _icon_loss_rows(db)] == [
        (carrier_uid, LOSS_SCOPE_SIGNAL)], _icon_loss_rows(db)


def test_a_nested_restore_failure_with_no_message_names_its_type(
        tmp_path, monkeypatch):
    """F10 (#435): the nested-restore `DATA_LOSS` text, `... sidecar ()`.

    The restore's `try` covers the loader metadata as well as the read,
    so a message-less raise there reaches the same arm. Export runs
    in-process so the patch reaches the worker.
    """
    db, _src = _ingest(tmp_path, "nestedfail", icons=[_icon_item()])
    monkeypatch.setattr(io_handlers, "run_parallel",
                        lambda fn, items, *args, **kwargs: [fn(x) for x in items])

    def refuse(*_args, **_kwargs):
        raise KeyError()

    monkeypatch.setattr(io_handlers, "_nested_loader_metadata", refuse)
    _export(db, tmp_path / "out")

    details = [d for d, _scope in _data_loss_rows(db)
               if "could not be restored from the sidecar" in d]
    assert len(details) == 1, _data_loss_rows(db)
    assert "restored from the sidecar (KeyError);" in details[0], details

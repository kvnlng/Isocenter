"""End-to-end regressions for the pixel-geometry guess (#186, #205).

`tests/test_pixel_geometry.py` covers the resolver in isolation. This file
covers what the four call sites do with it: the read path that used to
corrupt the graph, the export paths that used to write a file describing a
different image, the redaction axes, and the OCR frame split.

Tests numbered here match the design spec's §7 list
(`docs/superpowers/specs/2026-08-29-pixel-geometry-authority.md`).
"""
import os
from unittest.mock import patch

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian

from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.io_handlers import (
    DicomExporter,
    DicomStore,
    ExportContext,
    _export_instance_worker,
)
from isocenter.services import RedactionService
from isocenter.session import DicomSession


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def write_source(path, arr, photometric="MONOCHROME2", samples=1,
                 sop_uid=None, instance_num=1):
    """Write a Secondary Capture whose descriptors describe `arr` honestly.

    `arr` is (frames, rows, cols) or (rows, cols) for grayscale, and
    (rows, cols, samples) for colour.
    """
    sop_uid = sop_uid or pydicom.uid.generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientName = "TestGeometry"
    ds.PatientID = "PID_GEOMETRY"
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SeriesInstanceUID = "1.2.826.0.1.3680043.8.498.900001"
    ds.StudyInstanceUID = "1.2.826.0.1.3680043.8.498.900000"
    ds.Modality = "OT"
    ds.ConversionType = "WSD"
    ds.StudyDate = "20230101"
    ds.SeriesNumber = 1
    ds.InstanceNumber = instance_num

    if samples > 1:
        rows, cols = arr.shape[0], arr.shape[1]
        frames = 1
        ds.PlanarConfiguration = 0
    elif arr.ndim == 3:
        frames, rows, cols = arr.shape
    else:
        frames = 1
        rows, cols = arr.shape

    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = samples
    ds.PhotometricInterpretation = photometric
    if frames > 1:
        ds.NumberOfFrames = frames
    ds.BitsAllocated = arr.dtype.itemsize * 8
    ds.BitsStored = arr.dtype.itemsize * 8
    ds.HighBit = arr.dtype.itemsize * 8 - 1
    ds.PixelRepresentation = 0

    ds.PixelData = arr.tobytes()
    ds.save_as(str(path), enforce_file_format=True)
    return sop_uid


def only_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def build_graph(inst, machine_sn="SN-GEOM"):
    """Wrap one Instance in the minimum graph the exporters need."""
    p = Patient("P_GEOM", "Geometry Test")
    st = Study("S_GEOM", "20230101")
    se = Series("SE_GEOM", "OT", 1)
    se.equipment = Equipment("Man", "Mod", machine_sn)
    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    return p


# ---------------------------------------------------------------------------
# Test 1 -- a read must not write (#186)
# ---------------------------------------------------------------------------

def test_get_pixel_data_does_not_rewrite_the_geometry(tmp_path):
    """§7.1: every descriptor survives `release_memory` -> `get_pixel_data`.

    The array was reshaped by `SidecarPixelLoader` *from* these attributes,
    so a sync back from the array could only ever disagree with them.
    """
    src = tmp_path / "in"
    src.mkdir()
    arr = np.arange(3 * 4 * 4, dtype=np.uint16).reshape((3, 4, 4))
    write_source(src / "mf.dcm", arr)

    with DicomSession(str(tmp_path / "geom.db")) as session:
        session.ingest(str(src))
        session.save(sync=True)
        inst = only_instance(session)

        before = dict(inst.attributes)
        session.release_memory()

        # Distinguishes "the load dirtied it" from "it arrived dirty".
        assert inst.has_unsaved_changes is False

        loaded = inst.get_pixel_data()

        assert np.array_equal(loaded, arr)
        assert inst.attributes.get("0028,0002") == 1        # SamplesPerPixel
        assert inst.attributes.get("0028,0004") == "MONOCHROME2"
        assert inst.attributes.get("0028,0006") is None     # PlanarConfiguration
        assert int(inst.attributes.get("0028,0008")) == 3   # NumberOfFrames
        assert inst.attributes.get("0028,0010") == 4        # Rows
        assert inst.attributes.get("0028,0011") == 4        # Columns
        assert inst.attributes == before
        assert inst.has_unsaved_changes is False


# ---------------------------------------------------------------------------
# Test 2 -- and the corruption was durable (#186 §1.2)
# ---------------------------------------------------------------------------

def test_the_wrong_geometry_is_not_persisted_to_sqlite(tmp_path):
    """§7.2: reopening the DB shows the source geometry, not the guess.

    `set_pixel_data` ends in `mark_modified()`, so a *read* used to dirty
    the entity and the next `save()` wrote the guess into SQLite. The source
    file is untouched either way; the index the rest of the pipeline reads
    was not.
    """
    src = tmp_path / "in"
    src.mkdir()
    db = str(tmp_path / "durable.db")
    arr = np.arange(3 * 4 * 4, dtype=np.uint16).reshape((3, 4, 4))
    write_source(src / "mf.dcm", arr)

    with DicomSession(db) as session:
        session.ingest(str(src))
        session.save(sync=True)
        session.release_memory()
        only_instance(session).get_pixel_data()
        session.save(sync=True)

    with DicomSession(db) as reopened:
        inst = only_instance(reopened)
        assert inst.attributes.get("0028,0002") == 1
        assert inst.attributes.get("0028,0004") == "MONOCHROME2"
        assert inst.attributes.get("0028,0010") == 4
        assert inst.attributes.get("0028,0011") == 4
        assert int(inst.attributes.get("0028,0008")) == 3


# ---------------------------------------------------------------------------
# Test 3 -- the exported file round-trips (#186, #205)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", [(3, 4, 4), (2, 8, 4), (3, 8, 3), (3, 8, 8), (1, 4, 4)])
@pytest.mark.parametrize("use_compression", [False, True])
def test_export_round_trips_multiframe_grayscale(tmp_path, shape, use_compression):
    """§7.3: `dcmread(exported).pixel_array` equals the source array."""
    src = tmp_path / "in"
    src.mkdir()
    out = tmp_path / "out"

    arr = np.arange(int(np.prod(shape)), dtype=np.uint16).reshape(shape)
    # A single-frame source is written 2-D; NumberOfFrames=1 is not declared.
    source_arr = arr[0] if shape[0] == 1 else arr
    write_source(src / "mf.dcm", source_arr)

    with DicomSession(str(tmp_path / "rt.db")) as session:
        session.ingest(str(src))
        session.export(str(out), use_compression=use_compression)

    written = [os.path.join(root, f)
               for root, _, files in os.walk(out) for f in files
               if f.endswith(".dcm")]
    assert len(written) == 1, f"expected one exported file, got {written}"

    ds = pydicom.dcmread(written[0])
    assert ds.SamplesPerPixel == 1
    assert ds.Rows == source_arr.shape[-2]
    assert ds.Columns == source_arr.shape[-1]
    assert np.array_equal(ds.pixel_array, source_arr)


# ---------------------------------------------------------------------------
# Test 4 -- write_tree, the serializer alone (#205 §1.4)
# ---------------------------------------------------------------------------

def test_write_tree_writes_the_declared_geometry(tmp_path):
    """§7.4: the hand-built-graph path, with no session and no release_memory.

    This is the only way to see site 2's heuristic in isolation: through
    `session.export()` the pixels are never resident, so site 1 always fires
    first. That is why #186 measured SPP=4 and #205 measured SPP=1.
    """
    arr = np.arange(2 * 8 * 4, dtype=np.uint16).reshape((2, 8, 4))

    inst = Instance("I_GEOM", "1.2.826.0.1.3680043.8.498.900002", 1)
    inst.attributes["0028,0002"] = 1
    inst.attributes["0028,0008"] = 2
    inst.attributes["0028,0010"] = 8
    inst.attributes["0028,0011"] = 4
    inst.attributes["0028,0004"] = "MONOCHROME2"
    inst.attributes["0028,0100"] = 16
    inst.attributes["0028,0101"] = 16
    inst.attributes["0028,0102"] = 15
    inst.attributes["0028,0103"] = 0
    inst.attributes["0008,0060"] = "OT"
    inst.pixel_array = arr

    out = tmp_path / "tree"
    DicomExporter.write_tree(build_graph(inst), str(out), show_progress=False)

    written = [os.path.join(root, f)
               for root, _, files in os.walk(out) for f in files
               if f.endswith(".dcm")]
    assert len(written) == 1

    ds = pydicom.dcmread(written[0])
    assert ds.Rows == 8
    assert ds.Columns == 4
    assert int(ds.NumberOfFrames) == 2
    assert ds.SamplesPerPixel == 1
    assert np.array_equal(ds.pixel_array, arr)


# ---------------------------------------------------------------------------
# Test 5 -- the colour space is not relabelled (#186, new finding)
# ---------------------------------------------------------------------------

def test_ybr_full_survives_load_and_export(tmp_path):
    """§7.5: a plain single-frame 8x8 YBR_FULL instance stays YBR_FULL.

    Far broader than the 3-or-4-columns trigger: `if samples >= 3: "RGB"`
    relabels *every* non-RGB colour space, whatever its dimensions.
    """
    src = tmp_path / "in"
    src.mkdir()
    out = tmp_path / "out"

    arr = np.zeros((8, 8, 3), dtype=np.uint8)
    arr[3, 3] = [10, 20, 30]
    write_source(src / "ybr.dcm", arr, photometric="YBR_FULL", samples=3)

    with DicomSession(str(tmp_path / "ybr.db")) as session:
        session.ingest(str(src))
        session.save(sync=True)
        inst = only_instance(session)
        session.release_memory()

        inst.get_pixel_data()
        assert inst.attributes.get("0028,0004") == "YBR_FULL"

        session.export(str(out), use_compression=False)

    written = [os.path.join(root, f)
               for root, _, files in os.walk(out) for f in files
               if f.endswith(".dcm")]
    assert len(written) == 1
    ds = pydicom.dcmread(written[0])
    assert ds.PhotometricInterpretation == "YBR_FULL"
    assert ds.SamplesPerPixel == 3
    assert ds.Rows == 8
    assert ds.Columns == 8


# ---------------------------------------------------------------------------
# Test 6 -- redaction addresses the right axes (site 3)
# ---------------------------------------------------------------------------

def test_redaction_zeroes_the_zone_in_every_frame():
    """§7.6: the most severe of the four sites, named in neither issue.

    A 2-frame 8x4 grayscale array read as (rows=2, cols=8, samples=4) puts
    the zone on the wrong axes: the pipeline reports a successful redaction
    and the burned-in identifier is still in the exported pixels.
    """
    arr = np.ones((2, 8, 4), dtype=np.uint8) * 7

    inst = Instance("I_RED", "1.2.826.0.1.3680043.8.498.900003", 1)
    inst.attributes["0028,0002"] = 1
    inst.attributes["0028,0008"] = 2
    inst.attributes["0028,0010"] = 8
    inst.attributes["0028,0011"] = 4
    inst.attributes["0028,0004"] = "MONOCHROME2"
    inst.pixel_array = arr

    store = DicomStore()
    store.patients.append(build_graph(inst, machine_sn="SN-RED"))

    service = RedactionService(store)
    # Zone: rows 0-3, all four columns.
    service.redact_machine_instances("SN-RED", [(0, 3, 0, 4)],
                                     show_progress=False)

    result = inst.pixel_array
    assert result.shape == (2, 8, 4)
    for frame in range(2):
        assert np.all(result[frame, 0:3, :] == 0), (
            f"frame {frame} rows 0-2 were not redacted:\n{result[frame]}")
        assert np.all(result[frame, 3:, :] == 7), (
            f"frame {frame} rows 3+ were redacted and should not have been:\n"
            f"{result[frame]}")


# ---------------------------------------------------------------------------
# Test 7 -- OCR sees every frame (site 4)
# ---------------------------------------------------------------------------

def test_analyze_pixels_splits_a_multiframe_array_into_frames():
    """§7.7: a 3-frame 8x3 grayscale image is three frames, not one RGB one.

    Note this needs a *real* Instance, not the MagicMock the rest of
    `test_pixel_analysis.py` uses: with nothing declared, (3,8,3) resolves
    GUESSED to arm B and would legitimately be one frame.
    """
    from isocenter import pixel_analysis

    inst = Instance("I_OCR", "1.2.826.0.1.3680043.8.498.900004", 1)
    inst.attributes["0028,0002"] = 1
    inst.attributes["0028,0008"] = 3
    inst.attributes["0028,0010"] = 8
    inst.attributes["0028,0011"] = 3
    inst.pixel_array = np.zeros((3, 8, 3), dtype=np.uint8)

    with patch.object(pixel_analysis, "HAS_OCR", True), \
            patch.object(pixel_analysis, "detect_text_regions",
                         return_value=[]) as detect:
        pixel_analysis.analyze_pixels(inst)

    assert detect.call_count == 3
    for i, call in enumerate(detect.call_args_list):
        assert call.args[0].shape == (8, 3)
        assert call.kwargs["frame_idx"] == i


# ---------------------------------------------------------------------------
# Test 8 -- a contradiction raises (§3.6)
# ---------------------------------------------------------------------------

def test_set_pixel_data_raises_on_a_layout_contradiction():
    """§7.8: no axis of a (5,8,4) array can carry 3 samples per pixel."""
    inst = Instance("I_BAD", "1.2.826.0.1.3680043.8.498.900005", 1)
    inst.attributes["0028,0002"] = 3

    with pytest.raises(ValueError) as exc:
        inst.set_pixel_data(np.zeros((5, 8, 4), dtype=np.uint8))

    msg = str(exc.value)
    assert "(5, 8, 4)" in msg
    assert "SamplesPerPixel=3" in msg


def test_set_pixel_data_accepts_a_resized_array():
    """The setter contract of §3.1: magnitude changes are its job.

    Only a *layout* contradiction raises. `tests/test_compaction.py` replaces
    a (100,1000) array with another one, and that must keep working.
    """
    inst = Instance("I_RESIZE", "1.2.826.0.1.3680043.8.498.900006", 1)
    inst.set_pixel_data(np.zeros((100, 1000), dtype=np.uint8))
    inst.set_pixel_data(np.zeros((50, 500), dtype=np.uint8))
    assert inst.attributes["0028,0010"] == 50
    assert inst.attributes["0028,0011"] == 500


# ---------------------------------------------------------------------------
# The descriptors the worker writes come from one geometry, not two sources
# ---------------------------------------------------------------------------

def _export_one(tmp_path, arr, attrs, name="one.dcm", zones=None):
    """Put one hand-built instance through the export worker."""
    inst = Instance("I_COH", "1.2.826.0.1.3680043.8.498.900011", 1)
    inst.attributes["0008,0060"] = "OT"
    inst.attributes.update(attrs)
    inst.pixel_array = arr

    out_path = str(tmp_path / name)
    outcome = _export_instance_worker(ExportContext(
        instance=inst, output_path=out_path,
        patient_attributes={}, study_attributes={}, series_attributes={},
        redaction_zones=list(zones or [])))
    assert outcome.ok is True, outcome.error
    return pydicom.dcmread(out_path)


def test_worker_takes_samples_per_pixel_from_the_resolved_geometry(tmp_path):
    """Rows/Columns and SamplesPerPixel must come from the *same* answer.

    §4.3 of the spec calls `ds.SamplesPerPixel = geom.samples` the single
    most important line, and until this test nothing pinned it: taking
    Rows and Columns from the geometry while reading SamplesPerPixel
    straight out of `attributes` is the incoherence that made #186's
    export undecodable rather than merely wrong, and every existing
    fixture happens to have the two agreeing.

    A multi-frame colour instance that never declared SamplesPerPixel is
    the reachable case. Sourcing it from `attributes.get("0028,0002", 1)`
    writes `SamplesPerPixel=1` beside `Rows=4 Columns=4 NumberOfFrames=2`
    for a `(2,4,4,3)` array: the file decodes cleanly as `(6,4,4)` and is
    simply a different image, which is the silent half of #205.

    These two shapes and not others because they are the only two ways
    `geom.samples` and the declared `SamplesPerPixel` can disagree:
    the geometry supplies a count the attributes never declared (here),
    or it overrides one the attributes declared wrongly (the test
    below). Every other fixture in the suite has the two agreeing, which
    is why reverting this line passed all 906 tests.
    """
    arr = np.arange(2 * 4 * 4 * 3, dtype=np.uint8).reshape((2, 4, 4, 3))
    ds = _export_one(tmp_path, arr, {
        "0028,0100": 8, "0028,0101": 8, "0028,0102": 7, "0028,0103": 0,
    }, name="rank4.dcm")

    assert ds.SamplesPerPixel == 3
    assert ds.Rows == 4
    assert ds.Columns == 4
    assert int(ds.NumberOfFrames) == 2
    assert np.array_equal(ds.pixel_array, arr)


def test_worker_overrides_a_stale_samples_per_pixel(tmp_path):
    """A rank-2 array has one reading, whatever the attributes still say.

    The other half of the same guarantee, and the loud failure rather
    than the silent one: `SamplesPerPixel=3` beside a `(8,8)` array
    writes 64 bytes of Pixel Data under a header claiming 192, and
    `dcmread(...).pixel_array` raises `AttributeError: Missing required
    element (0028,0006)` -- spec §1.3's third row exactly.
    """
    arr = np.arange(8 * 8, dtype=np.uint8).reshape((8, 8))
    ds = _export_one(tmp_path, arr, {
        "0028,0002": 3,
        "0028,0100": 8, "0028,0101": 8, "0028,0102": 7, "0028,0103": 0,
    }, name="rank2.dcm")

    assert ds.SamplesPerPixel == 1
    assert np.array_equal(ds.pixel_array, arr)


# ---------------------------------------------------------------------------
# Tests 9 and 10 -- the deliberate asymmetry
# ---------------------------------------------------------------------------

def _ambiguous_instance():
    inst = Instance("I_AMB", "1.2.826.0.1.3680043.8.498.900007", 1)
    inst.attributes["0008,0060"] = "OT"
    for tag in ("0028,0002", "0028,0008", "0028,0010", "0028,0011"):
        assert tag not in inst.attributes
    return inst


def test_export_worker_refuses_to_write_a_guessed_geometry(tmp_path):
    """§7.9: a guess must not become a file on disk.

    A recipient cannot tell a guessed geometry apart from a correct one, so
    the export boundary is where the guess has to stop.
    """
    inst = _ambiguous_instance()
    inst.pixel_array = np.zeros((100, 200, 3), dtype=np.uint8)

    out_path = str(tmp_path / "guess.dcm")
    ctx = ExportContext(
        instance=inst, output_path=out_path,
        patient_attributes={}, study_attributes={}, series_attributes={})

    outcome = _export_instance_worker(ctx)

    assert outcome.ok is False
    assert not os.path.exists(out_path)
    msg = str(outcome.error)
    assert "(100, 200, 3)" in msg
    assert "SamplesPerPixel" in msg


def test_set_pixel_data_accepts_a_guessed_geometry_but_warns(caplog):
    """§7.10: the setter accepts what the export worker refuses.

    Deliberate, and pinned here so it is not "unified": a hand-built graph
    must be able to take pixels before its attributes -- that is what
    `DicomExporter.write_tree()` exists to serve -- while a guess must never
    reach disk. The guess stops being *silent*, not being *possible*.
    """
    import logging

    inst = _ambiguous_instance()

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        inst.set_pixel_data(np.zeros((100, 200, 3), dtype=np.uint8))

    assert inst.attributes["0028,0002"] == 3
    assert inst.attributes["0028,0010"] == 100
    assert inst.attributes["0028,0011"] == 200

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the guess must not be silent"
    assert any("SamplesPerPixel" in r.getMessage() for r in warnings)
    assert any("(100, 200, 3)" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------------------
# Dirty tracking -- setting pixels is a mutation the store has to hold
# ---------------------------------------------------------------------------

def test_replacing_the_pixels_dirties_the_instance():
    """§7.17: a same-shape, same-dtype, different-bytes array still needs saving.

    Every descriptor comparison in `set_pixel_data` is blind to this -- the
    assertions below spell that out: a redacted frame has exactly the Rows,
    Columns, SamplesPerPixel and BitsAllocated of the frame it replaced, so
    nothing in `attributes` changes and only the pixels did. Making
    `mark_modified()` conditional on a descriptor change leaves an
    incremental `save_all` skipping the instance, and the redacted pixels
    never reach the sidecar.

    A guard, not a regression: this passes on `692218c` too, where
    `set_pixel_data` ends in an unconditional `mark_modified()`. It exists
    because the pin that looks like it covers this --
    `test_persistence_incremental.py::test_unsaved_tracking_pixel_change` --
    sets pixels on a *fresh* instance, where the descriptors change and a
    conditional rule would pass as well.
    """
    inst = Instance("I_DIRTY", "1.2.826.0.1.3680043.8.498.900008", 1)
    inst.set_pixel_data(np.zeros((64, 64), dtype=np.uint8))
    inst.mark_persisted()
    assert inst.has_unsaved_changes is False
    before = dict(inst.attributes)

    inst.set_pixel_data(np.ones((64, 64), dtype=np.uint8))

    assert inst.attributes == before, (
        "the geometry is identical, so this test is only about the pixels")
    assert inst.has_unsaved_changes is True, (
        "replacing the pixel data must mark the instance for saving")


def test_setting_the_same_array_object_twice_still_dirties():
    """§7.18: object identity is not evidence that the contents are unchanged.

    Dirtying only when the array is a different object was considered and
    rejected: `RedactionService._redact_instance_pixels` mutates the
    instance's own array in place, so the object can be identical while
    every pixel in it has changed. Setting pixel data is a mutation of what
    the store holds, full stop.

    A guard, not a regression: this passes on `692218c` as well.
    """
    inst = Instance("I_SAME", "1.2.826.0.1.3680043.8.498.900009", 1)
    arr = np.zeros((64, 64), dtype=np.uint8)
    inst.set_pixel_data(arr)
    inst.mark_persisted()
    assert inst.has_unsaved_changes is False
    before = dict(inst.attributes)

    arr[0:10, 0:10] = 0  # a caller mutating in place, as redaction does
    inst.set_pixel_data(arr)

    assert inst.attributes == before
    assert inst.has_unsaved_changes is True


def test_redaction_dirties_the_instance_it_redacted():
    """The production path the identity rule would have got wrong.

    `_redact_instance_pixels` calls `set_pixel_data` with a *copy* on the
    **not-writeable arm** -- the arm this test forces, and the only one
    that reaches `set_pixel_data` at all. The copy's contents at that
    moment are identical to the original; it is mutated afterwards. So
    the setter is handed an array indistinguishable from the one already
    on the instance, and everything that dirties it has to be
    unconditional.

    The writeable arm does not go through the setter: it mutates in
    place and returns True, and `_redact_instance_pixels` leaves the
    instance *clean* (measured). Its dirtying comes from the caller --
    `RedactionService.redact` and the parallel worker both call
    `inst.mark_modified()` under `if modified:`, and both persist the
    pixels afterwards -- so the end-to-end behaviour is the same and
    nothing is wrong. It is simply not this test's subject, and the
    asymmetry between the two arms is worth knowing before moving code
    across it.

    **Every descriptor is declared, and declared correctly**, on purpose.
    An instance missing `PhotometricInterpretation` or `BitsAllocated`
    gets dirtied by the conditional descriptor writes of §3.11 rule 1 no
    matter what rule 2 does, so it would pass with `mark_modified()` made
    conditional and pin nothing this docstring claims. With the full set
    declared, `set_pixel_data` changes no attribute -- asserted below --
    and the unconditional `mark_modified()` is the only thing left that
    can dirty the instance. Redaction is exactly the case §3.11 rule 2
    exists for: same shape, same dtype, different bytes.

    Note what this deliberately does *not* pin: that the descriptor
    writes are conditional. Making them unconditional is invisible here
    and everywhere else in the suite -- an accepted gap, not an
    oversight, because writing an identical value back is only a
    performance and revision-churn question, and pinning it would mean
    asserting on `_revision` arithmetic rather than on behaviour.
    """
    inst = Instance("I_RD", "1.2.826.0.1.3680043.8.498.900010", 1)
    inst.attributes["0028,0002"] = 1
    inst.attributes["0028,0004"] = "MONOCHROME2"
    inst.attributes["0028,0010"] = 8
    inst.attributes["0028,0011"] = 8
    inst.attributes["0028,0100"] = 8
    arr = np.ones((8, 8), dtype=np.uint8) * 7
    arr.flags.writeable = False
    inst.pixel_array = arr
    before = dict(inst.attributes)
    inst.mark_persisted()
    assert inst.has_unsaved_changes is False

    service = RedactionService(DicomStore())
    assert service._redact_instance_pixels(inst, arr, [(0, 4, 0, 4)]) is True

    assert inst.attributes == before, (
        "the descriptors already described this array; if redaction "
        "changed one, this test is dirtying the instance by the wrong "
        "route and no longer pins the unconditional mark_modified()")
    assert inst.has_unsaved_changes is True


# ---------------------------------------------------------------------------
# Rank 1 -- the one behavioural change here that is not a bug fix
# ---------------------------------------------------------------------------

def test_a_flat_buffer_reshaped_from_the_descriptors_still_dirties():
    """§7.21: the rank-1 early return dirties the instance. A regression.

    Unlike §7.17 and §7.18 above, this **fails on `692218c`**, and it fails
    there because the behaviour is new rather than because it was broken.
    The 1-D branch reshapes, assigns `pixel_array` and `return`s before
    reaching any descriptor write, so under a literal reading of §3.7
    ("keep it verbatim") a flat array replaces what the store holds and
    leaves the instance clean -- §11.1's defect in its purest form, no
    descriptor changing because none is written at all. §3.7 was amended to
    say the early return must `mark_modified()`.

    Built to §7's fixture-hygiene rule 2: a *complete* descriptor set, an
    explicit `mark_persisted()`, and `has_unsaved_changes is False`
    asserted immediately before the call. A fresh `Instance` has empty
    `attributes`, so every descriptor write is a change and it would dirty
    under any rule -- which is exactly how
    `test_persistence_incremental.py::test_unsaved_tracking_pixel_change`
    came to look like a pin for this without being one.

    The reshape assertion is what pins the *arm*: the general path leaves a
    1-D array 1-D, so only the early return can produce `(8, 8)`.
    """
    inst = Instance("I_FLAT", "1.2.826.0.1.3680043.8.498.900021", 1)
    inst.set_attr("0028,0010", 8)          # Rows
    inst.set_attr("0028,0011", 8)          # Columns
    inst.set_attr("0028,0002", 1)          # SamplesPerPixel
    inst.set_attr("0028,0008", 1)          # NumberOfFrames
    inst.set_attr("0028,0100", 8)          # BitsAllocated
    inst.set_attr("0028,0004", "MONOCHROME2")
    inst.mark_persisted()
    assert inst.has_unsaved_changes is False, "setup must leave a clean instance"
    before = dict(inst.attributes)

    inst.set_pixel_data(np.arange(64, dtype=np.uint8))

    assert inst.attributes == before, (
        "the declared descriptors are the input to this reshape, so the "
        "early return writes nothing back")
    assert inst.pixel_array.shape == (8, 8), (
        "only the early return reshapes a flat buffer -- the general path "
        "would leave it 1-D, so this pins the arm as well as the effect")
    assert inst.has_unsaved_changes is True, (
        "a reshaped flat buffer is still a new array the store has to hold")


# ---------------------------------------------------------------------------
# BitsAllocated -- the descriptor the removed load-path write used to fix
# ---------------------------------------------------------------------------

def _write_binary_segmentation(path, arr):
    """A `BitsAllocated=1` source: 1 bit per pixel, packed, as PS3.5 requires.

    `write_source` derives the width from the dtype, so it can only build
    the coherent case. This builds the one that matters -- a declared
    width that no numpy array can have, which is exactly the class the
    load path used to reconcile and nothing does now.
    """
    sop_uid = pydicom.uid.generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.66.4"
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientName = "TestBits"
    ds.PatientID = "PID_BITS"
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SeriesInstanceUID = "1.2.826.0.1.3680043.8.498.900301"
    ds.StudyInstanceUID = "1.2.826.0.1.3680043.8.498.900300"
    ds.Modality = "SEG"
    ds.StudyDate = "20230101"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1

    frames, rows, cols = arr.shape
    ds.NumberOfFrames = frames
    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 1
    ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelRepresentation = 0
    ds.PixelData = np.packbits(arr.ravel(), bitorder="little").tobytes()
    ds.save_as(str(path), enforce_file_format=True)
    return sop_uid


def test_export_writes_bits_allocated_the_array_actually_has(tmp_path):
    """A declared BitsAllocated of 1 must not be written beside 8-bit bytes.

    **This is a regression test in the unusual direction: it fails on
    `cf9dcd6` and passes on `692218c`.** It is not a guard. Removing
    `set_pixel_data()` from `get_pixel_data()` was right for geometry,
    but that call's last act was an unconditional
    `set_attr("0028,0100", array.itemsize * 8)`, and that was the only
    thing reconciling a declared width with the dtype the loader
    actually produces. `SidecarPixelLoader` buckets dtype as
    `uint16 if bits > 8 else uint8`, so a declared 1 yields a `uint8`
    array, and the export then wrote `BitsAllocated=1` beside
    `arr.tobytes()` at 8 bits per pixel: pydicom reads 8x the pixels and
    reshapes a 2-frame 4x8 segmentation into 16 frames. Decodable,
    coherent, and a different image -- #186's failure mode exactly,
    reintroduced through the descriptor that was supposed to be immune.

    The fix puts the reconciliation where the bytes are produced, three
    lines below `ds.PixelData = arr.tobytes()`, which is what §3.10
    argues for: a width that disagrees with the array cannot be
    honoured, so "the attributes win" was never an option here.
    """
    arr = np.zeros((2, 4, 8), dtype=np.uint8)
    arr[0, 1, 2] = 1
    arr[1, 3, 7] = 1
    src = tmp_path / "src"
    src.mkdir()
    _write_binary_segmentation(src / "seg.dcm", arr)

    session = DicomSession(str(tmp_path / "bits.db"))
    try:
        session.ingest(str(src))
        assert only_instance(session).attributes["0028,0100"] in (1, "1"), (
            "the fixture must carry the declared width the defect needs")
        out = tmp_path / "out"
        session.export(str(out), show_progress=False)
    finally:
        session.close()

    written = sorted(out.rglob("*.dcm"))
    assert len(written) == 1, written
    ds = pydicom.dcmread(written[0])

    assert ds.BitsAllocated == 8, (
        "the file declares a width its Pixel Data does not have")
    assert ds.pixel_array.shape == (2, 4, 8), ds.pixel_array.shape
    assert np.array_equal(ds.pixel_array, arr)


def test_export_writes_bits_allocated_a_wide_array_actually_has(tmp_path):
    """Test 22, second arm: the one that tells `itemsize * 8` from `default_bits`.

    **Mutation killed:** `ds.BitsAllocated = arr.itemsize * 8` mutated to
    `ds.BitsAllocated = default_bits`, and equally to the original
    `inst.attributes.get("0028,0100", default_bits)`. Both write 16
    here; only the shipped expression writes 32.

    The first arm cannot kill either. Its array is `uint8`, where
    `itemsize * 8`, `default_bits` and the declared width all agree on
    8 -- it pins that *some* reconciliation happens, not which one, so
    the capped form could be reintroduced with the suite green.
    `default_bits` is `8 if arr.itemsize == 1 else 16`, so it is wrong
    for every width above 16, and `692218c` was right there: its
    `set_pixel_data` wrote `array.itemsize * 8` unconditionally.

    Fixture hygiene (section 7, trap 1): the array is assigned straight
    to `inst.pixel_array`, never through `set_pixel_data`, because the
    setter would overwrite `0028,0100` with 32 itself and the assertion
    would then hold no matter what the export line said. The declared
    width is left at 16 -- a value both rejected expressions produce --
    so the export is the only thing that can make this 32.

    Measured: 64 bytes of Pixel Data for 16 uint32 pixels, and pydicom
    round-trips it to `uint32 (4, 4)` even beside a stale `BitsStored`
    of 16, which stays declared because it does not constrain how many
    bytes `tobytes()` emits (section 8).
    """
    arr = np.arange(4 * 4, dtype=np.uint32).reshape((4, 4))
    ds = _export_one(tmp_path, arr, {
        "0028,0010": 4, "0028,0011": 4, "0028,0002": 1,
        "0028,0004": "MONOCHROME2",
        "0028,0100": 16, "0028,0101": 16, "0028,0102": 15, "0028,0103": 0,
    }, name="u32.dcm")

    assert ds.BitsAllocated == 32, (
        "the width came from the declaration or from default_bits, not "
        "from the array whose bytes were written")
    assert len(ds.PixelData) == 4 * 4 * 4
    assert np.array_equal(ds.pixel_array, arr)


# ---------------------------------------------------------------------------
# Gaps found by auditing this branch's own commits with the mutation probe.
# Each one is a production line that no test noticed the loss of.
# ---------------------------------------------------------------------------

def test_export_redacts_every_frame_of_a_multiframe_array(tmp_path):
    """The export worker's *own* redaction call, not the service's.

    **Mutation killed:** dropping `geometry=geom` from
    `RedactionService.apply_redaction_to_array(arr, ctx.redaction_zones,
    geometry=geom)` in `_export_instance_worker`. That reverts this call
    to the last-axis heuristic and survived the full 917-test suite.

    `test_redaction_zeroes_the_zone_in_every_frame` above looks like it
    covers this and does not: it exercises
    `RedactionService._redact_instance_pixels`, which is the *session*
    path. `session.export(redaction_zones=...)` reaches a second,
    independent call inside the worker, and that one had nothing on it.
    The site matters more than the one that was covered, because this is
    the last place the pixels are touched before they become a file --
    with the wrong axes the zone lands on rows 0-1 of a (2,8,4) array
    read as (rows=2, cols=8, samples=4), the pipeline reports a
    successful redaction, and the burned-in identifier is in the
    exported file.

    The shape is the same one §7.6 uses, for the same reason: a last
    axis of 4 is exactly where "frames" and "samples" are
    indistinguishable without the descriptors.
    """
    arr = np.ones((2, 8, 4), dtype=np.uint8) * 7
    ds = _export_one(tmp_path, arr, {
        "0028,0002": 1, "0028,0008": 2,
        "0028,0010": 8, "0028,0011": 4,
        "0028,0004": "MONOCHROME2",
        "0028,0100": 8, "0028,0101": 8, "0028,0102": 7, "0028,0103": 0,
    }, name="redacted.dcm", zones=[(0, 3, 0, 4)])

    out = ds.pixel_array
    assert out.shape == (2, 8, 4)
    for frame in range(2):
        assert np.all(out[frame, 0:3, :] == 0), (
            f"frame {frame} rows 0-2 reached the file unredacted:\n"
            f"{out[frame]}")
        assert np.all(out[frame, 3:, :] == 7), (
            f"frame {frame} rows 3+ were zeroed and should not have been:\n"
            f"{out[frame]}")


def test_set_pixel_data_corrects_a_stale_number_of_frames():
    """A NumberOfFrames the array contradicts is corrected, not left.

    **Mutation killed:** narrowing
    `if geom.frames > 1 or "0028,0008" in self.attributes:` to
    `if geom.frames > 1:` in `set_pixel_data`. That arm survived the full
    suite: every fixture either has no NumberOfFrames or has a correct
    one, so the `or` clause never decided anything.

    The reachable case is replacing a multi-frame array with a
    single-frame one. Rank 2 has one reading whatever the attributes
    say, so `geom.frames` is 1 and the declared 5 is now false. Leaving
    it is the same class of incoherence as #186: descriptors that
    describe an array the instance no longer holds, and the next export
    writes 64 bytes under a header claiming five frames of them.

    Note this does *not* pin the string-to-int canonicalisation the same
    clause also performs -- `_write_int_if_changed` compares through
    `declared_int`, so a stored `"1"` equals `1` and is deliberately left
    alone rather than churning the graph.
    """
    inst = Instance("I_NF", "1.2.826.0.1.3680043.8.498.900021", 1)
    inst.attributes["0028,0008"] = 5
    inst.attributes["0028,0010"] = 8
    inst.attributes["0028,0011"] = 8
    inst.attributes["0028,0002"] = 1

    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint8))

    assert inst.attributes["0028,0008"] == 1, (
        "a NumberOfFrames the array contradicts survived the setter")


def test_export_corrects_a_stale_number_of_frames(tmp_path):
    """The same clause in the export worker, and the louder failure.

    **Mutation killed:** narrowing
    `if geom.frames > 1 or "0028,0008" in inst.attributes:` to
    `if geom.frames > 1:` in `_export_instance_worker`. Survived the full
    suite for the same reason as the setter's copy.

    Here the consequence is on disk rather than in memory: the generic
    attribute merge has already put the declared `NumberOfFrames=5` on
    the dataset, so skipping this write leaves a file whose header
    claims five frames beside 64 bytes of Pixel Data. Writing
    `geom.frames` is what makes the header agree with the bytes that
    were actually emitted.
    """
    arr = np.arange(8 * 8, dtype=np.uint8).reshape((8, 8))
    ds = _export_one(tmp_path, arr, {
        "0028,0008": 5,
        "0028,0010": 8, "0028,0011": 8, "0028,0002": 1,
        "0028,0004": "MONOCHROME2",
        "0028,0100": 8, "0028,0101": 8, "0028,0102": 7, "0028,0103": 0,
    }, name="stale_frames.dcm")

    assert int(ds.NumberOfFrames) == 1, (
        "the file claims more frames than its Pixel Data carries")
    assert len(ds.PixelData) == 8 * 8
    assert np.array_equal(ds.pixel_array, arr)

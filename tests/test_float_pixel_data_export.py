"""Float pixel data must not leave under the integer tag (#170).

An instance whose only pixel element is (7fe0,0008) Float Pixel Data
exported as an instance whose only pixel element is (7fe0,0010) Pixel
Data. The bytes were byte-identical; the tag, and therefore the meaning,
was not. `float32 [0.5 1.5 2.5 3.5]` read back as
`uint32 [1056964608 1069547520 1075838976 1080033280]` -- 1056964608 is
0x3F000000, the IEEE-754 encoding of 0.5.

Two reasonable steps composed into a relabelling. Ingest declined the
element: `ingest_worker` extracts pixels from `if "PixelData" in ds`, a
top-level keyword lookup that is False here, and `populate_attrs` skipped
the whole `7fe0` group without a word (#169). Export then reached past
the object graph: with no cached array and no loader,
`Instance.get_pixel_data()` falls back to `pydicom.dcmread(file_path)`,
and pydicom returns Float Pixel Data through `.pixel_array` as float32.
The write was unconditional -- `ds.PixelData = arr.tobytes()` -- and the
array's dtype was never consulted between the two.

The routing decision this pins: (7fe0,0008) and (7fe0,0009) are
**carried**, and by a mechanism that is not the sidecar. Nothing
extracts them at ingest -- `SidecarPixelLoader` derives dtype from
BitsAllocated and PixelRepresentation alone (`uint16 if bits > 8`), so
it has no channel to say "these bytes are float32", and giving it one
is #183. But the array is in hand at the export writeback anyway,
because `get_pixel_data()` re-read it from the source file, so the right
tag is a lookup rather than a guess. It is written under (7fe0,0008) or
(7fe0,0009) by `itemsize`, and (7fe0,0010) is guaranteed absent -- PS3.5
Section 8.2 makes them mutually exclusive.

Refusing to write anything was the first cut, and it is what #193
rejected: it swapped a silent corruption for a quiet nonconformance,
since Float Pixel Data is Type 1 in the Floating Point Image Pixel
Module (PS3.3 C.7.6.24). float16 is the one arm that still loses, and it
is not reachable from a DICOM source at all.

Why the tag and not the bytes is the assertion here: a dropped element
is recoverable from the source, and a relabelled one invites nobody to
go back. #146's premise is that the compliance trail tells the reader
what to distrust; the old output was internally coherent -- 4x4, 32 bits
allocated, 64 bytes of Pixel Data -- so nothing downstream errored and
there was nothing to distrust.
"""
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import LOSS_SCOPE_STANDARD
from isocenter.session import DicomSession

PARAMETRIC_MAP = "1.2.840.10008.5.1.4.1.1.30"


def _write_float_src(folder, tag=0x7FE00008, vr='OF', dtype=np.float32,
                     bits=32):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = PARAMETRIC_MAP
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = PARAMETRIC_MAP
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.add_new(tag, vr, (np.arange(16, dtype=dtype) + 0.5).tobytes())

    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)
    return ds.SOPInstanceUID


def _run(tmp_path, name="fpx", **kwargs):
    """Ingest, export uncompressed, return (exported dataset, DATA_LOSS rows)."""
    src = tmp_path / "src"
    src.mkdir()
    _write_float_src(str(src), **kwargs)
    out = tmp_path / "out"

    session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
    try:
        session.ingest(str(src))
        session.export(str(out), format="dicom", use_compression=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    written = [os.path.join(r, f)
               for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, written

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details, loss_scope FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()
    return pydicom.dcmread(written[0]), rows


def test_float_pixel_data_is_not_exported_under_the_pixel_data_tag(tmp_path):
    """The defect itself, asserted on the tag rather than the bytes.

    Identical bytes under the wrong tag is the whole of #170; an
    assertion on content would have passed throughout.
    """
    exported, _rows = _run(tmp_path)

    assert (0x7FE0, 0x0010) not in exported, (
        "float pixel data was written back as Pixel Data: "
        f"{exported[(0x7FE0, 0x0010)]!r}")


def test_float_pixel_data_leaves_under_its_own_tag(tmp_path):
    """And it leaves *with* the payload, which is the second half (#193).

    Refusing to write anything was the first cut of this fix. It traded
    a silent corruption for a quiet nonconformance: Float Pixel Data is
    Type 1 in the Floating Point Image Pixel Module (PS3.3 C.7.6.24), so
    a Parametric Map with no pixel element at all is invalid -- the same
    "declares something it does not carry" shape #160 had just finished
    removing from the waveform path.

    The array is in hand at the writeback, because `get_pixel_data()`
    re-read it from the source, so the dtype is known and the right tag
    is a lookup rather than a guess.
    """
    exported, _rows = _run(tmp_path)

    assert (0x7FE0, 0x0008) in exported
    assert np.array_equal(
        np.frombuffer(exported[(0x7FE0, 0x0008)].value, dtype=np.float32),
        np.arange(16, dtype=np.float32) + 0.5)


def test_bits_allocated_agrees_with_the_tag_it_sits_next_to(tmp_path):
    """32 for (7fe0,0008), and the tag is what decides it.

    The original defect was an internally *coherent* file -- 4x4, 32
    bits allocated, 64 bytes of Pixel Data -- which is why nothing
    downstream errored. Writing the right tag while leaving a
    descriptor that contradicts it would rebuild the same trap one
    element over.
    """
    exported, _rows = _run(tmp_path)

    assert exported.BitsAllocated == 32


def test_nothing_is_reported_lost_when_nothing_was_lost(tmp_path):
    """The compliance record has to agree with the file (#194).

    Section 3 of the report promises the elements it lists "were present
    in the source and are **not** in the exported data". The payload is
    in the exported data, so there is nothing to list -- at ingest or at
    export. A `DATA_LOSS` row here would be the same defect as the
    spurious Extended Offset Table rows, on a different tag.
    """
    _exported, rows = _run(tmp_path)

    assert rows == [], rows


def test_the_export_still_succeeds(tmp_path):
    """A refusal to relabel is not a refusal to export."""
    exported, _rows = _run(tmp_path)

    assert exported.SOPClassUID == PARAMETRIC_MAP
    assert exported.Rows == 4


@pytest.mark.parametrize("tag,vr,dtype,bits", [
    (0x7FE00008, 'OF', np.float32, 32),
    (0x7FE00009, 'OD', np.float64, 64),
])
def test_both_float_pixel_elements_survive_under_their_own_tag(
        tmp_path, tag, vr, dtype, bits):
    """(7fe0,0009) Double Float Pixel Data is the same shape one VR over.

    pydicom surfaces it through `.pixel_array` as float64, so it reached
    the same unconditional write and came out as uint64. `itemsize` is
    what picks the tag -- it is the property the two elements are
    defined by, 32- and 64-bit IEEE-754 -- so pinning both stops the
    rule reading as "float32 is special".
    """
    exported, rows = _run(tmp_path, name=f"f{bits}", tag=tag, vr=vr,
                          dtype=dtype, bits=bits)

    assert (0x7FE0, 0x0010) not in exported
    assert (tag >> 16, tag & 0xFFFF) in exported
    assert exported.BitsAllocated == bits
    assert np.array_equal(
        np.frombuffer(exported[(tag >> 16, tag & 0xFFFF)].value, dtype=dtype),
        np.arange(16, dtype=dtype) + 0.5)
    assert rows == [], rows


def test_a_float16_array_is_refused_and_reported(tmp_path):
    """The one width with no DICOM home, and the arm that still loses.

    (7fe0,0008) and (7fe0,0009) are 32- and 64-bit IEEE-754. There is no
    16-bit float element, so this array cannot be written under any tag
    and the loss is real -- filed through `ExportOutcome.losses`, the
    channel #126 built, which reaches the audit log and section 3 via
    `_report_export_losses`.

    Driven through `_export_instance_worker` directly rather than from a
    fixture file, because float16 is not reachable from a source: no
    DICOM element decodes to it. The only way in is a caller handing
    `set_pixel_data` a float16 array. It also pins the guard as a
    `kind` test with an `itemsize` branch rather than a width test --
    narrowing it to `itemsize >= 4 and kind == 'f'` left the rest of the
    suite green.

    `0008,0016` is deliberately *not* CT Image Storage: `IODValidator`
    holds rules for that one SOP Class and its Type 1/2 checks would
    fail this minimal instance before the write, hiding whatever the
    guard did. `0008,0060` is `SR` for the neighbouring reason -- see
    the test below for what an image modality gets instead.
    """
    from isocenter.entities import Instance
    from isocenter.io_handlers import ExportContext, _export_instance_worker

    inst = Instance("1.2.3.4.5")
    inst.attributes.update({
        "0008,0016": "1.2.840.10008.5.1.4.1.1.7",   # Secondary Capture
        "0008,0060": "SR",
        "0028,0010": 4, "0028,0011": 4,
        "0028,0100": 16, "0028,0101": 16, "0028,0102": 15,
        "0028,0002": 1, "0028,0004": "MONOCHROME2", "0028,0103": 0,
        "0020,0013": 1,
    })
    inst.set_pixel_data((np.arange(16, dtype=np.float16) + 0.5).reshape(4, 4))

    outcome = _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "f16.dcm"),
        patient_attributes={"0010,0020": "P1"},
        study_attributes={"0020,000d": "1.2.3"},
        series_attributes={"0020,000e": "1.2.4"},
        compression=None))

    assert any(scope == LOSS_SCOPE_STANDARD and "float16" in msg
               for scope, msg in outcome.losses), outcome.losses
    assert (0x7FE0, 0x0010) not in pydicom.dcmread(str(tmp_path / "f16.dcm"))


def test_an_unwritable_float_on_an_image_modality_fails_the_export(tmp_path):
    """The two refusals to write a pixel-less file have to agree (#193).

    `_export_instance_worker` already raises `Pixels missing for Image
    Modality` when the source file has gone and the instance claims to
    be an image. The first cut of the float guard set `arr = None`
    *after* that check had passed, producing exactly the file it exists
    to prevent -- and `OT`, the modality a Parametric Map carries, is in
    the set. One module-level `_IMAGE_MODALITIES`, consulted by both, is
    what makes them agree by construction; this pins that they do.

    Only the unwritable widths reach it. float32 and float64 are written
    under their own tag, so an image modality carrying either never
    takes this path at all.
    """
    from isocenter.entities import Instance
    from isocenter.io_handlers import ExportContext, _export_instance_worker

    inst = Instance("1.2.3.4.6")
    inst.attributes.update({
        "0008,0016": "1.2.840.10008.5.1.4.1.1.7",
        "0008,0060": "OT",
        "0028,0010": 4, "0028,0011": 4,
        "0028,0100": 16, "0028,0101": 16, "0028,0102": 15,
        "0028,0002": 1, "0028,0004": "MONOCHROME2", "0028,0103": 0,
        "0020,0013": 1,
    })
    inst.set_pixel_data((np.arange(16, dtype=np.float16) + 0.5).reshape(4, 4))

    outcome = _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "f16img.dcm"),
        patient_attributes={"0010,0020": "P1"},
        study_attributes={"0020,000d": "1.2.3"},
        series_attributes={"0020,000e": "1.2.4"},
        compression=None))

    assert outcome.ok is False
    assert isinstance(outcome.error, RuntimeError)
    assert "Pixels missing for Image Modality" in str(outcome.error)
    assert not os.path.exists(str(tmp_path / "f16img.dcm"))


# --- The three arms the reworked guard has that nothing pinned ---
#
# Each of these was found by mutating the guard and watching the suite
# stay green. They pin behaviour that is already correct; none of them
# is a bug report.


def _export_one(tmp_path, arr, attrs=None, zones=None, name="x.dcm"):
    """Drive `_export_instance_worker` on a hand-built instance.

    Direct rather than through a fixture file because two of the three
    cases below cannot be expressed as a source DICOM at all: a caller's
    (7fe0,0010) sitting in `attributes` beside a float array, and a
    `BitsAllocated` that disagrees with the array's width. Both arrive
    only from `set_pixel_data`/`set_attr`, which is the same arm the
    float16 case is reachable through.
    """
    from isocenter.entities import Instance
    from isocenter.io_handlers import ExportContext, _export_instance_worker

    rows, cols = arr.shape[-2], arr.shape[-1]
    inst = Instance("1.2.3.4.5")
    inst.attributes.update({
        "0008,0016": PARAMETRIC_MAP,
        "0008,0060": "OT",
        "0028,0010": rows, "0028,0011": cols,
        "0028,0100": 32, "0028,0101": 32, "0028,0102": 31,
        "0028,0002": 1, "0028,0004": "MONOCHROME2", "0028,0103": 0,
        "0020,0013": 1,
    })
    # `attrs` is applied AFTER `set_pixel_data`, deliberately.
    # `Instance.set_pixel_data` ends with
    # `self.set_attr("0028,0100", array.itemsize * 8)`, so anything
    # written before it is silently corrected to agree with the array --
    # which is exactly the disagreement two of these tests need to
    # create. Applying it before produced a test that passed with the
    # behaviour it names deleted.
    inst.set_pixel_data(arr)
    inst.attributes.update(attrs or {})

    out = str(tmp_path / name)
    outcome = _export_instance_worker(ExportContext(
        instance=inst,
        output_path=out,
        patient_attributes={"0010,0020": "P1"},
        study_attributes={"0020,000d": "1.2.3"},
        series_attributes={"0020,000e": "1.2.4"},
        compression=None,
        redaction_zones=list(zones or [])))
    assert outcome.ok, outcome.error
    return pydicom.dcmread(out), outcome


@pytest.mark.parametrize("dtype,tag_kw,bits", [
    (np.float32, "FloatPixelData", 32),
    (np.float64, "DoubleFloatPixelData", 64),
])
def test_a_redacted_float_image_exports_redacted(tmp_path, dtype, tag_kw,
                                                 bits):
    """The guard must stay *below* the redaction block, and only a test
    keeps it there.

    The float writeback and `RedactionService.apply_redaction_to_array`
    both consume `arr`, and the guard ends by setting `arr = None`. Put
    the guard above the redaction block and the zeroing never runs --
    `if arr is not None` is already False -- so the burned-in
    identifiers leave inside (7fe0,0008). That is where the guard sat in
    the first cut of this fix. Mutating the redaction block to
    `if arr is not None and arr.dtype.kind != 'f':`, which is exactly
    what the guard sitting above it does, left all 766 tests green while
    the marker value survived into the exported element.

    Two assertions, because one is not enough. The band must be gone --
    that is the leak. And the pixel outside it must be untouched: a
    guard that zeroed the whole array would satisfy the first assertion
    while destroying the image, and "no PHI" is not the only
    requirement.
    """
    marker = 999.0
    arr = np.zeros((8, 8), dtype=dtype)
    arr[0:3, :] = marker      # the burned-in band
    arr[5, 5] = 42.5          # outside the zone: must survive

    exported, _outcome = _export_one(
        tmp_path, arr, zones=[(0, 3, 0, 8)], name=f"red{bits}.dcm")

    back = np.frombuffer(getattr(exported, tag_kw), dtype=dtype).reshape(8, 8)
    assert not (back[0:3, :] == marker).any(), back[0:3, :]
    assert back[5, 5] == 42.5


def test_the_integer_tag_is_deleted_when_a_float_element_is_written(tmp_path):
    """PS3.5 Section 8.2: one and only one pixel element (#193).

    `_merge` writes whatever `attributes` holds, so an instance carrying
    a (7fe0,0010) of its own would leave with both elements -- a file
    pydicom itself refuses to decode: "One and only one of 'Pixel Data',
    'Float Pixel Data' or 'Double Float Pixel Data' may be present".
    The guard deletes it rather than merely not writing it, and deleting
    is the half a reader could mistake for redundant.

    Ingest cannot build this instance: `populate_attrs` skips the whole
    `7fe0` group, so "7fe0,0010" never enters `attributes`. A caller
    can, via `set_attr`, which is why the deletion is not dead code.
    """
    exported, _outcome = _export_one(
        tmp_path,
        (np.arange(16, dtype=np.float32) + 0.5).reshape(4, 4),
        attrs={"7fe0,0010": np.arange(16, dtype=np.uint16).tobytes()},
        name="both.dcm")

    assert (0x7FE0, 0x0008) in exported
    assert (0x7FE0, 0x0010) not in exported


@pytest.mark.parametrize("dtype,stated,expected_tag,expected_bits", [
    (np.float64, 16, (0x7FE0, 0x0009), 64),
    (np.float32, 64, (0x7FE0, 0x0008), 32),
])
def test_bits_allocated_is_decided_by_the_array_not_the_source(
        tmp_path, dtype, stated, expected_tag, expected_bits):
    """`test_bits_allocated_agrees_with_the_tag_it_sits_next_to` cannot
    see this, and that is why this exists.

    That test reads `BitsAllocated == 32` off a fixture whose source
    already said 32, so it passes with `ds.BitsAllocated = 32` deleted
    from the guard -- measured. It asserts the source value survived,
    not that the tag decided it.

    Here the stated width disagrees with the array, which is the only
    configuration in which the assignment does anything: 16 beside a
    float64 array, and 64 beside a float32 one. A descriptor left
    contradicting its element is the internally-coherent-but-wrong file
    #170 is about, one element over.
    """
    exported, _outcome = _export_one(
        tmp_path,
        (np.arange(16, dtype=dtype) + 0.5).reshape(4, 4),
        attrs={"0028,0100": stated, "0028,0101": stated,
               "0028,0102": stated - 1},
        name=f"bits{stated}_{np.dtype(dtype).name}.dcm")

    assert expected_tag in exported
    assert exported.BitsAllocated == expected_bits


# --- #216: the float path's geometry descriptors ---
#
# `_export_instance_worker` resolved one `PixelGeometry` and then used it
# on the integer branch only. The float branch wrote no descriptors at
# all, and #215's refusal to write a guessed geometry sat *inside* the
# integer branch -- below a float branch that ends with `arr = None`, so
# a float array reached neither.
#
# Two shapes, and the filed one is the milder. Descriptors absent is
# undecodable and loud; descriptors present and stale is decodable and
# describes a different image. Every test below that names a declared
# Rows/Columns is pinning the second shape, which a fix that only hoists
# the refusal leaves standing.


def _export_raw(tmp_path, arr, attrs, name):
    """Drive the worker on an instance whose attributes are *exactly* `attrs`.

    `_export_one` above cannot express these cases. It seeds Rows,
    Columns, SamplesPerPixel and PhotometricInterpretation from the
    array's own shape and then calls `set_pixel_data`, which writes
    `0028,0002`, `0028,0004`, `0028,0010` and `0028,0011` back into
    `attributes` -- so every fixture built through it declares a geometry
    that already agrees with its array, which is the one configuration in
    which none of the clauses below does anything. It also asserts
    `outcome.ok`, and two of these expect a refusal.

    `inst.pixel_array = arr` is a plain assignment and writes nothing.
    """
    from isocenter.entities import Instance
    from isocenter.io_handlers import ExportContext, _export_instance_worker

    inst = Instance("1.2.3.4.5")
    inst.attributes.update(attrs)
    inst.pixel_array = arr

    out = str(tmp_path / name)
    outcome = _export_instance_worker(ExportContext(
        instance=inst,
        output_path=out,
        patient_attributes={"0010,0020": "P1"},
        study_attributes={"0020,000d": "1.2.3"},
        series_attributes={"0020,000e": "1.2.4"},
        compression=None))
    return inst, outcome, out


#: Enough to build a dataset and no descriptor of any kind. Every
#: geometry element is left out deliberately: the point of the fixtures
#: below is what `attributes` does and does not say.
_BARE = {"0008,0016": PARAMETRIC_MAP, "0008,0060": "OT", "0020,0013": 1}


def test_a_guessed_geometry_is_refused_on_the_float_path(tmp_path):
    """The issue as filed: a (5,6,3) float32 with nothing declared.

    Rank 3 with no `SamplesPerPixel`, no `NumberOfFrames` and no
    `Rows`/`Columns` is equally a 5-frame 6x3 grayscale image and a 5x6
    image with 3 samples per pixel. #215 decided that a guess must not
    become a file, and put the refusal in the integer branch -- which the
    float branch, sitting above it and ending in `arr = None`, never
    reaches. Measured on 258331c: this exact graph exported a file with
    `Rows`, `Columns` and `SamplesPerPixel` all absent, while the
    byte-for-byte identical uint8 graph was correctly refused. All three
    are Type 1 in the Floating Point Image Pixel Module (PS3.3 C.7.6.24).

    Fixture trap, and it is the mirror of the pixel-geometry spec's. The
    array must be assigned directly, never through `set_pixel_data`: the
    setter writes `SamplesPerPixel = 3` back into `attributes`, which
    makes the resolution DECLARED and leaves this test passing with the
    hoist deleted. The assertion below the call is what fails loudly if
    a future edit reintroduces the setter.
    """
    inst, outcome, out = _export_raw(
        tmp_path, np.zeros((5, 6, 3), dtype=np.float32), _BARE, "guess.dcm")

    assert "0028,0002" not in inst.attributes, (
        "the fixture declared SamplesPerPixel, so the geometry is DECLARED "
        "and this test cannot see the guess it exists to catch")

    assert outcome.ok is False
    assert not os.path.exists(out)
    msg = str(outcome.error)
    assert "(5, 6, 3)" in msg
    assert "SamplesPerPixel" in msg


def test_the_float_path_writes_the_geometry_it_resolved_not_the_one_declared(
        tmp_path):
    """The shape the issue does not measure, and the worse one.

    A `(4,4)` float32 array beside a declared `Rows = Columns = 10`
    exported, on 258331c, a file announcing 100 pixels and carrying 16 --
    `_merge` writes whatever `attributes` holds and only the integer
    branch corrected it. Not undecodable: a file describing a different
    image, which is the relabelling this project's changelog argues is
    worse than a drop because nothing invites the reader to go back.

    A fixture whose declared descriptors *agree* with the array passes
    with this clause deleted, because `_merge` wrote the right values by
    luck. That is why they disagree here, and why the byte-length
    identity is asserted rather than the numbers alone.

    `SamplesPerPixel` is declared **3**, not 1, for the same reason.
    Declaring the value the resolver was going to produce made that
    assertion vacuous: `_merge` had already put it on the dataset, so
    deleting `ds.SamplesPerPixel = geom.samples` from
    `_write_pixel_geometry` left every one of the #216 tests green and
    only the integer-path tests in `test_pixel_geometry_pipeline.py`
    noticed -- measured. A rank-2 array has one reading whatever the
    attributes claim, so 3 is stale and 1 is resolved.
    """
    arr = (np.arange(16, dtype=np.float32) + 0.5).reshape(4, 4)
    _inst, outcome, out = _export_raw(
        tmp_path, arr,
        dict(_BARE, **{"0028,0010": 10, "0028,0011": 10, "0028,0002": 3}),
        "stale.dcm")
    assert outcome.ok, outcome.error

    ds = pydicom.dcmread(out)
    assert ds.Rows == 4
    assert ds.Columns == 4
    assert ds.SamplesPerPixel == 1
    assert ds.Rows * ds.Columns * ds.SamplesPerPixel * 4 == \
        len(ds.FloatPixelData)


def test_a_multiframe_float_carries_the_frame_count_it_resolved(tmp_path):
    """The same relabelling one rank up, and the frame count with it.

    `(2,4,8)` float32 with a declared `NumberOfFrames` resolves to two
    4x8 frames -- the declaration settles which axis is which -- but on
    258331c the file left with the declared `Rows = Columns = 99` beside
    256 bytes. Asserting `NumberOfFrames` too is what stops a helper that
    writes Rows and Columns and quietly drops the frame count.

    The declared frame count is **5**, and it has to be a number the
    resolver will not produce. `PixelGeometry.frames` is never the
    declared value -- it is the array's first axis on the frames-major
    arm -- so declaring 2 asserted the number `_merge` had already
    written: deleting the `NumberOfFrames` clause from
    `_write_pixel_geometry` left all six #216 tests green, and only
    `test_export_corrects_a_stale_number_of_frames` on the integer path
    caught it. Measured, not reasoned.
    """
    arr = np.zeros((2, 4, 8), dtype=np.float32)
    _inst, outcome, out = _export_raw(
        tmp_path, arr,
        dict(_BARE, **{"0028,0002": 1, "0028,0008": 5,
                       "0028,0010": 99, "0028,0011": 99}),
        "mf.dcm")
    assert outcome.ok, outcome.error

    ds = pydicom.dcmread(out)
    assert ds.Rows == 4
    assert ds.Columns == 8
    assert ds.SamplesPerPixel == 1
    assert int(ds.NumberOfFrames) == 2
    assert (ds.Rows * ds.Columns * ds.SamplesPerPixel
            * int(ds.NumberOfFrames) * 4) == len(ds.FloatPixelData)


def test_photometric_interpretation_is_written_on_the_float_path(tmp_path):
    """Type 1, Enumerated Value MONOCHROME2 -- and still not forced.

    PS3.3 C.7.6.24 and C.7.6.25 both make Photometric Interpretation
    Type 1 and enumerate MONOCHROME2. On 258331c the element was simply
    absent from every exported float instance that had not declared one.

    The second arm is the guard against the obvious over-correction. "The
    module enumerates MONOCHROME2, so write MONOCHROME2" would put a
    second, disagreeing answer next to
    `resolve_photometric_interpretation`, whose whole `None` arm exists
    so a declared MONOCHROME1 (or YBR_ICT, on the integer path) survives
    a round trip.

    Both arms assign `pixel_array` directly: `set_pixel_data` writes
    `0028,0004` into `attributes`, so a fixture built through it declares
    a Photometric Interpretation whatever the caller intended and the
    first arm goes vacuous.
    """
    arr = (np.arange(16, dtype=np.float32) + 0.5).reshape(4, 4)

    inst, outcome, out = _export_raw(
        tmp_path, arr,
        dict(_BARE, **{"0028,0010": 4, "0028,0011": 4}), "pi_absent.dcm")
    assert "0028,0004" not in inst.attributes, (
        "the fixture declared a Photometric Interpretation, so this arm "
        "asserts that _merge copied it rather than that the float path "
        "supplied it")
    assert outcome.ok, outcome.error
    assert pydicom.dcmread(out).PhotometricInterpretation == "MONOCHROME2"

    _inst2, outcome2, out2 = _export_raw(
        tmp_path, arr,
        dict(_BARE, **{"0028,0010": 4, "0028,0011": 4,
                       "0028,0004": "MONOCHROME1"}), "pi_mono1.dcm")
    assert outcome2.ok, outcome2.error
    assert pydicom.dcmread(out2).PhotometricInterpretation == "MONOCHROME1"


def test_the_float_elements_are_deleted_when_pixel_data_is_written(tmp_path):
    """PS3.5 Section 8.2 in the second of its three directions (#216).

    `test_the_integer_tag_is_deleted_when_a_float_element_is_written`
    above pins the float branch deleting (7fe0,0010). The integer branch
    never deleted its counterparts, so on 258331c a uint8 instance
    carrying a "7fe0,0008" of its own in `attributes` exported a file
    with *both* pixel elements -- the same file pydicom refuses to
    decode, arrived at from the other side.

    Not writing the element was never the same as removing it: `_merge`
    writes whatever `attributes` holds. Both keywords are deleted
    because both are reachable the same way, through `set_attr` on a tag
    `populate_attrs` skips at ingest.
    """
    exported, _outcome = _export_one(
        tmp_path,
        np.zeros((4, 4), dtype=np.uint8),
        attrs={"7fe0,0008": b"\x00" * 64,
               "7fe0,0009": b"\x00" * 128},
        name="both_ways.dcm")

    assert (0x7FE0, 0x0010) in exported
    assert (0x7FE0, 0x0008) not in exported
    assert (0x7FE0, 0x0009) not in exported


@pytest.mark.parametrize("dtype,written,stale,stale_len", [
    (np.float32, (0x7FE0, 0x0008), "7fe0,0009", 128),
    (np.float64, (0x7FE0, 0x0009), "7fe0,0008", 64),
])
def test_the_other_float_element_is_deleted_too(
        tmp_path, dtype, written, stale, stale_len):
    """The third direction, and the one both halves of the pair missed.

    PS3.5 Section 8.2: "It is not permitted to have more than one of
    Pixel Data Provider URL (0028,7FE0), Pixel Data (7FE0,0010), Float
    Pixel Data (7FE0,0008) or Double Float Pixel Data (7FE0,0009) in the
    top level Data Set." Three of those four are reachable here, which
    makes three directions, not two: the float branch deleted
    (7fe0,0010) and the integer branch was taught to delete both float
    keywords, and neither of them stops a float32 array leaving beside a
    (7fe0,0009) that `_merge` copied out of `attributes`.

    Measured on `258331c` *and* on the first cut of the #216 fix: both
    exported a file carrying (7fe0,0008) and (7fe0,0009) together, on
    which `dcmread(...).pixel_array` raises the same `AttributeError`
    -- "One and only one of 'Pixel Data', 'Float Pixel Data' or 'Double
    Float Pixel Data' may be present" -- that the other two directions
    are pinned by.

    The array is assigned through `_export_raw`, not `_export_one`:
    `set_pixel_data` writes nothing to group 7fe0, but `_export_one`
    asserts `outcome.ok` and seeds descriptors this case does not want
    to depend on.
    """
    arr = (np.arange(16, dtype=dtype) + 0.5).reshape(4, 4)
    _inst, outcome, out = _export_raw(
        tmp_path, arr,
        dict(_BARE, **{"0028,0002": 1, "0028,0004": "MONOCHROME2",
                       "0028,0010": 4, "0028,0011": 4,
                       stale: b"\x00" * stale_len}),
        f"other_{np.dtype(dtype).name}.dcm")
    assert outcome.ok, outcome.error

    ds = pydicom.dcmread(out)
    assert written in ds
    assert (int(stale.split(",")[0], 16),
            int(stale.split(",")[1], 16)) not in ds
    # The point of the deletion, rather than a restatement of it.
    assert np.array_equal(ds.pixel_array, arr)


def test_a_guessed_float_geometry_stops_write_tree_too(tmp_path):
    """The serializer takes the same refusal, and only a test says so.

    `DicomExporter.write_tree` applies none of the pipeline -- no PHI
    scan, no subset filter, no redaction -- but it routes through the
    same `_export_instance_worker`, so the guess has to stop there too.
    It is the path the `scripts/` fixture generators use and the one
    `session.export()` cannot exercise, and it surfaces the refusal
    through its own "Export incomplete" `RuntimeError` wrapper rather
    than as an `ExportOutcome`.

    The message is asserted, not just the type. That wrapper raises a
    bare `RuntimeError` for *any* per-instance failure, and this fixture
    declares no Rows, no Columns, no BitsAllocated and no
    PixelRepresentation -- so a `pytest.raises(RuntimeError)` alone would
    pass just as well if the write had failed for an unrelated reason,
    and this test would be pinning "something went wrong" rather than
    which refusal caught it. The wrapper interpolates the first failure's
    message, so the geometry refusal's own words survive into it.
    """
    from isocenter.entities import Equipment, Instance, Patient, Series, Study
    from isocenter.io_handlers import DicomExporter

    inst = Instance("I_FLOAT", "1.2.826.0.1.3680043.8.498.216001", 1)
    inst.attributes.update(_BARE)
    inst.pixel_array = np.zeros((5, 6, 3), dtype=np.float32)
    assert "0028,0002" not in inst.attributes

    patient = Patient("P_FLOAT", "Float Test")
    study = Study("S_FLOAT", "20230101")
    series = Series("SE_FLOAT", "OT", 1)
    series.equipment = Equipment("Man", "Mod", "SN-FLOAT")
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)

    out = tmp_path / "tree"
    with pytest.raises(RuntimeError) as excinfo:
        DicomExporter.write_tree(patient, str(out), show_progress=False)

    msg = str(excinfo.value)
    assert "(5, 6, 3)" in msg, msg
    assert "SamplesPerPixel" in msg, msg

    written = [f for _r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert written == [], written

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
A.1 makes them mutually exclusive.

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

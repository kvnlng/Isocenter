"""Signed pixel data could not be exported at all (#404).

`session.export(folder)` compresses by **default** --
`_export_dicom(..., use_compression=True)`, an option frozen at
`docs/api/stability.md` -- and the JPEG 2000 encoder it used, Pillow's,
accepted exactly two dtypes. Measured, one array per dtype:

| dtype | Pillow J2K |
| --- | --- |
| `uint8`, `uint16` | exact |
| `int8`, `int16`, `uint32`, `int32`, `float32`, `bool` | `OSError: broken data stream when writing image file` |
| `uint64`, `int64` | `TypeError: Cannot handle this data type` (in `fromarray`) |

So **the axis is signedness, not width**, and `int16` with
`PixelRepresentation 1` is CT and MR. A default export of a CT study wrote
nothing: `ExportError ... wrote 0 of 1 planned instances; 1 failed and
nothing reached disk`, with an `ERROR` row carrying Pillow's sentence and
naming neither the dtype, nor `BitsAllocated`, nor `PixelRepresentation`,
nor the encoder, nor a remedy. The same data with `use_compression=False`
was bit-exact from the same store, which is what settles that the sidecar
and the loader were never implicated.

**Why the suite was green.** `tests/conftest.py`'s shared
`dummy_pixel_array_2d` was `np.zeros((512, 512), dtype=np.uint16)` -- one
of the exactly two dtypes that worked. It is `int16` as of this change.

The encoder is now `imagecodecs.jpeg2k_encode(arr, level=0,
codecformat="J2K")`. Not a new dependency: `imagecodecs` is already in
`install_requires` and already drives the **decode** side in
`imagecodecs_handler.py`, so the project already trusts it with pixel
fidelity in the other direction.

**What is refused is refused by name, and that refusal is the safety of
this fix rather than a rough edge.** Two separate cells would otherwise
have turned today's loud failure into `wrote 1 of 1` beside a file
**this library cannot read back** -- this milestone's own defect,
introduced by its own fix. The two cells fail that standard for
different reasons, and the 32-bit one is the worse of the two: it is not
merely unreadable here, it is **written wrong and read back wrong**.

- **32- and 64-bit.** The codec does not reject 32-bit: it encodes,
  exactly to 25 bits and wrong above that, and the DICOM file built from
  a 32-bit codestream cannot be decoded by any pydicom plugin.
- **16-bit multi-sample**, which this guard refused until #416 and now
  writes. The codestream was always *exact*; what this project lacked
  was a decoder at the door. Pillow is the only JPEG 2000 plugin pydicom
  has here and it reports `Pillow cannot decode 16-bit multi-sample data
  correctly`, so `session.ingest()` on such an export returned
  `ingested=0` -- the library could not re-ingest its own output, and
  the cell was refused on the standard the 32-bit cell is judged by:
  *what this library can read back*. #416 gave ingest an `imagecodecs`
  fallback, which reads these frames bit-exactly, so the cell now meets
  that standard; P8x pins the round trip that makes it true.

So the guard is a **matrix** over `(itemsize, samples > 1)`, not a list
of widths, and it is **positive** -- encode only what is measured to
survive the round trip -- and raised before any encode and before any
`ds` mutation. `int8` multi-sample, which Pillow also refused, is exact
and is now supported: the swap widens the matrix as well as fixing it.

Every value assertion here is against a **literal** and every dtype
assertion against an **absolute** dtype. `got.dtype == src.dtype` where
`src` is built in the same test is true whenever both sides are wrong the
same way.
"""
import glob
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, JPEG2000Lossless, generate_uid

from isocenter.session import DicomSession

#: A 4x4 corner of Hounsfield-shaped values, as a literal. Negative on
#: purpose: an encode that loses the sign is a different number, not
#: merely a different dtype.
CT_ROWS = [[-1024, -512, -100, 0],
           [100, 512, 1023, 2048],
           [-2048, -3000, 3000, 32767],
           [-32768, 1, -1, 12345]]

#: The same shape in the unsigned domain, for the dtypes that have no
#: negative half.
FLAT_ROWS = [[0, 1, 2, 3],
             [4, 5, 6, 7],
             [8, 9, 10, 11],
             [12, 13, 14, 200]]

BOOL_ROWS = [[True, False, True, False],
             [False, True, False, True],
             [True, True, False, False],
             [False, False, True, True]]


def _rows_for(dtype_name):
    """A literal that fits `dtype_name`, and exercises its whole domain."""
    if dtype_name == "bool":
        return BOOL_ROWS
    info = np.iinfo(dtype_name)
    if info.min < 0:
        return [[max(info.min, min(info.max, v)) for v in row]
                for row in CT_ROWS]
    return [[min(info.max, v) for v in row] for row in FLAT_ROWS]


def _write_src(folder, arr, pixel_representation, samples=1, frames=1,
               photometric="MONOCHROME2", name="one.dcm"):
    """One instance carrying `arr` as (7fe0,0010), declared honestly."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT404", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate = "20230101"

    if frames > 1:
        ds.NumberOfFrames = frames
        ds.Rows, ds.Columns = arr.shape[1], arr.shape[2]
    else:
        ds.Rows, ds.Columns = arr.shape[0], arr.shape[1]
    ds.SamplesPerPixel = samples
    ds.PhotometricInterpretation = photometric
    if samples > 1:
        ds.PlanarConfiguration = 0
    ds.BitsAllocated = ds.BitsStored = arr.dtype.itemsize * 8
    ds.HighBit = ds.BitsStored - 1
    ds.PixelRepresentation = pixel_representation
    ds.PixelData = arr.tobytes()

    path = os.path.join(folder, name)
    ds.save_as(path, enforce_file_format=True)
    return ds.SOPInstanceUID


def _written(out):
    return sorted(glob.glob(os.path.join(str(out), "**", "*.dcm"),
                            recursive=True))


def _audit_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log").fetchall()


def _export(tmp_path, arr, pixel_representation, prefix, samples=1, frames=1,
            photometric="MONOCHROME2", **export_kwargs):
    """Ingest, save, export -- with **no** compression keyword by default.

    The absent keyword is the point: `use_compression=True` is the default
    and is what a caller of `session.export(folder)` gets.

    A `bool` array takes the one detour, and the detour has two parts
    that both had to be measured rather than assumed.

    No DICOM file can hold a boolean frame -- (0028,0100) is a bit count
    and (0028,0103) a signedness flag, and neither of them spells
    "boolean" -- so an instance holds a bool array only when a caller has
    handed it one through `set_pixel_data`. Ingesting a file and calling
    the result `bool` is a second `uint8` arm wearing the `bool` label.

    And the source file must not carry the *same bytes* as the mask.
    `np.array(BOOL_ROWS, bool).tobytes()` equals
    `np.array(BOOL_ROWS, uint8).tobytes()`, and the save deduplicates
    frames on their SHA-256: identical bytes mean no frame is written,
    so no loader is rebuilt, so the instance keeps the ingest-time loader
    and `export()`'s `release_memory()` reloads the frame as `uint8`.
    Measured: with a same-bytes source, deleting `_compress_j2k`'s bool
    view left every bool test here green while the array they exported
    was never bool. `FLAT_ROWS` is the source instead, and the assertion
    below is against the reloaded frame rather than the resident one --
    the reload is what the export worker gets.
    """
    src = tmp_path / f"{prefix}_src"
    src.mkdir(exist_ok=True)
    out = tmp_path / f"{prefix}_out"
    db = str(tmp_path / f"{prefix}.db")

    in_memory = arr if arr.dtype.kind == 'b' else None
    _write_src(str(src),
               np.array(FLAT_ROWS, dtype=np.uint8) if in_memory is not None
               else arr,
               pixel_representation, samples, frames, photometric)

    error = None
    summary = None
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        if in_memory is not None:
            instance = session.store.patients[0].studies[0].series[0].instances[0]
            instance.set_pixel_data(in_memory)
            session.save(sync=True)
            instance.unload_pixel_data()
            assert instance.get_pixel_data().dtype == np.dtype(bool), (
                "fixture never entered the arm under test: the frame the "
                "export worker reloads is not bool")
        session.save()
        try:
            summary = session.export(str(out), format="dicom",
                                     show_progress=False, **export_kwargs)
        except Exception as exc:      # noqa: BLE001 -- the arm under test
            error = exc
        session.store_backend.flush_audit_queue()
    finally:
        session.close()

    return summary, error, _written(out), _audit_rows(db), str(out)


# ---------------------------------------------------------------------------
# P1/P2 -- the milestone tests
# ---------------------------------------------------------------------------

def test_a_signed_16_bit_study_exports_with_the_default_options(tmp_path):
    """A CT study, exported the way the documented quickstart exports.

    Three things asserted together, because before this all three were
    false: a file exists, its pixels are bit-exact, and the session's own
    accounting says so. `flush_audit_queue()` first -- the audit writer is
    a background thread, so an unflushed `SELECT` returns `[]` and "no
    ERROR row" cannot be told from "the row has not landed yet".

    *Red when:* the encoder is reverted to `Image.fromarray`.
    """
    arr = np.array(CT_ROWS, dtype="int16")
    summary, error, files, rows, out = _export(tmp_path, arr, 1, "p1")

    assert error is None, f"the default export raised: {error}"
    assert len(files) == 1
    assert summary.failures == []
    assert len(summary.written_uids) == 1

    ds = pydicom.dcmread(files[0])
    assert ds.pixel_array.dtype == np.dtype("int16")
    assert ds.pixel_array.tolist() == CT_ROWS

    exports = [d for action, _uid, d in rows if action == 'EXPORT']
    assert exports, "no EXPORT row in the audit log (was the queue flushed?)"
    assert any("wrote 1 of 1 planned instances" in d for d in exports), exports
    errors = [d for action, _uid, d in rows if action == 'ERROR']
    assert errors == [], f"an ERROR row was filed for a successful export: {errors}"


def test_the_written_file_declares_the_signedness_it_holds(tmp_path):
    """*Red when:* the descriptors are written from anything but the array."""
    arr = np.array(CT_ROWS, dtype="int16")
    _summary, error, files, _rows, _out = _export(tmp_path, arr, 1, "p2")

    assert error is None
    ds = pydicom.dcmread(files[0])
    assert ds.PixelRepresentation == 1
    assert ds.BitsAllocated == 16
    assert ds.file_meta.TransferSyntaxUID == JPEG2000Lossless


# ---------------------------------------------------------------------------
# P3 -- the supported matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dtype_name,pixel_representation", [
    ("uint8", 0),
    ("int8", 1),
    ("uint16", 0),
    ("int16", 1),
    ("bool", 0),
])
def test_every_supported_dtype_round_trips_through_the_compressed_path(
        tmp_path, dtype_name, pixel_representation):
    """The §11.13.3 table, as a test.

    *Red when:* the `bool` normalization is dropped (that arm alone -- the
    codec raises `ValueError` for kind `b`), or the encoder is reverted
    (all arms but `uint8` and `uint16`).
    """
    rows = _rows_for(dtype_name)
    arr = np.array(rows, dtype=dtype_name)
    _summary, error, files, _rows, _out = _export(
        tmp_path, arr, pixel_representation, f"p3_{dtype_name}")

    assert error is None, f"{dtype_name} failed the default export: {error}"
    assert len(files) == 1

    ds = pydicom.dcmread(files[0])
    got = ds.pixel_array
    if dtype_name == "bool":
        # A bool frame is stored, declared and written as 8-bit; the file
        # can only say `uint8`, and the uncompressed path writes the same.
        assert got.dtype == np.dtype("uint8")
        assert got.tolist() == [[int(v) for v in row] for row in rows]
    else:
        assert got.dtype == np.dtype(dtype_name)
        assert got.tolist() == rows


def test_the_bool_arm_writes_the_bytes_the_uncompressed_path_writes(tmp_path):
    """The two export paths must not disagree for one dtype.

    `bool` is the only dtype the compressed path converts, so it is the
    only one where the compressed and the uncompressed writer could
    diverge. `astype(np.uint8)` is what the uncompressed path already
    emits, since `set_pixel_data` declares such a frame
    `BitsAllocated 8, PixelRepresentation 0` and the worker writes
    `arr.tobytes()`.
    """
    arr = np.array(BOOL_ROWS, dtype=bool)
    _s1, e1, compressed, _r1, _o1 = _export(tmp_path, arr, 0, "p3b_c")
    _s2, e2, plain, _r2, _o2 = _export(tmp_path, arr, 0, "p3b_u",
                                       use_compression=False)

    assert e1 is None and e2 is None
    one = pydicom.dcmread(compressed[0])
    two = pydicom.dcmread(plain[0])

    assert one.pixel_array.tolist() == two.pixel_array.tolist()
    assert one.BitsAllocated == two.BitsAllocated == 8
    assert one.PixelRepresentation == two.PixelRepresentation == 0


# ---------------------------------------------------------------------------
# P4/P5 -- the widths that stay unsupported, refused by name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dtype_name,pixel_representation", [
    ("uint32", 0),
    ("int32", 1),
])
def test_a_32_bit_frame_is_refused_by_name_rather_than_written_wrong(
        tmp_path, dtype_name, pixel_representation):
    """The test that stops this fix from creating a new silence.

    `imagecodecs` does **not** refuse 32-bit: it encodes, exactly to 25
    bits and wrong above that, and the DICOM file built from a 32-bit
    codestream raises `RuntimeError: Unable to decode as exceptions were
    raised by all available plugins` on read here. So without the guard
    the encode succeeds, a file is written, and `wrote 1 of 1` appears
    beside a file that was **written wrong and would be read back wrong**
    -- the pixels are lost at the encoder, so a decoder that does open
    the codestream (`imagecodecs.jpeg2k_decode` does) returns the right
    dtype and shape with silently wrong values: `uint32` full-range wrote
    2147483647 and read back 67108863; `int32` full-range read back -1.
    That is worse than unreadable, not a milder form of it.

    *Red when:* the frame guard is removed. Measured, it is red on
    `assert error is not None` -- the export **succeeds** -- so the
    no-file-on-disk assertion below is never reached under that mutation.
    It is here anyway and it is independently sufficient: it is what
    distinguishes "the export refused" from "the export raised after
    leaving a file behind", and a future change that turns the refusal
    into a partial write would be red on it alone.
    """
    arr = np.array(_rows_for(dtype_name), dtype=dtype_name)
    _summary, error, files, rows, _out = _export(
        tmp_path, arr, pixel_representation, f"p4_{dtype_name}")

    assert error is not None, (
        "a 32-bit frame was accepted by the compressed path; the codec "
        "encodes it silently wrong above 25 bits")
    assert files == [], "a file reached disk for a refused width"

    message = " ".join(d for _a, _u, d in rows if d)
    assert dtype_name in message, f"the refusal does not name the dtype: {message}"
    assert "BitsAllocated 32" in message, message
    assert f"PixelRepresentation {pixel_representation}" in message, message
    assert "imagecodecs" in message, (
        f"the refusal does not name the encoder: {message}")
    assert "use_compression=False" in message, (
        f"the refusal does not give the remedy: {message}")
    assert "broken data stream" not in message, (
        "the refusal is still the codec's own sentence rather than ours")


@pytest.mark.parametrize("dtype_name,pixel_representation", [
    ("uint64", 0),
    ("int64", 1),
])
def test_a_64_bit_frame_is_refused_by_name(
        tmp_path, dtype_name, pixel_representation):
    """The codec's own error must not be what the user sees.

    `imagecodecs` raises here on its own, but with a different sentence on
    different releases -- `ValueError: item size not supported by codec` on
    2026.8.16, `Jpeg2kError: opj_encode or opj_write_tile failed` on
    2024.6.1 -- so a message the user can act on cannot come from the
    codec. *Red when:* the guard is narrowed to 32-bit only.
    """
    arr = np.array(_rows_for(dtype_name), dtype=dtype_name)
    _summary, error, files, rows, _out = _export(
        tmp_path, arr, pixel_representation, f"p5_{dtype_name}")

    assert error is not None
    assert files == []

    message = " ".join(d for _a, _u, d in rows if d)
    assert dtype_name in message, message
    assert "BitsAllocated 64" in message, message
    assert "use_compression=False" in message, message
    assert "opj_encode" not in message and "item size not supported" not in message, (
        f"the codec's own sentence reached the audit log: {message}")


# ---------------------------------------------------------------------------
# P6 -- float pixel data is a different element and a different path
# ---------------------------------------------------------------------------

def test_float_pixel_data_still_exports_uncompressed_under_the_default(tmp_path):
    """Floats never reach the encoder, and must keep not reaching it.

    The export's float arm writes (7fe0,0008)/(7fe0,0009) and deletes
    (7fe0,0010) per PS3.5 Section 8.2, so `_compress_j2k` returns at its
    `hasattr(ds, "PixelData")` guard and the file is written uncompressed.
    *Red when:* that early return is removed, or the deleted
    reconstruct-from-bytes arm is restored.
    """
    src = tmp_path / "p6_src"
    src.mkdir()
    out = tmp_path / "p6_out"

    session = DicomSession(persistence_file=str(tmp_path / "p6.db"))
    try:
        _write_src(str(src), np.array(FLAT_ROWS, dtype="uint16"), 0)
        session.ingest(str(src))
        for patient in session.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        instance.set_pixel_data(
                            np.array(FLAT_ROWS, dtype="float32"))
        summary = session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    assert summary.failures == []
    files = _written(out)
    assert len(files) == 1

    ds = pydicom.dcmread(files[0])
    assert "PixelData" not in ds, "the float arm wrote (7fe0,0010)"
    assert "FloatPixelData" in ds or "DoubleFloatPixelData" in ds
    assert ds.file_meta.TransferSyntaxUID != JPEG2000Lossless


# ---------------------------------------------------------------------------
# P7/P8 -- shapes
# ---------------------------------------------------------------------------

def test_a_multi_frame_signed_stack_survives(tmp_path):
    """*Red when:* the per-frame loop is replaced by one whole-array encode."""
    # Derived arithmetic stays inside int16 on purpose: CT_ROWS holds
    # both extremes, so `v + 1` on 32767 would overflow the fixture rather
    # than the code under test.
    stack = np.array([CT_ROWS,
                      [[v // 2 for v in row] for row in CT_ROWS],
                      [[-(v // 4) for v in row] for row in CT_ROWS]],
                     dtype="int16")
    _summary, error, files, _rows, _out = _export(
        tmp_path, stack, 1, "p7", frames=3)

    assert error is None, f"a 3-frame int16 stack failed: {error}"
    ds = pydicom.dcmread(files[0])
    got = ds.pixel_array
    assert got.dtype == np.dtype("int16")
    assert got.shape == (3, 4, 4)
    assert got.tolist() == stack.tolist()


#: One RGB frame, as a literal. Every channel differs from its
#: neighbours, so a channel swap or an axis reorder is a different list.
RGB_ROWS = [[[10, 20, 30], [40, 50, 60], [70, 80, 90], [100, 110, 120]],
            [[1, 2, 3], [4, 5, 6], [7, 8, 9], [11, 12, 13]],
            [[200, 201, 202], [203, 204, 205], [206, 207, 208], [209, 210, 211]],
            [[255, 0, 0], [0, 255, 0], [0, 0, 255], [127, 127, 127]]]


@pytest.mark.parametrize("frames,dtype_name,pixrep", [
    (1, "uint8", 0),
    (2, "uint8", 0),
    # Signed colour, which Pillow refused at `fromarray` exactly as it
    # refused signed greyscale. It is bit-exact through `imagecodecs`, so
    # the swap widens what compresses as well as fixing what was broken;
    # this arm is the evidence for that half of the claim.
    (1, "int8", 1),
])
def test_an_rgb_frame_still_round_trips(tmp_path, frames, dtype_name, pixrep):
    """The control against a colour regression from the encoder swap.

    *Red when:* the encoder is handed the frame with its axes reordered.
    """
    one = ([[[v - 128 for v in px] for px in row] for row in RGB_ROWS]
           if pixrep == 1 else RGB_ROWS)
    arr = (np.array([one, one], dtype=dtype_name) if frames == 2
           else np.array(one, dtype=dtype_name))

    _summary, error, files, _rows, _out = _export(
        tmp_path, arr, pixrep, f"p8_{frames}_{dtype_name}", samples=3,
        frames=frames, photometric="RGB")

    assert error is None, f"an RGB export failed: {error}"
    ds = pydicom.dcmread(files[0])
    # The label the codestream earns: an RGB source is encoded with the
    # multiple-component transform, and PS3.5 8.2.4 gives that codestream
    # `YBR_RCT` under a reversible encode (#490). The samples are what
    # this test is about and they are unchanged, asserted below against
    # `RGB_ROWS`.
    assert ds.PhotometricInterpretation == "YBR_RCT"
    assert ds.PlanarConfiguration == 0
    assert ds.BitsAllocated == 8
    assert ds.pixel_array.dtype == np.dtype(dtype_name)
    assert ds.pixel_array.tolist() == arr.tolist()


@pytest.mark.parametrize("frames", [1, 2])
@pytest.mark.parametrize("dtype_name,pixel_representation", [
    ("uint16", 0),
    ("int16", 1),
])
def test_our_own_compressed_16_bit_colour_export_re_ingests(
        tmp_path, dtype_name, pixel_representation, frames):
    """P8x, the round trip that lets the `(2, True)` cell be written (#416).

    Until #416 this cell was refused: `imagecodecs` encoded it
    bit-exactly, but Pillow -- pydicom's only JPEG 2000 plugin here --
    cannot decode 16-bit multi-sample data, so `session.ingest()` on the
    export returned `ingested=0`. The standard was what this library can
    read back. Ingest now falls back to `imagecodecs`, so the file is
    written, and this test is the reason that is allowed: it goes through
    `session.ingest()`, because a handler-only decode passed before the
    fallback existed and would pin nothing.

    *Red when:* the `(2, True)` cell is removed from
    `_J2K_ENCODABLE_FRAMES` (no file is written), or ingest's `imagecodecs`
    fallback is removed (the file is refused at the door).
    """
    # Above 255 on purpose: a value that fits in a byte would not tell a
    # 16-bit frame apart from an 8-bit one.
    first = ([[[(v - 128) * 128 for v in px] for px in row] for row in RGB_ROWS]
             if pixel_representation == 1
             else [[[v * 257 for v in px] for px in row] for row in RGB_ROWS])
    second = ([[[(v - 128) * 128 + 1 for v in px] for px in row]
               for row in RGB_ROWS]
              if pixel_representation == 1
              else [[[v * 256 + 1 for v in px] for px in row]
                    for row in RGB_ROWS])
    literal = first if frames == 1 else [first, second]
    arr = np.array(literal, dtype=dtype_name)

    _summary, error, files, _rows, out = _export(
        tmp_path, arr, pixel_representation,
        f"p8x_{dtype_name}_{frames}", samples=3, frames=frames,
        photometric="RGB")

    assert error is None, f"a 16-bit colour export failed: {error}"
    assert len(files) == 1
    written = pydicom.dcmread(files[0])
    assert written.file_meta.TransferSyntaxUID == JPEG2000Lossless
    assert written.BitsAllocated == 16
    assert written.PixelRepresentation == pixel_representation
    # The precondition: pydicom cannot read it, so the ingest below is
    # the fallback's, not pydicom's.
    with pytest.raises(RuntimeError):
        _ = written.pixel_array

    session = DicomSession(persistence_file=str(tmp_path / "reingest.db"))
    try:
        summary = session.ingest(out)
        assert summary.failures == []
        assert summary.ingested == 1
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert inst.unload_pixel_data() is True
        got = inst.get_pixel_data()
    finally:
        session.close()
    assert got.dtype == np.dtype(dtype_name)
    assert got.tolist() == literal


# ---------------------------------------------------------------------------
# P9 -- what the encapsulated fragment actually contains
# ---------------------------------------------------------------------------

def test_the_encapsulated_fragment_is_a_codestream_not_a_jp2_box(tmp_path):
    """Transfer syntax 1.2.840.10008.1.2.4.90 names a **codestream**.

    Pillow's `Image.save(bio, format="JPEG2000")` with no filename wraps
    the codestream in a JP2 box, so every compressed file this project has
    ever exported began `0000000c6a502020` -- a JP2 box -- under a transfer
    syntax naming a bare codestream. Lenient decoders read it, pydicom
    among them, which is why nothing noticed. `codecformat="J2K"` produces
    `ff4f ff51`, the SOC marker followed by SIZ, which is what the transfer
    syntax actually calls for.

    *Red when:* `codecformat="J2K"` is dropped.
    """
    from pydicom.encaps import generate_fragments

    arr = np.array(CT_ROWS, dtype="int16")
    _summary, error, files, _rows, _out = _export(tmp_path, arr, 1, "p9")
    assert error is None

    ds = pydicom.dcmread(files[0])
    assert ds.file_meta.TransferSyntaxUID == JPEG2000Lossless

    fragments = list(generate_fragments(ds.PixelData))
    payload = next(f for f in fragments if len(f) > 4)
    assert payload[:2] == b"\xff\x4f", (
        f"the fragment is not a bare codestream; it starts "
        f"{payload[:12].hex()} (a JP2 box starts 0000000c6a502020)")
    assert payload[2:4] == b"\xff\x51", (
        "SOC is not followed by SIZ; this is not a JPEG 2000 codestream")


# ---------------------------------------------------------------------------
# P10 -- the deleted branch, pinned
# ---------------------------------------------------------------------------

def test_compress_j2k_without_an_array_writes_nothing_and_raises_nothing():
    """A characterization test, not a promise about a feature.

    `_compress_j2k(ds, pixel_array=None)` used to rebuild the array from
    `ds.PixelData`, reading the bytes as `uint16` regardless of
    `PixelRepresentation` -- a silent-corruption sibling of #386 that
    would have compressed signed data to wrong values without raising.
    The branch is **deleted rather than corrected**, because it is
    unreachable: `_compress_j2k`'s only caller is `_finalize_dataset`,
    whose only caller is the export worker, which always passes
    `pixel_array=arr`; and when compression is on the worker never assigns
    `ds.PixelData` at all, which the worker's own comment already says:
    `# Only set PixelData if NOT compressing.` at io_handlers.py line 7704.
    `pixel_array is None` therefore means "nothing to
    compress" and nothing else.

    *Red when:* the reconstruct-from-bytes arm is restored.
    """
    from pydicom.dataset import Dataset, FileMetaDataset

    import isocenter.io_handlers as io_handlers

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel = 1
    ds.NumberOfFrames = 1
    ds.BitsAllocated = 16
    ds.PixelRepresentation = 1
    original = np.array(CT_ROWS, dtype="int16").tobytes()
    ds.PixelData = original

    io_handlers._compress_j2k(ds, pixel_array=None)

    assert ds.PixelData == original, "the no-array call rewrote PixelData"
    assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian, (
        "the no-array call changed the transfer syntax without encoding "
        "anything")

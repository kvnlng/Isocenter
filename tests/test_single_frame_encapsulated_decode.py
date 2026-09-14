"""`imagecodecs_handler`'s single-frame arm decodes what it is handed (#407).

That arm was `imagecodecs_handler.get_pixel_data`'s, deleted in #453
(Q10): the imagecodecs decode is `decode_declared_frames` behind
`io_handlers._decode_pixels`' fallback, so these tests ask that route,
with pydicom made unable to decode (`through_the_fallback`).

The arm joined **every item** of the encapsulated `PixelData` and handed
the result to `imagecodecs`. The first item is the Basic Offset Table
(PS3.5 A.4), so the codestream arrived with four zero bytes ahead of its
`ff4f ff51` marker and every J2K frame was refused with `not a J2K or JP2
data stream`. The arm had never decoded anything.

Every test here asserts on a **decoded array**, not on the absence of an
exception: the failure before the fix was a `RuntimeError`, so a test
pinned to a message would stay green if the join were fixed and the frame
came back wrong.
"""
import os
import struct

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import (encapsulate, generate_fragments,
                            parse_basic_offsets)
from pydicom.uid import (ExplicitVRLittleEndian, JPEG2000Lossless,
                         generate_uid)

import imagecodecs
from isocenter.entities import Instance
from isocenter.io_handlers import _compress_j2k
from support.decode_doors import through_the_fallback


def _item(payload: bytes) -> bytes:
    """One encapsulation item: (fffe,e000), a 32-bit length, the payload."""
    return b"\xfe\xff\x00\xe0" + struct.pack("<I", len(payload)) + payload


def _skeleton(arr, samples=1, photometric="MONOCHROME2"):
    """A minimal single-frame dataset carrying `arr` uncompressed."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Rows, ds.Columns = arr.shape[0], arr.shape[1]
    ds.SamplesPerPixel = samples
    ds.PhotometricInterpretation = photometric
    if samples > 1:
        ds.PlanarConfiguration = 0
    ds.BitsAllocated = ds.BitsStored = arr.dtype.itemsize * 8
    ds.HighBit = ds.BitsAllocated - 1
    ds.PixelRepresentation = 1 if arr.dtype.kind == "i" else 0
    ds.PixelData = arr.tobytes()
    return ds


def _project_compressed(arr, samples=1, photometric="MONOCHROME2"):
    """The fixture this project itself writes: `_compress_j2k` + `encapsulate`.

    Deliberately not a hand-rolled `_item(b"") + _item(codestream)`. With
    an **empty** Basic Offset Table the buggy join is accidentally
    correct, so such a fixture passes on unfixed code -- and
    `pydicom.encaps.encapsulate` (which `_compress_j2k` calls) writes a
    *populated* table, which is why every file this project compresses hit
    the defect. `test_the_fixture_carries_a_populated_basic_offset_table`
    is the pin that stops this drifting back.
    """
    ds = _skeleton(arr, samples, photometric)
    _compress_j2k(ds, pixel_array=arr)
    return ds


def test_a_single_frame_j2k_codestream_decodes_to_the_pixels_that_were_encoded():
    """The whole arm, on what `session.export(use_compression=True)` writes."""
    arr = (np.arange(16, dtype=np.uint16) * 4096).reshape(4, 4)
    ds = _project_compressed(arr)

    out, _label = through_the_fallback(ds)

    assert out.dtype == np.uint16
    assert np.array_equal(out.reshape(arr.shape), arr)


def test_the_fixture_carries_a_populated_basic_offset_table():
    """The fixture must be one the defect could actually reach.

    A hand-built `item(b"") + item(codestream)` has an empty offset table,
    the join is then a no-op, and the test above would pass on unfixed
    code -- the fixture-never-enters-the-arm shape.
    """
    arr = (np.arange(16, dtype=np.uint16) * 4096).reshape(4, 4)
    ds = _project_compressed(arr)

    assert parse_basic_offsets(ds.PixelData) == [0]
    first = list(generate_fragments(ds.PixelData))[0]
    assert first == b"\x00\x00\x00\x00"

    out, _label = through_the_fallback(ds)
    assert np.array_equal(out.reshape(arr.shape), arr)


def test_one_frame_split_across_two_fragments_is_reassembled():
    """One frame may legally span several fragments (PS3.5 A.4).

    This is what kills the cheap "take the last fragment" fix: it would
    hand `imagecodecs` the tail of the codestream.
    """
    arr = (np.arange(16, dtype=np.uint16) * 4096).reshape(4, 4)
    codestream = imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K")
    half = len(codestream) // 2
    if half % 2:
        half += 1  # items must be even-length

    ds = _skeleton(arr)
    ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
    ds.PixelData = (_item(b"")
                    + _item(codestream[:half])
                    + _item(codestream[half:]))

    assert len(list(generate_fragments(ds.PixelData))) == 3

    out, _label = through_the_fallback(ds)
    assert np.array_equal(out.reshape(arr.shape), arr)


def test_a_multi_frame_dataset_still_decodes_every_frame():
    """The arm this PR does not change, guarded against collateral."""
    frames = [(np.arange(16, dtype=np.uint16) * 4096).reshape(4, 4),
              ((np.arange(16, dtype=np.uint16) + 20) * 2048).reshape(4, 4)]
    ds = _skeleton(frames[0])
    ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
    ds.NumberOfFrames = 2
    ds.PixelData = encapsulate(
        [imagecodecs.jpeg2k_encode(f, level=0, codecformat="J2K")
         for f in frames])

    out, _label = through_the_fallback(ds)

    assert out.shape == (2, 4, 4)
    assert np.array_equal(out[0], frames[0])
    assert np.array_equal(out[1], frames[1])


def _write_16bit_rgb_j2k(folder):
    """A 16-bit RGB J2K file: exactly what Pillow cannot decode.

    Built by hand rather than by an export, so the fixture does not
    depend on the exporter. It is the population
    `Instance.get_pixel_data()`'s imagecodecs fallback exists for, and
    the one that fallback had never been able to read before #407. Since
    #416 the exporter writes this cell too, and ingest reads it back
    through the same codec (`tests/test_ingest_imagecodecs_fallback.py`).
    """
    arr = (np.arange(4 * 4 * 3, dtype=np.uint16) * 1000).reshape(4, 4, 3)
    ds = _skeleton(arr, samples=3, photometric="RGB")
    ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
    ds.PixelData = encapsulate(
        [imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K")])
    path = os.path.join(folder, "rgb16.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path, arr


def test_the_imagecodecs_fallback_reads_a_frame_pydicom_cannot(tmp_path):
    """`Instance.get_pixel_data()`'s fourth arm, end to end.

    Three things have to hold for this to be a test of the fallback
    rather than of the ordinary pydicom read:

    * `pydicom.dcmread(p).pixel_array` must raise -- the precondition. If
      Pillow could read this frame the instance would never reach the
      fallback and the assertion below would pass through the wrong door.
    * `set_pixel_data` then `discard_pixel_data` must run first, so the
      resident array is gone and `get_pixel_data()` has to go to the file.
      Without the discard the replacement is handed straight back.
    * `unload_pixel_data() is True` at the end. That is the assertion
      which pins the fallback arm's `self._pixel_array_unwritten = False`
      -- the clear whose comment said "SURVIVES DELETION UNTESTED" until
      #407 made this arm reachable.
    """
    path, want = _write_16bit_rgb_j2k(str(tmp_path))

    with pytest.raises(RuntimeError):
        _ = pydicom.dcmread(path).pixel_array

    inst = Instance(sop_instance_uid="1.2.3.407")
    inst.file_path = path
    inst.set_pixel_data(np.zeros((4, 4, 3), dtype=np.uint16))
    assert inst._pixel_array_unwritten is True
    assert inst.discard_pixel_data() is True

    got = inst.get_pixel_data()

    assert got is not None
    assert got.dtype == np.uint16
    assert np.array_equal(got.reshape(want.shape), want)
    assert inst.unload_pixel_data() is True

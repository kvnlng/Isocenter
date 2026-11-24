"""A native 1-bit file holding more than it declares keeps its declared frames (#1094).

A native BitsAllocated 1 frame is bit-packed and not padded to a byte,
so 3x3 is nine bits. Where Pixel Data holds more such frames than
NumberOfFrames declares, pydicom's default decode slices by a fraction
and fails in CPython's words, and #1062 refused the whole file in a
sentence of numbers (`imagecodecs_handler.bit_packed_excess_words`).
pydicom with `allow_excess_frames=False` reads the declared frames of
every such file: measured on all 27 cells of #1062's grid, 3.12.14.
Every other "more than declared" shape keeps the declared frames with a
`DATA_LOSS` row, so this one now does too (owner ruling 2026-11-22, Q1):
`imagecodecs_handler._native_excess` counts the frames in bits and names
them by a `NativeBits` marker, and the ingest decode, asked to drop the excess,
skips the refusal.

A direct `_decode_pixels(ds)`, which keeps every frame, still gets the
1-bit sentence; the read door refuses through `frame_count_mismatch`, as
it does for every other excess.

Names `isocenter.io_handlers`, `isocenter.imagecodecs_handler` and
`isocenter.entities`.
"""
import math

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.pixels import get_decoder
from pydicom.sequence import Sequence

from isocenter.entities import Instance
from isocenter.imagecodecs_handler import (NativeBits,
                                           bit_packed_excess_words,
                                           frame_count_mismatch_words,
                                           offset_table_frame_count)
from isocenter.io_handlers import _decode_pixels, ingest_worker
from support.decode_doors import EXPLICIT_LE, SOP_CLASS, at_ingest, dataset, write


def _file(rows, cols, frames, have, *, samples=1):
    return dataset(EXPLICIT_LE, rows=rows, cols=cols, samples=samples,
                   bits_allocated=1, bits_stored=1, frames=frames,
                   native=bytes([0b10110010, 0b01001101, 0b00000011, 0,
                                 0b01010101, 0b00001111] * 8)[:have],
                   photometric="MONOCHROME2" if samples == 1 else "RGB")


def _declared_only(ds):
    """pydicom's own read of the declared frames."""
    return get_decoder(ds.file_meta.TransferSyntaxUID).as_array(
        ds, allow_excess_frames=False)[0]


#: #1062's grid: the 27 cells pydicom's default decode cannot slice, as
#: `(rows, cols, frames, held)`, held already even, as a file holds it.
CELLS = sorted({(rows, cols, frames, held)
                for rows, cols in ((3, 3), (2, 2), (5, 3), (3, 1), (4, 4),
                                   (4, 2))
                for frames in (1, 2)
                for extra in range(5)
                for have in [math.ceil(rows * cols * frames / 8) + extra]
                for held in [have + have % 2]
                if (rows * cols) % 8
                and held > math.ceil(rows * cols * frames / 8)
                + math.ceil(rows * cols * frames / 8) % 2
                and held * 8 // (rows * cols) > frames})


def _row(rows, cols, frames, held, samples=1, declared_words=None):
    bits = rows * cols * samples
    need = math.ceil(bits * frames / 8)
    room = held * 8 // bits
    declared_words = declared_words or f"NumberOfFrames declares {frames}"
    return (f"Pixel Data holds {held} bytes, room for {room} frames of "
            f"{bits} bits; {declared_words}, which "
            f"{'needs' if frames == 1 else 'need'} {need} "
            f"{'byte' if need == 1 else 'bytes'}")


def test_the_grid_is_the_cells_1062_refused():
    """#1062's 27 cells set an odd length beside an even one; held even,
    as a file holds them, they are 16 distinct files."""
    assert len(CELLS) == 16


def test_the_count_and_its_words_by_literal():
    """Red if `_native_excess` still declines a fractional frame."""
    counted = offset_table_frame_count(_file(3, 3, 2, 6))
    assert counted is not None and counted[:3] == (5, 2, 2)
    assert counted[3] == NativeBits(held=6, frame_bits=9)
    assert frame_count_mismatch_words(counted) == (
        "Pixel Data holds 6 bytes, room for 5 frames of 9 bits; "
        "NumberOfFrames declares 2, which need 3 bytes")


@pytest.mark.parametrize("rows, cols, frames, held", CELLS)
def test_ingest_keeps_the_declared_frames_pydicom_reads(tmp_path, rows, cols,
                                                         frames, held):
    path = write(tmp_path, _file(rows, cols, frames, held))
    on_disk = pydicom.dcmread(path)
    assert len(on_disk.PixelData) == held
    want = _declared_only(on_disk)

    result = ingest_worker(path)

    assert result[-1] is None, result[-1]
    excess = result[0]["offset_table_excess"]
    assert excess[:3] == (held * 8 // (rows * cols), frames, frames)
    assert frame_count_mismatch_words(excess) == _row(rows, cols, frames, held)
    assert result[2] == np.ascontiguousarray(want).tobytes()


def test_a_session_ingest_writes_the_data_loss_row_and_exports(tmp_path):
    path = write(tmp_path, _file(3, 3, 2, 6))
    got = at_ingest(tmp_path, path)

    assert got["failure"] is None
    assert got["array"].shape == (2, 3, 3)
    assert np.array_equal(got["array"], _declared_only(pydicom.dcmread(path)))
    losses = [r for r in got["rows"] if r[0] == "DATA_LOSS"]
    assert [r[2] for r in losses] == [
        "Pixel Data holds 6 bytes, room for 5 frames of 9 bits; "
        "NumberOfFrames declares 2, which need 3 bytes. Kept the first 2 "
        "and discarded 3."]


def test_a_number_of_frames_holding_no_value_is_read_as_one(tmp_path):
    """From a file, a zero-length (0028,0008) is None, and pydicom reads 1."""
    ds = _file(3, 3, 1, 4)
    ds.NumberOfFrames = None
    path = write(tmp_path, ds)
    on_disk = pydicom.dcmread(path)
    assert "NumberOfFrames" in on_disk and on_disk.NumberOfFrames is None

    result = ingest_worker(path)

    assert result[-1] is None, result[-1]
    assert frame_count_mismatch_words(result[0]["offset_table_excess"]) == (
        "Pixel Data holds 4 bytes, room for 3 frames of 9 bits; "
        "NumberOfFrames is empty (read as 1), which needs 2 bytes")
    assert result[2] == _declared_only(on_disk).tobytes()


def test_an_empty_string_set_in_memory_is_still_pydicoms_to_refuse():
    """pydicom refuses `''` in its own words; nothing is counted for it."""
    ds = _file(3, 3, 1, 4)
    ds.NumberOfFrames = ""

    assert offset_table_frame_count(ds) is None
    with pytest.raises(ValueError) as caught:
        _decode_pixels(ds, allow_excess_frames=False)
    assert "invalid literal for int()" in str(caught.value)


def test_a_direct_decode_keeping_every_frame_still_gets_the_sentence():
    """Red if the refusal is deleted rather than skipped."""
    ds = _file(3, 3, 2, 6)
    words = bit_packed_excess_words(ds)
    assert words is not None

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == words
    assert _decode_pixels(ds, allow_excess_frames=False)[0].shape == (2, 3, 3)


def test_the_read_door_refuses_through_the_mismatch(tmp_path):
    ds = _file(3, 3, 2, 6)
    path = write(tmp_path, ds)
    inst = Instance(ds.SOPInstanceUID, SOP_CLASS, 1, file_path=path)

    with pytest.raises(RuntimeError) as read:
        inst.get_pixel_data()
    assert str(read.value) == (
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"RuntimeError: Pixel Data holds 6 bytes, room for 5 frames of 9 "
        f"bits; NumberOfFrames declares 2, which need 3 bytes")


def test_three_samples_of_27_bits_under_5_bytes_is_still_padding():
    """#1062's control: five bytes hold one 27-bit frame, so no excess."""
    ds = _file(3, 3, 1, 5, samples=3)

    assert offset_table_frame_count(ds) is None
    assert _decode_pixels(ds)[0].shape == (3, 3, 3)


def test_colour_counts_its_samples_in_the_bits():
    ds = _file(3, 3, 4, 18, samples=3)
    counted = offset_table_frame_count(ds)

    assert counted[:3] == (5, 4, 4)
    assert frame_count_mismatch_words(counted) == (
        "Pixel Data holds 18 bytes, room for 5 frames of 27 bits; "
        "NumberOfFrames declares 4, which need 14 bytes")
    assert _decode_pixels(ds, allow_excess_frames=False)[0].shape == (
        4, 3, 3, 3)


def test_ybr_full_422_is_still_pydicoms():
    """pydicom's excess arithmetic is the `elif` of its YBR_FULL_422 test."""
    ds = _file(3, 3, 4, 18, samples=3)
    ds.PhotometricInterpretation = "YBR_FULL_422"

    assert offset_table_frame_count(ds) is None


def test_a_whole_byte_one_bit_frame_is_still_the_byte_arm():
    """4x4 is 16 bits, two whole bytes: `NATIVE_FRAMES`, as before."""
    counted = offset_table_frame_count(_file(4, 4, 1, 6))

    assert counted is not None and counted[3] == "Pixel Data's length"


def test_a_one_bit_icon_with_bytes_beyond_it_keeps_its_declared_frames(
        tmp_path):
    """`_decode_nested_pixels` asks the same count, so the icon is kept."""
    ds = dataset(EXPLICIT_LE, rows=4, cols=4, bits_allocated=8, bits_stored=8,
                 frames=None, native=bytes(16), photometric="MONOCHROME2")
    icon = Dataset()
    icon.Rows, icon.Columns, icon.SamplesPerPixel = 3, 3, 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.BitsAllocated = icon.BitsStored = 1
    icon.HighBit, icon.PixelRepresentation = 0, 0
    icon.NumberOfFrames = 2
    icon.add_new(0x7FE00010, "OB", b"\xaa" * 6)
    ds.IconImageSequence = Sequence([icon])
    path = write(tmp_path, ds)

    got = at_ingest(tmp_path, path)

    assert got["failure"] is None
    rows = [r[2] for r in got["rows"] if r[0] in ("DATA_LOSS", "ERROR")]
    assert len(rows) == 1, got["rows"]
    assert rows[0].endswith(
        "Pixel Data holds 6 bytes, room for 5 frames of 9 bits; "
        "NumberOfFrames declares 2, which need 3 bytes. Kept the first 2 and "
        "discarded 3."), rows


def test_a_whole_byte_frame_under_an_empty_number_of_frames_reads_every_frame(
        tmp_path):
    """The shape #1094 does not fix, read from its source file.

    A 4x4 frame at 8 bits is a whole number of bytes, so the bit count
    does not apply. Under a NumberOfFrames that holds no value in the
    file, 32 bytes hold two whole frames, and the source read returns
    both, with no error: it does not refuse "as for every other excess".
    CHANGELOG and docs/codecs.md say so; this pins what they say.
    """
    ds = dataset(EXPLICIT_LE, rows=4, cols=4, bits_allocated=8,
                 native=bytes(range(32)), photometric="MONOCHROME2")
    ds.NumberOfFrames = None
    path = write(tmp_path, ds)
    assert pydicom.dcmread(path)["NumberOfFrames"].is_empty

    got = Instance(ds.SOPInstanceUID, SOP_CLASS, 1,
                   file_path=path).get_pixel_data()

    assert got.shape == (2, 4, 4)
    assert got.tobytes() == bytes(range(32))


def test_a_whole_byte_one_bit_frame_under_an_empty_number_of_frames_reads_every_frame(
        tmp_path):
    """A bit-packed frame that is a whole number of bytes is not counted in bits.

    8x8 at one bit is 64 bits, eight whole bytes, so `_native_excess`
    takes the byte arm. Under a NumberOfFrames that holds no value in the
    file, 16 bytes hold two whole frames, and the source read returns
    both, with no error, as for the 8-bit shape above. CHANGELOG and
    docs/codecs.md say the bit count, and the refusal of the source read,
    are for a bit-packed frame that is not a whole number of bytes; this
    pins the 1-bit shape that qualifier leaves out.
    """
    ds = _file(8, 8, None, 16)
    ds.NumberOfFrames = None
    path = write(tmp_path, ds)
    assert pydicom.dcmread(path)["NumberOfFrames"].is_empty

    got = Instance(ds.SOPInstanceUID, SOP_CLASS, 1,
                   file_path=path).get_pixel_data()

    assert got.shape == (2, 8, 8)

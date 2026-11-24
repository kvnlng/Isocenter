"""An Extended Offset Table with no Lengths is refused in this library's words (#1086).

An Extended Offset Table (7FE0,0001) names each frame's offset; its
Lengths element (7FE0,0002) names each frame's length, and pydicom's
decoder needs both. With Lengths empty (read from a file as None) or
absent, pydicom fails in CPython's words. Measured on 3e4ee91a (3.12.14
and 3.14.7t), JPEG 2000 Lossless, at ingest and the read door alike:

    16-byte table, NumberOfFrames 2, Lengths empty
        TypeError: object of type 'NoneType' has no len()
    16-byte table, NumberOfFrames 2, Lengths absent
        AttributeError: 'FileDataset' object has no attribute
        'ExtendedOffsetTableLengths'
    8-byte table, NumberOfFrames 1, Lengths empty     (the TypeError)
    8-byte table, NumberOfFrames 1, Lengths absent    (the AttributeError)

The file stays refused (owner ruling Q14), now in a sentence naming both
elements, made in `_decode_pixels` before pydicom is asked
(`imagecodecs_handler.missing_lengths_words`, the fourth of
`refusal_before_decode`). A header pydicom refuses for another element
keeps pydicom's sentence naming that element: the question is put to
pydicom on a copy of the dataset without the two table elements, because
pydicom fails on the table itself and would otherwise always answer
"refused". A table naming fewer frames than declared keeps the count
refusal, which comes first. A Lengths element of another length is a
table pydicom drops and reads without; it is not this refusal.

Names `isocenter.imagecodecs_handler`, `isocenter.io_handlers` and
`isocenter.entities`.
"""
import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.encaps import encapsulate

from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from isocenter.io_handlers import _decode_pixels, ingest_worker
from support.decode_doors import (J2K_LOSSLESS, SOP_CLASS, at_ingest,
                                  dataset, pydicom_cannot, route,  # noqa: F401  pylint: disable=unused-import
                                  write)

FRAMES = [(np.arange(64, dtype=np.uint8) + 60 * i).reshape(8, 8)
          for i in range(3)]
STREAMS = [imagecodecs.jpeg2k_encode(frame, level=0, codecformat="J2K")
           for frame in FRAMES]


def _file(tmp_path, fragments, declared, table_bytes, lengths, *, name="one",
          edit=None):
    """`fragments` J2K frames, an EOT cut to `table_bytes`, Lengths as asked.

    `lengths` is "empty" (a zero-length element, read back as None) or
    "absent". Written and read back, so the elements are what a file
    holds.
    """
    streams = STREAMS[:fragments]
    ds = dataset(J2K_LOSSLESS, streams, rows=8, cols=8, bits_allocated=8,
                 frames=declared)
    ds.PixelData = encapsulate(streams, has_bot=False)
    ds["PixelData"].is_undefined_length = True
    offsets, at = [], 0
    for stream in streams:
        offsets.append(at)
        at += 8 + len(stream) + len(stream) % 2
    raw = b"".join(o.to_bytes(8, "little") for o in offsets) + b"\0" * 64
    ds.add_new(0x7FE00001, "OV", raw[:table_bytes])
    if lengths == "empty":
        ds.add_new(0x7FE00002, "OV", b"")
    if edit is not None:
        edit(ds)
    path = write(tmp_path, ds, name)
    return path, pydicom.dcmread(path)


def _words(named, lengths):
    table = (f"names {named} frame{'' if named == 1 else 's'}"
             if isinstance(named, int) else f"is {named} long")
    held = ("there is no Extended Offset Table Lengths (7FE0,0002)"
            if lengths == "absent"
            else "its Extended Offset Table Lengths (7FE0,0002) is empty")
    return (f"Extended Offset Table (7FE0,0001) {table}, and {held}, so no "
            f"frame's length is stated and the frames cannot be read by "
            f"the table.")


def _door(path):
    inst = Instance(str(pydicom.dcmread(path).SOPInstanceUID), SOP_CLASS, 1,
                    file_path=path)
    with pytest.raises(Exception) as caught:
        inst.get_pixel_data()
    return inst, caught.value


@pytest.mark.parametrize("lengths", ["empty", "absent"])
@pytest.mark.parametrize("fragments, declared, table_bytes, named", [
    (2, 2, 16, 2), (1, 1, 8, 1)], ids=["16", "8"])
def test_the_measured_cells_are_refused_in_one_sentence(
        tmp_path, route, fragments, declared, table_bytes, named, lengths):  # pylint: disable=redefined-outer-name
    """Ingest and the read door, on pydicom's route and the fallback's."""
    path, ds = _file(tmp_path, fragments, declared, table_bytes, lengths)
    assert ("ExtendedOffsetTableLengths" in ds) == (lengths == "empty")
    want = _words(named, lengths)

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == want

    got = at_ingest(tmp_path, path)
    assert got["failure"] == f"Decompression Failed: RuntimeError: {want}"
    if route is not None:
        assert got["pydicom_calls"] == 0, "refused before pydicom is asked"

    inst, error = _door(path)
    # A refusal on what the file holds: no advice about a decoder.
    assert str(error) == (f"Lazy load failed for instance "
                          f"{inst.sop_instance_uid}: RuntimeError: {want}")


def test_a_table_naming_more_than_declared_is_refused_the_same_way(tmp_path):
    """The excess is counted, then the decode is asked and refused here."""
    path, _ds = _file(tmp_path, 2, 1, 16, "absent")
    assert ingest_worker(path)[-1] == (
        f"Decompression Failed: RuntimeError: {_words(2, 'absent')}")


def test_a_table_that_is_no_whole_number_of_offsets_names_its_length(
        tmp_path):
    path, _ds = _file(tmp_path, 1, 1, 12, "absent")
    assert ingest_worker(path)[-1] == (
        f"Decompression Failed: RuntimeError: {_words('12 bytes', 'absent')}")


@pytest.mark.parametrize("lengths", ["empty", "absent"])
def test_a_header_pydicom_refuses_keeps_pydicoms_words(tmp_path, lengths):
    """BitsStored 17: pydicom's sentence naming (0028,0101).

    Red if the guard is asked of the dataset with the table (it would
    always withhold, and the absent Lengths would then fail in CPython's
    `AttributeError`), or is not asked at all (this sentence would hide
    the element pydicom names).
    """
    def bits_stored_17(ds):
        ds.BitsStored = 17
    path, ds = _file(tmp_path, 2, 2, 16, lengths, edit=bits_stored_17)
    pydicom_words = ("A (0028,0101) 'Bits Stored' value of '17' is invalid, "
                     "it must be in the range (1, 64) and no greater than "
                     "the (0028,0100) 'Bits Allocated' value of '8'")

    with pytest.raises(ValueError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == pydicom_words
    assert ingest_worker(path)[-1] == (
        f"Decompression Failed: ValueError: {pydicom_words}")
    # The caller's dataset still holds both table elements.
    assert "ExtendedOffsetTable" in ds
    assert ("ExtendedOffsetTableLengths" in ds) == (lengths == "empty")


def test_the_question_never_changes_the_callers_dataset(tmp_path):
    path, ds = _file(tmp_path, 2, 2, 16, "empty")
    before = sorted(elem.tag for elem in ds)
    assert imagecodecs_handler.missing_lengths_words(ds) == _words(2, "empty")
    assert sorted(elem.tag for elem in ds) == before
    assert ds.ExtendedOffsetTableLengths is None


@pytest.mark.parametrize("lengths", ["empty", "absent"])
def test_a_table_naming_fewer_keeps_the_count_refusal(tmp_path, lengths):
    path, _ds = _file(tmp_path, 2, 2, 8, lengths)
    assert ingest_worker(path)[-1] == (
        "Extended Offset Table names 1 frame; NumberOfFrames declares 2")


def test_an_in_memory_empty_bytes_lengths_is_dropped_not_refused():
    """`b""` in memory is bytes of another length: pydicom drops the table."""
    ds = dataset(J2K_LOSSLESS, STREAMS[:2], rows=8, cols=8, bits_allocated=8,
                 frames=2)
    ds.PixelData = encapsulate(STREAMS[:2], has_bot=False)
    ds["PixelData"].is_undefined_length = True
    ds.add_new(0x7FE00001, "OV", b"\0" * 16)
    ds.add_new(0x7FE00002, "OV", b"")
    assert imagecodecs_handler.missing_lengths_words(ds) is None
    arr, _label = _decode_pixels(ds)
    assert arr.shape == (2, 8, 8)


def test_a_native_file_is_not_asked(tmp_path):
    """pydicom takes no table for native data."""
    ds = dataset("1.2.840.10008.1.2.1", rows=8, cols=8, bits_allocated=8,
                 native=FRAMES[0].tobytes())
    ds.add_new(0x7FE00001, "OV", b"\0" * 8)
    path = write(tmp_path, ds)
    assert imagecodecs_handler.missing_lengths_words(
        pydicom.dcmread(path)) is None

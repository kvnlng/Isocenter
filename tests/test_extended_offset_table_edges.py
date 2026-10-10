"""Two edges of the Extended Offset Table (#621 P7 and P1).

An Extended Offset Table (7FE0,0001) is 8 bytes per frame, beside a
Lengths element (7FE0,0002) of the same size. pydicom 3.0.2 walks by the
table when both are present and the same length, and **drops** it, with a
warning, when their lengths differ.

**P7 -- a table pydicom walks by whose length is not a multiple of 8.**
It names no whole number of frames. Measured on 067982ec (3.12.14 and
3.14.7t), a 12-byte table beside 12 bytes of Lengths was refused in two
unrelated sentences, by NumberOfFrames:

    NumberOfFrames 2   Extended Offset Table names 1 frames;
                       NumberOfFrames declares 2
    NumberOfFrames 1   Decompression Failed: error: unpack requires a
                       buffer of 8 bytes

The first counts `12 // 8` as a frame; the second is `struct.error`'s
words. Now one sentence, raised in `_decode_pixels`:

    Extended Offset Table is 12 bytes long, which is not a whole number
    of 8-byte offsets, so it names no whole number of frames.

**P1 -- a table pydicom drops that names fewer frames than declared.**
Offsets 8 bytes, Lengths 4, NumberOfFrames 2, two fragments: pydicom
reads both frames from the fragments, and ingest refused the file because
the dropped table "names 1 frames". The count exists for the other
direction (under NumberOfFrames 1 pydicom's walk returns frame 0 and
never mentions frame 1, so a dropped table is still counted and the
excess gets its row). In the shortfall direction nothing needs it: a
dropped table that names fewer is not counted, and the file is read as
pydicom reads it (owner ruling Q4 A).

Every dataset here is written and read back, so the elements are what a
file holds. Names `isocenter.io_handlers`, `isocenter.imagecodecs_handler`
and `isocenter.entities`.
"""
import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.encaps import encapsulate
from pydicom.pixels import get_decoder

from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from isocenter.imagecodecs_handler import (frame_count_mismatch,
                                           offset_table_frame_count,
                                           ragged_offset_table_words)
from isocenter.io_handlers import _decode_pixels, ingest_worker
from support.decode_doors import (EXPLICIT_LE, J2K_LOSSLESS, SOP_CLASS,
                                  at_ingest, dataset, through_the_fallback,
                                  write)

#: Three distinguishable 8x8 8-bit frames.
FRAMES = [(np.arange(64, dtype=np.uint8) + 60 * i).reshape(8, 8)
          for i in range(3)]
STREAMS = [imagecodecs.jpeg2k_encode(frame, level=0, codecformat="J2K")
           for frame in FRAMES]

NO_BOUNDARY = (", and NumberOfFrames declares 2: pydicom found no frame "
               "boundary in them, so the declared frames cannot be read")


def _table(fragments, number_of_frames, offsets_bytes, lengths_bytes):
    """A JPEG 2000 file: `fragments` fragments, an empty Basic Offset Table.

    The true offsets and lengths of the fragments, each cut (or
    zero-padded) to the size asked; `None` leaves the element absent.
    """
    ds = dataset(J2K_LOSSLESS, STREAMS[:fragments], rows=8, cols=8,
                 bits_allocated=8, frames=number_of_frames)
    ds.PixelData = encapsulate(STREAMS[:fragments], has_bot=False)
    ds["PixelData"].is_undefined_length = True
    offsets, lengths, at = [], [], 0
    for stream in STREAMS[:fragments]:
        offsets.append(at)
        lengths.append(len(stream))
        at += 8 + len(stream) + len(stream) % 2
    if offsets_bytes is not None:
        raw = b"".join(o.to_bytes(8, "little") for o in offsets) + b"\0" * 64
        ds.add_new(0x7FE00001, "OV", raw[:offsets_bytes])
    if lengths_bytes is not None:
        raw = b"".join(n.to_bytes(8, "little") for n in lengths) + b"\0" * 64
        ds.add_new(0x7FE00002, "OV", raw[:lengths_bytes])
    return ds


def _on_disk(tmp_path, ds, name="one"):
    path = write(tmp_path, ds, name)
    return path, pydicom.dcmread(path)


def _pydicom(path):
    """pydicom's own decode of the file: the array, or the exception."""
    ds = pydicom.dcmread(path)
    try:
        return get_decoder(J2K_LOSSLESS).as_array(ds)[0]
    except BaseException as exc:  # pylint: disable=broad-except
        return exc


def _door(path, uid):
    inst = Instance(uid, SOP_CLASS, 1, file_path=path)
    try:
        return inst, inst.get_pixel_data()
    except RuntimeError as exc:
        return inst, exc


def _ragged(length):
    return (f"Extended Offset Table is {length} bytes long, which is not a "
            f"whole number of 8-byte offsets, so it names no whole number "
            f"of frames.")


# ---------------------------------------------------------------------------
# P7: a table pydicom walks by, of a length that is not a multiple of 8
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length, number_of_frames", [
    (12, 2), (12, 1), (20, 2), (4, 2), (4, 1)])
def test_a_ragged_table_is_refused_in_one_sentence_at_every_door(
        tmp_path, length, number_of_frames):
    """Red on 067982ec, in a different sentence for each NumberOfFrames."""
    path, ds = _on_disk(tmp_path, _table(2, number_of_frames, length, length))
    theirs = _pydicom(path)
    assert type(theirs).__name__ == "error" and "unpack" in str(theirs), \
        "the control: pydicom alone fails on this table in struct's words"
    want = _ragged(length)

    # The count offers no number: `12 // 8` is not a frame.
    assert offset_table_frame_count(ds) is None
    assert frame_count_mismatch(ds) is None

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert type(caught.value) is RuntimeError
    assert str(caught.value) == want

    assert ingest_worker(path)[-1] == (
        f"Decompression Failed: RuntimeError: {want}")

    inst, read = _door(path, ds.SOPInstanceUID)
    assert isinstance(read, RuntimeError)
    assert str(read) == (f"Lazy load failed for instance "
                         f"{inst.sop_instance_uid}: RuntimeError: {want}")


def test_the_ragged_sentence_whole_and_what_it_does_not_hold(tmp_path):
    _path, ds = _on_disk(tmp_path, _table(2, 2, 12, 12))

    words = ragged_offset_table_words(ds)

    assert words == (
        "Extended Offset Table is 12 bytes long, which is not a whole "
        "number of 8-byte offsets, so it names no whole number of frames.")
    assert "error:" not in words and "unpack" not in words
    assert "no pixel data" not in words.lower()


def test_a_well_formed_table_is_read_as_before(tmp_path):
    path, ds = _on_disk(tmp_path, _table(2, 2, 16, 16))

    assert ragged_offset_table_words(ds) is None
    assert offset_table_frame_count(ds)[:2] == (2, 2)
    result = ingest_worker(path)
    assert result[-1] is None
    arr, _label = _decode_pixels(ds)
    assert arr.tolist() == np.stack(FRAMES[:2]).tolist()


@pytest.mark.parametrize("offsets, lengths", [(12, 16), (16, 12), (12, 4)])
def test_a_table_pydicom_drops_does_not_get_the_ragged_sentence(
        tmp_path, offsets, lengths):
    """Lengths of another size: pydicom ignores the table, ragged or not.

    The table then says nothing about the frames the decoder reads, so
    its length is not a reason to refuse them: both frames are read.
    """
    path, ds = _on_disk(tmp_path, _table(2, 2, offsets, lengths))
    assert imagecodecs_handler.extended_offsets(ds) is None
    theirs = _pydicom(path)
    assert isinstance(theirs, np.ndarray) and theirs.shape == (2, 8, 8)

    assert ragged_offset_table_words(ds) is None
    arr, _label = _decode_pixels(ds)
    assert arr.tolist() == np.stack(FRAMES[:2]).tolist()
    assert ingest_worker(path)[-1] is None


def test_a_native_file_carrying_a_ragged_table_is_not_refused_for_it(
        tmp_path):
    """pydicom takes no table for native data; neither element is read."""
    ds = dataset(EXPLICIT_LE, rows=8, cols=8, bits_allocated=8,
                 native=FRAMES[0].tobytes())
    ds.add_new(0x7FE00001, "OV", b"\0" * 12)
    ds.add_new(0x7FE00002, "OV", b"\0" * 12)
    path, read = _on_disk(tmp_path, ds)

    assert ragged_offset_table_words(read) is None
    assert _decode_pixels(read)[0].tolist() == FRAMES[0].tolist()
    assert ingest_worker(path)[-1] is None


def test_a_dataset_the_helper_cannot_read_is_none():
    """Never raises: no file meta, no table."""
    assert ragged_offset_table_words(pydicom.Dataset()) is None


# ---------------------------------------------------------------------------
# P1: a table pydicom drops, naming fewer frames than declared
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("offsets, lengths", [(8, 4), (12, 16), (4, 8)],
                         ids=["8/4", "12/16", "4/8"])
def test_a_dropped_table_naming_fewer_frames_is_read_as_pydicom_reads_it(
        tmp_path, offsets, lengths):
    """Red on 067982ec: `Extended Offset Table names 1 frames; ...`, refused.

    pydicom reads both frames. So does every door now, with no row: the
    table pydicom ignores is not counted against NumberOfFrames.
    """
    path, ds = _on_disk(tmp_path, _table(2, 2, offsets, lengths))
    both = np.stack(FRAMES[:2]).tolist()
    theirs = _pydicom(path)
    assert isinstance(theirs, np.ndarray) and theirs.tolist() == both
    assert imagecodecs_handler.extended_offsets(ds) is None

    assert offset_table_frame_count(ds) is None
    assert frame_count_mismatch(ds) is None

    result = ingest_worker(path)
    assert result[-1] is None, result[-1]
    assert "offset_table_excess" not in result[0]

    _inst, read = _door(path, ds.SOPInstanceUID)
    assert isinstance(read, np.ndarray) and read.tolist() == both

    got = at_ingest(tmp_path, path)
    assert got["failure"] is None
    assert got["array"].tolist() == both
    assert got["rows"] == []


def test_the_fallback_reads_the_same_two_frames(tmp_path):
    """With pydicom unable to decode, imagecodecs is handed the same frames."""
    _path, ds = _on_disk(tmp_path, _table(2, 2, 8, 4))

    arr, _label = through_the_fallback(ds)

    assert arr.tolist() == np.stack(FRAMES[:2]).tolist()


def test_a_table_pydicom_walks_by_naming_fewer_is_still_refused(tmp_path):
    """8/8: the table is the one the decoder reads frames by, and it names one."""
    path, ds = _on_disk(tmp_path, _table(2, 2, 8, 8))
    assert imagecodecs_handler.extended_offsets(ds) is not None

    assert offset_table_frame_count(ds) == (
        1, 2, 2, "Extended Offset Table")
    assert ingest_worker(path)[-1] == (
        "Extended Offset Table names 1 frames; NumberOfFrames declares 2")
    inst, read = _door(path, ds.SOPInstanceUID)
    assert str(read) == (
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"RuntimeError: Extended Offset Table names 1 frames; "
        f"NumberOfFrames declares 2")


def test_a_dropped_table_naming_more_is_still_counted(tmp_path):
    """The direction the count exists for: 16/8 under NumberOfFrames 1.

    pydicom's walk returns frame 0 and never mentions frame 1; the table
    is the only thing that knows it is there. Ingest keeps frame 0 and
    its `DATA_LOSS` facts; the whole pipeline is pinned by
    `test_offset_table_frame_count.py::test_an_extended_offset_table_with_short_lengths_still_reports_its_excess`.
    """
    path, ds = _on_disk(tmp_path, _table(2, 1, 16, 8))
    assert imagecodecs_handler.extended_offsets(ds) is None

    assert offset_table_frame_count(ds) == (
        2, 1, 1, "Extended Offset Table")
    result = ingest_worker(path)
    assert result[-1] is None
    assert result[0]["offset_table_excess"] == (
        2, 1, 1, "Extended Offset Table")


def test_a_dropped_table_agreeing_with_the_header_is_as_before(tmp_path):
    """16/8 under NumberOfFrames 2: the equal case, read with no row."""
    path, ds = _on_disk(tmp_path, _table(2, 2, 16, 8))

    assert offset_table_frame_count(ds) == (2, 2, 2, "Extended Offset Table")
    result = ingest_worker(path)
    assert result[-1] is None and "offset_table_excess" not in result[0]


def test_a_dropped_table_naming_fewer_over_more_fragments_gets_the_walks_row(
        tmp_path):
    """8/4, three fragments, NumberOfFrames 2: the excess is the walk's to name.

    No frame is lost in silence by not counting the table: the walk finds
    three frames, ingest keeps the declared two, and the row's facts name
    the fragments, not a table pydicom ignored.
    """
    path, ds = _on_disk(tmp_path, _table(3, 2, 8, 4))

    assert offset_table_frame_count(ds) == (3, 2, 2, "Pixel Data's fragments")
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None
    assert got["array"].tolist() == np.stack(FRAMES[:2]).tolist()
    assert [action for action, _uid, _details in got["rows"]] == ["DATA_LOSS"]
    assert ("Pixel Data's fragments hold 3 frames by their end markers; "
            "NumberOfFrames declares 2") in got["rows"][0][2]


def test_one_fragment_under_a_dropped_table_is_refused_without_a_false_clause(
        tmp_path):
    """8/4 over one fragment, NumberOfFrames 2: pydicom's bare `StopIteration`.

    The count no longer answers first for this file, so the no-boundary
    sentence does, and its "with no offset table" would be false of a
    dataset that holds one. It says what is true: none pydicom walks by.
    """
    path, ds = _on_disk(tmp_path, _table(1, 2, 8, 4))
    assert isinstance(_pydicom(path), StopIteration)
    want = ("Pixel Data holds 1 fragment with no offset table pydicom walks "
            "by" + NO_BOUNDARY)

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == want
    assert ingest_worker(path)[-1] == (
        f"Decompression Failed: RuntimeError: {want} (caused by "
        f"StopIteration)")


def test_one_fragment_with_no_table_keeps_its_sentence(tmp_path):
    """No Extended Offset Table at all: the clause is as it was."""
    _path, ds = _on_disk(tmp_path, _table(1, 2, None, None))

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == (
        "Pixel Data holds 1 fragment with no offset table" + NO_BOUNDARY)


@pytest.mark.parametrize("lengths", [0, None], ids=["empty", "absent"])
def test_a_table_pydicom_does_not_drop_is_still_counted(tmp_path, lengths):
    """Lengths empty or absent is not "dropped": pydicom fails on it.

    It raises `TypeError: object of type 'NoneType' has no len()` for an
    empty Lengths element and an `AttributeError` for an absent one, so
    deferring to the decoder would trade this sentence for those. The
    shortfall is only left to the decoder where pydicom reads the file.
    """
    path, ds = _on_disk(tmp_path, _table(2, 2, 8, lengths))
    assert isinstance(_pydicom(path), (TypeError, AttributeError))

    assert offset_table_frame_count(ds) == (1, 2, 2, "Extended Offset Table")
    assert ingest_worker(path)[-1] == (
        "Extended Offset Table names 1 frames; NumberOfFrames declares 2")

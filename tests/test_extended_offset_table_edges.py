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
never mentions frame 1, so a dropped table naming more is still counted
and the excess gets its row). A dropped table that names fewer is not
counted, and the file is read as pydicom reads it (owner ruling Q4 A).

**The invariant: a file whose table pydicom drops, naming no more frames
than declared, is counted exactly as its twin without the two
elements.** The first build returned pydicom's walk for the shortfall,
on the premise that a shortfall needs two declared frames. It does not
(a 4-byte table names 0 frames under NumberOfFrames 1), and the walk
skips the two counts a table-less file gets first: its Basic Offset
Table, and an RLE buffer's fragments. Measured at 32696465 (review of
#1062), 4 bytes beside 8 under NumberOfFrames 1:

    RLE, two fragments        ingested, 1 frame, no row
    JPEG 2000, a Basic        ingested, no row, two frames' samples
    Offset Table of two       under a one-frame header: the loader's
                              Integrity Error, export 0 of 1

where the twin gets a `DATA_LOSS` row and keeps frame 0. The agreeing
table (8 beside 4 under NumberOfFrames 1) had the same two outcomes on
`main`, by the same skip, and is the same fix.

Every dataset here is written and read back, so the elements are what a
file holds, except the count grid, which says so. Names
`isocenter.io_handlers`, `isocenter.imagecodecs_handler` and
`isocenter.entities`.
"""
import os
import struct

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.encaps import encapsulate, generate_frames
from pydicom.pixels import get_decoder
from pydicom.uid import RLELossless as RLE_LOSSLESS

from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from isocenter.imagecodecs_handler import (frame_count_mismatch,
                                           offset_table_frame_count,
                                           ragged_offset_table_words)
from isocenter.io_handlers import _decode_pixels, ingest_worker
from support.decode_doors import (EXPLICIT_LE, J2K_LOSSLESS, SOP_CLASS,
                                  at_ingest, dataset, pydicom_answer,
                                  pydicom_cannot,  # noqa: F401  pylint: disable=unused-import
                                  through_the_fallback, write)

#: Three distinguishable 8x8 8-bit frames.
FRAMES = [(np.arange(64, dtype=np.uint8) + 60 * i).reshape(8, 8)
          for i in range(3)]
STREAMS = [imagecodecs.jpeg2k_encode(frame, level=0, codecformat="J2K")
           for frame in FRAMES]

NO_BOUNDARY = (", and NumberOfFrames declares 2: pydicom found no frame "
               "boundary in them, so the declared frames cannot be read")


def _rle_streams():
    """The three frames as RLE Lossless frames, one fragment each."""
    ds = dataset(EXPLICIT_LE, rows=8, cols=8, bits_allocated=8, frames=3,
                 native=b"".join(frame.tobytes() for frame in FRAMES))
    ds.compress(RLE_LOSSLESS, encoding_plugin="pydicom")
    return [bytes(frame) for frame in generate_frames(
        ds.PixelData, number_of_frames=3)]


RLE_STREAMS = _rle_streams()


def _table(fragments, number_of_frames, offsets_bytes, lengths_bytes, *,
           ts=J2K_LOSSLESS, bot="empty"):
    """An encapsulated file of `fragments` fragments, one frame in each.

    JPEG 2000 with an empty Basic Offset Table unless told otherwise;
    `bot` is "empty", "true" (an offset per fragment) or "one" (a single
    offset, 0). The Extended Offset Table and its Lengths hold the true
    offsets and lengths of the fragments, each cut (or zero-padded) to
    the size asked; `None` leaves the element absent, as does a
    NumberOfFrames of `None`.
    """
    streams = (RLE_STREAMS if ts == RLE_LOSSLESS else STREAMS)[:fragments]
    ds = dataset(ts, streams, rows=8, cols=8, bits_allocated=8,
                 frames=number_of_frames)
    ds.PixelData = encapsulate(streams, has_bot=bot == "true")
    if bot == "one":
        assert ds.PixelData[:8] == b"\xfe\xff\x00\xe0\x00\x00\x00\x00"
        ds.PixelData = (b"\xfe\xff\x00\xe0\x04\x00\x00\x00" + b"\0" * 4
                        + ds.PixelData[8:])
    ds["PixelData"].is_undefined_length = True
    offsets, lengths, at = [], [], 0
    for stream in streams:
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


def _mono(ds):
    ds.add_new(0x00280004, "CS", "mono")


def _no_bits_stored_value(ds):
    ds.BitsStored = 0


def _no_pixel_representation(ds):
    del ds.PixelRepresentation


@pytest.mark.parametrize("number_of_frames", [1, 2])
@pytest.mark.parametrize("edit, row", [
    (_mono, "Decompression Failed: ValueError: Unknown (0028,0004) "
            "'Photometric Interpretation' value 'mono'"),
    (_no_bits_stored_value,
     "Decompression Failed: ValueError: A (0028,0101) 'Bits Stored' value "
     "of '0' is invalid, it must be in the range (1, 64) and no greater "
     "than the (0028,0100) 'Bits Allocated' value of '8'"),
    (_no_pixel_representation,
     "Decompression Failed: AttributeError: Missing required element: "
     "(0028,0103) 'Pixel Representation'"),
], ids=["label", "bits-stored", "pixel-representation"])
def test_a_ragged_table_under_a_header_pydicom_refuses_keeps_pydicoms_words(
        tmp_path, edit, row, number_of_frames):
    """The ragged sentence is for a header pydicom accepts.

    pydicom validates the header before it unpacks the table, and names
    the element it refuses. Under NumberOfFrames 1 that was the row on
    `main`; under NumberOfFrames 2 `main` counted the table first
    (`Extended Offset Table names 1 frames; ...`). Until the third build
    of this change the ragged sentence answered first under either,
    because it is made before pydicom is asked.
    """
    ds = _table(2, number_of_frames, 12, 12)
    edit(ds)
    path, ds = _on_disk(tmp_path, ds)
    theirs = _pydicom(path)
    assert isinstance(theirs, (ValueError, AttributeError)), theirs
    assert not isinstance(theirs, struct.error)

    assert ragged_offset_table_words(ds) is None
    assert ingest_worker(path)[-1] == row


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


def test_a_dropped_table_naming_more_is_named_as_the_table_under_two_frames(
        tmp_path):
    """24/8, three fragments, NumberOfFrames 2: reported from the table.

    The walk finds three frames as well; the row's subject is the table's
    all the same, as it was before #621. Only the shortfall moved.
    """
    path, ds = _on_disk(tmp_path, _table(3, 2, 24, 8))
    assert imagecodecs_handler.extended_offsets(ds) is None

    assert offset_table_frame_count(ds) == (
        3, 2, 2, "Extended Offset Table")
    assert ingest_worker(path)[0]["offset_table_excess"] == (
        3, 2, 2, "Extended Offset Table")


def test_a_dropped_table_agreeing_with_the_header_is_read_with_no_row(
        tmp_path):
    """16/8 under NumberOfFrames 2: read with no row, as its twin is.

    The count offers nothing of its own for it (it returned the table's
    own agreeing count until the review of #1062, which no caller could
    tell from None).
    """
    path, ds = _on_disk(tmp_path, _table(2, 2, 16, 8))

    assert offset_table_frame_count(ds) is None
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


@pytest.mark.parametrize("lengths", [0, None], ids=["empty", "absent"])
def test_a_table_pydicom_does_not_drop_naming_fewer_is_not_asked_of_the_walk(
        tmp_path, lengths):
    """Three fragments under it: the walk would find an excess, and is not asked.

    pydicom fails on this file whatever the fragments hold, so the
    table's shortfall is the refusal, in this library's sentence. Asked
    of the walk first, the file would be sent on to the decoder as an
    excess of three and refused in pydicom's `TypeError`.
    """
    path, ds = _on_disk(tmp_path, _table(3, 2, 8, lengths))
    assert isinstance(_pydicom(path), (TypeError, AttributeError))

    assert offset_table_frame_count(ds) == (1, 2, 2, "Extended Offset Table")
    assert ingest_worker(path)[-1] == (
        "Extended Offset Table names 1 frames; NumberOfFrames declares 2")


# ---------------------------------------------------------------------------
# The invariant: a dropped table naming no more than declared is counted
# exactly as the same file without the two elements
# ---------------------------------------------------------------------------

def _twin(ds):
    """The same dataset, UIDs and all, without the two elements."""
    twin = pydicom.dcmread(_bytes(ds), force=True)
    for tag in (0x7FE00001, 0x7FE00002):
        if tag in twin:
            del twin[tag]
    return twin


def _bytes(ds):
    from io import BytesIO  # pylint: disable=import-outside-toplevel
    buf = BytesIO()
    ds.save_as(buf, enforce_file_format=False)
    buf.seek(0)
    return buf


def test_a_dropped_table_naming_no_more_is_counted_as_its_twin_over_a_grid():
    """Every cell's count equals its twin's; none of them is all None.

    2 syntaxes x 1 to 3 fragments x 3 Basic Offset Tables x NumberOfFrames
    absent, 1, 2, 3 x every pair of unequal lengths from 4 to 24 bytes
    whose table names no more frames than declared. In memory, where the
    element holds the same bytes a file's does (the files are the tests
    below). Red at 32696465 in the cells under NumberOfFrames 1 or absent
    with a 4-byte table, and on `main` in those with an 8-byte one too.
    """
    sizes = (4, 8, 12, 16, 20, 24)
    cells, wrong, answers = 0, [], set()
    for ts in (J2K_LOSSLESS, RLE_LOSSLESS):
        for fragments in (1, 2, 3):
            for bot in ("empty", "true", "one"):
                for frames in (None, 1, 2, 3):
                    declared = frames or 1
                    twin = offset_table_frame_count(
                        _table(fragments, frames, None, None, ts=ts, bot=bot))
                    for offsets in sizes:
                        if offsets // 8 > declared:
                            continue
                        for lengths in sizes:
                            if lengths == offsets:
                                continue
                            cells += 1
                            ds = _table(fragments, frames, offsets, lengths,
                                        ts=ts, bot=bot)
                            assert imagecodecs_handler.extended_offsets(
                                ds) is None
                            got = offset_table_frame_count(ds)
                            answers.add(None if got is None else got[3])
                            if got != twin:
                                wrong.append((str(ts), fragments, bot, frames,
                                              offsets, lengths, got, twin))
    assert cells == 1530, cells
    assert not wrong, f"{len(wrong)} of {cells}: {wrong[:5]}"
    # The grid reaches every arm a table-less file is counted by.
    assert answers == {None, "Basic Offset Table", "RLE fragments",
                       "Pixel Data's fragments"}, answers


def _session(tmp_path, ds, name):
    """`ds` through `Session.ingest()` and `export()`: what each said.

    `{"failure", "rows", "array", "exported"}`: `at_ingest`'s three, and
    the Pixel Data arrays of the files the export wrote.
    """
    from isocenter.session import DicomSession  # pylint: disable=import-outside-toplevel
    home = tmp_path / name
    home.mkdir()
    path = write(home, ds)
    got = at_ingest(home, path)
    out = home / "out"
    with DicomSession(persistence_file=str(home / "s.db")) as session:
        try:
            session.export(str(out))
            got["export"] = None
        except Exception as exc:  # pylint: disable=broad-except
            got["export"] = exc
    got["exported"] = []
    for root, _dirs, files in os.walk(out):
        for name_ in files:
            written = os.path.join(root, name_)
            if "PixelData" in pydicom.dcmread(written, force=True):
                # pydicom's own decoder, whatever route the test is on.
                got["exported"].append(pydicom_answer(written)[0].tolist())
    return got


#: Under NumberOfFrames 1 or absent: (transfer syntax, Basic Offset
#: Table, the row a file with no Extended Offset Table gets).
LOSES_A_FRAME = [
    (RLE_LOSSLESS, "empty",
     "Pixel Data's fragments hold 2 frames, one per fragment; "
     "NumberOfFrames {declares}. Kept the first 1 and discarded 1."),
    (J2K_LOSSLESS, "true",
     "Basic Offset Table names 2 frames; NumberOfFrames {declares}. Kept "
     "the first 1 and discarded 1."),
]


@pytest.mark.parametrize("frames, declares", [
    (1, "declares 1"), (None, "is absent (read as 1)")],
    ids=["one", "absent"])
@pytest.mark.parametrize("offsets, lengths", [(4, 8), (8, 4)],
                         ids=["fewer-4/8", "agreeing-8/4"])
@pytest.mark.parametrize("ts, bot, row", LOSES_A_FRAME, ids=["rle", "j2k-bot"])
def test_a_second_frame_under_one_declared_gets_its_twins_row(
        tmp_path, ts, bot, row, offsets, lengths, frames, declares):
    """Two frames, one declared, a dropped table naming 0 or 1.

    The reviewer's two files and their agreeing-table siblings, through
    `Session.ingest()` and `export()`. Red at 32696465 for 4/8: the RLE
    file ingested with no row, and the JPEG 2000 one stored 128 samples
    under a one-frame 8x8 header, which the loader refuses and the
    export cannot write. Red on `main` for 8/4, the same two ways.
    """
    ds = _table(2, frames, offsets, lengths, ts=ts, bot=bot)
    assert imagecodecs_handler.extended_offsets(ds) is None

    got = _session(tmp_path, ds, "table")
    twin = _session(tmp_path, _twin(ds), "twin")

    assert got["failure"] is None and twin["failure"] is None
    assert [action for action, _uid, _details in got["rows"]] == ["DATA_LOSS"]
    assert row.format(declares=declares) in got["rows"][0][2], got["rows"]
    assert got["rows"] == twin["rows"]
    # Frame 0, stored and exported: the declared frame, and only it.
    assert got["array"].tolist() == FRAMES[0].tolist()
    assert twin["array"].tolist() == FRAMES[0].tolist()
    assert got["export"] is None, got["export"]
    assert got["exported"] == [FRAMES[0].tolist()] == twin["exported"]


@pytest.mark.parametrize("offsets, lengths", [(4, 8), (8, 4)],
                         ids=["fewer-4/8", "agreeing-8/4"])
def test_the_fallback_route_gives_the_same_row(
        tmp_path, pydicom_cannot, offsets, lengths):  # pylint: disable=redefined-outer-name
    """With pydicom unable to decode, the JPEG 2000 file is its twin too.

    Red at 32696465 for 4/8 and on `main` for 8/4: one frame stored and
    no row, so what the file got depended on the route. (RLE Lossless has
    no fallback.)
    """
    ds = _table(2, 1, offsets, lengths, ts=J2K_LOSSLESS, bot="true")

    got = _session(tmp_path, ds, "table")
    twin = _session(tmp_path, _twin(ds), "twin")

    assert pydicom_cannot["n"] > 0
    assert got["pydicom_calls"] > 0 and twin["pydicom_calls"] > 0
    assert got["failure"] is None
    assert [action for action, _uid, _details in got["rows"]] == ["DATA_LOSS"]
    assert ("Basic Offset Table names 2 frames; NumberOfFrames declares 1. "
            "Kept the first 1 and discarded 1.") in got["rows"][0][2]
    assert got["rows"] == twin["rows"]
    assert got["array"].tolist() == FRAMES[0].tolist()
    assert got["exported"] == [FRAMES[0].tolist()] == twin["exported"]


def test_an_agreeing_dropped_table_over_more_rle_fragments_is_read_as_its_twin(
        tmp_path):
    """16/4, three RLE fragments, NumberOfFrames 2: the declared two, and a row.

    Refused on `main` (`Pixel Data holds 3 fragments with no offset
    table, ...`: pydicom's walk finds no end marker in RLE), while the
    same file without the table was read. The fragments are counted now.
    """
    ds = _table(3, 2, 16, 4, ts=RLE_LOSSLESS)

    got = _session(tmp_path, ds, "table")
    twin = _session(tmp_path, _twin(ds), "twin")

    assert got["failure"] is None
    assert got["rows"] == twin["rows"]
    assert ("Pixel Data's fragments hold 3 frames, one per fragment; "
            "NumberOfFrames declares 2. Kept the first 2 and discarded 1."
            ) in got["rows"][0][2]
    assert got["array"].tolist() == np.stack(FRAMES[:2]).tolist()


@pytest.mark.parametrize("offsets, lengths", [(4, 8), (16, 4)],
                         ids=["fewer", "agreeing"])
def test_a_basic_offset_table_naming_fewer_answers_under_a_dropped_table(
        tmp_path, offsets, lengths):
    """One fragment, a Basic Offset Table of one offset, NumberOfFrames 2.

    The twin's sentence, which names the table pydicom walks by. At
    32696465 the 4/8 file said "with no offset table pydicom walks by"
    over that table; on `main` the 16/4 one said "with no offset table".
    """
    path, ds = _on_disk(tmp_path, _table(1, 2, offsets, lengths, bot="one"))

    assert offset_table_frame_count(ds) == (1, 2, 2, "Basic Offset Table")
    assert ingest_worker(path)[-1] == (
        "Basic Offset Table names 1 frames; NumberOfFrames declares 2")


def test_one_fragment_under_an_agreeing_dropped_table_gains_the_clause(
        tmp_path):
    """16/4 over one fragment, NumberOfFrames 2, an empty Basic Offset Table.

    Refused on `main` too, as "with no offset table" over a dataset that
    holds one. Named in the CHANGELOG's Output line.
    """
    path, _ds = _on_disk(tmp_path, _table(1, 2, 16, 4))

    assert ingest_worker(path)[-1] == (
        "Decompression Failed: RuntimeError: Pixel Data holds 1 fragment "
        "with no offset table pydicom walks by" + NO_BOUNDARY
        + " (caused by StopIteration)")


def test_the_clause_is_not_lengthened_over_a_populated_basic_offset_table(
        tmp_path):
    """24/4 (names more), a Basic Offset Table of one offset, one fragment.

    Counted from the dropped table, so the Basic Offset Table is not
    asked and the decode runs out of frames: `main`'s outcome and
    `main`'s words, which are false of this file (pydicom walks by the
    Basic Offset Table) and are not this change's. "pydicom walks by"
    would be false of it as well, so it is not added.
    """
    path, ds = _on_disk(tmp_path, _table(1, 2, 24, 4, bot="one"))
    assert offset_table_frame_count(ds) == (3, 2, 2, "Extended Offset Table")

    assert ingest_worker(path)[-1] == (
        "Decompression Failed: RuntimeError: Pixel Data holds 1 fragment "
        "with no offset table" + NO_BOUNDARY + " (caused by StopIteration)")


@pytest.mark.parametrize("fragments, declared, size", [
    (1, 2, 8), (2, 2, 8), (2, 3, 16)])
def test_the_clause_is_not_said_of_a_table_pydicom_walks_by(
        tmp_path, fragments, declared, size):
    """A direct `_decode_pixels` call on a table of equal Lengths, naming fewer.

    No door gets this far (the count refuses the file first, below), so
    the sentence is reached only by a caller of the decode itself. It
    keeps `main`'s words. "pydicom walks by" was said of it at fe439a55
    (review of #1062, round 2), over a table pydicom does walk by.
    """
    path, ds = _on_disk(tmp_path, _table(fragments, declared, size, size))
    assert imagecodecs_handler.extended_offsets(ds) is not None

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == (
        f"Pixel Data holds {fragments} fragment"
        f"{'' if fragments == 1 else 's'} with no offset table, and "
        f"NumberOfFrames declares {declared}: pydicom found no frame "
        f"boundary in them, so the declared frames cannot be read")
    assert ingest_worker(path)[-1] == (
        f"Extended Offset Table names {size // 8} frames; NumberOfFrames "
        f"declares {declared}")


def test_a_one_byte_table_is_a_byte_long():
    """In memory only: pydicom pads a 1-byte value to 2 when it writes."""
    ds = _table(2, 2, 16, 16)
    ds.ExtendedOffsetTable = b"\0"
    ds.ExtendedOffsetTableLengths = b"\0"

    assert ragged_offset_table_words(ds) == (
        "Extended Offset Table is 1 byte long, which is not a whole number "
        "of 8-byte offsets, so it names no whole number of frames.")

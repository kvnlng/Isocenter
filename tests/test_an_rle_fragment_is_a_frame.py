"""An RLE Lossless fragment is a frame, and an empty table costs neither (#664).

RLE streams carry no `FF D9` end marker, so pydicom's fragment walk finds
no frame boundary at all: with an empty Basic Offset Table it joins every
fragment into one frame and warns, and `DecodeRunner._as_array_encapsulated`
then asks for frame 1 and gets a bare `StopIteration`. Measured on a8b6d3f,
RLE 8x8 with an empty Basic Offset Table:

* NumberOfFrames 2 over three fragments ingested 0 instances with
  `ERROR: Ingest failed for ...: Decompression Failed: StopIteration` -- a
  message naming neither count -- and graded `REVIEW_REQUIRED`;
* NumberOfFrames 2 over four fragments did the same;
* **NumberOfFrames 1 over three fragments kept frame 0, discarded two
  frames with no row at all, and graded `PASS`** -- the silent sibling.

The same three-fragment shape under JPEG Lossless has had #620's
`DATA_LOSS` row since 0.9.8 (`test_the_jpeg_walked_arm_still_says_end_markers`
below), and the same RLE file with a *populated* table has had #418's, so
RLE with an empty table was the one arm left.

`offset_table_frame_count` now counts an RLE buffer's fragments as its
frames where there are **more fragments than declared frames** and **every
fragment's first 64 bytes parse as an RLE frame header**. The second gate
is not belt-and-braces: without it the arm fires on a file whose one frame
is split across two fragments -- which main decodes correctly today -- and
turns it into an `ERROR` row via pydicom's own segment-table refusal. That
file is `test_an_rle_frame_split_across_fragments_is_read_as_one_frame`,
and it is the only thing holding the pre-check.

No test here skips without an optional codec, so every one runs in the
local gate and the release matrix: pydicom decodes RLE with no plugin at
all, and `RLELosslessEncoder` builds every fixture.
"""
import os
import sqlite3
import struct
from io import BytesIO

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import (encapsulate, generate_fragments,
                            parse_basic_offsets, parse_fragments)
from pydicom.pixels import get_decoder
from pydicom.pixels.encoders import RLELosslessEncoder
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from isocenter.io_handlers import (LOSS_SCOPE_SIGNAL, LOSS_SCOPE_STANDARD,
                                   _decode_pixels)
from isocenter.session import DicomSession
from support.decode_doors import (J2K_LOSSLESS, LJPEG_SV1, SOP_CLASS, dataset,
                                  write)

RLE_LOSSLESS = "1.2.840.10008.1.2.5"

#: Three 8x8 frames with distinct values, so "kept frames 0 and 1" cannot
#: pass for "kept 0 and 2": an assertion on shape alone would.
FRAMES = [np.full((8, 8), value, dtype=np.uint8) for value in (10, 20, 30)]
#: A frame whose samples all differ, for the split-fragment fixture: a
#: constant frame compresses to bytes a half-split would not disturb.
RAMP = np.arange(64, dtype=np.uint8).reshape(8, 8)


def _rle(arr):
    return RLELosslessEncoder.encode(
        arr, rows=arr.shape[0], columns=arr.shape[1], samples_per_pixel=1,
        bits_allocated=8, bits_stored=8, pixel_representation=0,
        photometric_interpretation="MONOCHROME2", number_of_frames=1)


STREAMS = [_rle(frame) for frame in FRAMES]
RAMP_STREAM = _rle(RAMP)


def _split(stream):
    """One frame's bytes in two fragments, each of even length."""
    half = len(stream) // 2
    half += half % 2
    return [stream[:half], stream[half:]]


def _no_bot(ts, fragments, *, frames, bits=8, rows=8, cols=8):
    """`fragments` behind an **empty** Basic Offset Table."""
    ds = dataset(ts, [b"\0\0"], rows=rows, cols=cols, bits_allocated=bits,
                 frames=frames)
    ds.PixelData = encapsulate(list(fragments), has_bot=False)
    ds["PixelData"].is_undefined_length = True
    return ds


def _fragments(ds):
    buf = BytesIO(ds.PixelData)
    parse_basic_offsets(buf)
    return parse_fragments(buf)[0]


ROW_WORDS_2 = ("Pixel Data's fragments hold 3 frames, one per fragment; "
               "NumberOfFrames declares 2")
ROW_WORDS_1 = ("Pixel Data's fragments hold 3 frames, one per fragment; "
               "NumberOfFrames declares 1")
JPEG_WALKED_WORDS = ("Pixel Data's fragments hold 3 frames by their end "
                     "markers; NumberOfFrames declares 2")


def _rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT action_type, details, loss_scope FROM audit_log "
            "WHERE action_type IN ('DATA_LOSS', 'ERROR')").fetchall()


def _pipeline(tmp_path, path, name="s"):
    """Ingest, read back from the sidecar, export, grade."""
    db = str(tmp_path / f"{name}.db")
    out = tmp_path / f"{name}-out"
    report = tmp_path / f"{name}.md"
    got = {"failures": []}
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        got["failures"] = list(summary.failures)
        instances = [i for p in session.store.patients for st in p.studies
                     for se in st.series for i in se.instances]
        if instances:
            (inst,) = instances
            assert inst.unload_pixel_data() is True
            got["array"] = inst.get_pixel_data()
            got["uid"] = inst.sop_instance_uid
            session.export(str(out), format="dicom")
            got["written"] = [os.path.join(r, f)
                              for r, _d, fs in os.walk(str(out))
                              for f in fs if f.endswith(".dcm")]
        session.generate_report(str(report))
    got["grade"] = "\n".join(
        line for line in report.read_text(encoding="utf-8").splitlines()
        if "Validation Status" in line)
    got["rows"] = _rows(db)
    return got


def _signal_row(rows):
    losses = [r for r in rows if r[0] == "DATA_LOSS"]
    assert len(losses) == 1, rows
    assert not [r for r in rows if r[0] == "ERROR"], rows
    return losses[0]


def _read_door(path):
    """`Instance.get_pixel_data()` on a bare file-backed instance."""
    inst = Instance(generate_uid(), SOP_CLASS, 1, file_path=path)
    return inst.get_pixel_data()


# ---------------------------------------------------------------------------
# The fixtures carry the shapes they are named for
# ---------------------------------------------------------------------------

def test_the_fixtures_have_an_empty_table_and_the_fragment_counts_named():
    excess = _no_bot(RLE_LOSSLESS, STREAMS, frames=2)
    assert parse_basic_offsets(excess.PixelData) == []
    assert "ExtendedOffsetTable" not in excess
    assert _fragments(excess) == 3
    # The three frames are three distinct images.
    assert len({f.tobytes() for f in FRAMES}) == 3
    split = _no_bot(RLE_LOSSLESS, _split(RAMP_STREAM), frames=1)
    assert _fragments(split) == 2
    assert parse_basic_offsets(split.PixelData) == []


# ---------------------------------------------------------------------------
# T11 and T10: the files the arm must NOT touch. Written before the arm.
# ---------------------------------------------------------------------------

def test_an_rle_frame_split_across_fragments_is_read_as_one_frame(tmp_path):
    """One frame over two fragments, NumberOfFrames 1: unchanged, no row.

    The arm does not fire because the second fragment's first 64 bytes are
    not an RLE frame header -- it is the middle of a compressed segment.
    Without that pre-check this file counts 2 fragments against 1 declared
    frame, the decode is told to read two frames, and pydicom's RLE decoder
    refuses it (`The amount of decoded RLE segment data doesn't match the
    expected amount`) -- an `ERROR` row on a file main reads correctly.
    """
    ds = _no_bot(RLE_LOSSLESS, _split(RAMP_STREAM), frames=1)
    assert imagecodecs_handler.offset_table_frame_count(ds) is None
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert not got["failures"], got["failures"]
    assert got["array"].tolist() == RAMP.tolist()
    assert not got["rows"], got["rows"]
    assert "PASS" in got["grade"] and "REVIEW" not in got["grade"], \
        got["grade"]


@pytest.mark.parametrize("frames, want", [
    (3, [f.tolist() for f in FRAMES]),
    (2, [f.tolist() for f in FRAMES[:2]]),
], ids=["3-fragments-3-frames", "2-fragments-2-frames"])
def test_an_rle_file_whose_fragments_match_its_frames_is_unchanged(
        tmp_path, frames, want):
    """The counts agree, so the arm never asks: no row, PASS, same values."""
    ds = _no_bot(RLE_LOSSLESS, STREAMS[:frames], frames=frames)
    assert imagecodecs_handler.offset_table_frame_count(ds) is None
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert not got["failures"], got["failures"]
    assert got["array"].tolist() == want
    assert not got["rows"], got["rows"]
    assert "PASS" in got["grade"], got["grade"]


# ---------------------------------------------------------------------------
# T7 and T8: the excess is dropped, and said
# ---------------------------------------------------------------------------

def test_rle_fragments_beyond_the_declared_frames_are_dropped_with_a_row(
        tmp_path):
    """NumberOfFrames 2 over three fragments: the filed file."""
    ds = _no_bot(RLE_LOSSLESS, STREAMS, frames=2)
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert not got["failures"], got["failures"]
    _action, details, scope = _signal_row(got["rows"])
    assert details == f"{ROW_WORDS_2}. Kept the first 2 and discarded 1."
    assert scope == LOSS_SCOPE_SIGNAL
    assert got["array"].shape == (2, 8, 8)
    assert got["array"].tolist() == [f.tolist() for f in FRAMES[:2]]
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]


def test_a_single_declared_frame_over_several_rle_fragments_says_what_it_dropped(
        tmp_path):
    """The silent sibling: NumberOfFrames 1 over three fragments.

    Red on a8b6d3f with **no** row and `PASS`: frame 0 was kept and two
    frames were discarded without a word.
    """
    ds = _no_bot(RLE_LOSSLESS, STREAMS, frames=1)
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert not got["failures"], got["failures"]
    _action, details, scope = _signal_row(got["rows"])
    assert details == f"{ROW_WORDS_1}. Kept the first 1 and discarded 2."
    assert scope == LOSS_SCOPE_SIGNAL
    # One frame, and `pixel_array`'s shape for one frame.
    assert got["array"].shape == (8, 8)
    assert got["array"].tolist() == FRAMES[0].tolist()
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]


@pytest.mark.parametrize("declared, shape", [(1, (8, 8)), (2, (2, 8, 8))],
                         ids=["one-frame", "two-frames"])
def test_the_dropped_excess_returns_pixel_arrays_shape(declared, shape):
    """`_decode_pixels`' own return, not the sidecar's (rev-098j9 P7/R3).

    The docstring promises "a single kept frame is returned in
    `pixel_array`'s shape for one frame, without a leading axis", and
    `arr[0] if kept == 1 else arr[:kept]` is what keeps it. Nothing
    asserted it: the pipeline tests read `Instance.get_pixel_data()`
    after an `unload_pixel_data()`, and the sidecar loader rebuilds the
    shape from Rows, Columns and NumberOfFrames -- so `arr[:kept]` in
    place of `arr[0]` round-trips to `(8, 8)` anyway and the mutant
    survived. This calls the function.

    Both arms, so neither `arr[0]` nor `arr[:kept]` can answer for both.
    """
    ds = _no_bot(RLE_LOSSLESS, STREAMS, frames=declared)
    arr, _label = _decode_pixels(ds, allow_excess_frames=False,
                                 number_of_frames=len(STREAMS))

    assert arr.shape == shape
    expected = (FRAMES[0] if declared == 1
                else np.stack(FRAMES[:declared]))
    assert arr.tolist() == expected.tolist()


def test_four_rle_fragments_under_two_declared_frames_are_counted_too(
        tmp_path):
    """Four fragments, NumberOfFrames 2 -- the second shape that raised."""
    ds = _no_bot(RLE_LOSSLESS, STREAMS + [_rle(RAMP)], frames=2)
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert not got["failures"], got["failures"]
    _action, details, _scope = _signal_row(got["rows"])
    assert details == ("Pixel Data's fragments hold 4 frames, one per "
                       "fragment; NumberOfFrames declares 2. Kept the first "
                       "2 and discarded 2.")
    assert got["array"].tolist() == [f.tolist() for f in FRAMES[:2]]


# ---------------------------------------------------------------------------
# T9: the read door refuses in the row's words
# ---------------------------------------------------------------------------

def test_the_read_door_refuses_the_same_file_in_the_same_words(tmp_path):
    path = write(tmp_path, _no_bot(RLE_LOSSLESS, STREAMS, frames=2))

    with pytest.raises(RuntimeError) as caught:
        _read_door(path)

    assert ROW_WORDS_2 in str(caught.value), str(caught.value)
    # Not routed into the "no codec" or "no pixel data" arms by its words.
    assert "missing dependencies" not in str(caught.value)


# ---------------------------------------------------------------------------
# T7a: the icon, one depth down
# ---------------------------------------------------------------------------

def _icon(payload, *, frames):
    item = Dataset()
    item.Rows, item.Columns = 8, 8
    item.BitsAllocated = item.BitsStored = 8
    item.HighBit = 7
    item.SamplesPerPixel = 1
    item.PhotometricInterpretation = "MONOCHROME2"
    item.PixelRepresentation = 0
    item.NumberOfFrames = frames
    item.add_new(0x7FE00010, "OB", payload)
    item["PixelData"].is_undefined_length = True
    return item


def test_an_icon_with_excess_rle_fragments_keeps_its_declared_frames(tmp_path):
    """The same shape as an Icon Image Sequence item (#433's depth).

    Red without the `number_of_frames` keyword at the icon call site even
    once the top level is fixed: the row is written from the shared
    counter, and the decode then joins the fragments anyway.
    """
    ds = _no_bot(RLE_LOSSLESS, [STREAMS[0]], frames=1)
    ds.IconImageSequence = Sequence([
        _icon(encapsulate(list(STREAMS), has_bot=False), frames=2)])
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert not got["failures"], got["failures"]
    _action, details, scope = _signal_row(got["rows"])
    assert scope == LOSS_SCOPE_STANDARD
    assert details.startswith("Standard tag 7fe0,0010 (OB) at 0088,0200[0]: ")
    assert details.endswith(f"{ROW_WORDS_2}. Kept the first 2 and "
                            f"discarded 1.")

    (written,) = got["written"]
    icon = pydicom.dcmread(written).IconImageSequence[0]
    icon.file_meta = FileMetaDataset()
    icon.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    assert icon.pixel_array.tolist() == [f.tolist() for f in FRAMES[:2]]


# ---------------------------------------------------------------------------
# T12: the JPEG walk still says "by their end markers"
# ---------------------------------------------------------------------------

def test_the_jpeg_walked_arm_still_says_end_markers(tmp_path):
    """A `.70` file of the same shape keeps #620's exact wording.

    Pins that the RLE arm did not capture the walked one: the two share a
    subject ("Pixel Data's fragments") and not a verb.
    """
    streams = [imagecodecs.ljpeg_encode(f.astype(np.uint8), bitspersample=8)
               for f in FRAMES]
    ds = _no_bot(LJPEG_SV1, streams, frames=2)
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert not got["failures"], got["failures"]
    _action, details, _scope = _signal_row(got["rows"])
    assert details == f"{JPEG_WALKED_WORDS}. Kept the first 2 and discarded 1."


# ---------------------------------------------------------------------------
# T13: fragments with no frame boundary at all name both counts
# ---------------------------------------------------------------------------

def _padded_j2k():
    """Three J2K codestreams whose last ten bytes hold no `FF D9`.

    pydicom's walk searches there for the end marker; with twelve zero
    bytes appended it finds none in any fragment, joins all three and
    yields one frame, and `as_array` then asks for frame 1. Pillow still
    decodes a padded codestream (measured), so the refusal reached here is
    the `StopIteration`, not a plugin failure.
    """
    return [imagecodecs.jpeg2k_encode(f, level=0, codecformat="J2K")
            + b"\0" * 12 for f in FRAMES]


def test_encapsulated_fragments_with_no_frame_boundary_name_both_counts(
        tmp_path):
    """Red on a8b6d3f: `Decompression Failed: StopIteration`, naming nothing."""
    ds = _no_bot(J2K_LOSSLESS, _padded_j2k(), frames=2)
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert len(got["failures"]) == 1, got["failures"]
    reason = got["failures"][0][1]
    assert reason == (
        "Decompression Failed: RuntimeError: Pixel Data holds 3 fragments "
        "with no offset table, and NumberOfFrames declares 2: pydicom found "
        "no frame boundary in them, so the declared frames cannot be read "
        "(caused by StopIteration)")


def test_one_rle_fragment_under_two_declared_frames_names_both_counts(
        tmp_path):
    """The §1.4.6 shortfall pydicom short-circuits: one fragment, NOF 2.

    pydicom's generator answers "one fragment must be one frame" before it
    consults NumberOfFrames, so this shape -- alone among shortfalls --
    reached the same nameless `StopIteration`. The counting arm is
    excess-only and does **not** answer here: a `fragments != declared`
    arm would report "hold 1 frames, one per fragment" instead of these
    words.
    """
    ds = _no_bot(RLE_LOSSLESS, [STREAMS[0]], frames=2)
    assert imagecodecs_handler.offset_table_frame_count(ds) is None
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert len(got["failures"]) == 1, got["failures"]
    assert got["failures"][0][1] == (
        "Decompression Failed: RuntimeError: Pixel Data holds 1 fragment "
        "with no offset table, and NumberOfFrames declares 2: pydicom found "
        "no frame boundary in them, so the declared frames cannot be read "
        "(caused by StopIteration)")


def test_two_rle_fragments_under_three_declared_frames_take_pydicoms_words(
        tmp_path):
    """Every other shortfall is already named by pydicom itself.

    One spelling per behaviour: the counting arm adds no second shortfall
    message beside this one. An arm on `fragments != declared` would
    replace it.
    """
    ds = _no_bot(RLE_LOSSLESS, STREAMS[:2], frames=3)
    assert imagecodecs_handler.offset_table_frame_count(ds) is None
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert len(got["failures"]) == 1, got["failures"]
    assert got["failures"][0][1] == (
        "Decompression Failed: ValueError: Unable to generate frames from "
        "the encapsulated pixel data as there are fewer fragments than "
        "frames; the dataset may be corrupt or the number of frames may be "
        "incorrect")


@pytest.mark.parametrize("table, refusal", [
    ("basic", "Basic Offset Table names 1 frames; NumberOfFrames declares 2"),
    ("extended",
     "Extended Offset Table names 1 frames; NumberOfFrames declares 2"),
], ids=["populated-bot", "extended-table"])
def test_only_a_file_with_no_offset_table_reaches_the_no_boundary_refusal(
        tmp_path, table, refusal):
    """What makes `_no_frame_boundary_words`' table clause true (rev-098j9 P1).

    That sentence says "with no offset table" unconditionally, and the
    review was right that the function does not check it. The check
    belongs here, because the reason is upstream: `offset_table_frame_
    count` refuses a file whose Basic or Extended Offset Table disagrees
    with NumberOfFrames *before* any decode, in its own words, so neither
    table shape can reach the `StopIteration` arm.

    It is not a theoretical guard. Calling `_decode_pixels` directly on
    the populated-table file -- bypassing this one -- does reach the arm
    and does produce the clause, so nothing about the decoder makes the
    sentence true; only the order of the two refusals does. If that order
    ever changes, this goes red and the clause must become conditional.

    Per-arm refusals, deliberately: one shared assertion would pass with
    either guard answering for both.
    """
    ds = _no_bot(RLE_LOSSLESS, [STREAMS[0]], frames=2)
    if table == "basic":
        ds.PixelData = encapsulate([STREAMS[0]], has_bot=True)
        ds["PixelData"].is_undefined_length = True
    else:
        ds.ExtendedOffsetTable = struct.pack("<Q", 0)
        ds.ExtendedOffsetTableLengths = struct.pack("<Q", len(STREAMS[0]))
    counted = imagecodecs_handler.offset_table_frame_count(ds)
    assert counted is not None and counted[0] == 1, counted
    assert counted[3] == refusal.split(" names")[0], counted
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    assert len(got["failures"]) == 1, got["failures"]
    assert got["failures"][0][1] == refusal
    assert "no offset table" not in got["failures"][0][1]


# ---------------------------------------------------------------------------
# The 64-byte header test, clause by clause
# ---------------------------------------------------------------------------

def _header(count, offsets, tail=b"\0" * 64):
    """A 64-byte RLE frame header, then `tail` as the compressed segment."""
    filled = list(offsets) + [0] * (15 - len(offsets))
    return struct.pack("<16I", count, *filled) + tail


VALID = _header(1, [64])


@pytest.mark.parametrize("fragment, want, why", [
    (VALID, True, "one segment at 64"),
    (_header(3, [64, 80, 96]), True, "three rising segments inside"),
    (_header(15, list(range(64, 64 + 15 * 2, 2))), True, "fifteen segments"),
    (VALID[:63], False, "shorter than the header"),
    (_header(0, [64]), False, "segment count 0"),
    (_header(16, [64] + list(range(80, 80 + 15 * 2, 2))[:14]), False,
     "segment count 16"),
    (_header(1, [0]), False, "first offset 0, not 64"),
    (_header(1, [65]), False, "first offset above 64"),
    (_header(2, [64, 64]), False, "offsets that do not rise"),
    (_header(2, [64, 60]), False, "offsets that fall"),
    (_header(2, [64, 200]), False, "an offset past the fragment"),
    (_header(1, [64], tail=b"") + b"", False, "an offset at the fragment end"),
    (struct.pack("<16I", 1, 64, 5, *([0] * 13)) + b"\0" * 64, False,
     "an unused offset that is not zero"),
], ids=["one", "three", "fifteen", "short", "count-0", "count-16",
        "first-0", "first-65", "flat", "falling", "past-end", "at-end",
        "unused-nonzero"])
def test_the_rle_header_test_reads_each_clause(fragment, want, why):
    """Each clause of PS3.5 G.3's header, one fixture apiece.

    The split-fragment file above trips the segment-count clause; these
    hold the rest, so no clause ships untested.
    """
    assert imagecodecs_handler._is_rle_frame_header(fragment) is want, why


def test_the_split_fragments_second_half_is_not_a_header():
    """The mechanism the regression guard above rests on, stated directly."""
    first, second = _split(RAMP_STREAM)
    assert imagecodecs_handler._is_rle_frame_header(first) is True
    assert imagecodecs_handler._is_rle_frame_header(second) is False


def test_a_split_frame_can_be_built_whose_both_halves_are_headers():
    """The heuristic's counter-example, measured (rev-098j9 P2).

    The test above is the mechanism the whole arm rests on, and it is a
    heuristic: nothing stops a frame's *segment data* from holding bytes
    that parse as a header. A 256-byte frame laid out
    header + data + header + data, split down the middle, has both halves
    pass -- so on such a file the arm reads one frame as two and pydicom's
    segment-table refusal turns a readable file into an `ERROR` row.

    Pinned here so the limit is a measured fact rather than a sentence in
    a docstring, and so a future clause added to `_is_rle_frame_header`
    that happened to exclude this shape would show up as a change here.
    No encoder is known to emit it: the layout needs a 64-byte first
    segment whose data begins with a legal offset table.
    """
    frame = _header(1, [64]) + _header(1, [64])
    assert len(frame) == 256
    first, second = frame[:128], frame[128:]
    assert imagecodecs_handler._is_rle_frame_header(first) is True
    assert imagecodecs_handler._is_rle_frame_header(second) is True


def test_every_excess_fixtures_fragments_are_headers():
    """Each fragment of each file the arm must fire on passes the test."""
    for ds in (_no_bot(RLE_LOSSLESS, STREAMS, frames=2),
               _no_bot(RLE_LOSSLESS, STREAMS, frames=1),
               _no_bot(RLE_LOSSLESS, STREAMS + [_rle(RAMP)], frames=2)):
        buf = BytesIO(ds.PixelData)
        parse_basic_offsets(buf)
        assert all(imagecodecs_handler._is_rle_frame_header(f)
                   for f in generate_fragments(buf))


def test_the_counter_names_the_rle_arm(tmp_path):
    """The tuple the row and the decode are both built from."""
    ds = _no_bot(RLE_LOSSLESS, STREAMS, frames=2)
    assert imagecodecs_handler.offset_table_frame_count(ds) == (
        3, 2, 2, imagecodecs_handler.RLE_FRAGMENTS)
    assert imagecodecs_handler.RLE_FRAGMENTS != \
        imagecodecs_handler.WALKED_FRAMES
    # A populated table still answers first: that is #418's case.
    full = dataset(RLE_LOSSLESS, list(STREAMS), rows=8, cols=8,
                   bits_allocated=8, frames=2)
    assert imagecodecs_handler.offset_table_frame_count(full) == (
        3, 2, 2, "Basic Offset Table")


def test_a_populated_table_keeps_its_own_words(tmp_path):
    """#418's row is untouched by the new arm."""
    ds = dataset(RLE_LOSSLESS, list(STREAMS), rows=8, cols=8,
                 bits_allocated=8, frames=2)
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    _action, details, _scope = _signal_row(got["rows"])
    assert details == ("Basic Offset Table names 3 frames; NumberOfFrames "
                       "declares 2. Kept the first 2 and discarded 1.")
    assert got["array"].tolist() == [f.tolist() for f in FRAMES[:2]]


def test_a_native_file_is_not_counted_by_fragments(tmp_path):
    """Nothing native reaches the arm: it is asked only of encapsulated data."""
    ds = dataset(ExplicitVRLittleEndian, rows=8, cols=8, bits_allocated=8,
                 frames=1, native=FRAMES[0].tobytes())
    assert imagecodecs_handler.offset_table_frame_count(ds) is None


def test_the_decode_reads_the_fragments_pydicom_would_have_joined():
    """The decode is told the count the row names, so they cannot drift."""
    ds = _no_bot(RLE_LOSSLESS, STREAMS, frames=2)
    joined, _meta = get_decoder(RLE_LOSSLESS).as_array(
        ds, number_of_frames=3)
    assert joined.shape == (3, 8, 8)
    assert joined.tolist() == [f.tolist() for f in FRAMES]

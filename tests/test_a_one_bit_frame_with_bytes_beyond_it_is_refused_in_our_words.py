"""A native 1-bit file pydicom cannot slice is refused in this library's words (#669).

A native (not encapsulated) BitsAllocated 1 frame is bit-packed: 3x3 is
nine bits, 1.125 bytes, and frames are not padded to a byte. pydicom
3.0.2 reads a Pixel Data element longer than its declared frames and one
pad byte as holding `len // frame_length` frames, and when that is more
than NumberOfFrames it decodes that many -- a count that is a `float`
here, which it then slices by. Measured on 067982ec, 3.12.14 and 3.14.7t,
27 of 60 cells:

    Decompression Failed: TypeError: slice indices must be integers or
    None or have an __index__ method

Those are CPython's words for a list index, about nothing in the file,
and a row is not to quote text an interpreter chose.

The file is still refused (owner ruling Q1 A; keeping the declared
frames is a follow-up). The refusal is raised at the top of
`io_handlers._decode_pixels`, so ingest, `Instance.get_pixel_data()`, the
export readback and an icon all say it, in a sentence of numbers:

    Pixel Data holds 6 bytes, room for 5 frames of 3x3 at one bit per
    sample, where NumberOfFrames is read as 2 and they need 3: a frame
    here is 9 bits, not a whole number of bytes, so the bytes beyond the
    declared frames cannot be read as frames.

**The predicate is pydicom's, not "more bytes than the frames and a
pad".** A 3x3 frame of three samples is 27 bits; five or six bytes under
NumberOfFrames 1 hold only one such frame, pydicom reads the file with
its excess-padding warning, and so does this library. Only where the
bytes hold more *frames* than declared does pydicom fail. Every cell
below is held to pydicom's own decoder, so the two cannot drift.

Names `isocenter.io_handlers`, `isocenter.imagecodecs_handler` and
`isocenter.entities`.
"""
import math
import os
import sqlite3

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.pixels import get_decoder
from pydicom.sequence import Sequence

from isocenter.entities import Instance
from isocenter.imagecodecs_handler import bit_packed_excess_words
from isocenter.io_handlers import _decode_pixels, ingest_worker
from isocenter.session import DicomSession
from support.decode_doors import EXPLICIT_LE, SOP_CLASS, dataset, write

IMPLICIT_LE = "1.2.840.10008.1.2"
EXPLICIT_BE = "1.2.840.10008.1.2.2"

#: CPython's words on 3.12.14 and 3.14.7t; asserted only of pydicom's own
#: decoder, as the control, and never of anything this library says.
CPYTHON_WORDS = "slice indices must be integers"


def _file(rows, cols, frames, have, *, samples=1, ts=EXPLICIT_LE,
          bits_allocated=1):
    return dataset(ts, rows=rows, cols=cols, samples=samples,
                   bits_allocated=bits_allocated, bits_stored=bits_allocated,
                   frames=frames, native=bytes([0b10101010]) * have,
                   photometric="MONOCHROME2" if samples == 1 else "RGB")


def _pydicom(ds):
    """pydicom's own decode: the array's shape, or the exception."""
    try:
        return get_decoder(ds.file_meta.TransferSyntaxUID).as_array(ds)[0].shape
    except Exception as exc:  # pylint: disable=broad-except
        return exc


def _sentence(rows, cols, frames, have, samples=1):
    """The refusal, by arithmetic written out here and not imported."""
    bits = rows * cols * samples
    need = math.ceil(bits * frames / 8)
    room = have * 8 // bits
    of = (f"{rows}x{cols}" if samples == 1
          else f"{rows}x{cols} with {samples} samples per pixel")
    return (f"Pixel Data holds {have} bytes, room for {room} frames of {of} "
            f"at one bit per sample, where NumberOfFrames is read as "
            f"{frames} and {'it needs' if frames == 1 else 'they need'} "
            f"{need}: a frame here is {bits} bit{'' if bits == 1 else 's'}, "
            f"not a whole number of bytes, so the bytes beyond the declared "
            f"frames cannot be read as frames.")


#: The 60 measured cells (six geometries, NumberOfFrames 1 and 2, zero to
#: four bytes beyond the frames), as `(rows, cols, frames, have)`. `have`
#: is what is set; **a file holds `have` padded to an even length**
#: (PS3.5 7.1.1, and pydicom pads on write), and that is the length every
#: door reads, so that is the length judged: `_held`.
CELLS = [(rows, cols, frames, math.ceil(rows * cols * frames / 8) + extra)
         for rows, cols in ((3, 3), (2, 2), (5, 3), (3, 1), (4, 4), (4, 2))
         for frames in (1, 2)
         for extra in range(5)]


def _held(have):
    return have + have % 2


def _is_refused(rows, cols, frames, held, samples=1):
    """pydicom's rule, read from `DecodeRunner._validate_buffer`."""
    bits = rows * cols * samples
    need = math.ceil(bits * frames / 8)
    return (bits % 8 != 0 and held > need + need % 2
            and held * 8 // bits > frames)


REFUSED = [cell for cell in CELLS if _is_refused(*cell[:3], _held(cell[3]))]
READ = [cell for cell in CELLS if cell not in REFUSED]


def test_the_cells_split_as_measured():
    """27 refused and 33 read: the count of `TypeError` rows on 067982ec."""
    assert (len(REFUSED), len(READ)) == (27, 33)


# ---------------------------------------------------------------------------
# The 27 cells: one sentence at every door
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rows, cols, frames, have", REFUSED)
def test_a_cell_pydicom_cannot_slice_is_refused_in_one_sentence(
        tmp_path, rows, cols, frames, have):
    """Red on 067982ec at all three doors, in CPython's words."""
    ds = _file(rows, cols, frames, have)
    path = write(tmp_path, ds)
    on_disk = pydicom.dcmread(path)
    assert len(on_disk.PixelData) == _held(have)
    theirs = _pydicom(on_disk)
    assert isinstance(theirs, TypeError) and CPYTHON_WORDS in str(theirs), \
        "the control: pydicom alone still fails this cell that way"
    want = _sentence(rows, cols, frames, _held(have))

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(pydicom.dcmread(path))
    assert type(caught.value) is RuntimeError
    assert str(caught.value) == want

    assert ingest_worker(path)[-1] == (
        f"Decompression Failed: RuntimeError: {want}")

    inst = Instance(ds.SOPInstanceUID, SOP_CLASS, 1, file_path=path)
    with pytest.raises(RuntimeError) as read:
        inst.get_pixel_data()
    assert str(read.value) == (
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"RuntimeError: {want}")


def test_the_sentence_whole_for_two_cells():
    """Literals, so the arithmetic in `_sentence` is not the only witness."""
    assert bit_packed_excess_words(_file(3, 3, 2, 6)) == (
        "Pixel Data holds 6 bytes, room for 5 frames of 3x3 at one bit per "
        "sample, where NumberOfFrames is read as 2 and they need 3: a frame "
        "here is 9 bits, not a whole number of bytes, so the bytes beyond "
        "the declared frames cannot be read as frames.")
    assert bit_packed_excess_words(_file(2, 2, 1, 3)) == (
        "Pixel Data holds 3 bytes, room for 6 frames of 2x2 at one bit per "
        "sample, where NumberOfFrames is read as 1 and it needs 1: a frame "
        "here is 4 bits, not a whole number of bytes, so the bytes beyond "
        "the declared frames cannot be read as frames.")


@pytest.mark.parametrize("rows, cols, frames, have", REFUSED[:6])
def test_the_refusal_quotes_no_interpreter_and_trips_no_substring(
        rows, cols, frames, have):
    """Numbers only, and not the words the read door turns into `None`.

    `Instance.get_pixel_data` reads a failure whose message holds "no
    pixel data" as a file with no pixels and returns `None`.
    """
    words = bit_packed_excess_words(_file(rows, cols, frames, _held(have)))

    assert "TypeError" not in words and "slice indices" not in words
    assert "__index__" not in words
    assert "no pixel data" not in words.lower()
    assert "/" not in words and "\\" not in words


# ---------------------------------------------------------------------------
# The 33 cells: unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rows, cols, frames, have", READ)
def test_a_cell_pydicom_reads_is_read_as_before(tmp_path, rows, cols, frames,
                                                have):
    """Read; a whole-byte frame's excess keeps its `DATA_LOSS` facts."""
    path = write(tmp_path, _file(rows, cols, frames, have))
    on_disk = pydicom.dcmread(path)
    held = len(on_disk.PixelData)
    assert held == _held(have)
    theirs = _pydicom(on_disk)
    assert isinstance(theirs, tuple), theirs
    assert bit_packed_excess_words(on_disk) is None

    result = ingest_worker(path)

    assert result[-1] is None, result[-1]
    bits = rows * cols
    need = bits * frames // 8
    excess = result[0].get("offset_table_excess")
    if (bits % 8 == 0 and held > need + need % 2
            and held // (bits // 8) > frames):
        assert excess[:2] == (held // (bits // 8), frames), excess
        assert excess[3] == "Pixel Data's length"
    else:
        assert excess is None


# ---------------------------------------------------------------------------
# Beyond the 60: the predicate is pydicom's
# ---------------------------------------------------------------------------

#: `(rows, cols, frames, have, samples, ts)`. 27-bit and 100-bit frames
#: hold bytes beyond one frame and its pad that are not a second frame;
#: the other two native syntaxes take the same route.
BEYOND = [
    (3, 3, 1, have, 3, EXPLICIT_LE) for have in range(4, 10)
] + [
    (10, 10, 1, have, 1, EXPLICIT_LE) for have in (13, 14, 15, 24, 25, 26)
] + [
    (10, 10, 2, have, 1, EXPLICIT_LE) for have in (25, 26, 27, 37, 38)
] + [
    (3, 3, 2, have, 1, ts) for have in (3, 4, 5, 6)
    for ts in (IMPLICIT_LE, EXPLICIT_BE)
]


@pytest.mark.parametrize("rows, cols, frames, have, samples, ts", BEYOND)
def test_the_refusal_is_exactly_where_pydicom_cannot_slice(
        rows, cols, frames, have, samples, ts):
    """The helper answers where, and only where, pydicom's decoder fails so.

    `(3, 3, 1, 5, 3)` is the cell a "more bytes than the frames and a
    pad" predicate gets wrong: five bytes hold one 27-bit frame and
    pydicom reads it.
    """
    ds = _file(rows, cols, frames, have, samples=samples, ts=ts)
    theirs = _pydicom(ds)
    words = bit_packed_excess_words(ds)

    if isinstance(theirs, TypeError):
        assert CPYTHON_WORDS in str(theirs)
        assert words == _sentence(rows, cols, frames, have, samples)
        with pytest.raises(RuntimeError) as caught:
            _decode_pixels(ds)
        assert str(caught.value) == words
    else:
        assert isinstance(theirs, tuple), theirs
        assert words is None
        assert _decode_pixels(ds)[0].shape == theirs
    assert (words is not None) == _is_refused(rows, cols, frames, have,
                                              samples)


def test_the_three_by_three_colour_cell_is_read():
    """The named counter-example, by itself and by literal."""
    ds = _file(3, 3, 1, 5, samples=3)

    assert bit_packed_excess_words(ds) is None
    assert _decode_pixels(ds)[0].shape == (3, 3, 3)


@pytest.mark.parametrize("number_of_frames, read_as", [
    ("absent", 1), (0, 1)], ids=["absent", "zero"])
def test_an_absent_or_zero_number_of_frames_is_read_as_one(number_of_frames,
                                                           read_as):
    """pydicom reads both as one frame, and fails to slice the same way."""
    ds = _file(3, 3, None if number_of_frames == "absent" else 0, 4)
    theirs = _pydicom(ds)
    assert isinstance(theirs, TypeError) and CPYTHON_WORDS in str(theirs)

    assert bit_packed_excess_words(ds) == _sentence(3, 3, read_as, 4)


@pytest.mark.parametrize("number_of_frames, pydicom_says", [
    (-1, "must be greater than or equal to 1"),
    ("", "invalid literal for int()"),
], ids=["negative", "empty"])
def test_a_number_of_frames_pydicom_refuses_is_left_to_pydicom(
        number_of_frames, pydicom_says):
    """Its `ValueError` names the element; this sentence would hide it."""
    ds = _file(3, 3, 2, 6)
    ds.NumberOfFrames = number_of_frames

    assert bit_packed_excess_words(ds) is None
    with pytest.raises(ValueError) as caught:
        _decode_pixels(ds)
    assert pydicom_says in str(caught.value)


def test_a_one_by_one_frame_is_one_bit(tmp_path):
    """Singular, from a file: four bytes under one 1x1 frame."""
    path = write(tmp_path, _file(1, 1, 1, 4))

    assert ingest_worker(path)[-1] == (
        "Decompression Failed: RuntimeError: Pixel Data holds 4 bytes, room "
        "for 32 frames of 1x1 at one bit per sample, where NumberOfFrames "
        "is read as 1 and it needs 1: a frame here is 1 bit, not a whole "
        "number of bytes, so the bytes beyond the declared frames cannot be "
        "read as frames.")


def _ybr_422(rows, cols, frames, have):
    ds = _file(rows, cols, frames, have, samples=3)
    ds.PhotometricInterpretation = "YBR_FULL_422"
    return ds


def test_a_ybr_full_422_file_keeps_pydicoms_own_refusal(tmp_path):
    """pydicom never slices this one by a fraction: it names (0028,0004).

    Its excess-frames arithmetic is the `elif` of its `YBR_FULL_422`
    test (`DecodeRunner._validate_buffer`, 3.0.2), so a buffer a third
    larger than expected is refused in a `ValueError` about the label.
    That row is `main`'s, whole. At 32696465 this file got the 1-bit
    sentence instead (review of #1062): the same geometry under RGB,
    below, is the one that sentence is for.
    """
    ds = _ybr_422(3, 3, 4, 18)
    path = write(tmp_path, ds)
    theirs = _pydicom(pydicom.dcmread(path))
    assert isinstance(theirs, ValueError), theirs

    assert bit_packed_excess_words(pydicom.dcmread(path)) is None
    assert ingest_worker(path)[-1] == (
        "Decompression Failed: ValueError: The number of bytes of pixel "
        "data is a third larger than expected (18 vs 8 bytes) which "
        "indicates the set (0028,0004) 'Photometric Interpretation' value "
        "of 'YBR_FULL_422' is incorrect and may need to be changed to "
        "either 'RGB' or 'YBR_FULL'")

    rgb = _file(3, 3, 4, 18, samples=3)
    assert isinstance(_pydicom(rgb), TypeError)
    assert bit_packed_excess_words(rgb) == _sentence(3, 3, 4, 18, samples=3)


@pytest.mark.parametrize("rows, cols, frames, have", [
    (3, 3, 1, 4), (3, 3, 1, 6), (3, 3, 1, 12), (3, 3, 2, 8), (3, 3, 2, 14),
    (5, 3, 1, 8), (5, 3, 1, 16), (2, 2, 1, 4), (2, 2, 2, 6), (2, 2, 2, 12)])
def test_under_ybr_full_422_the_helper_never_answers(rows, cols, frames, have):
    """Whatever pydicom makes of the cell, it is not the slice `TypeError`."""
    ds = _ybr_422(rows, cols, frames, have)
    theirs = _pydicom(ds)
    assert not (isinstance(theirs, TypeError)
                and CPYTHON_WORDS in str(theirs)), theirs

    assert bit_packed_excess_words(ds) is None
    if isinstance(theirs, Exception):
        with pytest.raises(type(theirs)) as caught:
            _decode_pixels(ds)
        assert str(caught.value) == str(theirs)
    else:
        assert _decode_pixels(ds)[0].shape == theirs


def test_a_short_buffer_is_left_to_pydicom():
    ds = _file(3, 3, 2, 2)

    assert bit_packed_excess_words(ds) is None
    with pytest.raises(ValueError) as caught:
        _decode_pixels(ds)
    assert "less than expected (2 vs 4 bytes)" in str(caught.value)


# ---------------------------------------------------------------------------
# What the helper is never asked to judge
# ---------------------------------------------------------------------------

def test_an_eight_bit_frame_is_not_this_refusals():
    """BitsAllocated 8 with bytes beyond its frames: not bit-packed."""
    ds = _file(3, 3, 2, 40, bits_allocated=8)

    assert bit_packed_excess_words(ds) is None


@pytest.mark.parametrize("keyword", [
    "Rows", "Columns", "SamplesPerPixel", "BitsAllocated", "PixelData"])
def test_a_dataset_missing_an_element_is_the_decoders_to_refuse(keyword):
    """Never raises: anything it cannot read, the decode that follows refuses."""
    ds = _file(3, 3, 2, 6)
    delattr(ds, keyword)

    assert bit_packed_excess_words(ds) is None


def test_a_dataset_with_no_file_meta_or_no_rows_at_all_is_none():
    assert bit_packed_excess_words(Dataset()) is None
    ds = _file(3, 3, 2, 6)
    ds.Rows = 0
    assert bit_packed_excess_words(ds) is None


def test_an_encapsulated_one_bit_frame_is_not_this_refusals():
    """Encapsulated frames are padded to a byte; pydicom's length is whole."""
    ds = dataset("1.2.840.10008.1.2.5", [b"\xaa" * 6], rows=3, cols=3,
                 bits_allocated=1, bits_stored=1, frames=2)

    assert bit_packed_excess_words(ds) is None


# ---------------------------------------------------------------------------
# An icon: the icon is dropped, never the file
# ---------------------------------------------------------------------------

def test_a_one_bit_icon_with_bytes_beyond_it_costs_the_icon_not_the_file(
        tmp_path):
    """`_decode_nested_pixels` turns any refusal into a loss row for the icon."""
    ds = _file(4, 4, None, 16, bits_allocated=8)
    icon = Dataset()
    icon.Rows, icon.Columns, icon.SamplesPerPixel = 3, 3, 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.BitsAllocated = icon.BitsStored = 1
    icon.HighBit, icon.PixelRepresentation = 0, 0
    icon.NumberOfFrames = 2
    icon.add_new(0x7FE00010, "OB", b"\xaa" * 6)
    ds.IconImageSequence = Sequence([icon])
    assert bit_packed_excess_words(
        _file(3, 3, 2, 6)) is not None, "the icon's own shape is a refused one"
    path = write(tmp_path, ds)
    db = str(tmp_path / "s.db")

    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        instances = [i for p in session.store.patients for st in p.studies
                     for se in st.series for i in se.instances]

    assert (summary.ingested, list(summary.failures)) == (1, [])
    assert len(instances) == 1
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT action_type, details FROM audit_log WHERE action_type "
            "IN ('DATA_LOSS', 'ERROR')").fetchall()
    assert [action for action, _details in rows] == ["DATA_LOSS"], rows
    assert "7fe0,0010" in rows[0][1].lower(), rows

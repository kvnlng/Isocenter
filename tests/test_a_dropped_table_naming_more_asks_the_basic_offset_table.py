""""With no offset table" is said only where there is none (#1067).

**The count.** A dropped Extended Offset Table (its Lengths element is
bytes of another length, which pydicom ignores with a warning) that names
*more* frames than declared is counted from the table, so that under
NumberOfFrames 1 the frame pydicom's walk would lose in silence gets its
row (#621). Over a populated Basic Offset Table, pydicom does not walk:
it reads by the Basic Offset Table, and when that names *fewer* frames
than declared the decode runs out. Measured on 3e4ee91a (3.12.14 and
3.14.7t), NumberOfFrames 2, one fragment, a Basic Offset Table naming 1,
an Extended Offset Table of three offsets beside 4 bytes of Lengths:

    Pixel Data holds 1 fragment with no offset table, and NumberOfFrames
    declares 2: pydicom found no frame boundary in them, so the declared
    frames cannot be read (caused by StopIteration)

The file holds two tables. Such a file is now counted by the Basic
Offset Table pydicom walks by, and refused in the count's words:

    Basic Offset Table names 1 frame; NumberOfFrames declares 2

Only that shape moves: over an empty Basic Offset Table the dropped
table's count stands (the #621 guard), and over a Basic Offset Table
naming as many as declared, or more, the existing rows stand.

**The clause.** `_no_frame_boundary_words` builds its clause from what the
file holds: an Extended Offset Table pydicom walks by, or a populated
Basic Offset Table, is named with its count, and "pydicom found no frame
boundary" is said only where no table is walked (a walked table runs
out; that is not a missing boundary). No door reaches the walked cases:
each refuses on the count first. A direct `_decode_pixels` call does.

Names `isocenter.imagecodecs_handler` and `isocenter.io_handlers`.
"""
import struct

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.encaps import encapsulate

from isocenter.imagecodecs_handler import (extended_offsets,
                                           offset_table_frame_count)
from isocenter.io_handlers import _decode_pixels, ingest_worker
from support.decode_doors import J2K_LOSSLESS, dataset, write

STREAMS = [imagecodecs.jpeg2k_encode(
    (np.arange(64, dtype=np.uint8) + 60 * i).reshape(8, 8), level=0,
    codecformat="J2K") for i in range(3)]


def _file(tmp_path, fragments, declared, *, bot, table=None, lengths=None,
          name="one"):
    """`fragments` J2K frames; `bot` "empty", "true" or "one" (a single 0).

    `table` and `lengths` are byte counts of the two Extended Offset
    Table elements, `None` for absent.
    """
    streams = STREAMS[:fragments]
    ds = dataset(J2K_LOSSLESS, streams, rows=8, cols=8, bits_allocated=8,
                 frames=declared)
    data = encapsulate(streams, has_bot=bot == "true")
    if bot == "one":
        data = b"\xfe\xff\x00\xe0\x04\x00\x00\x00" + b"\0" * 4 + data[8:]
    ds.PixelData = data
    ds["PixelData"].is_undefined_length = True
    if table is not None:
        ds.add_new(0x7FE00001, "OV", struct.pack("<3Q", 0, 1, 2)[:table])
    if lengths is not None:
        true = b"".join(len(s).to_bytes(8, "little") for s in STREAMS)
        ds.add_new(0x7FE00002, "OV", true[:lengths])
    path = write(tmp_path, ds, name)
    return path, pydicom.dcmread(path)


def test_the_measured_cell_is_counted_by_the_basic_offset_table(tmp_path):
    """Red while the dropped table's count answers first."""
    path, ds = _file(tmp_path, 1, 2, bot="one", table=24, lengths=4)
    assert extended_offsets(ds) is None

    assert offset_table_frame_count(ds) == (1, 2, 2, "Basic Offset Table")
    assert ingest_worker(path)[-1] == (
        "Basic Offset Table names 1 frame; NumberOfFrames declares 2")
    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == (
        "Pixel Data holds 1 fragment with a Basic Offset Table naming 1 "
        "frame, and NumberOfFrames declares 2: the declared frames cannot "
        "be read")


def test_over_an_empty_basic_offset_table_the_dropped_table_still_counts(
        tmp_path):
    """The #621 guard: under NumberOfFrames 1 the walk would lose frame 1.

    Red if the fall-through ignores whether the Basic Offset Table is
    empty.
    """
    path, ds = _file(tmp_path, 2, 1, bot="empty", table=16, lengths=4)
    assert offset_table_frame_count(ds) == (2, 1, 1, "Extended Offset Table")
    meta = ingest_worker(path)[0]
    assert meta["offset_table_excess"] == (2, 1, 1, "Extended Offset Table")


@pytest.mark.parametrize("bot", ["true"])
def test_a_basic_offset_table_naming_as_many_or_more_is_not_asked(
        tmp_path, bot):
    """Not widened: a graded row would move (the brief's §3.5)."""
    path, ds = _file(tmp_path, 2, 2, bot=bot, table=24, lengths=4)
    assert offset_table_frame_count(ds) == (3, 2, 2, "Extended Offset Table")


# ---------------------------------------------------------------------------
# The clause, at a direct call of the decode
# ---------------------------------------------------------------------------

def _refusal(ds):
    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    return str(caught.value)


TAIL = ", and NumberOfFrames declares 2"
NO_BOUNDARY = (TAIL + ": pydicom found no frame boundary in them, so the "
               "declared frames cannot be read")
RUNS_OUT = TAIL + ": the declared frames cannot be read"


def test_the_no_boundary_clause_is_true_of_every_table(tmp_path):
    """Each of the four table states, one fragment, NumberOfFrames 2."""
    # An Extended Offset Table pydicom walks by, naming 1.
    _p, ds = _file(tmp_path, 1, 2, bot="empty", table=8, lengths=8,
                   name="walked")
    assert extended_offsets(ds) is not None
    assert _refusal(ds) == ("Pixel Data holds 1 fragment with an Extended "
                            "Offset Table naming 1 frame" + RUNS_OUT)

    # A populated Basic Offset Table, naming 1.
    _p, ds = _file(tmp_path, 1, 2, bot="one", name="basic")
    assert _refusal(ds) == ("Pixel Data holds 1 fragment with a Basic Offset "
                            "Table naming 1 frame" + RUNS_OUT)

    # An Extended Offset Table pydicom drops, over an empty Basic one.
    _p, ds = _file(tmp_path, 1, 2, bot="empty", table=8, lengths=4,
                   name="dropped")
    assert _refusal(ds) == ("Pixel Data holds 1 fragment with no offset "
                            "table pydicom walks by" + NO_BOUNDARY)

    # Neither.
    _p, ds = _file(tmp_path, 1, 2, bot="empty", name="none")
    assert _refusal(ds) == ("Pixel Data holds 1 fragment with no offset "
                            "table" + NO_BOUNDARY)

"""A count of one frame is "1 frame" in every mismatch sentence (#1087).

`frame_count_mismatch_words` spells the count four ways, one per thing
that counted it: a table "names", pydicom's walk "hold ... by their end
markers", an RLE buffer "hold ... one per fragment", and a native
element's length "holds ... whole frames". Measured on 3e4ee91a (3.12.14
and 3.14.7t), an Extended Offset Table of one offset under
NumberOfFrames 2 was refused in

    Extended Offset Table names 1 frames; NumberOfFrames declares 2

Now `names 1 frame`. The ingest excess row's `Kept the first N and
discarded M.` is not this sentence and is unchanged. Names
`isocenter.imagecodecs_handler` and `isocenter.io_handlers`.
"""
import struct

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.encaps import encapsulate

from isocenter.imagecodecs_handler import (NATIVE_FRAMES, RLE_FRAGMENTS,
                                           WALKED_FRAMES,
                                           frame_count_mismatch_words)
from isocenter.io_handlers import ingest_worker
from support.decode_doors import J2K_LOSSLESS, dataset, write


@pytest.mark.parametrize("table_name, one, two", [
    ("Basic Offset Table", "Basic Offset Table names 1 frame",
     "Basic Offset Table names 2 frames"),
    ("Extended Offset Table", "Extended Offset Table names 1 frame",
     "Extended Offset Table names 2 frames"),
    (WALKED_FRAMES, "Pixel Data's fragments hold 1 frame by their end markers",
     "Pixel Data's fragments hold 2 frames by their end markers"),
    (RLE_FRAGMENTS, "Pixel Data's fragments hold 1 frame, one per fragment",
     "Pixel Data's fragments hold 2 frames, one per fragment"),
    (NATIVE_FRAMES, "Pixel Data's length holds 1 whole frame",
     "Pixel Data's length holds 2 whole frames"),
])
def test_each_subject_agrees_with_its_count(table_name, one, two):
    """Red under the old `{n} frames` for a count of 1; 2 is unchanged."""
    assert frame_count_mismatch_words((1, 3, 3, table_name)) == (
        f"{one}; NumberOfFrames declares 3")
    assert frame_count_mismatch_words((2, 3, 3, table_name)) == (
        f"{two}; NumberOfFrames declares 3")


def test_the_measured_file_is_refused_in_the_singular(tmp_path):
    """The issue's file: an Extended Offset Table of one offset, two declared."""
    frames = [imagecodecs.jpeg2k_encode(
        np.full((8, 8), 10 * i, np.uint8), level=0, codecformat="J2K")
        for i in range(2)]
    ds = dataset(J2K_LOSSLESS, frames, rows=8, cols=8, bits_allocated=8,
                 frames=2)
    ds.PixelData = encapsulate(frames, has_bot=False)
    ds["PixelData"].is_undefined_length = True
    ds.add_new(0x7FE00001, "OV", struct.pack("<Q", 0))
    ds.add_new(0x7FE00002, "OV", struct.pack("<Q", len(frames[0])))
    path = write(tmp_path, ds)
    assert pydicom.dcmread(path).NumberOfFrames == 2

    assert ingest_worker(path)[-1] == (
        "Extended Offset Table names 1 frame; NumberOfFrames declares 2")

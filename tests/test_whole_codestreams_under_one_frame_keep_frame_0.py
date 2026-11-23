"""Whole codestreams under one declared frame keep frame 0, with a row (#1088).

An encapsulated Pixel Data whose fragments are each a whole codestream
-- start marker to end marker -- under NumberOfFrames 1 or absent, with
no offset table, holds one image per fragment. pydicom does not walk the
fragments under one declared frame: it joins them into one buffer and
the decoder reads the first stream, so the other images were lost with
no row and the grade was PASS. Measured on every marker-delimited syntax
(T.81 Baseline, Extended and Lossless, JPEG-LS, JPEG 2000, HTJ2K) on
3.12.14 and 3.14.7t.

`imagecodecs_handler._whole_stream_fragments` now counts such fragments,
and every door treats the count as every other excess: ingest keeps
frame 0 with a `DATA_LOSS` row, an icon keeps its frame 0 with its own
row, and `Instance.get_pixel_data()` on the source file refuses in the
count's words (owner ruling 2026-11-22, Q2 and Q3: every marker-delimited
syntax, a test per syntax).

A frame split across fragments is still one frame: its first fragment
has no end marker and its later fragments no start marker, and the
count asks both of every fragment.

Names `isocenter.io_handlers`, `isocenter.imagecodecs_handler` and
`isocenter.entities`.
"""
import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.encaps import encapsulate
from pydicom.sequence import Sequence

from isocenter.imagecodecs_handler import (WholeStreams,
                                           frame_count_mismatch_words,
                                           offset_table_frame_count)
from isocenter.io_handlers import _decode_pixels, ingest_worker
from support.decode_doors import (at_ingest, at_instance, dataset,
                                  through_the_fallback, write)

FRAME_0 = (np.arange(16).reshape(4, 4) * 13 % 256).astype(np.uint8)
FRAME_1 = ((np.arange(16).reshape(4, 4) * 13 + 100) % 256).astype(np.uint8)

#: Every marker-delimited syntax, its encoder, and the stream's name in
#: the row. Each encoder writes a whole stream: SOI to EOI for T.81 and
#: JPEG-LS, SOC to EOC for a raw JPEG 2000 or HTJ2K codestream.
SYNTAXES = {
    "1.2.840.10008.1.2.4.50": (lambda a: imagecodecs.jpeg8_encode(a, level=95),
                               "JPEG stream"),
    "1.2.840.10008.1.2.4.51": (lambda a: imagecodecs.jpeg8_encode(a, level=95),
                               "JPEG stream"),
    "1.2.840.10008.1.2.4.57": (imagecodecs.ljpeg_encode, "JPEG stream"),
    "1.2.840.10008.1.2.4.70": (imagecodecs.ljpeg_encode, "JPEG stream"),
    "1.2.840.10008.1.2.4.80": (imagecodecs.jpegls_encode, "JPEG-LS stream"),
    "1.2.840.10008.1.2.4.81": (lambda a: imagecodecs.jpegls_encode(a, level=2),
                               "JPEG-LS stream"),
    "1.2.840.10008.1.2.4.90": (
        lambda a: imagecodecs.jpeg2k_encode(a, codecformat="J2K", level=0),
        "JPEG 2000 codestream"),
    "1.2.840.10008.1.2.4.91": (
        lambda a: imagecodecs.jpeg2k_encode(a, codecformat="J2K", level=0),
        "JPEG 2000 codestream"),
    "1.2.840.10008.1.2.4.201": (lambda a: imagecodecs.htj2k_encode(a, level=0),
                                "JPEG 2000 codestream"),
    "1.2.840.10008.1.2.4.202": (lambda a: imagecodecs.htj2k_encode(a, level=0),
                                "JPEG 2000 codestream"),
    "1.2.840.10008.1.2.4.203": (lambda a: imagecodecs.htj2k_encode(a, level=0),
                                "JPEG 2000 codestream"),
}


def _file(ts, fragments, frames=1):
    ds = dataset(ts, [b"\0"], rows=4, cols=4, bits_allocated=8, frames=frames)
    ds.PixelData = encapsulate(list(fragments), has_bot=False)
    ds["PixelData"].is_undefined_length = True
    return ds


def _streams(ts):
    encode, _name = SYNTAXES[ts]
    return encode(FRAME_0), encode(FRAME_1)


def _frame_0_alone(tmp_path, ts):
    """What the decode makes of frame 0's stream as the file's one fragment."""
    one = _file(ts, _streams(ts)[:1])
    return _decode_pixels(pydicom.dcmread(write(tmp_path, one, "alone")))[0]


@pytest.mark.parametrize("ts", sorted(SYNTAXES))
@pytest.mark.parametrize("frames, words", [
    (1, "NumberOfFrames declares 1"),
    (None, "NumberOfFrames is absent (read as 1)")])
def test_ingest_keeps_frame_0_with_a_row_under_every_syntax(tmp_path, ts,
                                                            frames, words):
    """Red if the count is missing for any marker-delimited syntax."""
    path = write(tmp_path, _file(ts, _streams(ts), frames))
    ds = pydicom.dcmread(path)
    counted = offset_table_frame_count(ds)
    name = SYNTAXES[ts][1]
    assert counted == (2, 1, frames, WholeStreams(name))
    assert frame_count_mismatch_words(counted) == (
        f"Pixel Data's fragments hold 2 frames, one whole {name} per "
        f"fragment; {words}")

    result = ingest_worker(path)

    assert result[-1] is None, result[-1]
    assert result[0]["offset_table_excess"] == counted
    want = _frame_0_alone(tmp_path, ts)
    assert want.shape == (4, 4)
    assert result[2] == np.ascontiguousarray(want).tobytes()


def test_a_session_ingest_writes_the_data_loss_row(tmp_path):
    ts = "1.2.840.10008.1.2.4.90"
    path = write(tmp_path, _file(ts, _streams(ts)))
    got = at_ingest(tmp_path, path)

    assert got["failure"] is None
    assert got["array"].shape == (4, 4)
    assert np.array_equal(got["array"], FRAME_0)
    assert ("DATA_LOSS", got["uid"],
            "Pixel Data's fragments hold 2 frames, one whole JPEG 2000 "
            "codestream per fragment; NumberOfFrames declares 1. Kept the "
            "first 1 and discarded 1.") in got["rows"]


@pytest.mark.parametrize("ts", ["1.2.840.10008.1.2.4.90",
                                "1.2.840.10008.1.2.4.50"])
def test_the_fallback_route_reads_frame_0(tmp_path, ts, monkeypatch):
    """The codec is handed frame 0's stream alone. Red if the fallback
    walks the joined buffer: every decoder installed here reads the first
    stream of a joined buffer and ignores the rest, so the array alone
    cannot tell, and the bytes handed to the codec are asserted."""
    from isocenter import imagecodecs_handler
    real, handed = imagecodecs_handler._decode_frame, []

    def spy(transfer_syntax, bitstream, ds, beyond=None):
        handed.append(bytes(bitstream))
        return real(transfer_syntax, bitstream, ds, beyond)
    monkeypatch.setattr(imagecodecs_handler, "_decode_frame", spy)
    first = _streams(ts)[0]
    path = write(tmp_path, _file(ts, _streams(ts)))
    arr, _label = through_the_fallback(pydicom.dcmread(path),
                                       allow_excess_frames=False,
                                       number_of_frames=2)
    assert [h.rstrip(b"\0") for h in handed] == [first]
    assert arr.shape == (4, 4)
    assert np.array_equal(arr, _frame_0_alone(tmp_path, ts))


def test_the_fallback_route_keeps_only_frame_0_without_the_count(tmp_path):
    """The fallback counts for itself: `allow_excess_frames=False` alone."""
    ts = "1.2.840.10008.1.2.4.90"
    path = write(tmp_path, _file(ts, _streams(ts)))
    arr, _label = through_the_fallback(pydicom.dcmread(path),
                                       allow_excess_frames=False)
    assert np.array_equal(arr, FRAME_0)


def test_a_direct_decode_keeping_every_frame_is_refused_on_the_fallback(tmp_path):
    ts = "1.2.840.10008.1.2.4.90"
    path = write(tmp_path, _file(ts, _streams(ts)))
    with pytest.raises(RuntimeError, match=(
            "one whole JPEG 2000 codestream per fragment; NumberOfFrames "
            "declares 1, and this decode was not asked to drop the excess")):
        through_the_fallback(pydicom.dcmread(path))


def test_the_read_door_refuses_in_the_counts_words(tmp_path):
    """`Breaking`: this returned frame 0."""
    ts = "1.2.840.10008.1.2.4.90"
    path = write(tmp_path, _file(ts, _streams(ts)))
    got = at_instance(path)
    assert isinstance(got, Exception)
    assert ("Pixel Data's fragments hold 2 frames, one whole JPEG 2000 "
            "codestream per fragment; NumberOfFrames declares 1") in str(got)


def test_a_frame_split_across_fragments_is_one_frame(tmp_path):
    ts = "1.2.840.10008.1.2.4.90"
    stream = _streams(ts)[0]
    half = len(stream) // 2 + (len(stream) // 2) % 2
    path = write(tmp_path, _file(ts, (stream[:half], stream[half:])))
    ds = pydicom.dcmread(path)
    assert offset_table_frame_count(ds) is None
    result = ingest_worker(path)
    assert result[-1] is None and "offset_table_excess" not in result[0]
    assert result[2] == FRAME_0.tobytes()


def test_one_fragment_without_its_end_marker_is_not_counted(tmp_path):
    """Red if only the start marker is asked: both fragments begin a
    codestream, and the second has no end marker."""
    ts = "1.2.840.10008.1.2.4.90"
    first, second = _streams(ts)
    assert second.endswith(b"\xff\xd9")
    ds = _file(ts, (first, second[:-2] + b"\0\0"))
    assert offset_table_frame_count(ds) is None


def test_one_fragment_without_its_start_marker_is_not_counted(tmp_path):
    """Red if only the end marker is asked: both fragments end with one,
    and the second begins no codestream."""
    ts = "1.2.840.10008.1.2.4.90"
    first, second = _streams(ts)
    ds = _file(ts, (first, b"\0\0" + second[2:]))
    assert offset_table_frame_count(ds) is None


def test_the_end_marker_is_found_under_a_pad_byte(tmp_path):
    """An odd-length codestream is padded with one 0x00 in its fragment."""
    ts = "1.2.840.10008.1.2.4.90"
    first, second = _streams(ts)
    assert len(first) % 2 == 1
    assert offset_table_frame_count(_file(ts, (first, second)))[0] == 2


def test_two_declared_frames_are_not_this_count(tmp_path):
    """Under NumberOfFrames 2 pydicom walks the fragments and finds both:
    no excess, and the walk's own count speaks for any more."""
    ts = "1.2.840.10008.1.2.4.90"
    assert offset_table_frame_count(_file(ts, _streams(ts), 2)) is None
    counted = offset_table_frame_count(_file(ts, _streams(ts) * 2, 2))
    assert counted is not None and counted[3] == "Pixel Data's fragments"


@pytest.mark.parametrize("name", ["examples_jpeg2k.dcm", "JPEG-LL.dcm"])
def test_the_cohort_split_frames_are_not_counted(name):
    ds = pydicom.dcmread(get_testdata_file(name))
    assert offset_table_frame_count(ds) is None


def test_an_icon_of_whole_codestreams_keeps_its_frame_0(tmp_path):
    ts = "1.2.840.10008.1.2.4.90"
    ds = dataset("1.2.840.10008.1.2.1", rows=2, cols=2, bits_allocated=8,
                 native=bytes(4))
    icon = Dataset()
    icon.Rows = icon.Columns = 4
    icon.SamplesPerPixel = 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit, icon.PixelRepresentation = 7, 0
    icon.NumberOfFrames = 1
    icon.PixelData = encapsulate(list(_streams(ts)), has_bot=False)
    ds.IconImageSequence = Sequence([icon])
    # An icon is read by the file's own syntax: the top level is native,
    # so the icon's fragments are written under J2K by hand.
    ds.file_meta.TransferSyntaxUID = ts
    ds.PixelData = encapsulate([imagecodecs.jpeg2k_encode(
        np.zeros((2, 2), np.uint8), codecformat="J2K", level=0)])
    ds["PixelData"].is_undefined_length = True
    ds.IconImageSequence[0]["PixelData"].is_undefined_length = True
    path = write(tmp_path, ds)

    result = ingest_worker(path)

    assert result[-1] is None, result[-1]
    tables = result[0]["nested_offset_table"]
    assert len(tables) == 1
    _path, _tag, _vr, counted, kind = tables[0]
    assert kind == "excess"
    assert counted == (2, 1, 1, WholeStreams("JPEG 2000 codestream"))

"""Bytes beyond the declared frames that are not a frame are said (#1090).

A native Pixel Data may hold one pad byte past an odd-length run of
frames (PS3.5 7.1.1). Where it holds more than that, but less than one
more whole frame, pydicom reads the declared frames and the rest is
dropped: the export has no place for it. That was silent and the run
graded PASS. pydicom's own `MR_small_padded.dcm` holds 8320 bytes for
an 8192-byte frame.

Ingest now writes one `DATA_LOSS` row naming the bytes held and the
bytes the declared frames need, in STANDARD scope (owner ruling
2026-11-22, Q4 and Q5): the bytes are not a frame, and leaving them out
makes the export no less complete as an image, so the row does not cost
PASS. The stored pixels do not change. Bytes that hold a whole further
frame keep their own row (`_native_excess`), never both.

Names `isocenter.io_handlers` and `isocenter.imagecodecs_handler`.
"""
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter.imagecodecs_handler import native_trailing_words
from isocenter.io_handlers import ingest_worker
from support.decode_doors import EXPLICIT_LE, at_ingest, dataset, write


def _native(rows, cols, held, *, frames=None, bits=8, samples=1):
    return dataset(EXPLICIT_LE, rows=rows, cols=cols, samples=samples,
                   bits_allocated=bits, frames=frames,
                   native=bytes((7 * i) % 256 for i in range(held)))


def _rows(got):
    return [details for action, _uid, details in got["rows"]
            if action == "DATA_LOSS"]


def test_22_bytes_for_a_16_byte_frame_get_the_row(tmp_path):
    path = write(tmp_path, _native(4, 4, 22))
    assert len(pydicom.dcmread(path).PixelData) == 22
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None
    assert _rows(got) == [
        "Pixel Data holds 22 bytes, where the declared 1 frame of 4x4 "
        "needs 16 (and one pad byte where that is odd): the 6 bytes beyond "
        "it are not a frame and are not in the exported file."]
    assert got["array"].tobytes() == bytes((7 * i) % 256 for i in range(16))


def test_the_row_is_standard_scope_and_the_grade_stays(tmp_path):
    import sqlite3
    path = write(tmp_path, _native(4, 4, 22))
    at_ingest(tmp_path, path)
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        scopes = conn.execute(
            "SELECT loss_scope FROM audit_log WHERE action_type = "
            "'DATA_LOSS'").fetchall()
    assert scopes == [("STANDARD",)]


def test_one_pad_byte_after_an_odd_frame_is_no_row():
    """3x3 at 8 bits is 9 bytes; a file holds 10."""
    ds = _native(3, 3, 10)
    assert native_trailing_words(ds) is None


def test_a_pad_byte_after_an_even_frame_is_a_row():
    """Red if the pad rule is `> need + 1` whatever the parity: 16 + 2
    for an even frame is two bytes too many."""
    ds = _native(4, 4, 18)
    assert native_trailing_words(ds) == (
        "Pixel Data holds 18 bytes, where the declared 1 frame of 4x4 "
        "needs 16 (and one pad byte where that is odd): the 2 bytes beyond "
        "it are not a frame and are not in the exported file.")


def test_the_odd_frame_bound_is_one_byte():
    ds = _native(3, 3, 12)
    assert native_trailing_words(ds) == (
        "Pixel Data holds 12 bytes, where the declared 1 frame of 3x3 "
        "needs 9 (and one pad byte where that is odd): the 3 bytes beyond "
        "it are not a frame and are not in the exported file.")


def test_two_frames_need_their_sum():
    ds = _native(4, 4, 36, frames=2)
    assert native_trailing_words(ds) == (
        "Pixel Data holds 36 bytes, where the declared 2 frames of 4x4 "
        "need 32 (and one pad byte where that is odd): the 4 bytes beyond "
        "them are not a frame and are not in the exported file.")


def test_a_whole_extra_frame_keeps_its_own_row_alone(tmp_path):
    """32 bytes for one 16-byte frame: the whole-frame excess row, and
    not this one beside it."""
    assert native_trailing_words(_native(4, 4, 32)) is None
    assert native_trailing_words(_native(4, 4, 40)) is None
    path = write(tmp_path, _native(4, 4, 32))
    got = at_ingest(tmp_path, path)
    rows = _rows(got)
    assert len(rows) == 1 and rows[0].startswith(
        "Pixel Data's length holds 2 whole frames")


def test_a_one_bit_frame_is_counted_in_bits():
    """3x3 at 1 bit is 9 bits, so one frame needs 2 bytes."""
    ds = _native(3, 3, 2, bits=1)
    assert native_trailing_words(ds) is None
    ds = dataset(EXPLICIT_LE, rows=4, cols=4, bits_allocated=1,
                 native=bytes(4))
    assert native_trailing_words(ds) == (
        "Pixel Data holds 4 bytes, where the declared 1 frame of 4x4 "
        "needs 2 (and one pad byte where that is odd): the 2 bytes beyond "
        "it are not a frame and are not in the exported file.")


def test_ybr_full_422_is_pydicoms():
    ds = _native(4, 4, 40, samples=3)
    ds.PhotometricInterpretation = "YBR_FULL_422"
    assert native_trailing_words(ds) is None


def test_a_number_of_frames_pydicom_refuses_is_not_read():
    ds = _native(4, 4, 22)
    ds.NumberOfFrames = -1
    assert native_trailing_words(ds) is None


def test_an_encapsulated_file_is_not_asked():
    ds = _native(4, 4, 22)
    ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2.4.90"
    assert native_trailing_words(ds) is None


def test_mr_small_padded_gets_the_row(tmp_path):
    path = get_testdata_file("MR_small_padded.dcm")
    ds = pydicom.dcmread(path)
    assert native_trailing_words(ds) == (
        "Pixel Data holds 8320 bytes, where the declared 1 frame of 64x64 "
        "needs 8192 (and one pad byte where that is odd): the 128 bytes "
        "beyond it are not a frame and are not in the exported file.")
    result = ingest_worker(path)
    assert result[-1] is None
    assert result[0]["native_trailing"] == native_trailing_words(ds)
    assert len(result[2]) == 8192

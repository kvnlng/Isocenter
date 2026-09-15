"""JPEG Baseline and Extended files Pillow cannot decode read through imagecodecs (#604).

pydicom's only JPEG plugin here is Pillow, and Pillow refuses 12-bit JPEG
Extended ("Pillow does not support 'JPEG Extended' for samples with 12-bit
precision"). Until #453 the Instance door handed that refusal to
`imagecodecs_handler.get_pixel_data`, whose `.50`/`.51` arm read the file
with `imagecodecs.jpeg_decode`, and `ingest()` refused it, because `.50`
and `.51` were not fallback syntaxes: pydicom's own test file
`JPEG-lossy.dcm` read at one door and failed at the other (#604). #453
deleted that door, and the file was refused at both.

Measured first (dev-F1/m2_jpeg_lossy.raw): `jpeg_decode` returns
`JPEG-lossy.dcm` and `JPGExtended.dcm` value for value as DCMTK's
`dcmdjpeg` 3.7.0 does, at imagecodecs 2024.6.1 and 2026.8.16, on 3.12 and
3.14t. So `.50` and `.51` are fallback syntaxes now, **monochrome only**:
the same decoder differs from pydicom and DCMTK by up to 137 on an RGB
baseline stream with no Adobe marker (`SC_jpeg_no_color_transform.dcm`),
so a colour row would be a guess, and every 8-bit colour stream this
project has met is one Pillow decodes before the fallback is asked.
"""
import hashlib
import os

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file

from support.decode_doors import (at_decode_pixels, at_ingest, at_instance,
                                  dataset, pydicom_answer, pydicom_cannot,
                                  same, through_the_fallback,
                                  write)  # noqa: F401 pylint: disable=unused-import

JPEG_BASELINE = "1.2.840.10008.1.2.4.50"
JPEG_EXTENDED = "1.2.840.10008.1.2.4.51"

#: A 32x32 12-bit gradient. `jpeg8_encode(level=100)` writes it as an SOF1
#: stream that decodes to exactly these values (measured).
_YY, _XX = np.mgrid[0:32, 0:32]
GREY12 = ((_YY * 64 + _XX * 32) % 4096).astype(np.uint16)
GREY8 = ((_YY * 4 + _XX * 3) % 256).astype(np.uint8)


def _file(tmp_path, arr, *, ts=JPEG_EXTENDED, bits_stored=12, level=100,
          pixel_representation=0, photometric=None, name="one"):
    samples = arr.shape[2] if arr.ndim == 3 else 1
    options = {"bitspersample": bits_stored} if bits_stored > 8 else {}
    codestream = imagecodecs.jpeg8_encode(arr, level=level, **options)
    ds = dataset(
        ts, [codestream], rows=arr.shape[0], cols=arr.shape[1],
        samples=samples, bits_allocated=16 if bits_stored > 8 else 8,
        bits_stored=bits_stored, pixel_representation=pixel_representation,
        photometric=photometric)
    # Declared as a DCT file honestly is (PS3.3 C.7.6.1.1.5), so the
    # ingest rows these tests read are about the decode: since #601 an
    # absent 0028,2110 over a `.50`/`.51` stream whose first frame header is
    # a DCT SOFn (every `jpeg8_encode` stream here is SOF0 or SOF1) has its
    # own WARNING.
    ds.LossyImageCompression = "01"
    return write(tmp_path, ds, name=name)


@pytest.mark.parametrize("name", ["JPEG-lossy.dcm", "JPGExtended.dcm"])
def test_pydicoms_12_bit_jpeg_extended_file_reads_at_every_door(
        tmp_path, monkeypatch, name):
    """#604: refused at ingest, read at the Instance door, then refused at both.

    The expected array is DCMTK's decode (`dcmdjpeg` 3.7.0), pinned by
    digest: 1024x256 `uint16`, values 0..264. `imagecodecs.jpeg_decode`
    matched it value for value at 2024.6.1 and 2026.8.16; so no decode
    run here is its own reference.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    src = get_testdata_file(name)
    folder = tmp_path / "src"
    folder.mkdir()
    path = str(folder / "one.dcm")
    with open(src, "rb") as fh, open(path, "wb") as out:
        out.write(fh.read())
    refused = pydicom_answer(path)
    assert isinstance(refused, RuntimeError), refused
    assert "12-bit precision" in str(refused), str(refused)

    def reference(arr):
        return (arr.dtype == np.uint16 and arr.shape == (1024, 256)
                and hashlib.sha256(arr.astype("<u2").tobytes()).hexdigest()
                == "d30242775a414c01d616447854ebe3f2b20259822894bcd6891f879bcdcbf313")

    decoded = at_decode_pixels(path)
    assert isinstance(decoded, tuple), decoded
    assert reference(decoded[0]) and decoded[1] == "MONOCHROME2", decoded
    read = at_instance(path)
    assert isinstance(read, tuple), read
    assert reference(read[0]), read
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got["failure"]
    assert reference(got["array"]) and got["label"] == "MONOCHROME2", got


def test_a_12_bit_jpeg_extended_stream_reads_its_source_at_every_door(
        tmp_path, monkeypatch):
    """The encoder's input, at every door, where Pillow refuses the stream."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    path = _file(tmp_path, GREY12)
    assert isinstance(pydicom_answer(path), RuntimeError)
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, tuple), decoded
    assert same(decoded[0], GREY12) and decoded[1] == "MONOCHROME2", decoded
    read = at_instance(path)
    assert isinstance(read, tuple), read
    assert same(read[0], GREY12), read
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got["failure"]
    assert same(got["array"], GREY12), got["array"]
    assert got["rows"] == [], got["rows"]


@pytest.mark.parametrize("ts", [JPEG_BASELINE, JPEG_EXTENDED],
                         ids=[".50", ".51"])
def test_the_fallback_returns_pillows_array_for_an_8_bit_grey_stream(
        tmp_path, pydicom_cannot, ts):
    """Where Pillow does decode, the fallback's answer is Pillow's.

    A lossy stream (level 90), so the comparison is with a decode, and it
    is Pillow's own, read through pydicom's `as_array` captured before
    the fixture patched it.
    """
    path = _file(tmp_path, GREY8, ts=ts, bits_stored=8, level=90)
    want = pydicom_answer(path)
    assert isinstance(want, tuple), want
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, tuple), decoded
    assert same(decoded[0], want[0]) and decoded[1] == want[1], decoded
    assert pydicom_cannot["n"] > 0


@pytest.mark.parametrize("ts,photometric", [
    (JPEG_BASELINE, "RGB"), (JPEG_EXTENDED, "YBR_FULL_422"),
], ids=[".50-RGB", ".51-YBR_FULL_422"])
def test_a_colour_jpeg_stream_is_refused_by_the_fallback(
        tmp_path, pydicom_cannot, ts, photometric):
    """Colour is not a fallback row under `.50`/`.51`: unmeasured, and once wrong.

    The decoder's colour answer for a stream with no Adobe marker is not
    pydicom's (measured, 137 apart), so an RGB declaration is refused in
    the allow-list's words rather than stored under a label that may be
    false. The YBR row under `.51` is the same refusal through a door
    (#623 P6): until then only the table literal pinned it, and a
    `YBR_FULL_422` row added to `_FALLBACK_JPEG` was killed by nothing
    that decodes a file.
    """
    rgb = np.stack([GREY8, GREY8[::-1], GREY8.T], -1)
    path = _file(tmp_path, rgb, ts=ts, bits_stored=8, photometric=photometric)
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, RuntimeError), decoded
    assert ("imagecodecs could not decode it either: its declared colour "
            f"space {photometric!r} is not one this fallback labels under"
            in str(decoded)), str(decoded)
    assert pydicom_cannot["n"] > 0


def test_a_monochrome1_jpeg_extended_file_keeps_its_label_at_every_door(
        tmp_path, monkeypatch):
    """MONOCHROME1 under `.51` decodes and is stored under its own label (#623 P6).

    The row is the identity map, and until this test only the table
    literal said so: dropping `MONOCHROME1` from `_FALLBACK_JPEG`, or
    relabelling it `MONOCHROME2`, was killed by no door. Pillow refuses
    12-bit JPEG Extended, so the fallback answers at every door without
    a fixture forcing it; `through_the_fallback` is asked as well so the
    `_decode_pixels` answer is measured as the fallback's own.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    path = _file(tmp_path, GREY12, photometric="MONOCHROME1")
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, tuple), decoded
    assert same(decoded[0], GREY12) and decoded[1] == "MONOCHROME1", decoded
    fallback = through_the_fallback(pydicom.dcmread(path))
    assert same(fallback[0], GREY12) and fallback[1] == "MONOCHROME1", fallback
    # A bare file-backed Instance carries no attributes, so its label is
    # not observable at that door; the array is (as the MONOCHROME2 test
    # above asserts it), and the stored label is read at ingest.
    read = at_instance(path)
    assert isinstance(read, tuple), read
    assert same(read[0], GREY12), read
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got["failure"]
    assert same(got["array"], GREY12), got["array"]
    assert got["label"] == "MONOCHROME1", got["label"]
    assert got["rows"] == [], got["rows"]


def test_a_signed_12_bit_jpeg_extended_file_is_refused_in_the_dtype_words(
        tmp_path, monkeypatch):
    """PixelRepresentation 1: the decode is unsigned, and nothing here re-signs it.

    pydicom applies no sign correction to JPEG Baseline or Extended
    either, and no reference decode of a signed lossy stream was measured,
    so the header's disagreement with the decode is a refusal, at every
    door. Before #453 the Instance door returned the `uint16` patterns.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    path = _file(tmp_path, GREY12, pixel_representation=1)
    words = ("imagecodecs could not decode it either: it decoded to uint16, "
             "where BitsAllocated 16 and PixelRepresentation 1 declare int16")
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, RuntimeError), decoded
    assert words in str(decoded), str(decoded)
    read = at_instance(path)
    assert isinstance(read, RuntimeError), read
    assert words in str(read), str(read)
    assert os.path.dirname(path) not in str(read), str(read)
    got = at_ingest(tmp_path, path)
    assert got["failure"] is not None and words in got["failure"], got

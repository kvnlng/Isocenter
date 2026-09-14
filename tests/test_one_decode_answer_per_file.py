"""One file, one answer, at every pixel decode door (#453, #461).

`ingest()` decoded through `io_handlers._decode_pixels`: pydicom first,
then, on pydicom's plugin failure alone, `_decode_with_imagecodecs` and
its photometric allow-list, 16-bit conversion refusal, frame count and
dtype, shape and size guards. `Instance.get_pixel_data()` decoded
through pydicom, then handed **any** exception to
`imagecodecs_handler.get_pixel_data`, which had none of those guards. So
every guard present in one stack and absent from the other was a file
refused at ingest and read at the Instance door, or the reverse.

Now the Instance door calls `_decode_pixels` too, and `_decode_pixels`
runs pydicom's own header validation (`DecodeRunner.validate()`) before
the fallback. That second half matters because pydicom validates only
when it has a plugin to decode with: for JPEG Lossless and JPEG-LS it
has none here, so `ingest()` admitted a file missing BitsStored,
PixelRepresentation or PlanarConfiguration, or declaring BitsStored 17,
where a JPEG 2000 file with the same header was refused.

Each test asks three doors -- `ingest()`, `_decode_pixels` and a bare
file-backed `Instance` -- and runs on both routes a fallback syntax can
take (`route`): pydicom first, and `pydicom_cannot`, where the fallback
answers. The counter proves the second route measured the fallback.
Every expected string is pydicom's or the fallback's own words, written
out here, never read back from the code under test.
"""
import os

import imagecodecs
import numpy as np
import pytest

from support.decode_doors import (J2K_LOSSLESS, JPEGLS, LJPEG_SV1,
                                  at_decode_pixels, at_ingest, at_instance,
                                  dataset, pydicom_answer, pydicom_cannot,
                                  route, same,
                                  write)  # noqa: F401 pylint: disable=unused-import

#: 4x4 unsigned 16-bit monochrome, every value above 255.
MONO16 = (np.arange(16, dtype=np.int64) * 4000).astype(np.uint16).reshape(4, 4)
#: 4x4 RGB, 16-bit, every channel distinct.
RGB16 = (np.arange(48, dtype=np.int64) * 1000).astype(np.uint16).reshape(4, 4, 3)
#: 4x4 RGB, 8-bit.
RGB8 = (np.arange(48, dtype=np.int64) * 5).astype(np.uint8).reshape(4, 4, 3)


def _j2k(arr, **kwargs):
    return imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K", **kwargs)


def _refused_everywhere(tmp_path, ds, words, counter):
    """Assert every door refuses `ds`, each carrying `words`."""
    path = write(tmp_path, ds)
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, Exception), decoded
    assert words in str(decoded), str(decoded)
    read = at_instance(path)
    assert isinstance(read, RuntimeError), read
    # Either of the door's two raises: a reason that carries pydicom's
    # "missing dependencies" takes the codecs-missing one. Both name the
    # instance and never the file, whose folder may be named for the
    # patient.
    assert str(read).startswith(("Lazy load failed for instance ",
                                 "Failed to decompress pixel data for "
                                 "instance ")), str(read)
    assert os.path.dirname(path) not in str(read), str(read)
    assert words in str(read), str(read)
    got = at_ingest(tmp_path, path)
    assert got["array"] is None, got
    assert got["failure"] is not None and words in got["failure"], got
    assert got["failure"].startswith("Decompression Failed:"), got
    if counter is not None:
        assert counter["n"] > 0, "the fallback route was never taken"


# ---------------------------------------------------------------------------
# F1a -- a header pydicom rejects is rejected at every door, in its words
# ---------------------------------------------------------------------------

def test_a_j2k_file_missing_planar_configuration_is_refused_at_every_door(
        tmp_path, route):
    """16-bit RGB JPEG 2000 with PlanarConfiguration absent (#453's cell).

    Before: ingest and `_decode_pixels` refused it in pydicom's words, and
    the Instance door read it through the handler, which ignores
    PlanarConfiguration. On the `pydicom_cannot` route the fallback read
    it at every door, because the fallback never validated.
    """
    ds = dataset(J2K_LOSSLESS, [_j2k(RGB16)], rows=4, cols=4, samples=3,
                 bits_allocated=16, planar=None)
    _refused_everywhere(
        tmp_path, ds,
        "Missing required element: (0028,0006) 'Planar Configuration'",
        route)


@pytest.mark.parametrize("name, ts, codestream, kwargs, words", [
    ("ljpeg-no-bits-stored", LJPEG_SV1, imagecodecs.ljpeg_encode(MONO16),
     dict(rows=4, cols=4, bits_allocated=16, drop=("BitsStored",)),
     "Missing required element: (0028,0101) 'Bits Stored'"),
    ("ljpeg-bits-stored-17", LJPEG_SV1, imagecodecs.ljpeg_encode(MONO16),
     dict(rows=4, cols=4, bits_allocated=16, bits_stored=17),
     "A (0028,0101) 'Bits Stored' value of '17' is invalid"),
    ("ljpeg-no-pixel-representation", LJPEG_SV1,
     imagecodecs.ljpeg_encode(MONO16),
     dict(rows=4, cols=4, bits_allocated=16, drop=("PixelRepresentation",)),
     "Missing required element: (0028,0103) 'Pixel Representation'"),
    ("jpeg-ls-ybr8-no-planar", JPEGLS, imagecodecs.jpegls_encode(RGB8),
     dict(rows=4, cols=4, samples=3, bits_allocated=8,
          photometric="YBR_FULL", planar=None),
     "Missing required element: (0028,0006) 'Planar Configuration'"),
    ("j2k-mono16-no-pixel-representation", J2K_LOSSLESS, _j2k(MONO16),
     dict(rows=4, cols=4, bits_allocated=16, drop=("PixelRepresentation",)),
     "Missing required element: (0028,0103) 'Pixel Representation'"),
], ids=lambda value: value if isinstance(value, str) and "-" in value
   and " " not in value else "")
def test_a_header_pydicom_rejects_is_refused_at_every_door(
        tmp_path, route, name, ts, codestream, kwargs, words):
    """pydicom's validation, whether or not pydicom has a plugin (Q1).

    Before, pydicom validated only when it had a plugin: `as_array` checks
    its plugins before its options, so with none (JPEG Lossless, JPEG-LS)
    it raised "missing dependencies" and the fallback decoded the file.
    Ingest stored the first with BitsStored None and the second with
    HighBit 16. Now each is refused, at every door, in the words a JPEG
    2000 file with the same header already got.
    """
    _refused_everywhere(tmp_path, dataset(ts, [codestream], **kwargs), words,
                        route)


def test_a_nonsense_photometric_is_refused_in_pydicoms_words(tmp_path, route):
    """Validation runs before the fallback's own allow-list (M3).

    PhotometricInterpretation `NONSENSE` fails both. Ingest refused it in
    the allow-list's words; the Instance door read it and labelled the
    array `NONSENSE`. The words asserted are pydicom's, so a validation
    moved after the allow-list is caught.
    """
    ds = dataset(LJPEG_SV1, [imagecodecs.ljpeg_encode(MONO16)], rows=4,
                 cols=4, bits_allocated=16, photometric="NONSENSE")
    _refused_everywhere(
        tmp_path, ds,
        "Unknown (0028,0004) 'Photometric Interpretation' value 'NONSENSE'",
        route)
    got = at_decode_pixels(write(tmp_path, ds, name="again"))
    assert "is not one this fallback labels" not in str(got), str(got)


# ---------------------------------------------------------------------------
# F1b -- 16-bit YBR_FULL is refused at every door (#461, Q5)
# ---------------------------------------------------------------------------

def test_a_16_bit_ybr_full_jpeg_ls_file_is_refused_at_every_door(
        tmp_path, route):
    """The conversion takes 8-bit samples only; every door says so.

    Before: ingest refused it naming the depth, and both read doors
    returned the YBR samples as stored under the file's YBR_FULL label.
    pydicom refuses the native form too ("Invalid ndarray.dtype 'uint16'
    for color space conversion"), so there is no reference answer to read
    it by, and `docs/installation.md` records the limit.
    """
    ds = dataset(JPEGLS, [imagecodecs.jpegls_encode(RGB16)], rows=4, cols=4,
                 samples=3, bits_allocated=16, photometric="YBR_FULL")
    _refused_everywhere(
        tmp_path, ds,
        "its declared colour space 'YBR_FULL' is 16-bit, and the conversion "
        "to RGB this fallback would make, pydicom's `convert_color_space`, "
        "takes 8-bit samples only", route)


def test_a_16_bit_ybr_full_j2k_file_is_refused_at_every_door(tmp_path, route):
    """JPEG 2000 has no YBR_FULL row: the allow-list refuses it everywhere.

    A codestream with the colour transform off carries the YBR samples.
    Before: ingest refused it; the Instance door returned them under
    YBR_FULL.
    """
    ds = dataset(J2K_LOSSLESS, [_j2k(RGB16, mct=False)], rows=4, cols=4,
                 samples=3, bits_allocated=16, photometric="YBR_FULL")
    _refused_everywhere(
        tmp_path, ds,
        "its declared colour space 'YBR_FULL' is not one this fallback "
        "labels under", route)


# ---------------------------------------------------------------------------
# What still reads, reads the same everywhere
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, ts, codestream, kwargs, want, label", [
    ("ljpeg-mono16", LJPEG_SV1, imagecodecs.ljpeg_encode(MONO16),
     dict(rows=4, cols=4, bits_allocated=16), MONO16, "MONOCHROME2"),
    ("j2k-rgb16", J2K_LOSSLESS, _j2k(RGB16),
     dict(rows=4, cols=4, samples=3, bits_allocated=16), RGB16, "RGB"),
    ("j2k-mono16", J2K_LOSSLESS, _j2k(MONO16),
     dict(rows=4, cols=4, bits_allocated=16), MONO16, "MONOCHROME2"),
], ids=lambda value: value if isinstance(value, str) and "-" in value
   and " " not in value else "")
def test_a_conformant_file_reads_the_same_array_at_every_door(
        tmp_path, route, name, ts, codestream, kwargs, want, label):
    """The control: validation and the shared door refuse nothing valid."""
    path = write(tmp_path, dataset(ts, [codestream], **kwargs))
    arr, got_label = at_decode_pixels(path)
    assert same(arr, want) and got_label == label, (arr, got_label)
    arr, got_label = at_instance(path)
    assert same(arr, want), arr
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got
    assert same(got["array"], want) and got["label"] == label, got
    if route is not None:
        assert route["n"] > 0


# ---------------------------------------------------------------------------
# F1c -- the JPEG 2000 fallback returns what Pillow returns (#523)
# ---------------------------------------------------------------------------

def _grid():
    """Every JPEG 2000 shape Pillow decodes, whose sign agrees or is unsigned.

    `(id, samples, precision, signed codestream, BitsAllocated, BitsStored,
    PixelRepresentation)`. Monochrome at precision 8, 12 and 16; colour at
    8 only, because Pillow refuses 16-bit multi-sample data and so those
    files reach the fallback on both routes already. A *signed* codestream
    under PixelRepresentation 0 is not here: nothing decodes it to the
    file's samples (#524, below).
    """
    cells = []
    for samples, precisions in ((1, (8, 12, 16)), (3, (8,))):
        for precision in precisions:
            for signed in (False, True):
                for allocated in sorted({8 if precision <= 8 else 16, 16}):
                    for stored in sorted({precision, allocated}):
                        for representation in (0, 1):
                            if signed and not representation:
                                continue
                            cells.append((
                                f"{'mono' if samples == 1 else 'rgb'}-p{precision}"
                                f"-{'s' if signed else 'u'}cs-BA{allocated}"
                                f"-BS{stored}-PR{representation}",
                                samples, precision, signed, allocated, stored,
                                representation))
    return cells


def _grid_codestream(samples, precision, signed):
    rng = np.random.default_rng(523)
    if signed:
        low, high = -(1 << (precision - 1)), (1 << (precision - 1)) - 1
        dtype = np.int8 if precision <= 8 else np.int16
    else:
        low, high = 0, (1 << precision) - 1
        dtype = np.uint8 if precision <= 8 else np.uint16
    shape = (4, 4, 3) if samples == 3 else (4, 4)
    source = rng.integers(low, high + 1, size=shape).astype(dtype)
    source.flat[0], source.flat[1] = low, high
    return _j2k(source, bitspersample=precision, mct=False)


@pytest.mark.parametrize(
    "samples, precision, signed, allocated, stored, representation",
    [cell[1:] for cell in _grid()], ids=[cell[0] for cell in _grid()])
def test_the_fallback_returns_pillows_array_for_every_shape_pillow_decodes(
        tmp_path, pydicom_cannot, samples, precision, signed, allocated,
        stored, representation):
    """The imagecodecs route and the Pillow route give one array (A6).

    Before: a precision-8 codestream under BitsAllocated 16 came back from
    `jpeg2k_decode` as `uint8` or `int8`, and the fallback refused it
    against BitsAllocated ("it decoded to uint8") or could not sign-extend
    it ("cannot sign-extend a uint8 decode from BitsStored 16"), where
    Pillow returned the same samples in `uint16` or `int16` -- one file
    refused by the fallback and read by pydicom, so its answer depended on
    which plugins were installed. The container is exact either way
    (#523's ruling: fixed because one file had two answers, not because a
    value was at risk), so the widening writes no row and logs nothing.

    The reference is pydicom's own decoder, read through the `as_array`
    captured before the patch; the fallback answers `_decode_pixels` and
    the Instance door, and the counter proves it did.
    """
    path = write(tmp_path, dataset(
        J2K_LOSSLESS, [_grid_codestream(samples, precision, signed)], rows=4,
        cols=4, samples=samples, bits_allocated=allocated, bits_stored=stored,
        pixel_representation=representation))
    want, want_label = pydicom_answer(path)
    assert pydicom_cannot["n"] == 0
    arr, label = at_decode_pixels(path)
    assert same(arr, want) and label == want_label, (arr, want)
    arr, _label = at_instance(path)
    assert same(arr, want), (arr, want)
    assert pydicom_cannot["n"] == 2


#: A precision-8 codestream's samples, and the same values in the 16-bit
#: container its header declares: every 8-bit pattern of the source once.
P8_UNSIGNED = np.arange(256, dtype=np.int64).astype(np.uint8).reshape(16, 16)
P8_SIGNED = (np.arange(256, dtype=np.int64) - 128).astype(np.int8).reshape(
    16, 16)


@pytest.mark.parametrize("name, source, representation, want", [
    ("unsigned-under-PR0", P8_UNSIGNED, 0, P8_UNSIGNED.astype(np.uint16)),
    ("unsigned-under-PR1", P8_UNSIGNED, 1,
     P8_UNSIGNED.view(np.int8).astype(np.int16)),
    ("signed-under-PR1", P8_SIGNED, 1, P8_SIGNED.astype(np.int16)),
], ids=lambda value: value if isinstance(value, str) else "")
@pytest.mark.parametrize("samples", [1, 3], ids=["mono", "rgb"])
def test_a_precision_8_codestream_under_bits_allocated_16_ingests_in_16_bits(
        tmp_path, route, name, source, representation, want, samples):
    """#523's container half, and the `int8` arm, at ingest on both routes.

    An unsigned precision-8 codestream under PixelRepresentation 1 is
    reinterpreted from its own precision (#460), inside the 16-bit
    container: 128 reads -128. A signed one is widened `int8` -> `int16`,
    which only JPEG 2000 needs -- lj92 and CharLS return unsigned patterns
    and `_sign_extend` makes the signed dtype after the widening. Values
    are the module's literals, never an array the code produced.
    """
    if samples == 3:
        source = np.stack([source] * 3, -1)
        want = np.stack([want] * 3, -1)
    path = write(tmp_path, dataset(
        J2K_LOSSLESS, [_j2k(source, bitspersample=8, mct=False)], rows=16,
        cols=16, samples=samples, bits_allocated=16, bits_stored=8,
        pixel_representation=representation))
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got
    assert same(got["array"], want), got["array"]
    assert got["rows"] == [], got["rows"]
    arr, _label = at_decode_pixels(path)
    assert same(arr, want), arr
    if route is not None:
        assert route["n"] > 0


def test_a_jpeg_lossless_stream_narrower_than_bits_stored_is_refused_not_shifted(
        tmp_path):
    """`_sign_extend` refuses a width its container cannot hold (attack A7).

    A precision-8 JPEG Lossless stream under BitsAllocated 32, BitsStored
    12, PixelRepresentation 1. pydicom's validation passes it (12 <= 32),
    the codec returns `uint8`, and nothing widens a decode into 32 bits,
    so a sign extension from bit 11 inside 8 bits would shift by -4. The
    guard is the one thing that stands between this header and that shift
    now that validation refuses BitsStored above BitsAllocated before any
    decode (S6), and the container arm widens every 8-bit decode under
    BitsAllocated 16: this is the header that still reaches it. Refused
    at every door, in words that name BitsStored, which is the width JPEG
    Lossless is read by.
    """
    source = np.arange(16, dtype=np.uint8).reshape(4, 4)
    _refused_everywhere(
        tmp_path, dataset(
            LJPEG_SV1, [imagecodecs.ljpeg_encode(source, bitspersample=8)],
            rows=4, cols=4, bits_allocated=32, bits_stored=12,
            pixel_representation=1),
        "cannot sign-extend a uint8 decode from BitsStored 12", None)

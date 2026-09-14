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

from support.decode_doors import (HTJ2K, J2K_LOSSLESS, JPEGLS, LJPEG_SV1,
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
    return got


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


# ---------------------------------------------------------------------------
# F1e -- a signed JPEG 2000 codestream under PixelRepresentation 0 (#524)
# ---------------------------------------------------------------------------

#: The codestream's own samples: the extremes and either side of zero.
SIGNED16 = np.array([-32768, -800, -1, 0, 32767, 5, -5, 100] * 2,
                    np.int16).reshape(4, 4)
SIGNED8 = np.array([-128, -80, -1, 0, 127, 5, -5, 100] * 2,
                   np.int8).reshape(4, 4)

#: The refusal's words, in full up to the reason, as ingest reports them.
SIGNED_REFUSAL = ("the JPEG 2000 codestream is signed at precision {}, "
                  "where PixelRepresentation 0 declares unsigned samples")


@pytest.mark.parametrize("name, source, samples", [
    ("mono16", SIGNED16, 1),
    ("mono8", SIGNED8, 1),
    ("rgb8", np.stack([SIGNED8] * 3, -1), 3),
    ("rgb16", np.stack([SIGNED16] * 3, -1), 3),
], ids=lambda value: value if isinstance(value, str) else "")
def test_a_signed_codestream_under_pixel_representation_0_is_refused_at_every_door(
        tmp_path, route, name, source, samples):
    """M14: one refusal, before pydicom is asked, at every door (Q4).

    Before: pydicom's Pillow plugin decoded monochrome at any depth and
    8-bit colour, and returned the samples shifted by 2^(bits-1) --
    `[-32768, -800, -1]` stored as `uint16 [0, 31968, 32767]` -- with no
    row, at ingest and at the Instance door; only the handler refused. The
    pinning test said so (#524's premise, partly false: ingest's answer was
    pinned, as the divergence). 16-bit colour, which Pillow cannot decode,
    was already refused everywhere, and is here as the control.

    The words start the ingest reason, not the fallback's "imagecodecs
    could not decode it either": the SIZ is read ahead of pydicom, so no
    decoder is asked at all.
    """
    bits = 16 if source.dtype.itemsize == 2 else 8
    words = SIGNED_REFUSAL.format(bits)
    got = _refused_everywhere(tmp_path, dataset(
        J2K_LOSSLESS, [_j2k(source, mct=False)], rows=4, cols=4,
        samples=samples, bits_allocated=bits), words, None)
    assert got["failure"].startswith(
        f"Decompression Failed: RuntimeError: {words}"), got["failure"]
    if route is not None:
        # Refused before pydicom: the fallback route is never taken.
        assert route["n"] == 0


def test_the_signedness_gate_reads_every_frame(tmp_path):
    """M15: frame 1's codestream is signed, frame 0's is not.

    One frame's SIZ does not speak for another's samples. A gate that
    read frame 0 alone let Pillow shift frame 1.
    """
    unsigned = SIGNED16.view(np.uint16)
    _refused_everywhere(tmp_path, dataset(
        J2K_LOSSLESS, [_j2k(unsigned), _j2k(SIGNED16)], rows=4, cols=4,
        bits_allocated=16, frames=2), SIGNED_REFUSAL.format(16), None)


def test_the_signedness_gate_counts_frames_with_no_offset_table(tmp_path):
    """The gate's frame count when no table names one: NumberOfFrames.

    An empty Basic Offset Table and no Extended one, so
    `offset_table_frame_count` has nothing to count and the gate falls
    back to NumberOfFrames 2; frame 1 is the signed one. Found by the
    stride-1 probe: with the table's count read when there is *no* table
    (`is not None` flipped), or NumberOfFrames read as 1 (`or` to
    `and`), the gate reads frame 0 alone, or raises inside its own
    `try` and says nothing, and Pillow shifts frame 1 again.
    """
    from pydicom.encaps import encapsulate  # pylint: disable=import-outside-toplevel
    unsigned = SIGNED16.view(np.uint16)
    ds = dataset(J2K_LOSSLESS, [_j2k(unsigned)], rows=4, cols=4,
                 bits_allocated=16, frames=2)
    ds.PixelData = encapsulate([_j2k(unsigned), _j2k(SIGNED16)],
                               has_bot=False)
    ds["PixelData"].is_undefined_length = True
    _refused_everywhere(tmp_path, ds, SIGNED_REFUSAL.format(16), None)


def test_the_gate_reads_only_the_declared_frames(tmp_path, monkeypatch):
    """Attack A13: an excess frame's sign does not refuse a file ingest truncates.

    The offset table names two frames and NumberOfFrames declares one:
    ingest keeps frame 0 and writes the #418 DATA_LOSS row. Frame 1 is
    signed under PixelRepresentation 0, but it is not part of the image,
    so it is not the gate's to refuse.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    unsigned = SIGNED16.view(np.uint16)
    path = write(tmp_path, dataset(
        J2K_LOSSLESS, [_j2k(unsigned), _j2k(SIGNED16)], rows=4, cols=4,
        bits_allocated=16, frames=1))
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got
    assert same(got["array"], unsigned), got["array"]
    assert [row[0] for row in got["rows"]] == ["DATA_LOSS"], got["rows"]


def test_an_encapsulation_the_gate_cannot_walk_is_left_to_pydicom(tmp_path):
    """Attack A12: a buffer `generate_frames` cannot parse is not the gate's.

    The second item's tag is `(0000,0000)`. Through `_decode_pixels` the
    answer is pydicom's own `ValueError` whether or not the gate swallows
    it -- the gate would raise those same words -- so the gate is asked
    directly: it returns None rather than raising, which is what its
    docstring promises and what keeps a future gate from inventing its
    own refusal for a file pydicom describes better.
    """
    from isocenter import imagecodecs_handler  # pylint: disable=import-outside-toplevel
    codestream = _j2k(SIGNED16)
    ds = dataset(J2K_LOSSLESS, [codestream], rows=4, cols=4, bits_allocated=16)
    ds.PixelData = (b"\xfe\xff\x00\xe0\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00"
                    + len(codestream).to_bytes(4, "little") + codestream)
    ds["PixelData"].is_undefined_length = True
    assert imagecodecs_handler.signed_codestream_refusal(ds) is None
    decoded = at_decode_pixels(write(tmp_path, ds))
    assert isinstance(decoded, ValueError), decoded
    assert "Unexpected tag '(0000,0000)'" in str(decoded), str(decoded)


def test_a_signed_codestream_with_no_pixel_representation_is_refused_by_pydicom(
        tmp_path):
    """The gate's words cite "PixelRepresentation 0"; this file never wrote it.

    A missing Type 1 element is pydicom's to name, and it does, at both
    doors. The gate read absent as 0 until review, and refused the file
    with a sentence about a declaration the file does not make.
    """
    ds = dataset(J2K_LOSSLESS, [_j2k(SIGNED16)], rows=4, cols=4,
                 bits_allocated=16)
    del ds.PixelRepresentation
    path = write(tmp_path, ds)
    for got in (at_decode_pixels(path), at_instance(path)):
        assert isinstance(got, Exception), got
        assert "(0028,0103) 'Pixel Representation'" in str(got), str(got)
        assert "codestream is signed" not in str(got), str(got)


@pytest.mark.parametrize("name, fragment", [
    ("not-a-codestream", b"\x00\x01" * 32),
    ("soc-siz-cut-short", b"\xff\x4f\xff\x51\x00\x29"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_a_malformed_codestream_is_left_to_the_decoder(tmp_path, name,
                                                       fragment):
    """Attack A12: the gate reads a SIZ or nothing, and never raises itself.

    A payload under .90 that is not a codestream, or whose SIZ is cut
    short, has no sign to read: the gate says nothing and the decoders
    refuse the file in their own words, as before. An `IndexError` from a
    short read would have been a new, wordless refusal.
    """
    path = write(tmp_path, dataset(J2K_LOSSLESS, [fragment], rows=4, cols=4,
                                   bits_allocated=16))
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, Exception), decoded
    assert not isinstance(decoded, IndexError), decoded
    assert "codestream is signed" not in str(decoded), str(decoded)


# ---------------------------------------------------------------------------
# Review of #606, M1 -- every walk of the frames takes the Extended Offset
# Table pydicom's decoder takes, or none when pydicom drops it
# ---------------------------------------------------------------------------

def _item(fragment):
    if len(fragment) % 2:
        fragment += b"\x00"
    return b"\xfe\xff\x00\xe0" + len(fragment).to_bytes(4, "little") + fragment


def _with_eot(ds, fragments, offsets, lengths):
    """`fragments` one item each, an empty BOT, and the EOT given."""
    import struct  # pylint: disable=import-outside-toplevel
    ds.PixelData = _item(b"") + b"".join(_item(f) for f in fragments)
    ds["PixelData"].is_undefined_length = True
    ds.ExtendedOffsetTable = struct.pack(f"<{len(offsets)}Q", *offsets)
    ds.ExtendedOffsetTableLengths = struct.pack(f"<{len(lengths)}Q", *lengths)
    return ds


#: A fragment no frame's offset names: 20 bytes that are not a codestream.
_JUNK = b"\x00" * 20


def test_the_signedness_gate_walks_the_frames_the_extended_offset_table_names(
        tmp_path, route):
    """Review M1: the gate reads frame 1 where the decoder reads it.

    Three fragments, `[unsigned, junk, signed]`, with an EOT naming the
    first and the third. PS3.3 C.7.6.3 allows one fragment per frame
    under an EOT, so this file is non-conformant; pydicom decodes it by
    the table all the same. The gate walked it without the table: its EOI
    search glued the junk onto the signed codestream, no SIZ parsed, and
    the gate said nothing. Pillow then read frame 1 shifted, with no row,
    while the fallback, also walking without the table, refused it as
    "not a J2K or JP2 data stream": two answers, by plugin.
    """
    unsigned = _j2k(SIGNED16.view(np.uint16))
    signed = _j2k(SIGNED16)
    ds = _with_eot(
        dataset(J2K_LOSSLESS, [unsigned], rows=4, cols=4, bits_allocated=16,
                frames=2),
        [unsigned, _JUNK, signed],
        [0, len(_item(unsigned)) + len(_item(_JUNK))],
        [len(unsigned), len(signed)])
    words = SIGNED_REFUSAL.format(16)
    got = _refused_everywhere(tmp_path, ds, words, None)
    assert got["failure"].startswith(
        f"Decompression Failed: RuntimeError: {words}"), got["failure"]
    if route is not None:
        assert route["n"] == 0


def test_the_fallback_decodes_the_frames_the_extended_offset_table_names(
        tmp_path, route):
    """Review M1: the fallback walks the table too, so both routes agree.

    The same layout with both frames unsigned. Pillow decodes it by the
    table; the fallback glued the junk onto frame 1 and refused the file.
    """
    first = MONO16
    second = (MONO16[::-1] // 2).astype(np.uint16)
    ds = _with_eot(
        dataset(J2K_LOSSLESS, [_j2k(first)], rows=4, cols=4,
                bits_allocated=16, frames=2),
        [_j2k(first), _JUNK, _j2k(second)],
        [0, len(_item(_j2k(first))) + len(_item(_JUNK))],
        [len(_j2k(first)), len(_j2k(second))])
    path = write(tmp_path, ds)
    want = np.stack([first, second])
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, tuple), decoded
    assert same(decoded[0], want), decoded
    read = at_instance(path)
    assert isinstance(read, tuple), read
    assert same(read[0], want), read
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got["failure"]
    assert same(got["array"], want), got["array"]
    if route is not None:
        assert route["n"] > 0, "the fallback route was never taken"


def test_an_extended_offset_table_pydicom_drops_is_not_walked_by_the_gate(
        tmp_path, route):
    """Review M1: the table is taken only when its two elements agree in length.

    pydicom's `_validate_options` deletes `extended_offsets` when the
    Extended Offset Table and its Lengths differ in item count, warns,
    and walks the frames without it. Here the table names frame 0 twice
    and its Lengths hold one entry, so pydicom walks by fragment: frame 1
    is the signed codestream. A gate that took the table regardless read
    frame 0 twice, found nothing signed, and Pillow shifted frame 1.
    """
    unsigned = _j2k(SIGNED16.view(np.uint16))
    signed = _j2k(SIGNED16)
    ds = _with_eot(
        dataset(J2K_LOSSLESS, [unsigned], rows=4, cols=4, bits_allocated=16,
                frames=2),
        [unsigned, signed], [0, 0], [len(unsigned)])
    words = SIGNED_REFUSAL.format(16)
    got = _refused_everywhere(tmp_path, ds, words, None)
    assert got["failure"].startswith(
        f"Decompression Failed: RuntimeError: {words}"), got["failure"]


# ---------------------------------------------------------------------------
# Review of #606, M3 -- "Missing image codecs" only where no codec decoded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, ts, codestreams, kwargs, why", [
    ("jpeg-ls-ybr16", JPEGLS, lambda: [imagecodecs.jpegls_encode(RGB16)],
     dict(rows=4, cols=4, samples=3, bits_allocated=16,
          photometric="YBR_FULL"),
     "its declared colour space 'YBR_FULL' is 16-bit"),
    ("ljpeg-p16-under-ba8", LJPEG_SV1,
     lambda: [imagecodecs.ljpeg_encode(MONO16)],
     dict(rows=4, cols=4, bits_allocated=8),
     "it decoded to uint16, where BitsAllocated 8"),
    ("htj2k-ybr16", HTJ2K, lambda: [_j2k(RGB16, mct=False)],
     dict(rows=4, cols=4, samples=3, bits_allocated=16,
          photometric="YBR_FULL"),
     "its declared colour space 'YBR_FULL' is not one this fallback"),
], ids=["jpeg-ls-ybr16", "ljpeg-p16-under-ba8", "htj2k-ybr16"])
def test_a_refusal_imagecodecs_made_gives_no_missing_codecs_advice(
        tmp_path, name, ts, codestreams, kwargs, why):
    """Review M3: the advice names a remedy only a missing codec needs.

    pydicom's own words for these syntaxes say "decompress ... missing
    dependencies", and the Instance door took any reason saying so as a
    missing codec: "Missing image codecs. Please ensure 'pillow',
    'pylibjpeg', or 'gdcm' are installed." Here imagecodecs was present,
    decoded, and refused the file on what it holds; installing those
    packages changes nothing. The first two read at this door before
    #453 and gained that advice with the refusal. The door now raises
    `Lazy load failed for instance <uid>: RuntimeError: <reason>`, as it
    does for a JPEG 2000 file whose Pillow refusal says "decode". The
    advice where imagecodecs is not available stays, pinned by
    `test_ingest_imagecodecs_fallback::test_the_read_door_names_why_imagecodecs_is_unavailable`.
    """
    path = write(tmp_path, dataset(ts, codestreams(), **kwargs))
    read = at_instance(path)
    assert isinstance(read, RuntimeError), read
    words = str(read)
    assert words.startswith("Lazy load failed for instance "), words
    assert ": RuntimeError: " in words, words
    assert "imagecodecs could not decode it either: " in words, words
    assert why in words, words
    assert "Missing image codecs" not in words, words
    assert "Active pydicom handlers" not in words, words

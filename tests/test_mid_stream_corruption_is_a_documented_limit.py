"""A stream corrupted mid-stream can decode with no error: a documented limit (#452).

`README.md` said a frame that "cannot be decompressed, because of a
missing codec or a corrupt stream" fails its export. That promised more
than any decoder here does. openjpeg (JPEG 2000, through Pillow at
pydicom's door and through `imagecodecs.jpeg2k_decode` at the fallback)
and lj92 (JPEG Lossless) decode a codestream with bytes flipped in the
middle to plausible wrong values and report nothing -- no exception, no
warning, nothing on fd 2 at `verbose=2` -- and the second decoder a
cross-check would reach for returns the *same* wrong array, so it
detects nothing either. Owner ruling Q7: record the limit
(`docs/installation.md`, and README's "a stream the decoder rejects"),
and pin it here.

**These tests are pins on a limit, not on a behaviour anyone wants.** If
one goes red because a codec started refusing corruption, that is good
news: revisit `docs/installation.md`'s decode limits and README's
wording, then invert the test. What the docs do promise is pinned too: a
truncated stream is refused, and JPEG-LS (CharLS) refuses all three
corruptions.

The fixtures are the probe's (brief F, `p452`): a smooth 128x128 image
with a little noise, so a flipped byte lands in entropy-coded data rather
than in a header, and two corruptions -- 16 bytes XOR 0xFF at half-way,
and one bit flipped at 60%.
"""
import imagecodecs
import numpy as np
import pydicom
import pytest

from support.decode_doors import (J2K_LOSSLESS, JPEGLS, LJPEG_SV1,
                                  at_decode_pixels, dataset, pydicom_answer,
                                  through_the_fallback, write)


def _smooth(dtype, bits):
    y, x = np.mgrid[0:128, 0:128]
    base = (np.sin(x / 7.0) + np.cos(y / 5.0) + 2) / 4
    noise = np.random.default_rng(1).integers(0, 16, (128, 128))
    return ((base * ((1 << bits) - 64)).astype(np.int64) + noise).astype(dtype)


MONO8 = _smooth(np.uint8, 8)
MONO16 = _smooth(np.uint16, 16)
RGB16 = np.stack([MONO16, MONO16[::-1], MONO16.T], -1)


def _xor16_at_half(stream):
    out = bytearray(stream)
    for i in range(len(out) // 2, len(out) // 2 + 16):
        out[i] ^= 0xFF
    return bytes(out)


def _bit_at_60(stream):
    out = bytearray(stream)
    out[int(len(out) * 0.6)] ^= 0x10
    return bytes(out)


def _truncated_half(stream):
    """The first half and the last two bytes, so the end marker is kept."""
    return bytes(stream[:len(stream) // 2]) + bytes(stream[-2:])


CORRUPT = {"xor16@50%": _xor16_at_half, "bit@60%": _bit_at_60}

#: `(name, syntax, source, encoder)`. JPEG 2000 monochrome reaches Pillow at
#: pydicom's door; 16-bit colour, which Pillow refuses, reaches
#: `jpeg2k_decode`; JPEG Lossless reaches lj92.
_UNDETECTED = [
    ("j2k-mono8", J2K_LOSSLESS, MONO8,
     lambda a: imagecodecs.jpeg2k_encode(a, level=0, codecformat="J2K")),
    ("j2k-mono16", J2K_LOSSLESS, MONO16,
     lambda a: imagecodecs.jpeg2k_encode(a, level=0, codecformat="J2K")),
    ("j2k-rgb16", J2K_LOSSLESS, RGB16,
     lambda a: imagecodecs.jpeg2k_encode(a, level=0, codecformat="J2K",
                                         mct=False)),
    ("ljpeg-mono16", LJPEG_SV1, MONO16, imagecodecs.ljpeg_encode),
]


def _file(tmp_path, ts, source, stream, name="one"):
    samples = source.shape[2] if source.ndim == 3 else 1
    return write(tmp_path, dataset(
        ts, [stream], rows=128, cols=128, samples=samples,
        bits_allocated=source.dtype.itemsize * 8,
        photometric="RGB" if samples == 3 else None), name=name)


@pytest.mark.parametrize("corrupt", list(CORRUPT))
@pytest.mark.parametrize("name, ts, source, encode", _UNDETECTED,
                         ids=[case[0] for case in _UNDETECTED])
def test_a_mid_stream_corruption_decodes_to_wrong_values_with_no_error(
        tmp_path, name, ts, source, encode, corrupt):
    """The limit: an array comes back, in the right shape, and it is wrong.

    Asserted at `_decode_pixels`, the decode every door runs (#453), so
    ingest, the Instance door and the export readback all admit it.
    """
    stream = encode(source)
    path = _file(tmp_path, ts, source, CORRUPT[corrupt](stream))
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, tuple), (
        f"{name} {corrupt} was refused -- a codec now detects this "
        f"corruption: revisit docs/installation.md and README, then invert "
        f"this test. {decoded!r}")
    arr = decoded[0]
    assert arr.shape == source.shape and arr.dtype == source.dtype
    assert not np.array_equal(arr, source), (
        "the corruption did not change the decode, so this case pins nothing")
    # Control: the same file uncorrupted decodes exactly.
    clean = at_decode_pixels(_file(tmp_path, ts, source, stream, name="clean"))
    assert np.array_equal(clean[0], source)


@pytest.mark.parametrize("corrupt", list(CORRUPT))
def test_a_second_jpeg_2000_decoder_returns_the_same_wrong_array(tmp_path,
                                                                 corrupt):
    """Pillow and openjpeg agree on the corrupted samples: no cross-check.

    Pillow at pydicom's door, `jpeg2k_decode` at the fallback. Both are
    openjpeg, so their agreement is not independent evidence, which is
    exactly why a second decoder is no detector here.
    """
    stream = imagecodecs.jpeg2k_encode(MONO16, level=0, codecformat="J2K")
    path = _file(tmp_path, J2K_LOSSLESS, MONO16, CORRUPT[corrupt](stream))
    pillow = pydicom_answer(path)
    fallback = through_the_fallback(pydicom.dcmread(path))
    assert not np.array_equal(pillow[0], MONO16)
    assert np.array_equal(pillow[0], fallback[0])


@pytest.mark.parametrize("corrupt", list(CORRUPT))
def test_a_second_jpeg_lossless_decoder_returns_the_same_wrong_array(corrupt):
    """lj92, libjpeg-turbo and jpegsof3 return one wrong array between them."""
    bad = CORRUPT[corrupt](imagecodecs.ljpeg_encode(MONO16))
    if len(bad) % 2:
        bad += b"\x00"  # lj92's pad, as `_decode_frame` adds it
    lj92 = imagecodecs.ljpeg_decode(bad)
    assert not np.array_equal(lj92, MONO16)
    assert np.array_equal(imagecodecs.jpegsof3_decode(bad), lj92)
    assert np.array_equal(imagecodecs.jpeg8_decode(bad), lj92)


@pytest.mark.parametrize("name, ts, source, encode", _UNDETECTED,
                         ids=[case[0] for case in _UNDETECTED])
def test_a_truncated_stream_is_refused(tmp_path, name, ts, source, encode):
    """What the docs promise: half a stream is refused, at every door."""
    path = _file(tmp_path, ts, source, _truncated_half(encode(source)))
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, RuntimeError), f"{name}: {decoded!r}"


@pytest.mark.parametrize("corrupt", [*CORRUPT, "truncated-half"])
def test_jpeg_ls_refuses_every_corruption(tmp_path, corrupt):
    """CharLS detects all three, so a JPEG-LS file is the one family covered."""
    damage = {**CORRUPT, "truncated-half": _truncated_half}[corrupt]
    path = _file(tmp_path, JPEGLS, MONO16,
                 damage(imagecodecs.jpegls_encode(MONO16)))
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, RuntimeError), decoded
    assert "Invalid JPEG-LS stream" in str(decoded), str(decoded)

"""Signed JPEG Lossless and JPEG-LS pixels decode as the values they are (#446).

`imagecodecs.ljpeg_decode` and `jpegls_decode` return the masked unsigned
bit pattern of every sample, at every BitsStored, and never sign-extend.
So a signed (PixelRepresentation 1) 12-bit frame holding -800 came back
from `Instance.get_pixel_data()` as `uint16` 3296, with no error, while
`ingest()` refused the same file since #416 ("decoded to uint16, where
... declare int16"). Two doors, two answers, and one of them wrong.

The ruling: decode correctly. The handler sign-extends from BitsStored,
the rule pydicom applies with its own plugins, measured bit-exact against
pydicom with pylibjpeg-libjpeg and pyjpegls at 8, 12 and 16 bits. It
lives in the handler's `_decode_frame`, which every door reaches through
`io_handlers._decode_pixels` and `decode_declared_frames` -- so there is
one rule, not a guard at one door and a fix at the other. Every case here
is asserted at all three: `ingest()`, the store's answer after it;
`Instance(file_path).get_pixel_data()`; and `_decode_pixels` itself, with
pydicom made unable to decode, which is the column the handler's own
`get_pixel_data` held until #453 deleted it (Q10).

JPEG 2000 is not touched by *this* rule: `jpeg2k_decode` returns signed
samples already. It has a rule of its own, keyed on the codestream's SIZ
header rather than on BitsStored (#460); S4 is the boundary between the
two, and
`tests/test_j2k_signedness_against_pixel_representation.py` pins it.

**Every fixture first asserts that `pydicom.dcmread(p).pixel_array`
raises**, so no case passes through pydicom's door and says nothing about
this one. Every value assertion is against a module literal.
"""
import os
import struct

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import generate_uid

from isocenter.entities import Instance
from isocenter.session import DicomSession
from support.decode_doors import through_the_fallback

LJPEG = "1.2.840.10008.1.2.4.57"
LJPEG_SV1 = "1.2.840.10008.1.2.4.70"
JPEGLS = "1.2.840.10008.1.2.4.80"
JPEGLS_NEAR = "1.2.840.10008.1.2.4.81"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"

#: One row per BitsStored: the extremes, -800 clipped to fit, and values
#: either side of zero, so a missing sign extension (3296 for -800), a
#: shift from the wrong width, and an unsigned view all show. Tiled to
#: 16x16 below.
SIGNED_ROWS = {
    8: [-128, -128, -1, 0, 127, 5, -5, 100],
    12: [-2048, -800, -1, 0, 2047, 5, -5, 100],
    16: [-32768, -800, -1, 0, 32767, 5, -5, 100],
}
SIGNED = {bs: np.tile(np.array(row, dtype=np.int8 if bs <= 8 else np.int16),
                      (16, 2))
          for bs, row in SIGNED_ROWS.items()}

#: A JPEG-LS stream of `SIGNED[12]`'s 12-bit pattern written by pyjpegls
#: (CharLS) at precision 12 -- what a conformant encoder writes for
#: BitsStored 12. `imagecodecs.jpegls_encode` cannot write it: it always
#: writes precision 16 for `uint16`. Checked in as bytes so the test needs
#: no encoder this package does not install.
P12_JPEGLS = bytes.fromhex(
    "ffd8fff7000b0c0010001001011100ffda0008010100000000000000001ffd000000"
    "0019be63b8027fe8ff0a933c000000007cd8b76c6f004ffd1ff294cb81020410421082"
    "084444408108888921092490410924922249504115551155421155511554212aaa4af0"
    "84fe4fc213f93f113f93f113f93f113fafc45fd7e22febf117f5f88bfafc00ffd9")


def _pattern(signed, bits_stored):
    """The two's-complement pattern masked to BitsStored, unsigned."""
    unsigned = np.dtype(f"u{signed.dtype.itemsize}")
    return (signed.astype(np.int64) & ((1 << bits_stored) - 1)).astype(
        unsigned)


def _ljpeg(pattern, bits_stored):
    return imagecodecs.ljpeg_encode(pattern, bitspersample=bits_stored)


def _sof3_predictor_6(pattern, bits_stored):
    # libjpeg-turbo's lossless mode at predictor 6, a second encoder
    # beside lj92's own, so the rule is not pinned on one encoder's
    # streams alone.
    return imagecodecs.jpeg8_encode(pattern, lossless=True, predictor=6,
                                    bitspersample=bits_stored)


def _jpegls(pattern, _bits_stored):
    return imagecodecs.jpegls_encode(pattern)


def _dataset(ts, codestream, shape, bits_stored, *, bits_allocated=None,
             pixel_representation=1, high_bit=None,
             photometric="MONOCHROME2", samples=1):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ts
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT446", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows, ds.Columns = shape[0], shape[1]
    ds.SamplesPerPixel = samples
    if samples > 1:
        ds.PlanarConfiguration = 0
    ds.PhotometricInterpretation = photometric
    ds.BitsAllocated = bits_allocated or (8 if bits_stored <= 8 else 16)
    ds.BitsStored = bits_stored
    ds.HighBit = bits_stored - 1 if high_bit is None else high_bit
    ds.PixelRepresentation = pixel_representation
    ds.PixelData = encapsulate([codestream], has_bot=True)
    ds["PixelData"].is_undefined_length = True
    return ds


def _write(folder, ds):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "one.dcm")
    ds.save_as(path, enforce_file_format=True)
    with pytest.raises(RuntimeError):
        _ = pydicom.dcmread(path).pixel_array
    return path


@pytest.fixture
def doors(tmp_path):
    """Write `ds`; return what each of the three doors makes of it.

    `ingest` is `(ingested, failures, stored)`, `stored` being the store's
    array after `unload_pixel_data()` -- the sidecar's answer, not a
    resident one -- or None when nothing ingested. The two read doors are
    the array or the exception.
    """
    sessions = []

    def _run(ds, name="one"):
        src = tmp_path / f"src_{name}"
        path = _write(str(src), ds)
        session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
        sessions.append(session)
        summary = session.ingest(str(src))
        stored = None
        if summary.ingested:
            inst = [i for p in session.store.patients for st in p.studies
                    for se in st.series for i in se.instances][0]
            assert inst.unload_pixel_data() is True
            stored = inst.get_pixel_data()
        out = {"ingest": (summary.ingested, summary.failures, stored)}
        for door, read in (
                ("instance", lambda: Instance(
                    generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                    file_path=path).get_pixel_data()),
                ("decode_pixels", lambda: through_the_fallback(
                    pydicom.dcmread(path))[0])):
            try:
                out[door] = read()
            except Exception as exc:  # pylint: disable=broad-except
                out[door] = exc
        return out

    yield _run
    for session in sessions:
        session.close()


def _assert_reads(got, want):
    """Each door returned exactly `want`, dtype included."""
    ingested, failures, stored = got["ingest"]
    assert (ingested, failures) == (1, []), failures
    for door, arr in (("ingest", stored), ("instance", got["instance"]),
                      ("decode_pixels", got["decode_pixels"])):
        assert isinstance(arr, np.ndarray), f"{door}: {arr!r}"
        assert arr.dtype == want.dtype, f"{door}: {arr.dtype}"
        assert arr.tolist() == want.tolist(), f"{door}: {arr[0].tolist()}"


# ---------------------------------------------------------------------------
# S1 -- signed frames read their values, at all three doors
# ---------------------------------------------------------------------------

#: `(ts, bits_stored, encoder)`. Every stream here is written at precision
#: = BitsStored, so the stream and the header agree on what a sample is.
#: The JPEG-LS 12-bit case `imagecodecs.jpegls_encode` writes is
#: precision 16 under BitsStored 12, where they disagree; it is S1b.
_S1_CASES = [
    (LJPEG, 8, _ljpeg), (LJPEG, 12, _ljpeg), (LJPEG, 16, _ljpeg),
    (LJPEG_SV1, 8, _ljpeg), (LJPEG_SV1, 12, _ljpeg), (LJPEG_SV1, 16, _ljpeg),
    (LJPEG, 8, _sof3_predictor_6), (LJPEG, 12, _sof3_predictor_6),
    (LJPEG, 16, _sof3_predictor_6),
    (JPEGLS, 8, _jpegls), (JPEGLS, 16, _jpegls),
    (JPEGLS_NEAR, 8, _jpegls), (JPEGLS_NEAR, 16, _jpegls),
]


@pytest.mark.parametrize(
    "ts,bits_stored,encode", _S1_CASES,
    ids=[f"{ts[-3:]}-{bs}-{enc.__name__.strip('_')}"
         for ts, bs, enc in _S1_CASES])
def test_a_signed_lossless_jpeg_frame_reads_its_values_at_both_doors(
        doors, ts, bits_stored, encode):
    """S1: ingest, the Instance door and `_decode_pixels` agree, on the values.

    Before: ingest refused (`decoded to uint16 ... declare int16`) and
    both read doors returned the unsigned pattern -- 3296 for -800 at
    BitsStored 12, 64736 at 16. Near-lossless (.81) is encoded at NEAR 0
    so the values are exact.
    """
    want = SIGNED[bits_stored]
    codestream = encode(_pattern(want, bits_stored), bits_stored)
    _assert_reads(doors(_dataset(ts, codestream, want.shape, bits_stored)),
                  want)


#: `SIGNED[12]`'s 12-bit pattern read as a 16-bit signed sample: what a
#: precision-16 JPEG-LS stream holding that pattern contains (S1b). The
#: pattern of -800 is 3296, and no bit above bit 11 is set, so nothing is
#: negative. Written out, not derived, so a wrong rule cannot also move
#: the expected value.
PATTERN_12_IN_16 = np.tile(np.array(
    [2048, 3296, 4095, 0, 2047, 5, 4091, 100], dtype=np.int16), (16, 2))

#: `SIGNED[8]`'s 8-bit pattern read as a 16-bit signed sample, for a
#: precision-16 stream under BitsStored 8 (S1c).
PATTERN_8_IN_16 = np.tile(np.array(
    [128, 128, 255, 0, 127, 5, 251, 100], dtype=np.int16), (16, 2))


def _multiframe(ds, codestreams):
    """`ds` with its pixel data replaced by one frame per codestream."""
    ds.NumberOfFrames = len(codestreams)
    ds.PixelData = encapsulate(codestreams, has_bot=True)
    ds["PixelData"].is_undefined_length = True
    return ds


@pytest.mark.parametrize("ts", [JPEGLS, JPEGLS_NEAR])
def test_a_precision_16_jpeg_ls_stream_under_bits_stored_12_reads_by_its_precision(
        doors, ts):
    """S1b: where the stream's precision and BitsStored disagree, the stream (#478).

    `imagecodecs.jpegls_encode` always writes precision 16 for `uint16`,
    so a 12-bit pattern under a BitsStored 12 header is a precision-16
    stream holding 12-bit samples. The owner's ruling (#478, reversing
    the BitsStored reading #463 shipped): read it by the stream's
    precision, as pydicom with pyjpegls does, so -800's pattern reads
    3296. The stream is what the decoder was told a sample is; a JPEG-LS
    stream has no other place to say it, and pydicom is the reference
    every other door here is measured against.
    """
    codestream = _jpegls(_pattern(SIGNED[12], 12), 12)
    assert codestream[codestream.index(b"\xff\xf7") + 4] == 16
    _assert_reads(doors(_dataset(ts, codestream, (16, 16), 12)),
                  PATTERN_12_IN_16)


def test_a_precision_12_jpeg_ls_stream_under_bits_stored_16_reads_by_its_precision(
        doors):
    """S1c: the other direction -- a stream narrower than BitsStored (#478).

    CharLS's precision-12 stream under a BitsStored 16 header. Read by
    BitsStored, the 12-bit pattern is a pure view and -800 came back as
    3296; read by the stream's precision it is -800, which is pydicom
    with pyjpegls's answer (measured, 3.12.14, pydicom 3.0.2, pyjpegls
    1.5.1). "Whenever that differs" is the ruling's wording, and it
    covers this row as well as S1b's.
    """
    _assert_reads(doors(_dataset(JPEGLS, P12_JPEGLS, (16, 16), 16)),
                  SIGNED[12])


def test_a_precision_16_jpeg_ls_stream_under_bits_stored_8_reads_by_its_precision(
        doors):
    """S1d: an 8-bit pattern in a precision-16 stream, BitsAllocated 16 (#478).

    The stream says each sample is 16 bits, so -128's 8-bit pattern is
    128 -- pydicom with pyjpegls's answer (measured). By BitsStored it
    read -128.
    """
    codestream = _jpegls(_pattern(SIGNED[8], 8).astype(np.uint16), 8)
    assert codestream[codestream.index(b"\xff\xf7") + 4] == 16
    _assert_reads(doors(_dataset(JPEGLS, codestream, (16, 16), 8,
                                 bits_allocated=16)),
                  PATTERN_8_IN_16)


def test_each_jpeg_ls_frame_is_read_by_its_own_precision(doors):
    """S1e: a precision-12 frame then a precision-16 frame, BitsStored 12.

    Precision is a property of a codestream, and each frame is its own
    codestream, so frame 0 reads -800 and frame 1 reads 3296. That is
    pydicom's answer frame by frame (`pixel_array(path, index=i)`,
    measured). pydicom's whole-array read applies the *last* frame's
    precision to every frame -- [P12, P16] reads 3296 in both, [P16, P12]
    reads -800 in both -- which is one frame's header answering for
    another's samples; this does not follow it there.
    """
    ds = _multiframe(_dataset(JPEGLS, P12_JPEGLS, (16, 16), 12),
                     [P12_JPEGLS, _jpegls(_pattern(SIGNED[12], 12), 12)])
    _assert_reads(doors(ds), np.stack([SIGNED[12], PATTERN_12_IN_16]))


#: `P12_JPEGLS` with a comment segment ahead of its frame header whose
#: payload holds the two bytes of a SOF55 marker and a precision of 16.
#: A COM (or APPn) payload is opaque bytes, so `FF F7` can legally occur
#: in one; CharLS skips it and decodes the stream exactly (measured at
#: imagecodecs 2024.6.1 and 2026.8.16).
_DECOY = b"\xff\xf7\x00\x0b\x10\x00\x10\x00\x10\x01\x01\x11\x00"
P12_BEHIND_A_DECOY = (P12_JPEGLS[:2] + b"\xff\xfe"
                      + (len(_DECOY) + 2).to_bytes(2, "big") + _DECOY
                      + P12_JPEGLS[2:])


def test_the_precision_is_read_from_the_frame_header_not_the_first_ff_f7(
        doors):
    """S1f: the precision comes from walking the segments to SOF55.

    A search for the first `FF F7` finds the comment's payload and reads
    precision 16, so -800 would come back 3296. The frame header says 12.
    """
    assert P12_BEHIND_A_DECOY[P12_BEHIND_A_DECOY.index(b"\xff\xf7") + 4] \
        == 16
    _assert_reads(doors(_dataset(JPEGLS, P12_BEHIND_A_DECOY, (16, 16), 12)),
                  SIGNED[12])


#: `P12_JPEGLS` with two fill bytes ahead of its frame header. ITU-T T.81
#: B.1.1.2 lets any marker be preceded by `FF` fill, and CharLS skips it
#: and decodes the stream exactly (measured, imagecodecs 2026.8.16).
P12_BEHIND_FILL_BYTES = P12_JPEGLS[:2] + b"\xff\xff" + P12_JPEGLS[2:]


def test_fill_bytes_ahead_of_the_frame_header_do_not_hide_its_precision(
        doors):
    """S1h: the walk steps over `FF` fill to reach SOF55.

    A walk that took the fill byte for a marker would read `FF F7` as a
    segment length, run off the end, fall back to BitsStored 16, and
    return the raw 12-bit patterns: -2048 would come back 2048. That is
    what pydicom's own header parser does with this stream (a divergence
    the CHANGELOG states): the frame header says precision 12, and CharLS,
    which decoded the samples, read it.
    """
    assert P12_BEHIND_FILL_BYTES[2:6] == b"\xff\xff\xff\xf7"
    _assert_reads(doors(_dataset(JPEGLS, P12_BEHIND_FILL_BYTES, (16, 16), 16)),
                  SIGNED[12])


@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1])
def test_a_lossless_jpeg_stream_wider_than_bits_stored_still_reads_by_bits_stored(
        doors, ts):
    """S1g: the ruling is JPEG-LS's; JPEG Lossless keeps BitsStored.

    A precision-16 SOF3 stream holding a 12-bit pattern under BitsStored
    12. pydicom reads JPEG Lossless by BitsStored (`_correct_unused_bits`,
    not the JPEG-LS precision branch of `_apply_sign_correction`) and
    returns -800 (measured with pylibjpeg-libjpeg), so this does too.
    """
    codestream = imagecodecs.ljpeg_encode(_pattern(SIGNED[12], 12),
                                          bitspersample=16)
    _assert_reads(doors(_dataset(ts, codestream, (16, 16), 12)), SIGNED[12])


# ---------------------------------------------------------------------------
# S2 -- a precision-12 JPEG-LS stream, as a conformant encoder writes it
# ---------------------------------------------------------------------------

def test_a_precision_12_jpeg_ls_stream_from_another_encoder_reads_exactly(
        doors):
    """S2: the one JPEG-LS fixture whose stream precision is BitsStored.

    `imagecodecs.jpegls_encode` always writes precision 16, so every other
    JPEG-LS fixture here disagrees with a 12-bit header by construction.
    This one is CharLS's, at precision 12, and it is what a scanner's
    encoder writes for BitsStored 12.
    """
    # The SOF55 precision byte, so the literal cannot drift into another
    # stream without this line saying so.
    assert P12_JPEGLS[P12_JPEGLS.index(b"\xff\xf7") + 4] == 12
    _assert_reads(doors(_dataset(JPEGLS, P12_JPEGLS, (16, 16), 12)),
                  SIGNED[12])


# ---------------------------------------------------------------------------
# S6 -- a decode narrower than BitsStored is refused, never shifted
# ---------------------------------------------------------------------------

def test_a_signed_decode_narrower_than_bits_stored_is_refused_at_both_doors(
        doors):
    """S6: an 8-bit JPEG Lossless stream under BitsAllocated 8 and a
    signed BitsStored 12.

    The codec returns `uint8`, the header's container is 8 bits too, so
    there is nothing to widen it into (S7), and 12 bits cannot be
    sign-extended inside 8: the shift the rule computes would be -4.
    Found by the probe (#446 review): with `and` in place of `or` in the
    handler's guard, a `uint8` decode passed it and came back as whatever
    a negative shift made of it, with no error at the read door.

    **Refused before any decode now, in pydicom's words (#453).** The
    header is one pydicom's own validation rejects -- BitsStored greater
    than BitsAllocated -- and pydicom never got to say so, because it
    validates only once it has a plugin and has none for JPEG Lossless
    here. `_decode_pixels` runs that validation ahead of the fallback, at
    ingest and at the Instance door alike, so the handler's guard is no
    longer what stands between this file and the shift. The handler
    column is gone: it is no longer a door.

    JPEG Lossless under BitsAllocated 8, where until #454 this was a
    JPEG-LS stream under BitsAllocated 16. That stream is now widened into
    its 16-bit container and read, as pydicom reads it (S7b).
    """
    source = np.arange(16 * 16, dtype=np.uint8).reshape(16, 16)
    got = doors(_dataset(LJPEG_SV1, _ljpeg(source, 8), source.shape, 12,
                         bits_allocated=8))
    ingested, failures, _stored = got["ingest"]
    assert ingested == 0
    words = ("A (0028,0101) 'Bits Stored' value of '12' is invalid, it must "
             "be in the range (1, 64) and no greater than the (0028,0100) "
             "'Bits Allocated' value of '8'")
    assert words in failures[0][1], failures
    assert isinstance(got["instance"], RuntimeError), got["instance"]
    assert words in str(got["instance"]), str(got["instance"])


# ---------------------------------------------------------------------------
# S7 -- a precision-8 stream under BitsAllocated 16 reads in that container
# ---------------------------------------------------------------------------

#: Every 8-bit pattern once, so each value a sign bit can change shows.
UNSIGNED_8 = np.arange(256, dtype=np.uint8).reshape(16, 16)

#: `(ts, pixel_representation, encoder)`: JPEG-LS at both syntaxes, and
#: JPEG Lossless from both encoders, each signed and unsigned.
_S7_CASES = [
    (JPEGLS, 0, _jpegls), (JPEGLS, 1, _jpegls),
    (JPEGLS_NEAR, 0, _jpegls), (JPEGLS_NEAR, 1, _jpegls),
    (LJPEG_SV1, 0, _ljpeg), (LJPEG_SV1, 1, _ljpeg),
    (LJPEG, 0, _sof3_predictor_6), (LJPEG, 1, _sof3_predictor_6),
]


@pytest.mark.parametrize(
    "ts,pixel_representation,encode", _S7_CASES,
    ids=[f"{ts[-3:]}-pr{pr}-{enc.__name__.strip('_')}"
         for ts, pr, enc in _S7_CASES])
def test_a_precision_8_stream_under_a_16_bit_header_reads_in_that_container(
        doors, ts, pixel_representation, encode):
    """S7 (#454): widened to the container BitsAllocated 16 declares.

    Before: both read doors returned `uint8` or `int8`, holding the right
    values, and ingest refused the file (`it decoded to uint8, where
    BitsAllocated 16 and PixelRepresentation 0 declare uint16`). Two
    doors, two answers. pydicom with pyjpegls returns `uint16`/`int16`
    for these JPEG-LS streams (measured, pydicom 3.0.2, pyjpegls 1.5.1).
    pydicom with pylibjpeg-libjpeg raises on the JPEG Lossless ones
    (`could not broadcast input array from shape (128,) into shape
    (256,)`), so for those the three doors agree with each other, and
    there is no pydicom answer to agree with.

    Mutant: `_decode_frame` without the widening. Every case goes red,
    at ingest's refusal and at the read doors' 8-bit dtype.
    """
    if pixel_representation:
        source, want = _pattern(SIGNED[8], 8), SIGNED[8].astype(np.int16)
    else:
        source, want = UNSIGNED_8, UNSIGNED_8.astype(np.uint16)
    _assert_reads(doors(_dataset(ts, encode(source, 8), want.shape, 8,
                                 bits_allocated=16,
                                 pixel_representation=pixel_representation)),
                  want)


@pytest.mark.parametrize("layout", ["rgb", "two-frames"])
def test_a_precision_8_colour_or_multi_frame_stream_is_widened_too(
        doors, layout):
    """S7a: through the multi-frame arm and at three samples.

    The handler decodes a multi-frame file frame by frame in a different
    arm from the single-frame one, and each frame is widened as it is
    decoded. RGB, because a colour frame is where the container width
    decides whether `colour_conversion` runs (it converts 8-bit only), and
    an RGB frame is not converted, so its samples are the source's.
    """
    if layout == "rgb":
        source = np.stack([UNSIGNED_8, UNSIGNED_8[::-1], UNSIGNED_8.T], -1)
        ds = _dataset(JPEGLS, imagecodecs.jpegls_encode(source), source.shape,
                      8, bits_allocated=16, pixel_representation=0,
                      photometric="RGB", samples=3)
    else:
        source = np.stack([UNSIGNED_8, UNSIGNED_8[::-1]])
        streams = [imagecodecs.jpegls_encode(np.ascontiguousarray(f))
                   for f in source]
        # `_dataset` takes one codestream (`encapsulate` refuses an empty
        # frame), and `_multiframe` then replaces it with both.
        ds = _multiframe(
            _dataset(JPEGLS, streams[0], source.shape[1:], 8,
                     bits_allocated=16, pixel_representation=0),
            streams)
    _assert_reads(doors(ds), source.astype(np.uint16))


#: `UNSIGNED_8`'s patterns sign-extended from 8 bits, in the `int16`
#: container: 0..127, then -128..-1. What pydicom with pyjpegls returns for
#: S7b's file (measured, 3.12.14, pydicom 3.0.2, pyjpegls 1.5.1).
P8_SIGNED_IN_16 = np.concatenate(
    [np.arange(0, 128), np.arange(-128, 0)]).astype(np.int16).reshape(16, 16)


def test_a_precision_8_jpeg_ls_stream_under_signed_bits_stored_12_reads_as_pydicom_does(  # noqa: E501  pylint: disable=line-too-long
        doors):
    """S7b: S6's fixture until #454, which every door used to refuse.

    A JPEG-LS stream of precision 8 under BitsAllocated 16 and a signed
    BitsStored 12. Widened into the `int16` container it now fits, and
    sign-extended from the stream's own precision, 8 (#478), so the
    patterns 128..255 read -128..-1. That is pydicom's answer. Until #454
    every door refused it: `cannot sign-extend a uint8 decode from
    BitsStored 12`.

    Mutant: `_decode_frame` without the widening. Refused again in S6's
    words, at every door.
    """
    _assert_reads(doors(_dataset(JPEGLS, imagecodecs.jpegls_encode(UNSIGNED_8),
                                 UNSIGNED_8.shape, 12, bits_allocated=16)),
                  P8_SIGNED_IN_16)


#: `UNSIGNED_8`'s patterns in `int16` with **no** extension: 0..255. What a
#: width-16 sign extension leaves them as, because the shift is 0.
P8_UNEXTENDED_IN_16 = UNSIGNED_8.astype(np.int16)

#: The same patterns extended from 8 bits: 0..127, then -128..-1.
P8_EXTENDED_FROM_8 = np.concatenate(
    [np.arange(0, 128), np.arange(-128, 0)]).astype(np.int16).reshape(16, 16)


@pytest.mark.parametrize("ts,encode,want", [
    (LJPEG_SV1, lambda a: imagecodecs.ljpeg_encode(a),
     P8_UNEXTENDED_IN_16),
    (JPEGLS, lambda a: imagecodecs.jpegls_encode(a), P8_EXTENDED_FROM_8),
], ids=["jpeg-lossless-by-bits-stored", "jpeg-ls-by-its-own-precision"])
def test_a_precision_8_stream_under_a_signed_bits_stored_16_reads_per_syntax(
        doors, ts, encode, want):
    """S7c (#454, N4): the third transition, and the two syntaxes differ.

    A precision-8 stream under BitsAllocated 16, BitsStored 16, HighBit
    15 and PixelRepresentation 1. **Every door refused this file before
    #454** -- the codec returned `uint8`, and `_sign_extend` refused a
    BitsStored wider than the container it was handed (`cannot
    sign-extend a uint8 decode from BitsStored 16`). Since #454 the
    decode is widened to `uint16` first, so it now returns `int16` at
    every door, and the values follow each syntax's own documented rule
    rather than one shared answer:

    - **JPEG Lossless** is read by BitsStored, which is pydicom's rule
      for it too (`_correct_unused_bits`). Width 16 in a 16-bit
      container is a shift of 0, so the patterns are reinterpreted and
      not extended: 0..255 stay 0..255.
    - **JPEG-LS** is read by the stream's own precision (#478, the
      owner's ruling), which is 8, so the same patterns extend to
      0..127 then -128..-1.

    The two rows differing is the point: the same header and the same
    bytes read differently because the syntaxes carry precision
    differently, and a reader who assumes one answer for "precision 8
    under a signed 16-bit header" is wrong for one of them.

    No pydicom column: neither syntax has a plugin in the environment
    this package installs (only Pillow is present; pydicom raises
    `Unable to decompress ... all plugins are missing dependencies`),
    which is why these files reach the imagecodecs fallback at all.

    Mutant: `_decode_frame` without the widening. Both rows go red,
    refused in S6's words at every door.
    """
    _assert_reads(doors(_dataset(ts, encode(UNSIGNED_8), UNSIGNED_8.shape, 16,
                                 bits_allocated=16, pixel_representation=1)),
                  want)


# ---------------------------------------------------------------------------
# S5 -- a signed header whose HighBit is not BitsStored - 1 is read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("high_bit", [15, 10], ids=["above", "below"])
def test_a_signed_frame_whose_high_bit_is_not_bits_stored_minus_one_is_read_at_every_door(  # noqa: E501  pylint: disable=line-too-long
        doors, high_bit):
    """S5: read as right-aligned BitsStored-bit samples, as pydicom reads it.

    #446 refused this file (owner question Q1, answered with the
    recommendation pending confirmation): HighBit 15 under BitsStored 12
    says the samples sit in bits 4..15, which no decoder output does, and
    refusing was the one reading that could not return a wrong value.
    #455 and #523 overruled it (Q2). No decoder here reads HighBit, the
    same header over an unsigned frame was read in silence, and the
    refusal claimed an extension "from BitsStored" on routes that extend
    from the stream's precision -- so the file is read, at every door,
    and ingest writes one `WARNING` row saying so
    (`tests/test_high_bit_is_a_header_warning.py`).

    Both sides of BitsStored - 1: with HighBit 15 alone, a read that
    broke only below would stay green (found in review of #463, when it
    was the refusal being pinned as an inequality).
    """
    want = SIGNED[12]
    codestream = _ljpeg(_pattern(want, 12), 12)
    _assert_reads(doors(_dataset(LJPEG_SV1, codestream, want.shape, 12,
                                 high_bit=high_bit)), want)


def test_an_unsigned_frame_whose_high_bit_is_not_bits_stored_minus_one_is_untouched(  # noqa: E501  pylint: disable=line-too-long
        doors):
    """S5's twin: an unsigned frame reads the same.

    An unsigned frame is returned as the codec decoded it, whatever its
    HighBit says, which is what pydicom does at every door.
    """
    want = _pattern(SIGNED[12], 12)
    codestream = _ljpeg(want, 12)
    _assert_reads(doors(_dataset(LJPEG_SV1, codestream, want.shape, 12,
                                 high_bit=15, pixel_representation=0)), want)


# ---------------------------------------------------------------------------
# S3 -- a JPEG Lossless fragment of odd length decodes
# ---------------------------------------------------------------------------

def _item(value):
    return struct.pack("<HHI", 0xFFFE, 0xE000, len(value)) + value


def test_an_odd_length_jpeg_lossless_fragment_decodes(doors):
    """S3: lj92 needs the pad byte DICOM framing is supposed to add.

    `imagecodecs.ljpeg_decode` (lj92) reads one byte past the end of its
    input, so an odd-length codestream with nothing after it raises
    `LJ92_ERROR_CORRUPT`. A conformant writer pads every item to even
    length (PS3.5 7.5) and the pad is that byte, so a conformant file
    never shows it -- but pydicom writes and reads an odd item length
    without complaint, and `generate_frames` hands the frame over as
    stored. This is that file: an 8x8 flat 8-bit frame, whose codestream
    is 59 bytes, framed by hand with its odd length. Before the handler
    padded, all three doors refused it; `jpegsof3_decode` reads the same
    59 bytes exactly, so nothing about the stream is wrong.
    """
    want = np.full((8, 8), 7, dtype=np.uint8)
    codestream = imagecodecs.ljpeg_encode(want)
    assert len(codestream) % 2 == 1, len(codestream)
    ds = _dataset(LJPEG_SV1, codestream, want.shape, 8,
                  pixel_representation=0)
    # Replaced by hand: `encapsulate` pads the fragment, which is the
    # whole difference. Empty offset table, the fragment at its odd
    # length, the sequence delimiter.
    ds.PixelData = (_item(b"") + _item(codestream)
                    + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0))
    _assert_reads(doors(ds), want)


# ---------------------------------------------------------------------------
# S4 -- JPEG 2000 is read against its own header, not by this rule
# ---------------------------------------------------------------------------

def test_a_j2k_codestream_is_read_against_its_own_header(doors):
    """S4: BitsStored's correction never reaches J2K; #460's does.

    `jpeg2k_decode` returns the codestream's own signedness, so a J2K
    frame is never the case this module is about: a signed codestream
    arrives signed and already sign-extended (F1's `int16` cases in
    `tests/test_ingest_imagecodecs_fallback.py`), and applying this rule
    to it would raise on its dtype.

    An *unsigned* codestream under PixelRepresentation 1 is the
    contradiction the file makes, and until #460 every door refused it
    (`decoded to uint16, where ... declare int16`). The owner's ruling is
    to read it by the header, at the codestream's own precision, as
    pydicom's plugins read its own `J2K_pixelrep_mismatch.dcm`. So this
    case now reads its values at all three doors. It stays here as the
    boundary between the two rules -- the sign extension is keyed on the
    syntax, and the J2K arm is `_against_pixel_representation`'s, keyed on
    the SIZ header. `tests/test_j2k_signedness_against_pixel_representation.py`
    is where both directions are pinned.

    Three samples, because only 16-bit multi-sample J2K reaches the
    fallback here: pydicom's Pillow plugin decodes 16-bit monochrome
    J2K itself, and a monochrome fixture never leaves pydicom's door.
    """
    want = np.stack([SIGNED[16]] * 3, axis=-1)
    codestream = imagecodecs.jpeg2k_encode(_pattern(want, 16), level=0,
                                           codecformat="J2K", mct=False)
    got = doors(_dataset(J2K_LOSSLESS, codestream, want.shape, 16,
                         photometric="RGB", samples=3))
    _assert_reads(got, want)

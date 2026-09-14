"""A J2K codestream whose signedness contradicts PixelRepresentation (#460).

`jpeg2k_decode` returns the codestream's own signedness, `int16` from a
signed codestream and `uint16` from an unsigned one, whatever
PixelRepresentation says. So one file could carry two answers, and the two
directions of the contradiction were both wrong in their own way.

**The owner's ruling, option C: reinterpret one direction, refuse the
other.**

*Unsigned codestream under PixelRepresentation 1: read by the header.*
This is the shape of a real file -- pydicom's own
`J2K_pixelrep_mismatch.dcm`, a CT from pydicom issue 1149, whose
precision-13 unsigned codestream holds 6192 for -2000. Both pydicom
plugins read it by the header, so `ingest()` and `Instance.get_pixel_data()`
read it as `int16` from -2000 (16-bit monochrome J2K goes through Pillow),
while the handler returned `uint16` from 6192. The handler now
reinterprets it, at the *codestream's* precision, which is pydicom's rule
(`_apply_sign_correction`, keyed on `j2k_precision`) and not BitsStored's:
`test_the_reinterpretation_is_by_the_codestream_precision_not_bits_stored`
is the case where the two differ, asserted against pydicom's own array.

*Signed codestream under PixelRepresentation 0: refused.* There is no one
pydicom answer to agree with. pydicom's correction is keyed on a nonzero
bit shift, so at a precision equal to the container's it does nothing and
the value returned is whatever the plugin reinterpreted: for the
codestream's `[-32768, -800, -1]`, pydicom 3.0.2 with Pillow returns
`uint16 [0, 31968, 32767]`, and with pylibjpeg-openjpeg `uint16 [32768,
64736, 65535]` (measured). Two plugins, two arrays, neither of them the
file's samples.

**The refusal reaches every door since #524.** Until then only a
codestream Pillow cannot decode -- 16-bit colour -- reached the refusal
from all three doors. For monochrome at any depth and for 8-bit colour,
Pillow decoded the file first (#416 is pydicom-first), so `ingest()`
admitted it and the Instance door returned Pillow's reading, the
codestream's samples with the top bit flipped, while only the handler
refused: two answers. The owner's ruling on #524 (Q4) is a gate on the
SIZ ahead of pydicom, in `io_handlers._decode_pixels`, so every door
refuses every shape in one set of words;
`test_every_door_refuses_a_signed_codestream_under_pixel_representation_0`
is the measurement, and pydicom's own shifted reading is asserted beside
it as what the gate keeps out. The real file is asked at
all three too, because its answer is the same at all three. Every value
assertion is against a module literal or against pydicom's own array.
"""
import os
import shutil

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import generate_uid

from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from isocenter.session import DicomSession
from support.decode_doors import through_the_fallback

J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"

#: The extremes and values either side of zero, so an unsigned view
#: (32768 for -32768) and an offset (0 for -32768) both show.
SIGNED_16 = np.tile(np.array([-32768, -800, -1, 0, 32767, 5, -5, 100],
                             dtype=np.int16), (16, 2))
SIGNED_8 = np.tile(np.array([-128, -80, -1, 0, 127, 5, -5, 100],
                            dtype=np.int8), (16, 2))
#: The same samples as unsigned patterns, for the unsigned codestreams.
UNSIGNED_16 = np.tile(np.array([32768, 64736, 65535, 0, 32767, 5, 65531,
                                100], dtype=np.uint16), (16, 2))
UNSIGNED_8 = np.tile(np.array([128, 176, 255, 0, 127, 5, 251, 100],
                              dtype=np.uint8), (16, 2))

#: A 12-bit pattern, for the precision case. Written as a precision-12
#: codestream under BitsStored 16, where reading by BitsStored shifts by 0
#: and returns these patterns and reading by the precision returns
#: `WIDE_12_AS_SIGNED`.
WIDE_12 = np.tile(np.array([3296, 4095, 0, 2047, 2048, 1, 2049, 100],
                           dtype=np.uint16), (16, 2))
WIDE_12_AS_SIGNED = np.tile(np.array([-800, -1, 0, 2047, -2048, 1, -2047,
                                      100], dtype=np.int16), (16, 2))

#: pydicom's own mismatched file: an unsigned precision-13 codestream
#: under PixelRepresentation 1, BitsStored 13, HighBit 12 (issue 1149).
REAL_MIN, REAL_MAX = -2000, 1896
#: What the codestream holds for those, unreinterpreted.
REAL_UNSIGNED_MIN, REAL_UNSIGNED_MAX = 0, 8191

REFUSAL = "codestream is signed"


def _colour(arr):
    return np.stack([arr, arr[::-1], arr.T], axis=-1)


def _dataset(arr, pixel_representation, bits_stored=None, encode=None):
    """A J2K dataset whose codestream carries `arr`'s own signedness.

    `bits_stored` overrides the container's width, for the precision case.
    `encode` overrides the encoder options, for the same.
    """
    samples = 3 if arr.ndim == 3 else 1
    bits = arr.dtype.itemsize * 8
    stored = bits_stored or bits
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = J2K_LOSSLESS
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT460", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows, ds.Columns = arr.shape[:2]
    ds.SamplesPerPixel = samples
    if samples > 1:
        ds.PlanarConfiguration = 0
    ds.PhotometricInterpretation = "RGB" if samples > 1 else "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored = bits, stored
    ds.HighBit, ds.PixelRepresentation = stored - 1, pixel_representation
    # No colour transform, so every sample is carried exactly as given.
    options = dict(encode or {})
    options.setdefault("codecformat", "J2K")
    if samples > 1:
        options.setdefault("mct", False)
    codestream = imagecodecs.jpeg2k_encode(arr, level=0, **options)
    ds.PixelData = encapsulate([codestream], has_bot=True)
    ds["PixelData"].is_undefined_length = True
    return ds


def _write(tmp_path, ds, name="one.dcm"):
    """`ds` in its own folder, and the path to it."""
    src = tmp_path / "src"
    os.makedirs(src, exist_ok=True)
    path = str(src / name)
    ds.save_as(path, enforce_file_format=True)
    return path


def _ingest(tmp_path, folder):
    """`(summary, the one stored instance's pixels or None)`."""
    session = DicomSession(persistence_file=str(tmp_path / "one.db"))
    try:
        summary = session.ingest(folder)
        arr = None
        if summary.ingested:
            instance = [i for p in session.store.patients
                        for st in p.studies for se in st.series
                        for i in se.instances][0]
            instance.unload_pixel_data()
            arr = instance.get_pixel_data()
        return summary, arr
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The refused direction: a signed codestream under PixelRepresentation 0
# ---------------------------------------------------------------------------

_REFUSED_AT_THE_HANDLER = {
    "mono-16": SIGNED_16, "mono-8": SIGNED_8,
    "rgb-16": _colour(SIGNED_16), "rgb-8": _colour(SIGNED_8),
}


@pytest.mark.parametrize("name", list(_REFUSED_AT_THE_HANDLER))
def test_the_handler_refuses_a_signed_codestream_under_pixel_representation_0(
        name):
    """Before: `int16` or `int8` under a header declaring unsigned samples.

    Mutant: `_decode_frame`'s JPEG 2000 branch without the check. Every
    case goes red, returning the signed array.

    Asked of `decode_declared_frames`, the handler's decode, directly:
    every door refuses this file at #524's gate before any decoder runs,
    so this is the one call that still reaches the handler's own raise.
    """
    ds = _dataset(_REFUSED_AT_THE_HANDLER[name], 0)
    with pytest.raises(RuntimeError) as caught:
        imagecodecs_handler.decode_declared_frames(ds, 1)
    words = str(caught.value)
    assert REFUSAL in words, words
    assert "PixelRepresentation 0" in words, words
    # Refused before any relabel: the dataset says what it said.
    assert ds.PixelRepresentation == 0


def test_a_signed_16_bit_colour_codestream_under_pixel_representation_0_is_refused_at_all_three_doors(  # noqa: E501  pylint: disable=line-too-long
        tmp_path):
    """The one synthetic shape that reaches the imagecodecs doors from all three.

    Before: ingest refused it in the fallback's dtype words, and the
    Instance door and the handler returned `int16`. Now all three refuse
    it in the handler's words, which ingest reports from the decode.
    """
    path = _write(tmp_path, _dataset(_colour(SIGNED_16), 0))
    with pytest.raises(RuntimeError):
        _ = pydicom.dcmread(path).pixel_array

    summary, _arr = _ingest(tmp_path, os.path.dirname(path))
    assert summary.ingested == 0
    assert REFUSAL in summary.failures[0][1], summary.failures

    for door, read in (
            ("instance", lambda: Instance(
                generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                file_path=path).get_pixel_data()),
            ("decode_pixels", lambda: through_the_fallback(
                pydicom.dcmread(path)))):
        with pytest.raises(RuntimeError) as caught:
            read()
        assert REFUSAL in str(caught.value), f"{door}: {caught.value}"


# ---------------------------------------------------------------------------
# B1: the refusal reaches every door (#524)
# ---------------------------------------------------------------------------

#: The shapes Pillow decodes, which ingest and the Instance door used to
#: admit with Pillow's shifted reading. 16-bit colour is absent: Pillow
#: cannot decode it, so it was refused at all three doors already and
#: `test_a_signed_16_bit_colour_codestream_..._refused_at_all_three_doors`
#: is its case.
_PILLOW_STILL_READS = {
    "mono-16": SIGNED_16, "mono-8": SIGNED_8, "rgb-8": _colour(SIGNED_8),
}


@pytest.mark.parametrize("name", list(_PILLOW_STILL_READS))
def test_every_door_refuses_a_signed_codestream_under_pixel_representation_0(
        tmp_path, name):
    """One answer, a refusal, for every shape Pillow can decode (B1, #524).

    This pinned the **limit** of #460's refusal: `ingest()` and
    `Instance.get_pixel_data()` go through pydicom first (#416), Pillow
    decodes monochrome at any depth and 8-bit colour, and so these files
    ingested with no error and no row, carrying Pillow's reading -- the
    codestream's samples with the sign bit flipped, `source + 2**(bits-1)`
    exactly, not the file's values -- while only the handler refused. It
    said that it would go red at `summary.ingested` if the SIZ were ever
    gated ahead of pydicom. #524 did that (Q4), so it is inverted: every
    door refuses, in the same words, and pydicom's own reading is still
    asserted, as the shift the gate keeps out of the store.
    """
    arr = _PILLOW_STILL_READS[name]
    ds = _dataset(arr, 0)
    bits = arr.dtype.itemsize * 8

    # pydicom, asked directly, still reads the samples with the top bit
    # flipped: that is what ingest stored until #524.
    flipped = (arr.astype(np.int32) + 2 ** (bits - 1)).astype(f"u{bits // 8}")
    assert np.array_equal(ds.pixel_array, flipped), "pydicom's own reading"

    path = _write(tmp_path, ds)
    summary, stored = _ingest(tmp_path, os.path.dirname(path))
    assert (summary.ingested, stored) == (0, None), "ingest refuses it"
    reason = summary.failures[0][1]
    assert reason.startswith(
        f"Decompression Failed: RuntimeError: the JPEG 2000 codestream is "
        f"signed at precision {bits}, where PixelRepresentation 0 declares "
        f"unsigned samples"), reason

    for door, read in (
            ("instance", lambda: Instance(
                generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                file_path=path).get_pixel_data()),
            ("decode_pixels", lambda: through_the_fallback(
                pydicom.dcmread(path)))):
        with pytest.raises(RuntimeError, match=REFUSAL):
            read()


# ---------------------------------------------------------------------------
# The reinterpreted direction: an unsigned codestream under
# PixelRepresentation 1
# ---------------------------------------------------------------------------

_REINTERPRETED = {
    "mono-16": (UNSIGNED_16, SIGNED_16),
    "mono-8": (UNSIGNED_8, SIGNED_8),
    "rgb-16": (_colour(UNSIGNED_16), _colour(SIGNED_16)),
    "rgb-8": (_colour(UNSIGNED_8), _colour(SIGNED_8)),
}


@pytest.mark.parametrize("name", list(_REINTERPRETED))
def test_the_handler_reinterprets_an_unsigned_codestream_under_pixel_representation_1(  # noqa: E501  pylint: disable=line-too-long
        name):
    """Before: the codec's unsigned array, 32768 where the file means -32768.

    Mutants: the branch removed (every case returns `uint16`/`uint8`
    patterns); the refusal applied to this direction too (every case
    raises).
    """
    codestream, want = _REINTERPRETED[name]
    got, _label = through_the_fallback(_dataset(codestream, 1))
    assert got.dtype == want.dtype, f"{name}: {got.dtype}"
    assert got.tolist() == want.tolist(), name


def test_an_unsigned_16_bit_colour_codestream_under_pixel_representation_1_reads_the_same_at_all_three_doors(  # noqa: E501  pylint: disable=line-too-long
        tmp_path):
    """The synthetic shape all three doors reach, now ingesting.

    Before: ingest refused it (`decoded to uint16, where ... declare
    int16`) and both read doors returned the unsigned patterns. This is
    the flip of `test_signed_lossless_jpeg_decode.py`'s S4, which pinned
    that refusal.
    """
    want = _colour(SIGNED_16)
    path = _write(tmp_path, _dataset(_colour(UNSIGNED_16), 1))
    summary, ingested = _ingest(tmp_path, os.path.dirname(path))
    assert summary.ingested == 1, summary.failures
    assert ingested.dtype == want.dtype
    assert ingested.tolist() == want.tolist()

    for door, arr in (
            ("instance", Instance(generate_uid(),
                                  "1.2.840.10008.5.1.4.1.1.7", 1,
                                  file_path=path).get_pixel_data()),
            ("decode_pixels", through_the_fallback(
                pydicom.dcmread(path))[0])):
        assert arr.dtype == want.dtype, f"{door}: {arr.dtype}"
        assert arr.tolist() == want.tolist(), door


def test_the_reinterpretation_is_by_the_codestream_precision_not_bits_stored(
        tmp_path):
    """A precision-12 codestream under BitsStored 16, where the two differ.

    `imagecodecs.jpeg2k_encode(..., bitspersample=12)` writes `Ssiz` 0x0b:
    unsigned, precision 12. Reading by BitsStored 16 shifts by nothing and
    returns `WIDE_12`'s patterns reinterpreted, which is `WIDE_12` itself
    for every value under 32768. Reading by the precision shifts by 4 and
    returns `WIDE_12_AS_SIGNED`. pydicom reads it the second way
    (`j2k_precision`), so its own array is asserted here beside the
    literal.

    Mutant: `_against_pixel_representation` passing no precision to
    `_sign_extend`, so BitsStored is used. Red.
    """
    ds = _dataset(WIDE_12, 1, bits_stored=16,
                  encode={"bitspersample": 12})
    assert imagecodecs_handler._j2k_sample_layout(
        next(pydicom.encaps.generate_frames(
            ds.PixelData, number_of_frames=1))) == (False, 12)
    path = _write(tmp_path, ds)

    # pydicom decodes 16-bit monochrome J2K itself, so it is a reference
    # here rather than a door that raises.
    reference = pydicom.dcmread(path).pixel_array
    assert reference.dtype == np.int16
    assert reference.tolist() == WIDE_12_AS_SIGNED.tolist()

    got, _label = through_the_fallback(pydicom.dcmread(path))
    assert got.dtype == np.int16, got.dtype
    assert got.tolist() == WIDE_12_AS_SIGNED.tolist()
    # And not the BitsStored reading, which returns the patterns.
    assert got.tolist() != WIDE_12.astype(np.int64).tolist()


def test_pydicoms_own_mismatched_file_reads_minus_2000_at_every_door(tmp_path):
    """`J2K_pixelrep_mismatch.dcm`, the real file behind the ruling.

    A CT from pydicom issue 1149: unsigned precision-13 codestream,
    PixelRepresentation 1, BitsStored 13, HighBit 12. pydicom reads it
    `int16 [-2000, 1896]`, and so did ingest and the Instance door, which
    reach pydicom's Pillow plugin for 16-bit monochrome J2K. The handler
    returned `uint16 [0, 8191]`, the codestream's patterns -- one file,
    two answers. Now all three say -2000.

    Mutant: the reinterpretation removed. The `_decode_pixels` row goes red at
    `uint16 [0, 8191]` while the other two stay green, which is the
    disagreement this closes.
    """
    source = get_testdata_file("J2K_pixelrep_mismatch.dcm")
    assert source, "pydicom's own test file is not installed"
    src = tmp_path / "src"
    os.makedirs(src)
    path = str(src / "mismatch.dcm")
    shutil.copyfile(source, path)

    ds = pydicom.dcmread(path)
    assert (int(ds.PixelRepresentation), int(ds.BitsStored),
            int(ds.HighBit)) == (1, 13, 12)
    codestream = next(pydicom.encaps.generate_frames(
        ds.PixelData, number_of_frames=1))
    assert imagecodecs_handler._j2k_sample_layout(codestream) == (False, 13)
    # What the codestream holds, before any reinterpretation.
    raw = imagecodecs.jpeg2k_decode(codestream)
    assert (raw.dtype, int(raw.min()), int(raw.max())) == (
        np.uint16, REAL_UNSIGNED_MIN, REAL_UNSIGNED_MAX)

    summary, ingested = _ingest(tmp_path, str(src))
    assert summary.ingested == 1, summary.failures
    doors = {
        "ingest": ingested,
        "instance": Instance(generate_uid(), str(ds.SOPClassUID), 1,
                             file_path=path).get_pixel_data(),
        "decode_pixels": through_the_fallback(pydicom.dcmread(path))[0],
        "pydicom": pydicom.dcmread(path).pixel_array,
    }
    for door, arr in doors.items():
        assert arr.dtype == np.int16, f"{door}: {arr.dtype}"
        assert (int(arr.min()), int(arr.max())) == (REAL_MIN, REAL_MAX), door
    # The same array, element for element, at every door.
    for door, arr in doors.items():
        assert arr.tolist() == doors["pydicom"].tolist(), door


# ---------------------------------------------------------------------------
# The agreeing shapes, which neither rule may touch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,arr,pixel_representation,want", [
    ("signed-under-1", _colour(SIGNED_16), 1, _colour(SIGNED_16)),
    ("unsigned-under-0", _colour(UNSIGNED_16), 0, _colour(UNSIGNED_16)),
], ids=["signed-under-1", "unsigned-under-0"])
def test_a_codestream_whose_signedness_agrees_with_its_header_still_reads(
        name, arr, pixel_representation, want):
    """The two agreeing shapes, which neither rule must reach.

    Mutants: the refusal keyed on PixelRepresentation 0 alone (refusing an
    unsigned codestream under 0), or on the codestream's sign alone
    (refusing a signed one under 1). Each turns one case red. And the
    reinterpretation reaching a signed codestream under 1 raises in
    `_sign_extend`'s dtype check, which turns the first case red.
    """
    got, _label = through_the_fallback(_dataset(arr, pixel_representation))
    assert got.dtype == want.dtype, f"{name}: {got.dtype}"
    assert got.tolist() == want.tolist(), name


def test_a_jp2_wrapped_codestream_reaches_the_same_rule(tmp_path):
    """A JP2 *box* under .90, which this project itself wrote until #404.

    `_j2k_sample_layout` walks the boxes to `jp2c` before reading the SIZ,
    so a JP2-wrapped unsigned codestream under PixelRepresentation 1 is
    reinterpreted exactly as the bare one is. Without the unwrap the
    parse returns None and the file keeps the codec's unsigned array --
    silently, which is the shape this rule exists to close.

    Mutant: the JP2 branch deleted. Red, returning `uint16` patterns.
    """
    want = SIGNED_16
    ds = _dataset(UNSIGNED_16, 1, encode={"codecformat": "JP2"})
    codestream = next(pydicom.encaps.generate_frames(
        ds.PixelData, number_of_frames=1))
    assert bytes(codestream).startswith(b"\x00\x00\x00\x0c\x6a\x50\x20\x20")
    assert imagecodecs_handler._j2k_sample_layout(codestream) == (False, 16)
    got, _label = through_the_fallback(ds)
    assert got.dtype == want.dtype, got.dtype
    assert got.tolist() == want.tolist()


def test_a_jp2_box_of_zero_length_is_refused_rather_than_walked_forever():
    """The `length <= 0` guard in the box walk (N3).

    A JP2 signature followed by a box declaring length 0 advances the
    offset by nothing, so without this guard the walk never terminates
    and `_j2k_sample_layout` **hangs** instead of returning. A hang is
    the #250 class of defect -- an interpreter that must be killed rather
    than a test that goes red -- and the guard was the only arm of this
    parser with no case, so deleting it looked free.

    Built by hand rather than by the encoder, because no encoder emits
    it: the signature box, then a box whose 4-byte length is zero.
    """
    # The zero-length box must not be `jp2c`: that type is recognised and
    # broken out of before the guard is consulted, so a `jp2c` fixture
    # tests nothing here (measured -- it leaves the mutant alive). `ftyp`
    # is an ordinary box the walk must step over, and a declared length of
    # 0 advances the offset by nothing.
    forever = (b"\x00\x00\x00\x0c\x6a\x50\x20\x20\x0d\x0a\x87\x0a"
               + b"\x00\x00\x00\x00" + b"ftyp"
               + b"\x00\x00\x00\x08" + b"jp2c" + b"\xff\x4f\xff\x51")
    assert imagecodecs_handler._j2k_sample_layout(forever) is None


def test_a_bitstream_that_is_no_codestream_leaves_the_array_alone():
    """`_j2k_sample_layout` returns None rather than guessing.

    Unreachable through a decode -- the SIZ is what tells a decoder the
    image's size -- so it is asserted on the parser directly. The None
    arm is what keeps a stream this cannot read from being refused for
    what its header might have said.
    """
    assert imagecodecs_handler._j2k_sample_layout(b"") is None
    assert imagecodecs_handler._j2k_sample_layout(b"\xff\xd8\xff\xf7") is None
    # SOC, but the second marker is not SIZ.
    assert imagecodecs_handler._j2k_sample_layout(
        b"\xff\x4f\xff\x52" + b"\x00" * 64) is None
    # SOC then SIZ, but the segment stops before Ssiz.
    assert imagecodecs_handler._j2k_sample_layout(
        b"\xff\x4f\xff\x51" + b"\x00" * 8) is None
    # A JP2 signature box with no `jp2c` box after it.
    assert imagecodecs_handler._j2k_sample_layout(
        b"\x00\x00\x00\x0c\x6a\x50\x20\x20\x0d\x0a\x87\x0a") is None

"""The codec advice at the read door is decided from state, not from words (#623 P5).

`Instance.get_pixel_data()` ends a failed read of the source file in one
of two messages. `Lazy load failed for instance <uid>: ...` repeats the
reason. `Failed to decompress pixel data for instance <uid> ...` adds
advice: a decoder is missing, install it.

Until #623 the advice was chosen by two substrings of the failure's
message, "decompress" and "missing dependencies". Measured on 067982ec
(3.12.14 and 3.14.7t, pydicom 3.0.2):

- a JPEG Extended (`.51`) 12-bit file with imagecodecs not importable got
  **no advice**, because pydicom's words for it ("Unable to decode as
  exceptions were raised by all available plugins") hold neither
  substring;
- a JPEG Lossless (`.70`) file in the same state got advice that named
  `'pillow', 'pylibjpeg', or 'gdcm'` and **not imagecodecs**, the one
  package that was missing, under an `Active pydicom handlers: [...]`
  line read from an API pydicom 4.0 removes;
- a `TypeError` whose message said "cannot decompress this" got the
  advice, with every decoder present.

The advice now has two arms, each a fact about the installation:

1. the imagecodecs fallback was asked and imagecodecs did not import
   (`imagecodecs_handler.ImagecodecsUnavailable` in the failure's cause
   chain);
2. the file's transfer syntax is not one the fallback reads, and
   pydicom's own decoder for it has no usable plugin.

Arm 2 has no real file in pydicom 3.0.2: every decoder outside the
fallback's syntaxes (the native ones, Deflated, RLE) needs no plugin. It
is tested with a decoder made unavailable.

Names `isocenter.entities` and `isocenter.imagecodecs_handler`.
"""
import imagecodecs
import numpy as np
import pytest
from pydicom.pixels.decoders.base import Decoder
from pydicom.uid import generate_uid

from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from support.decode_doors import (EXPLICIT_LE, J2K_LOSSLESS, LJPEG_SV1,
                                  SOP_CLASS, dataset, write)

JPEG_EXTENDED = "1.2.840.10008.1.2.4.51"
RLE = "1.2.840.10008.1.2.5"

_YY, _XX = np.mgrid[0:8, 0:8]
#: Precision-12 samples, up to 3920.
IMG12 = (_XX * 500 + _YY * 60).astype(np.uint16)
IMG8 = (_XX * 30 + _YY * 4).astype(np.uint8)

#: A broken wheel's shape: installed, with a shared library missing.
BROKEN_IMPORT = ImportError(
    "libjpeg.so.8: cannot open shared object file: No such file or directory")

ARM_1 = ("Missing image codecs: imagecodecs did not import. This library "
         "installs it and reads this transfer syntax with it where pydicom "
         "cannot; reinstall imagecodecs. Running `python -c \"import "
         "imagecodecs\"` shows the import's own error.")


def _door(tmp_path, ds, name="one"):
    """The `RuntimeError` a bare file-backed Instance raises for `ds`."""
    path = write(tmp_path, ds, name)
    inst = Instance(ds.SOPInstanceUID, SOP_CLASS, 1, file_path=path)
    with pytest.raises(RuntimeError) as caught:
        inst.get_pixel_data()
    return inst, caught.value


def _without_imagecodecs(monkeypatch):
    monkeypatch.setattr(imagecodecs_handler, "imagecodecs", None)
    monkeypatch.setattr(imagecodecs_handler, "IMPORT_ERROR", BROKEN_IMPORT)


def _extended_12_bit():
    return dataset(JPEG_EXTENDED,
                   [imagecodecs.jpeg8_encode(IMG12, bitspersample=12)],
                   rows=8, cols=8, bits_allocated=16, bits_stored=12)


def _lossless():
    return dataset(LJPEG_SV1,
                   [imagecodecs.ljpeg_encode(IMG12, bitspersample=12)],
                   rows=8, cols=8, bits_allocated=16, bits_stored=12)


# ---------------------------------------------------------------------------
# Arm 1: imagecodecs did not import
# ---------------------------------------------------------------------------

def test_the_two_files_read_while_imagecodecs_imports(tmp_path):
    """The control: each fixture is refused below only for the import.

    `.51` is lossy, so its array is held to the samples within 2; `.70`
    is exact.
    """
    for name, ds, slack in (("a", _extended_12_bit(), 2),
                            ("b", _lossless(), 0)):
        path = write(tmp_path, ds, name)
        inst = Instance(ds.SOPInstanceUID, SOP_CLASS, 1, file_path=path)
        got = inst.get_pixel_data()
        assert got.shape == IMG12.shape and got.dtype == np.uint16
        assert int(np.abs(got.astype(np.int64) - IMG12).max()) <= slack


@pytest.mark.parametrize("build, ts", [
    (_extended_12_bit, JPEG_EXTENDED),
    (_lossless, LJPEG_SV1),
], ids=[".51", ".70"])
def test_an_unimportable_imagecodecs_gets_advice_that_names_it(
        tmp_path, monkeypatch, build, ts):
    """Red on 067982ec for both: `.51` got no advice, `.70` named three others.

    The whole message is asserted but for the underlying error's line,
    which is pydicom's and differs by syntax.
    """
    _without_imagecodecs(monkeypatch)
    inst, error = _door(tmp_path, build())
    lines = str(error).split("\n")

    assert lines[0] == (f"Failed to decompress pixel data for instance "
                        f"{inst.sop_instance_uid} (Transfer Syntax: {ts}).")
    assert lines[1].startswith("Underlying Error: RuntimeError: "), lines[1]
    assert lines[-1] == ARM_1
    whole = str(error)
    # The import failure's class is in the underlying error; its words
    # are not (they can hold an install path), and the advice line names
    # the command that shows them.
    assert ("imagecodecs could not decode it either: ImagecodecsUnavailable: "
            "imagecodecs is not available: its import raised ImportError "
            "(caused by ") in whole
    assert "libjpeg.so.8" not in whole
    assert "Active pydicom handlers" not in whole
    assert "gdcm' are installed" not in whole
    assert str(tmp_path) not in whole


def test_the_refusal_for_an_unimportable_imagecodecs_is_its_own_class(
        monkeypatch):
    """`_unavailable()` returns the class arm 1 looks for, still a `RuntimeError`.

    It is raised `from IMPORT_ERROR` and wrapped by
    `_decode_with_imagecodecs`, so the door finds it as a cause. Its name
    is in the row a user reads; pinned so a rename is a decision.
    """
    monkeypatch.setattr(imagecodecs_handler, "IMPORT_ERROR", BROKEN_IMPORT)
    refusal = imagecodecs_handler._unavailable()  # pylint: disable=protected-access

    assert type(refusal) is imagecodecs_handler.ImagecodecsUnavailable
    assert isinstance(refusal, RuntimeError)
    assert type(refusal).__name__ == "ImagecodecsUnavailable"
    assert type(refusal).__module__ == "isocenter.imagecodecs_handler"


# ---------------------------------------------------------------------------
# No advice for a failure that only mentions decompression
# ---------------------------------------------------------------------------

def _raising(monkeypatch, uid, error):
    real = Decoder.as_array
    calls = []

    def raises(self, src, **kwargs):
        if str(self.UID) != uid:
            return real(self, src, **kwargs)
        calls.append(uid)
        raise error

    monkeypatch.setattr(Decoder, "as_array", raises)
    return calls


def _j2k():
    return dataset(J2K_LOSSLESS,
                   [imagecodecs.jpeg2k_encode(IMG8, level=0,
                                              codecformat="J2K")],
                   rows=8, cols=8, bits_allocated=8)


def _native():
    return dataset(EXPLICIT_LE, rows=8, cols=8, bits_allocated=8,
                   native=IMG8.tobytes())


def test_a_type_error_that_says_decompress_gets_no_advice(tmp_path,
                                                          monkeypatch):
    """Red on 067982ec: the substring chose the advice for any class."""
    calls = _raising(monkeypatch, J2K_LOSSLESS,
                     TypeError("cannot decompress this, says a TypeError"))
    inst, error = _door(tmp_path, _j2k())

    assert calls, "pydicom was never asked"
    assert str(error) == (
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"TypeError: cannot decompress this, says a TypeError")


def test_missing_dependencies_in_the_words_alone_gets_no_advice(
        tmp_path, monkeypatch):
    """A `RuntimeError` in pydicom's own words, where every decoder is present.

    Explicit VR Little Endian needs no plugin, so nothing is missing
    whatever the message says. Red on 067982ec.
    """
    words = ("Unable to decompress 'Explicit VR Little Endian' pixel data "
             "because all plugins are missing dependencies")
    calls = _raising(monkeypatch, EXPLICIT_LE, RuntimeError(words))
    inst, error = _door(tmp_path, _native())

    assert calls, "pydicom was never asked"
    assert str(error) == (f"Lazy load failed for instance "
                          f"{inst.sop_instance_uid}: RuntimeError: {words}")


# ---------------------------------------------------------------------------
# Arm 2: pydicom has no usable plugin for a syntax the fallback does not read
# ---------------------------------------------------------------------------

NO_PLUGIN = ("Unable to decompress 'RLE Lossless' pixel data because all "
             "plugins are missing dependencies:\n\tpylibjpeg - requires "
             "pylibjpeg>=2.0 and pylibjpeg-rle>=2.0")


@pytest.fixture
def rle_has_no_plugin(monkeypatch):
    """pydicom's RLE decoder reports no usable plugin, and refuses as it then would.

    `._error` is what the decoder raises; a test sets it. The real decoder
    object is kept, so `missing_dependencies` is pydicom's own list.
    """
    real_available = Decoder.is_available
    state = {"error": RuntimeError(NO_PLUGIN), "asked": 0}

    def raises(self, src, **kwargs):  # pylint: disable=unused-argument
        assert str(self.UID) == RLE
        state["asked"] += 1
        raise state["error"]

    monkeypatch.setattr(Decoder, "is_available", property(
        lambda self: False if str(self.UID) == RLE
        else real_available.fget(self)))
    monkeypatch.setattr(Decoder, "as_array", raises)
    return state


def _rle(frames=None, streams=1):
    # The bytes are never decoded: the decoder is patched to refuse.
    return dataset(RLE, [b"\x01\x02\x03\x04"] * streams, rows=8, cols=8,
                   bits_allocated=8, frames=frames)


def test_a_syntax_with_no_usable_pydicom_plugin_gets_advice_naming_pydicoms_list(
        tmp_path, rle_has_no_plugin):
    """The whole message. The list is `Decoder.missing_dependencies`."""
    inst, error = _door(tmp_path, _rle())

    assert rle_has_no_plugin["asked"] == 1
    assert str(error) == (
        f"Failed to decompress pixel data for instance "
        f"{inst.sop_instance_uid} (Transfer Syntax: {RLE}).\n"
        f"Underlying Error: RuntimeError: {NO_PLUGIN}\n"
        f"Missing image codecs: no installed pydicom plugin reads this "
        f"transfer syntax. pydicom lists: pylibjpeg - requires "
        f"pylibjpeg>=2.0 and pylibjpeg-rle>=2.0.")


def test_a_value_error_under_a_missing_plugin_gets_no_advice(
        tmp_path, rle_has_no_plugin):
    """Arm 2 is for the `RuntimeError` pydicom raises when it has no plugin."""
    rle_has_no_plugin["error"] = ValueError("a header pydicom refuses")
    inst, error = _door(tmp_path, _rle())

    assert rle_has_no_plugin["asked"] == 1
    assert str(error) == (
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"ValueError: a header pydicom refuses")


def test_a_refusal_made_before_any_decoder_is_asked_gets_no_advice(
        tmp_path, rle_has_no_plugin):
    """The door's own frame-count refusal is about the file, not the install.

    One offset in the Basic Offset Table under NumberOfFrames 2: refused
    before the decoder is asked, so that no decoder could have read it is
    not what went wrong.
    """
    inst, error = _door(tmp_path, _rle(frames=2))

    assert rle_has_no_plugin["asked"] == 0
    assert str(error) == (
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"RuntimeError: Basic Offset Table names 1 frames; NumberOfFrames "
        f"declares 2")


def test_a_refusal_the_decode_door_makes_before_pydicom_gets_no_advice(
        tmp_path, rle_has_no_plugin):
    """A 12-byte Extended Offset Table beside 12 of Lengths, with no RLE plugin.

    `_decode_pixels` refuses the table before it asks pydicom anything,
    and that is past the point where the read door notes a decoder is
    about to be asked. At 32696465 this file got arm 2's advice under
    its own refusal (review of #1062, finding 4): told to install a
    plugin for a file no plugin reads.
    """
    ds = _rle()
    ds.add_new(0x7FE00001, "OV", b"\0" * 12)
    ds.add_new(0x7FE00002, "OV", b"\0" * 12)
    inst, error = _door(tmp_path, ds)

    assert rle_has_no_plugin["asked"] == 0
    assert str(error) == (
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"RuntimeError: Extended Offset Table is 12 bytes long, which is "
        f"not a whole number of 8-byte offsets, so it names no whole number "
        f"of frames.")


def test_a_fallback_syntax_refused_on_its_content_gets_no_advice(tmp_path):
    """`.70` has no pydicom plugin here, and that is not arm 2's case.

    imagecodecs read the stream and refused it on what it holds: a
    precision-12 stream under BitsAllocated 8. Installing a plugin changes
    nothing. Green before #623 by the substring's exemption; pinned so the
    state predicate keeps it.
    """
    ds = dataset(LJPEG_SV1,
                 [imagecodecs.ljpeg_encode(IMG12, bitspersample=12)],
                 rows=8, cols=8, bits_allocated=8, bits_stored=8)
    inst, error = _door(tmp_path, ds)

    words = str(error)
    assert words.startswith(
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"RuntimeError: Unable to decompress "), words
    assert ("imagecodecs could not decode it either: it decoded to uint16, "
            "where BitsAllocated 8 and PixelRepresentation 0 declare uint8"
            ) in words, words
    assert "Missing image codecs" not in words


def test_a_syntax_no_decoder_implements_gets_no_advice(tmp_path):
    """An MPEG2 file: asking pydicom for its decoder raises, inside the `except`.

    `get_decoder` answers `NotImplementedError`, which is a `RuntimeError`
    subclass; the lookup that decides arm 2 must not let its own escape.
    """
    ds = dataset("1.2.840.10008.1.2.4.100", [b"\x00\x01\x02\x03"], rows=8,
                 cols=8, bits_allocated=8)
    path = write(tmp_path, ds)
    inst = Instance(generate_uid(), SOP_CLASS, 1, file_path=path)

    with pytest.raises(RuntimeError) as caught:
        inst.get_pixel_data()

    words = str(caught.value)
    assert words.startswith(
        f"Lazy load failed for instance {inst.sop_instance_uid}: "
        f"NotImplementedError: "), words
    assert "Missing image codecs" not in words

"""The signed JPEG 2000 refusal yields to pydicom's refusal of the header (#1085).

A signed codestream under PixelRepresentation 0 is refused before pydicom
is asked (#524), because Pillow decodes it with the samples shifted and
no error. Asked before pydicom, it answered first for a header pydicom
refuses too: BitsStored 17 under BitsAllocated 16 was refused in the
signed sentence, where every other early refusal (the 1-bit and the
ragged Extended Offset Table) yields to pydicom's sentence naming the
element. Measured on 3e4ee91a, 3.12.14 and 3.14.7t.

Now the signed refusal asks `imagecodecs_handler._header_verdict` and
withholds its sentence only where pydicom refuses the header
(`HEADER_REFUSED`): the decode that follows then raises pydicom's own
sentence. Where pydicom cannot be asked at all (`HEADER_UNASKED`: no
runner, or no `_validate_options`), it refuses in the signed sentence
all the same -- failing closed, because withholding there would hand
the file to Pillow (owner ruling 2026-11-22, Q6).

A count that overrides NumberOfFrames for the decode cannot make that
verdict wrong (review of X7, finding 1): two whole signed codestreams
under NumberOfFrames -1 were refused by the verdict, so the signed
sentence was withheld, and then counted one frame per fragment
(`_whole_stream_fragments`, #1088), whose `number_of_frames` overrode
the element pydicom refused, so Pillow decoded the samples shifted by
2**15 and ingest stored them. The whole-stream count now declines a
NumberOfFrames pydicom refuses, and the decode raises pydicom's sentence
naming it.

Names `isocenter.imagecodecs_handler` and `isocenter.io_handlers`.
"""
import os
from io import BytesIO

import numpy as np
import pydicom
import pytest
from pydicom.encaps import encapsulate, generate_fragments, parse_basic_offsets
from pydicom.pixels.decoders.base import DecodeRunner

from isocenter import imagecodecs_handler
from isocenter.imagecodecs_handler import (HEADER_REFUSED, HEADER_UNASKED,
                                           _header_verdict,
                                           signed_codestream_refusal)
from isocenter.io_handlers import _decode_pixels, ingest_worker
from support.decode_doors import through_the_fallback
from test_j2k_signedness_against_pixel_representation import (SIGNED_16,
                                                              _dataset,
                                                              _ingest,
                                                              _write)

SIGNED = "codestream is signed"


def _signed():
    return _dataset(SIGNED_16, 0)


def _bits_stored_17(ds):
    ds.BitsStored = 17


def _label(ds):
    ds.PhotometricInterpretation = "NONSENSE"


def _no_rows(ds):
    ds.Rows = 0


#: `(id, edit, pydicom's class, words its sentence holds)`.
BAD_HEADERS = [
    ("bits-stored-17", _bits_stored_17, ValueError,
     "(0028,0101) 'Bits Stored' value of '17' is invalid"),
    ("label-nonsense", _label, ValueError,
     "(0028,0004) 'Photometric Interpretation' value 'NONSENSE'"),
    ("rows-0", _no_rows, ValueError, "(0028,0010) 'Rows'"),
]


def test_a_valid_header_keeps_the_signed_sentence(tmp_path):
    ds = _signed()
    words = signed_codestream_refusal(ds)
    assert words is not None and SIGNED in words

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == words
    path = _write(tmp_path, _signed())
    assert ingest_worker(path)[-1] == (
        f"Decompression Failed: RuntimeError: {words}")


@pytest.mark.parametrize("edit, kind, holds",
                         [case[1:] for case in BAD_HEADERS],
                         ids=[case[0] for case in BAD_HEADERS])
def test_a_header_pydicom_refuses_gets_pydicoms_sentence(tmp_path, edit,
                                                         kind, holds):
    """Red while the signed sentence answers first."""
    ds = _signed()
    edit(ds)
    assert _header_verdict(ds) == HEADER_REFUSED

    assert signed_codestream_refusal(ds) is None
    with pytest.raises(kind) as caught:
        _decode_pixels(ds)
    assert holds in str(caught.value), caught.value
    assert SIGNED not in str(caught.value)

    path = _write(tmp_path, ds)
    row = ingest_worker(path)[-1]
    assert row.startswith(f"Decompression Failed: {kind.__name__}: "), row
    assert holds in row and SIGNED not in row, row


def test_the_signed_refusal_holds_when_pydicom_cannot_be_asked(monkeypatch):
    """Red if the signed refusal withholds on `HEADER_UNASKED`.

    The fail-open trap: the 1-bit and ragged refusals lose only their
    wording when the guard cannot ask; withholding this one would hand a
    signed codestream to Pillow, which shifts its samples with no error.
    """
    # Defined on the runner's base class too, so removed from each.
    for cls in DecodeRunner.__mro__:
        if "_validate_options" in vars(cls):
            monkeypatch.delattr(cls, "_validate_options")
    ds = _signed()
    _bits_stored_17(ds)
    assert _header_verdict(ds) == HEADER_UNASKED

    words = signed_codestream_refusal(ds)
    assert words is not None and SIGNED in words
    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)
    assert str(caught.value) == words


def test_an_unimportable_runner_is_unasked_and_still_refused(monkeypatch):
    """The other way the question cannot be put: no runner class to import."""
    import pydicom.pixels.decoders.base as base  # pylint: disable=import-outside-toplevel
    monkeypatch.delattr(base, "DecodeRunner")
    ds = _signed()
    assert _header_verdict(ds) == HEADER_UNASKED

    words = signed_codestream_refusal(ds)
    assert words is not None and SIGNED in words


def test_a_raise_inside_the_question_is_a_refusal_not_unasked(monkeypatch):
    """`UNASKED` is decided before the call, never from what it raises."""
    def boom(_self):
        raise AttributeError("inside the validation")
    monkeypatch.setattr(DecodeRunner, "_validate_options", boom)
    ds = _signed()

    assert _header_verdict(ds) == HEADER_REFUSED
    assert signed_codestream_refusal(ds) is None


def test_an_unsigned_file_is_not_this_refusals(tmp_path):
    ds = _signed()
    ds.PixelRepresentation = 1
    assert signed_codestream_refusal(ds) is None
    assert imagecodecs_handler.refusal_before_decode(ds) is None
    assert pydicom.dcmread(_write(tmp_path, ds)).PixelRepresentation == 1


@pytest.mark.parametrize("edit, kind, holds",
                         [case[1:] for case in BAD_HEADERS],
                         ids=[case[0] for case in BAD_HEADERS])
def test_the_fallback_route_gives_pydicoms_sentence_too(edit, kind, holds):
    """pydicom unable to decode: `_validate_like_pydicom` names the element."""
    ds = _signed()
    edit(ds)

    with pytest.raises(kind) as caught:
        through_the_fallback(ds)
    assert holds in str(caught.value), caught.value
    assert SIGNED not in str(caught.value)


def _two_whole_signed_codestreams(number_of_frames):
    """The signed file's codestream twice, one per fragment, no offset table."""
    ds = _signed()
    buf = BytesIO(ds.PixelData)
    parse_basic_offsets(buf)
    stream = bytes(next(generate_fragments(buf)))
    ds.PixelData = encapsulate([stream, stream], has_bot=False)
    ds["PixelData"].is_undefined_length = True
    if number_of_frames is None:
        if "NumberOfFrames" in ds:
            del ds.NumberOfFrames
    else:
        ds.NumberOfFrames = number_of_frames
    return ds


NUMBER_OF_FRAMES = "(0028,0008) 'Number of Frames' value of '-1' is invalid"


@pytest.mark.parametrize("number_of_frames", [None, 1, 0, -1],
                         ids=["absent", "1", "0", "-1"])
def test_two_whole_signed_codestreams_are_never_stored(tmp_path,
                                                       number_of_frames):
    """Red if a count's `number_of_frames` overrides the element pydicom refused.

    The outcome, whatever NumberOfFrames holds: the file is not ingested,
    and nothing is stored shifted by 2**15. Under -1, pydicom refuses the
    header, and its sentence naming the element is the one given (Q6).
    """
    path = _write(tmp_path, _two_whole_signed_codestreams(number_of_frames))

    row = ingest_worker(path)[-1]

    assert row is not None, "a signed codestream under PR 0 was decoded"
    if number_of_frames == -1:
        assert row == f"Decompression Failed: ValueError: {NUMBER_OF_FRAMES}" \
            ", it must be greater than or equal to 1", row
    else:
        assert SIGNED in row, row
    summary, stored = _ingest(tmp_path, os.path.dirname(path))
    shifted = (SIGNED_16.astype(np.int32) + 2 ** 15).astype(np.uint16)
    assert stored is None or not np.array_equal(stored, shifted), (
        "signed samples stored shifted by 2**15")
    assert summary.ingested == 0


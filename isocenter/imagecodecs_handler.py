"""A pixel decoder built on `imagecodecs`, and the frame-count check (#418).

`offset_table_frame_count` compares the frame count an encapsulated
`PixelData`'s offset table names with the one `NumberOfFrames` declares.
It is shared by this module's `get_pixel_data`, by
`Instance.get_pixel_data`'s file arm, by `ingest_worker` for the top
level, by `_decode_nested_pixels` for an icon (#433) and by
`_decode_pixels`' imagecodecs fallback (#416), so none of them can
disagree about what a mismatch is.

Two decoders share `_decode_frame`: `get_pixel_data`, the read path's
fallback, which refuses a mismatch itself; and `decode_declared_frames`,
ingest's fallback, which decodes exactly the frames its caller asks for
because that caller has already counted the table and decided (#418's
truncation, which a refusal here would turn into a rejected file).

**Signed samples (#446).** `ljpeg_decode` and `jpegls_decode` return the
masked unsigned pattern of every sample; `_decode_frame` sign-extends it
when PixelRepresentation is 1 -- from BitsStored for JPEG Lossless, from
the stream's own precision for JPEG-LS (#478) -- so both decoders return
the signed values the file stores. See `_sign_extend`.

**JPEG 2000 carries its own signedness (#460).** `jpeg2k_decode` returns
what the codestream's SIZ declares, signed or unsigned, whatever
PixelRepresentation says, so the two can contradict each other.
`_j2k_sample_layout` reads that header and `_decode_frame` acts on the
disagreement: an *unsigned* codestream under PixelRepresentation 1 is
reinterpreted by the header, through the same `_sign_extend`, at the
codestream's own precision; a *signed* codestream under
PixelRepresentation 0 is refused. See `_against_pixel_representation`.

**Colour (#464, #482).** `CONVERTS_TO` is the one table of conversions
this handler makes (8-bit YBR_FULL JPEG-LS to RGB). `get_pixel_data`
applies it and relabels its dataset, and ingest applies it through
`colour_conversion` and `convert_colour`, so the read doors and ingest
return the same bytes under the same label. `DECODER_RELABELS` is the
other half: the conversions the codec has already made (JPEG 2000
YBR_RCT/ICT to RGB), which `get_pixel_data` relabels without converting.

**Its limit, stated.** An *empty* Basic Offset Table with no Extended
Offset Table is legal (PS3.5 A.4) and names no frames, and the fragments
alone do not say where one frame ends and the next begins -- one frame
may legally span several fragments. So a multi-fragment file with an
empty table cannot be checked, and it is decoded as it always was: the
single-frame arm yields frame 0. That is a known silence, not a closed
one.
"""
import struct
import sys
from itertools import islice
from typing import Optional, Tuple, Union

import numpy as np
from pydicom.uid import UID
from pydicom.encaps import generate_frames, parse_basic_offsets
from pydicom.pixels import convert_color_space

from .logger import describe_exception
IMPORT_ERROR = None
try:
    import imagecodecs
except ImportError as e:
    imagecodecs = None
    IMPORT_ERROR = e


def is_available():
    """
    Checks if `imagecodecs` library is installed and importable.

    Returns:
        bool: True if available, False otherwise.
    """
    if imagecodecs is None:
        # Log to stderr so it appears in logs even if pydicom swallows the handler check
        #
        # `describe_exception`, not `{IMPORT_ERROR}` (#500). A broken
        # `imagecodecs` install whose `__init__` ends in a bare `raise
        # ImportError` renders as `str()` of nothing, so this line said
        # "NOT AVAILABLE. Import Error: " and named neither the type nor
        # a reason. The `if` is not defensive about a missing global: a
        # test may set `imagecodecs` to None on its own to exercise the
        # unavailable path (`tests/test_imagecodecs_edge_cases.py::
        # test_is_available_import_error`), leaving `IMPORT_ERROR` at its
        # import-time None, and `describe_exception(None)` has no type to
        # name. `_unavailable()` below guards the same case for the same
        # reason.
        reason = (describe_exception(IMPORT_ERROR)
                  if IMPORT_ERROR is not None else "none recorded")
        print(
            f"[isocenter_imagecodecs_handler] NOT AVAILABLE. Import Error: {reason}",
            file=sys.stderr)
        return False
    return True


def _unavailable() -> RuntimeError:
    """The one refusal both decoders raise when imagecodecs did not import.

    It carries the import failure's own words (#444). They used to reach
    only the stderr print in `is_available()`, which a worker, a notebook
    or a log-only deployment may never show, while the raise said a bare
    "imagecodecs is not available" -- and "libjpeg.so.8: cannot open
    shared object file" is the whole clue to the fix. Read at call time,
    not bound at import, so the module global is what it describes.
    Callers raise it `from IMPORT_ERROR`, so the cause is chained as well
    as quoted.
    """
    if IMPORT_ERROR is None:
        return RuntimeError("imagecodecs is not available")
    return RuntimeError(
        f"imagecodecs is not available: "
        f"{type(IMPORT_ERROR).__name__}: {IMPORT_ERROR}")


# UID Constants
JPEGLossless = UID("1.2.840.10008.1.2.4.57")
JPEGLosslessSV1 = UID("1.2.840.10008.1.2.4.70")
JPEG2000Lossless = UID("1.2.840.10008.1.2.4.90")
JPEG2000 = UID("1.2.840.10008.1.2.4.91")
JPEGBaseline = UID("1.2.840.10008.1.2.4.50")
JPEGExtended = UID("1.2.840.10008.1.2.4.51")
JPEGLSLossless = UID("1.2.840.10008.1.2.4.80")
JPEGLSLossy = UID("1.2.840.10008.1.2.4.81")

#: The syntaxes whose frames are JPEG 2000 codestreams, and so carry a
#: SIZ marker `_j2k_sample_layout` reads, and the JPEG-LS ones, whose
#: frame header `_jpegls_precision` reads. One set each, so the decode,
#: the signedness gate and ingest's HighBit row cannot disagree about
#: which files have a stream precision.
J2K_SYNTAXES = frozenset({JPEG2000Lossless, JPEG2000})
JPEGLS_SYNTAXES = frozenset({JPEGLSLossless, JPEGLSLossy})

HANDLER_NAME = "isocenter_imagecodecs_handler"

DEPENDENCIES = {
    "imagecodecs": ("http://www.lfd.uci.edu/~gohlke/pythonlibs/#imagecodecs", "imagecodecs"),
}

#: No RLE Lossless, deliberately (#447). This list named it, and the arm
#: that decoded it called `imagecodecs.rle_decode`, which no imagecodecs
#: this package supports has ever had -- so `supports_transfer_syntax`
#: said yes and every RLE decode here raised `AttributeError`. pydicom's
#: own RLE decoder needs no dependency and is what always read RLE, so
#: `Instance.get_pixel_data()` never needs this handler for it. Do not
#: re-add it on the strength of `imagecodecs.dicomrle_decode`: that
#: returns planar big-endian bytes, and a second RLE decoder behind one
#: that cannot fail for want of a plugin would have no caller.
SUPPORTED_TRANSFER_SYNTAXES = [
    JPEGLossless,
    JPEGLosslessSV1,
    JPEG2000Lossless,
    JPEG2000,
    JPEGBaseline,
    JPEGExtended,
    JPEGLSLossless,
    JPEGLSLossy,
]


def supports_transfer_syntax(transfer_syntax):
    """
    Checks if the transfer syntax is supported by this handler.

    Args:
        transfer_syntax (UID): The Transfer Syntax UID.

    Returns:
        bool: True if supported.
    """
    return transfer_syntax in SUPPORTED_TRANSFER_SYNTAXES


#: What `offset_table_frame_count` returns: ``(table_frames,
#: declared_frames, declared_raw, table_name)``. ``declared_raw`` is
#: NumberOfFrames as the file states it -- an int, ``""`` when the element
#: is present and empty, None when it is absent.
FrameCount = Tuple[int, int, Optional[Union[int, str]], str]


def offset_table_frame_count(ds) -> Optional[FrameCount]:
    """The frames the offset table names, beside the frames declared (#418).

    Args:
        ds (pydicom.Dataset): A dataset carrying `PixelData`.

    Returns:
        ``(table_frames, declared_frames, declared_raw, table_name)``,
        or None when there is nothing to compare: the transfer syntax is
        not encapsulated (or cannot be read at all -- a `force=True` read
        of a header-less file has an empty `file_meta`, #281), there is no
        `PixelData`, the offset table is empty with no Extended Offset
        Table beside it (the documented limit in the module docstring), or
        the table does not parse. In every None case the caller decodes as
        it did before this check existed; None never means "consistent".

        ``declared_frames`` is ``NumberOfFrames`` as the decoder reads it,
        which is 1 when the element is absent or 0. Measured on pydicom
        3.0.2: `as_array(ds, allow_excess_frames=False)` on a two-offset
        table with no NumberOfFrames returns frame 0 alone. The decoder
        reads nothing else as 1: it *refuses* a negative value ("must be
        greater than or equal to 1") and an empty one ("invalid literal
        for int()"). For those two, ``declared_frames`` is 1 only so the
        table has a number to be compared with; it is not a reading.

        ``declared_raw`` is the value the file states -- an int, ``""``
        when the element is present and empty, None when it is absent --
        so a message says "absent (read as 1)", "is 0 (read as 1)", "is -1
        (invalid)" or "is empty" rather than put a number in the dataset's
        mouth. Presence is asked of the dataset, not read off the value:
        an empty element assigned in memory is ``""`` but written and read
        back is None, and it is present either way.
    """
    # `ValueError` too: pydicom raises `ValueError("UID is not a transfer
    # syntax.")` for a UID it cannot classify -- a private syntax such as
    # GE's 1.2.840.113619.5.2, a SOP Class UID in the TS slot, an empty
    # UID. That is the decoder's refusal to make, in its own words, which
    # name the UID; this check runs outside `ingest_worker`'s decode `try`,
    # so raising here replaced that reason with one that did not.
    try:
        if not ds.file_meta.TransferSyntaxUID.is_encapsulated:
            return None
    except (AttributeError, ValueError):
        return None
    if "PixelData" not in ds:
        return None

    if "NumberOfFrames" not in ds:
        declared_raw = None
    elif ds.NumberOfFrames in (None, ""):
        declared_raw = ""
    else:
        try:
            declared_raw = int(ds.NumberOfFrames)
        except (TypeError, ValueError):
            return None
    declared = (declared_raw
                if isinstance(declared_raw, int) and declared_raw > 0
                else 1)

    # The EOT first: when it is present the BOT is required to be empty
    # (PS3.5 A.4), so a BOT-only count would see nothing. Eight bytes per
    # frame, one 64-bit offset each. `ds.get` hands back the raw bytes
    # here rather than a DataElement (measured, pydicom 3.0.2); the
    # `getattr` takes either, so neither shape reads as "no table".
    eot = ds.get("ExtendedOffsetTable")
    if eot:
        eot_bytes = getattr(eot, "value", eot)
        return (len(eot_bytes) // 8, declared, declared_raw,
                "Extended Offset Table")

    try:
        offsets = parse_basic_offsets(ds.PixelData)
    except (ValueError, struct.error, TypeError, AttributeError):
        # Unparsable -- including a `PixelData` of None, which
        # `parse_basic_offsets` meets as `AttributeError: 'NoneType' object
        # has no attribute 'read'`. Not this check's question: the decoder
        # that runs next refuses such a buffer on its own terms.
        return None
    if not offsets:
        return None
    return (len(offsets), declared, declared_raw, "Basic Offset Table")


def frame_count_mismatch(ds) -> Optional[str]:
    """The refusal message when the offset table and NumberOfFrames disagree.

    None when they agree or cannot be compared (see
    `offset_table_frame_count`). The wording is load-bearing for
    `Instance.get_pixel_data`: its file arm turns a message containing
    "no pixel data" into ``return None`` and one containing "decompress"
    or "missing dependencies" into the codecs-missing message. "names N
    frames; NumberOfFrames declares M" contains none of the three; keep it
    that way.
    """
    counted = offset_table_frame_count(ds)
    if counted is None or counted[0] == counted[1]:
        return None
    return frame_count_mismatch_words(counted)


def frame_count_mismatch_words(counted: FrameCount) -> str:
    """One spelling of the mismatch, for every refusal and loss row (#418).

    Args:
        counted: What `offset_table_frame_count` returned.
    """
    table_frames, declared, declared_raw, table_name = counted
    # "(read as 1)" only where the decoder does read 1 -- absent and 0.
    # Saying "declares 1" for either would put a number in the file's mouth
    # that it never wrote; saying "read as 1" for an empty or negative
    # value would describe a reading the decoder refuses to make.
    if declared_raw is None:
        declared_words = f"NumberOfFrames is absent (read as {declared})"
    elif declared_raw == "":
        declared_words = "NumberOfFrames is empty"
    elif declared_raw == 0:
        declared_words = f"NumberOfFrames is 0 (read as {declared})"
    elif declared_raw < 0:
        declared_words = f"NumberOfFrames is {declared_raw} (invalid)"
    else:
        declared_words = f"NumberOfFrames declares {declared}"
    return f"{table_name} names {table_frames} frames; {declared_words}"


#: The declared colour spaces whose decoded samples this handler converts
#: itself, per syntax, and the label the result is in (#448, #464). A
#: JPEG-LS stream carries no colour transform, so `jpegls_decode` returns
#: the YBR samples exactly as the file stores them. The conversion is
#: pydicom's `convert_color_space`, the function pydicom's own door
#: applies (#372). pydicom with pyjpegls returns the same bytes for these
#: files and labels them RGB (measured).
#:
#: **Every door converts through this one table.** Ingest
#: (`io_handlers._decode_with_imagecodecs`) and both read doors (this
#: module's `get_pixel_data`, and so `Instance.get_pixel_data()`) all
#: read it. Until #464, ingest converted for itself, and the read doors
#: returned the YBR samples while the file's label still said YBR_FULL:
#: one file, two answers. JPEG 2000's YBR rows are not here, because
#: `jpeg2k_decode` has already converted them
#: (`io_handlers._FALLBACK_DECODER_CONVERTS`).
#: `test_the_handler_converts_exactly_the_relabels_ingest_leaves_to_it`
#: holds this table and `io_handlers._FALLBACK_PHOTOMETRICS` together.
CONVERTS_TO = {
    str(JPEGLSLossless): {"YBR_FULL": "RGB"},
    str(JPEGLSLossy): {"YBR_FULL": "RGB"},
}

#: The declared colour spaces whose decode *the codec* has already
#: converted, per syntax, and the label its output is in (#482). A
#: relabel with no conversion, which is why these rows are not in
#: `CONVERTS_TO`: `convert_colour` would hand `YBR_RCT` to
#: `convert_color_space`, which has no such conversion, and ingest reads
#: `CONVERTS_TO` too. `jpeg2k_decode` undoes the codestream's colour
#: transform and returns RGB, at 8 and 16 bits (#448's measurement,
#: `io_handlers._FALLBACK_DECODER_CONVERTS`). Until #482 this handler
#: returned that RGB with `ds` still saying `YBR_RCT`, so a hand-built
#: instance read through it kept `YBR_RCT` over RGB bytes and exported
#: them so. No depth gate, unlike `colour_conversion`: the codec converts
#: at every depth it decodes.
#: `test_the_handler_relabels_exactly_the_rows_ingest_relabels_without_converting`
#: holds this table to `io_handlers._FALLBACK_PHOTOMETRICS`.
DECODER_RELABELS = {
    str(JPEG2000Lossless): {"YBR_RCT": "RGB", "YBR_ICT": "RGB"},
    str(JPEG2000): {"YBR_RCT": "RGB", "YBR_ICT": "RGB"},
}


def colour_conversion(ds) -> Optional[Tuple[str, str]]:
    """`(declared, converted)` when this handler converts `ds`'s decode.

    None when there is nothing to convert. None, too, when the frame is
    not 8-bit. `convert_color_space` refuses `uint16`, and whether 16-bit
    YBR_FULL is converted here or recorded as a limit is #461, left open
    by #464 on the owner's instruction. So a 16-bit frame is returned as
    stored, under its own YBR_FULL label, which is true of it. (Ingest
    refuses it before this is asked, naming the depth.)

    Raises:
        RuntimeError: before any decode, for a signed (PixelRepresentation
            1) 8-bit frame -- "its declared colour space 'YBR_FULL' is
            signed 8-bit, ...". `convert_color_space` has no `int8` path.
            YBR_FULL's chroma is defined with a +128 offset over unsigned
            samples (PS3.3 C.7.6.3.1.2), so no signed layout has a
            conversion to honour. pydicom refuses the same file at both
            its doors, pyjpegls and native (measured). Ingest refused it
            in `convert_color_space`'s words while both read doors
            returned `int8` YBR samples. Now every door refuses, in these
            words.

    Read with `getattr`, never `ds.get`, for `_sign_extend`'s reason.
    """
    declared = str(getattr(ds, "PhotometricInterpretation", "") or "")
    converted = CONVERTS_TO.get(
        str(ds.file_meta.TransferSyntaxUID), {}).get(declared)
    if converted is None:
        return None
    if int(getattr(ds, "BitsAllocated", 0) or 0) != 8:
        return None
    if int(getattr(ds, "PixelRepresentation", 0) or 0) == 1:
        raise RuntimeError(
            f"its declared colour space {declared!r} is signed 8-bit, and "
            f"the conversion to {converted} this handler makes, pydicom's "
            f"`convert_color_space`, takes unsigned samples only: "
            f"{declared}'s chroma is defined with a +128 offset over "
            f"unsigned samples, so a signed sample has no conversion")
    return declared, converted


def convert_colour(arr, conversion):
    """`arr`, converted as `colour_conversion` said (#464).

    On the last axis, so `(rows, cols, 3)` and `(frames, rows, cols, 3)`
    alike. The caller has the array in the header's shape first.
    """
    declared, converted = conversion
    return convert_color_space(arr, declared, converted)


def _jpegls_precision(codestream) -> Optional[int]:
    """The sample precision P a JPEG-LS frame header declares, or None (#478).

    Walks the marker segments from SOI to the SOF55 (`FF F7`) header, by
    each segment's own length. **It does not search for `FF F7`.** A COM
    or APPn payload is opaque bytes and can legally hold that pair, and a
    search then reads a precision out of a comment:
    `test_the_precision_is_read_from_the_frame_header_not_the_first_ff_f7`
    has such a stream, which CharLS decodes exactly.

    None when no SOF55 precedes the scan. The caller then reads by
    BitsStored, which is pydicom's default in the same place
    (`jls_info.get("precision", bits_stored)`). No stream `jpegls_decode`
    accepted can reach that: CharLS needs the header to decode at all.
    """
    data = bytes(codestream)
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            # A fill byte ahead of the marker (ITU-T T.81 B.1.1.2).
            pos += 1
            continue
        if marker == 0xF7:
            return data[pos + 4] if pos + 4 < len(data) else None
        if marker in (0xD9, 0xDA):
            # EOI or SOS: the scan began with no frame header before it.
            return None
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    return None


def _j2k_sample_layout(codestream) -> Optional[Tuple[bool, int]]:
    """The `(is_signed, precision)` a JPEG 2000 codestream declares (#460).

    Read from `Ssiz^0`, the first component's sample descriptor in the SIZ
    marker segment: bit 7 is the sign, and the low seven bits are the
    precision minus one (ISO/IEC 15444-1 A.5.1). The first component
    speaks for the frame because every door above reads one array; a
    codestream whose components differ in depth is not one this project
    can carry either way.

    **It walks to the SIZ rather than searching for `FF 51`**, for
    `_jpegls_precision`'s reason: a search finds the pair wherever it
    falls. Here the walk is two steps and the standard makes them
    mandatory -- SOC first, SIZ immediately after it (15444-1 A.3,
    Figure A-3) -- so a stream that fails them is not a codestream.

    A JP2 *box* is unwrapped first. The transfer syntax names a bare
    codestream, but this project itself wrote the box under it until
    #404: every JPEG 2000 file Isocenter exported before that release is
    JP2-wrapped, `imagecodecs` decodes it, and it must reach the same
    rule as the codestream.

    The offsets are pydicom's own, from `pydicom.pixels.utils.
    get_j2k_parameters`, and were checked byte for byte against it.
    Ported rather than imported, and **not because it is private** -- it
    is a public module-level name, though not in any `__all__`. It is
    ported because the name a caller would reach it by has already moved
    once (`pixel_data_handlers.utils` re-exports it and that module is
    deprecated for removal in pydicom 4.0, which this package's `<4.0`
    cap is counting down to), and because this handler exists precisely
    for the files pydicom cannot decode: taking the rule that decides
    whether to refuse from the library being worked around is a
    dependency this module should not have. One arm is ours and not
    pydicom's: `get_j2k_parameters` has no guard for a JP2 box declaring
    length 0, and this returns None where that walk would not terminate
    (`test_a_jp2_box_of_zero_length_is_refused_rather_than_walked_forever`).

    None when neither form parses, which no stream `jpeg2k_decode`
    accepted can reach -- the SIZ is what tells a decoder the image's
    size and depth. The caller then leaves the array exactly as the codec
    returned it, which is what every door did before this rule existed.
    """
    data = bytes(codestream)
    offset = 0
    if data.startswith(b"\x00\x00\x00\x0c\x6a\x50\x20\x20"):
        # A JP2 file: 12-byte signature box, then boxes until `jp2c`,
        # whose payload is the codestream.
        offset = 12
        while offset + 8 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            if data[offset + 4:offset + 8] == b"\x6a\x70\x32\x63":
                offset += 8
                break
            if length <= 0:
                # A box claiming the rest of the file (0) or a corrupt
                # length: stepping by it would loop or run backwards.
                return None
            offset += length
        else:
            return None
    if data[offset:offset + 2] != b"\xff\x4f":
        return None
    if data[offset + 2:offset + 4] != b"\xff\x51":
        return None
    if offset + 42 >= len(data):
        return None
    ssiz = data[offset + 42]
    return bool(ssiz & 0x80), (ssiz & 0x7F) + 1


def _sign_extend(arr, ds, precision=None):
    """A lossless-JPEG or JPEG-LS decode, as the signed values it holds (#446).

    `ljpeg_decode`, `jpegsof3_decode` and `jpegls_decode` return every
    sample as its masked unsigned bit pattern and never sign-extend, at
    every BitsStored: a signed 12-bit -800 comes back as `uint16` 3296.
    `Instance.get_pixel_data()` returned exactly that, with no error, and
    `ingest()` refused the same file (#416). This is the rule pydicom
    applies with its own plugins -- keep the low `width` bits and extend
    bit `width` - 1 -- measured bit-exact against pydicom with
    pylibjpeg-libjpeg and pyjpegls at 8, 12 and 16 bits. At a width equal
    to the output's it is a pure reinterpretation.

    `width` is `precision` when the caller passes one, and BitsStored
    otherwise. `_decode_frame` passes a JPEG-LS frame's own precision
    (#478) and nothing for JPEG Lossless, which pydicom reads by
    BitsStored too (`_correct_unused_bits`).

    Called for .57/.70/.80/.81 unconditionally, and for JPEG 2000 in one
    case only: an unsigned codestream under PixelRepresentation 1, where
    `_against_pixel_representation` passes the codestream's own precision
    (#460). A *signed* J2K codestream never reaches here --
    `jpeg2k_decode` has already returned signed, sign-extended samples,
    and extending them again would raise on the dtype check below. That
    is pydicom's split too: `_apply_sign_correction` shifts a J2K decode
    only when the codestream's signedness and PixelRepresentation
    disagree.

    With PixelRepresentation other than 1 it returns the codec's array
    object itself, not a view: an unsigned decode is untouched. Read with
    `getattr`, never `ds.get`: on a `MagicMock(spec=Dataset)` `ds.get`
    hands back a mock, where `getattr(..., 0)` hands back the default.
    """
    if int(getattr(ds, "PixelRepresentation", 0) or 0) != 1:
        return arr
    bits = arr.dtype.itemsize * 8
    bits_stored = int(getattr(ds, "BitsStored", bits) or bits)
    # No HighBit check here, deliberately (#455, #523; owner rulings Q2
    # and Q3). There was one, #446's Q1: a signed frame whose HighBit was
    # not BitsStored - 1 was refused, in words claiming a sign extension
    # "from BitsStored" -- false on the JPEG-LS and JPEG 2000 routes,
    # which extend from the stream's precision. HighBit is an input to no
    # decoder here, and whether the refusal fired depended on the sign
    # bit, while the same header over an unsigned frame was read in
    # silence. A header rule now asks the question for every route, and
    # answers it with a WARNING row at ingest:
    # `io_handlers._high_bit_mismatch`. Do not put a refusal back here: it
    # would refuse on one route what every other route reads.
    # The stream's precision where it has one, not BitsStored (#478, the
    # owner's ruling, reversing the BitsStored reading #463 shipped). The
    # two agree for every conformant encoder. `imagecodecs.jpegls_encode`
    # cannot write precision 12, so it writes a 12-bit pattern as a
    # precision-16 stream, and pydicom with pyjpegls reads that stream by
    # its precision: 3296 for -800's pattern. A stream narrower than
    # BitsStored (precision 12 under BitsStored 16) is read by its 12 bits
    # the same way, -800. A precision-8 stream under BitsStored 12 and
    # BitsAllocated 16 reaches here already widened to `uint16`
    # (`_in_declared_container`, #454), so it is extended from 8 inside
    # `int16`, pydicom's answer.
    width = precision or bits_stored
    if not 1 <= width <= bits:
        # A width the container cannot hold: the shift below would be
        # negative. One guard, naming where the width came from. There
        # were two until #523, and neither could fire alone: the first
        # also refused a *signed* decode, which never reaches here (lj92
        # and CharLS return unsigned patterns, and a signed JPEG 2000
        # codestream is already signed), and refused BitsStored above
        # the container, which pydicom's validation now refuses before
        # any decode (#453) unless the container is one nothing widens --
        # a precision-8 JPEG Lossless stream under BitsAllocated 32,
        # BitsStored 12, the header
        # `test_a_jpeg_lossless_stream_narrower_than_bits_stored_is_refused_not_shifted`
        # reaches this with. CharLS and openjpeg return a container at
        # least as wide as the precision they read, so a precision width
        # reaches here only from a misread header.
        source = ("its stream's precision" if precision
                  else "BitsStored")
        raise RuntimeError(
            f"cannot sign-extend a {arr.dtype} decode from {source} "
            f"{width}: the codec returns samples at most {bits} bits wide")
    shift = bits - width
    # Shift left while unsigned, reinterpret, then shift right while
    # signed: numpy's `>>` is arithmetic on a signed dtype and logical on
    # an unsigned one, so the order is the whole of the sign extension.
    return (arr << shift).view(np.dtype(f"i{arr.dtype.itemsize}")) >> shift


def _in_declared_container(arr, ds):
    """A JPEG decode, in the container the header declares (#454, #523).

    lj92, libjpeg-turbo, CharLS and openjpeg return the narrowest
    container that holds the stream's precision: `uint8` for a precision
    of 8 or less, whatever BitsAllocated says -- `int8` from openjpeg when
    a JPEG 2000 codestream is signed. So a precision-8 stream under
    BitsAllocated 16 came back as `uint8` (or `int8`, once
    `_sign_extend` had it) from both read doors, with the right values,
    while ingest's dtype guard refused the same file against
    BitsAllocated 16. One file, two answers. pydicom with pyjpegls widens
    the same stream to `uint16`, or `int16` when it is signed (measured),
    and so all three doors do now. For JPEG Lossless pydicom with
    pylibjpeg-libjpeg raises on these files, so there the doors agree with
    each other and not with pydicom.

    Exact, since every 8-bit sample fits in 16 bits, so it writes no row
    and logs nothing (#523's ruling). Called before `_sign_extend`, so a
    signed sample is sign-extended inside the declared container rather
    than the codec's. A stream of precision 8 under BitsStored 12 then
    reads as pydicom reads it, where it used to be refused at every door.

    **JPEG 2000 since #523.** The J2K arm did not call this, so a
    precision-8 codestream under BitsAllocated 16 was refused by the
    fallback and read by Pillow, and the handler returned `uint8`. Its
    `int8` arm is JPEG 2000's alone: lj92 and CharLS return unsigned
    patterns, and `_sign_extend` makes the signed dtype after this call.
    A signed codestream is widened with its sign, which is Pillow's
    `int16` answer (measured).

    It only widens, and only an 8-bit decode under BitsAllocated 16. A
    decode wider than its container, a precision-16 stream under
    BitsAllocated 8, is left alone, so the dtype guard still refuses it.
    Read with `getattr`, never `ds.get`, for `_sign_extend`'s reason.
    """
    if int(getattr(ds, "BitsAllocated", 0) or 0) == 16:
        if arr.dtype == np.uint8:
            return arr.astype(np.uint16)
        if arr.dtype == np.int8:
            return arr.astype(np.int16)
    return arr


def _against_pixel_representation(arr, ds, layout):
    """A JPEG 2000 decode against the signedness its header declares (#460).

    `jpeg2k_decode` returns the codestream's own signedness -- `int16`
    from a signed codestream, `uint16` from an unsigned one -- whatever
    PixelRepresentation says, so one file could carry two answers. The
    two disagreements are not symmetrical and this does not treat them
    alike.

    **Unsigned codestream, PixelRepresentation 1: reinterpreted by the
    header.** This is the shape of a real file -- pydicom's own
    `J2K_pixelrep_mismatch.dcm`, a CT from pydicom issue 1149, whose
    precision-13 unsigned codestream holds `6192` for `-2000`. Both of
    pydicom's plugins read it by the header and return `int16 -2000`, and
    so does `Instance.get_pixel_data()` wherever pydicom decodes (16-bit
    monochrome J2K, which Pillow takes). Leaving the handler on `uint16
    6192` was one file with two answers. The reinterpretation is
    `_sign_extend` at the *codestream's* precision, not BitsStored: that
    is pydicom's `_apply_sign_correction` (`j2k_precision`), and where
    the two differ -- a precision-12 stream under BitsStored 16 -- only
    the precision gives pydicom's values (measured).

    **Signed codestream, PixelRepresentation 0: refused.** There is no
    pydicom answer to agree with here. pydicom's correction is keyed on
    `bit_shift`, so at a precision equal to the container's it does
    nothing at all and the value it returns is whatever its plugin
    reinterpreted: for `[-32768, -800, -1]`, `uint16 [0, 31968, 32767]`
    with Pillow and `uint16 [32768, 64736, 65535]` with
    pylibjpeg-openjpeg (measured). Two plugins, two arrays, neither of
    them the file's samples. Since #524 the refusal is made ahead of any
    decoder, by `signed_codestream_refusal` in `io_handlers._decode_pixels`,
    so every door says one thing; the raise below is its twin for this
    module's own decode.

    A `layout` of None -- a codestream whose SIZ did not parse, which no
    decode reaches -- returns the array untouched, the answer every door
    gave before this rule.
    """
    if layout is None:
        return arr
    codestream_signed, precision = layout
    declared_signed = int(getattr(ds, "PixelRepresentation", 0) or 0) == 1
    if codestream_signed == declared_signed:
        return arr
    if codestream_signed:
        # Unreachable through `io_handlers._decode_pixels`, whose
        # `signed_codestream_refusal` gate refuses the file before any
        # decoder is asked (#524). Kept for `decode_declared_frames`
        # called alone, in the gate's own words.
        raise RuntimeError(signed_codestream_words(precision))
    # Unsigned codestream under PixelRepresentation 1. `_sign_extend`
    # reads that 1 for itself and would return the array untouched under
    # any other value, which is why the branch above cannot fall through
    # to it.
    return _sign_extend(arr, ds, precision)


def signed_codestream_words(precision) -> str:
    """The one spelling of the signed-codestream refusal (#460, #524)."""
    return (f"the JPEG 2000 codestream is signed at precision {precision}, "
            f"where PixelRepresentation 0 declares unsigned samples: no "
            f"decoder here returns these samples unsigned -- Pillow shifts "
            f"them by 2^(precision-1), pylibjpeg-openjpeg returns their bit "
            f"patterns, and `jpeg2k_decode` returns them signed -- so there "
            f"is no unsigned reading of this file to stand behind")


def signed_codestream_refusal(ds) -> Optional[str]:
    """The refusal for a signed JPEG 2000 codestream under PixelRepresentation 0 (#524).

    None when there is nothing to refuse: the syntax is not JPEG 2000,
    PixelRepresentation is not 0, or no declared frame's SIZ says signed.
    None, too, when PixelRepresentation is absent, empty or not an
    integer: the words cite "PixelRepresentation 0", which such a file
    never wrote, and pydicom's own validation refuses it in words that
    name the element (a Type 1 element, PS3.3 C.7.6.3).
    `io_handlers._decode_pixels` raises the words **before pydicom is
    asked**, which is the point. pydicom's Pillow plugin decodes such a
    file at monochrome depths and 8-bit colour and returns the samples
    shifted by 2^(bits-1) -- `[-32768, -800, -1]` as `uint16 [0, 31968,
    32767]` -- with no error, so ingest stored the shift and only the
    handler refused. Asked of the SIZ, it is one answer whichever decoder
    would have run.

    **Every declared frame, and only those.** A frame is a codestream, and
    one frame's header does not speak for another's samples. An excess
    frame the offset table names beyond NumberOfFrames is not read: ingest
    drops it with its #418 row, and its sign is not a reason to refuse the
    frames it keeps. The count is `offset_table_frame_count`'s reading.

    **It never raises of its own.** A buffer `generate_frames` cannot walk,
    or a frame whose SIZ does not parse (`_j2k_sample_layout` returns
    None), has no sign to read: the decoder that runs next refuses it in
    its own words, as it did before this gate.

    Cost: a few bytes per frame, from a buffer `dcmread` already holds.
    """
    ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    if ts not in J2K_SYNTAXES:
        return None
    try:
        if int(ds.PixelRepresentation) != 0:
            return None
        counted = offset_table_frame_count(ds)
        declared = (counted[1] if counted is not None
                    else max(1, int(getattr(ds, "NumberOfFrames", 1) or 1)))
        for frame in islice(generate_frames(ds.PixelData,
                                            number_of_frames=declared),
                            declared):
            layout = _j2k_sample_layout(frame)
            if layout is not None and layout[0]:
                return signed_codestream_words(layout[1])
    except Exception:  # pylint: disable=broad-except
        # Not this gate's refusal to make: see the docstring.
        return None
    return None


def _decode_frame(transfer_syntax, bitstream, ds):
    """One frame's codestream to an array, by the codec its syntax names."""
    if transfer_syntax in [JPEGLossless, JPEGLosslessSV1]:
        # lj92 reads one byte past the end of its input. On an odd-length
        # codestream with nothing after it -- `ljpeg_encode` output handed
        # straight in -- it raises `LJ92_ERROR_CORRUPT` (measured on
        # imagecodecs 2024.6.1 and 2026.8.16: 11 of 1260 random and flat
        # streams, and every flat 8-bit frame at 4x4 and 8x8). A
        # conformant file pads every item to even length (PS3.5 7.5), and
        # that pad is the byte lj92 reads: through `generate_frames`, 0 of
        # the same 1260 fail. But pydicom writes and reads an odd item
        # length without complaint and `generate_frames` hands it over as
        # stored, so a nonconformant file reached here unpadded at every
        # door and was refused (#446). So the pad is added here, the one
        # call every door reaches. Appended, after the EOI marker, where
        # conformant framing would have written it; never prepended, which
        # breaks the SOI. `jpegsof3_decode` needs no pad and is exact on
        # all of the above; it is the codec to switch to if this is ever
        # not enough.
        if len(bitstream) % 2:
            bitstream = bytes(bitstream) + b"\x00"
        return _sign_extend(
            _in_declared_container(imagecodecs.ljpeg_decode(bitstream), ds),
            ds)
    if transfer_syntax in [JPEGBaseline, JPEGExtended]:
        return imagecodecs.jpeg_decode(bitstream)
    if transfer_syntax in [JPEG2000Lossless, JPEG2000]:
        # The codestream's own SIZ header, per frame, for the same reason
        # the JPEG-LS branch below reads its own: a frame is a codestream
        # and one frame's header does not speak for another's samples.
        #
        # In the declared container first, as the other JPEG arms are
        # (#523). openjpeg returns `uint8` or `int8` for a precision of 8
        # or less whatever BitsAllocated says, and Pillow, at pydicom's
        # door, returns the same samples in 16 bits. Without the widening
        # the fallback refused such a file against BitsAllocated 16, or
        # could not sign-extend an unsigned one from its precision, while
        # pydicom read it: an answer that depended on which plugins were
        # installed. Before `_against_pixel_representation`, so an
        # unsigned precision-8 stream under PixelRepresentation 1 is
        # extended inside `uint16`, which is Pillow's `int16` answer.
        return _against_pixel_representation(
            _in_declared_container(imagecodecs.jpeg2k_decode(bitstream), ds),
            ds, _j2k_sample_layout(bitstream))
    if transfer_syntax in [JPEGLSLossless, JPEGLSLossy]:
        # Each frame's own precision, parsed from its own header: a frame
        # is a codestream, and one frame's header does not speak for
        # another's samples (#478). pydicom's whole-array read applies
        # the last frame's precision to every frame; its per-frame read
        # does what this does.
        return _sign_extend(
            _in_declared_container(imagecodecs.jpegls_decode(bitstream), ds),
            ds, _jpegls_precision(bitstream))
    raise RuntimeError(f"Unsupported syntax: {transfer_syntax}")


def decode_declared_frames(ds, number_of_frames):
    """Decode the first `number_of_frames` frames, and ask nothing else.

    For `io_handlers._decode_pixels`' fallback (#416). **It does not
    compare the offset table with NumberOfFrames**, and that is the point:
    its caller has already asked `offset_table_frame_count` and decided --
    refuse fewer, drop an excess only when told to -- and a second check
    here would refuse the excess #418 truncates, rejecting a file ingest
    means to keep. `get_pixel_data` below is the one that refuses a
    mismatch; this is not a second spelling of it.

    `islice`, because `generate_frames(buf, number_of_frames=1)` yields
    every frame a populated Basic Offset Table names, not one (measured,
    pydicom 3.0.2): without it an excess would be decoded whole.

    Returns:
        np.ndarray: the frame for one, the frames stacked for more. The
        caller checks dtype and size against the header.
    """
    if not is_available():
        raise _unavailable() from IMPORT_ERROR
    transfer_syntax = ds.file_meta.TransferSyntaxUID
    frames = [_decode_frame(transfer_syntax, bitstream, ds)
              for bitstream in islice(
                  generate_frames(ds.PixelData,
                                  number_of_frames=number_of_frames),
                  number_of_frames)]
    return frames[0] if number_of_frames == 1 else np.stack(frames)


def get_pixel_data(ds):
    """
    Decodes pixel data from an encapsulated dataset using `imagecodecs`.

    Handles the transfer syntaxes in `SUPPORTED_TRANSFER_SYNTAXES` --
    JPEG, JPEG Lossless, JPEG 2000 and JPEG-LS, not RLE (#447) -- and
    encapsulated bitstreams (fragments).

    Args:
        ds (pydicom.Dataset): The dataset containing PixelData.

    Returns:
        np.ndarray: The decoded pixel array. A signed (PixelRepresentation
        1) JPEG Lossless or JPEG-LS frame comes back signed (#446), where
        it used to come back as its unsigned bit pattern: sign-extended
        from BitsStored for JPEG Lossless, and from each JPEG-LS frame's
        own precision (#478). A JPEG Lossless or JPEG-LS stream of
        precision 8 or less under BitsAllocated 16 comes back in that
        16-bit container, `uint16` or `int16` (#454), where it came back
        `uint8` or `int8`. An 8-bit YBR_FULL JPEG-LS frame comes back
        converted to RGB, **and `ds.PhotometricInterpretation` is set to
        `RGB`** (#464): this mutates the dataset it is given, so the label
        stays true of the bytes. A JPEG 2000 `YBR_RCT` or `YBR_ICT` frame,
        which the codec returns as RGB, is relabelled `RGB` the same way
        (#482), at any depth. An *unsigned* JPEG 2000 codestream under
        PixelRepresentation 1 comes back reinterpreted by the header, from
        the codestream's own precision, where it came back as the codec's
        unsigned array (#460): pydicom's `J2K_pixelrep_mismatch.dcm` reads
        `int16 -2000`, as it does at pydicom's own door.

    Raises:
        RuntimeError: If imagecodecs is missing (naming the import
            failure, #444) or decoding fails, or if
            the offset table names a different number of frames from
            NumberOfFrames (#418) -- "<table> names N frames;
            NumberOfFrames declares M". Before any decode, for a signed
            8-bit YBR_FULL JPEG-LS frame -- "its declared colour space
            'YBR_FULL' is signed 8-bit, ..." (#464). For a *signed* JPEG
            2000 codestream under PixelRepresentation 0 -- "the JPEG 2000
            codestream is signed at precision P, where PixelRepresentation
            0 declares unsigned samples ..." (#460).
    """
    if not is_available():
        raise _unavailable() from IMPORT_ERROR

    transfer_syntax = ds.file_meta.TransferSyntaxUID
    pixel_bytes = ds.PixelData

    # Before either arm, and outside the `try` below, so the refusal
    # reaches the caller in its own words rather than prefixed with
    # "imagecodecs failed to decode". Both arms trust NumberOfFrames:
    # the single-frame arm asks for one frame and so returned frame 0 of
    # a two-frame table, and the multi-frame arm returned whatever the
    # table held -- a silent short read when it named fewer (#418).
    mismatch = frame_count_mismatch(ds)
    if mismatch is not None:
        raise RuntimeError(mismatch)
    # Also before the decode and outside the `try`, for the same reason:
    # a signed 8-bit YBR_FULL frame is refused in its own words (#464).
    conversion = colour_conversion(ds)
    # The label the codec's own output is in, where it undid a colour
    # transform (#482). Looked up here, applied only after the decode.
    decoded_label = DECODER_RELABELS.get(str(transfer_syntax), {}).get(
        str(getattr(ds, "PhotometricInterpretation", "") or ""))

    # Handle encapsulated data (fragments)

    try:
        num_frames = getattr(ds, 'NumberOfFrames', 1)

        # Multi-Frame Handling
        if num_frames > 1 and ds.file_meta.TransferSyntaxUID.is_encapsulated:

            # generate_frames handles BOT and fragments logic
            frames = []
            for frame_bitstream in generate_frames(ds.PixelData, number_of_frames=num_frames):
                decoded = _decode_frame(transfer_syntax, frame_bitstream, ds)
                frames.append(decoded)

            arr = np.array(frames)

        # Single-Frame Handling
        else:
            if ds.file_meta.TransferSyntaxUID.is_encapsulated:
                # `generate_fragments` yields EVERY item of the
                # encapsulated pixel data, and the first item is the
                # Basic Offset Table (PS3.5 A.4). Joining them therefore
                # prefixed the codestream with the BOT's own bytes -- four
                # zeros ahead of `ff4f ff51` for a single-frame file --
                # and `imagecodecs` refused the result with `not a J2K or
                # JP2 data stream`, so this arm had never decoded
                # anything. It failed identically on the JP2 container
                # this project wrote before #404, so it is not that
                # container's fault and predates it (#407).
                #
                # Only a *populated* offset table breaks the join, which
                # is why nothing noticed: with an empty table the join is
                # accidentally correct. `pydicom.encaps.encapsulate`
                # writes a populated one by default and `_compress_j2k`
                # calls it that way, so every file this project
                # compresses hit it -- and a hand-built
                # `item(b"") + item(codestream)` fixture would pass
                # without this fix.
                #
                # `generate_frames` is what the multi-frame arm above
                # already uses, and it is the right answer here for a
                # second reason as well as the BOT: one frame may legally
                # be split across several fragments, so "take the last
                # fragment" would decode the tail of such a frame.
                frames = list(generate_frames(pixel_bytes,
                                              number_of_frames=1))
                if not frames:
                    # Unreachable against pydicom 3.x, and kept anyway as
                    # a pin on its contract rather than on a log line
                    # anyone will read. Measured: `generate_frames(buf,
                    # number_of_frames=1)` yields at least one frame for
                    # every buffer that parses at all -- an empty offset
                    # table alone, an empty table plus an empty fragment,
                    # and a populated table alone all come back as
                    # `[b""]` -- and a buffer too short to parse raises
                    # `struct.error` above this line instead. So `frames`
                    # is never `[]` today. What this costs is one branch;
                    # what it buys is that if that contract ever changes,
                    # the log says which dataset had no frame instead of
                    # `IndexError: list index out of range`. Do not
                    # write a test for it: there is no input that reaches
                    # it (#407).
                    raise RuntimeError(
                        "encapsulated PixelData holds no frame")
                codestream = frames[0]
            else:
                codestream = pixel_bytes

            arr = _decode_frame(transfer_syntax, codestream, ds)

        if conversion is not None:
            # The conversion ingest makes, by the same table (#464). Inside
            # the `try`, so a decode in a shape the conversion cannot read
            # is refused as a decode failure.
            arr = convert_colour(arr, conversion)

    except Exception as e:
        print(
            f"[isocenter_imagecodecs_handler] Decode error for {transfer_syntax}: {describe_exception(e)}",
            file=sys.stderr)
        raise RuntimeError(f"imagecodecs failed to decode {transfer_syntax}: {e}") from e

    if conversion is not None:
        # Converted, so say so: RGB bytes under the file's YBR_FULL label
        # would be #372's defect at this door. Relabelled only after the
        # conversion succeeded, so a refusal leaves `ds` as it was.
        # `Instance.get_pixel_data()` reads this change to relabel the
        # instance. Do not drop it as a side effect nobody asked for: it
        # is the only way the door says the bytes changed colour space.
        ds.PhotometricInterpretation = conversion[1]
    elif decoded_label is not None:
        # The codec converted rather than this handler, and the label
        # follows it just the same (#482). After the `try`, for the reason
        # above: a JPEG 2000 decode that fails leaves `ds` saying what it
        # said. Relabel before the decode and a failed read leaves RGB on
        # a dataset nothing was decoded from.
        ds.PhotometricInterpretation = decoded_label
    return arr

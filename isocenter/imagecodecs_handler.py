"""A pixel decoder built on `imagecodecs`, and the frame-count check (#418).

`offset_table_frame_count` compares the frame count an encapsulated
`PixelData`'s offset table names -- or, with no table pydicom walks by,
the frames pydicom's walk of the fragments finds, and for native data the
whole frames the element's length holds (#620) -- with the one
`NumberOfFrames` declares.
It is shared by `Instance.get_pixel_data`'s file arm, by `ingest_worker`
for the top level, by `_decode_nested_pixels` for an icon (#433), by
`_decode_pixels`' imagecodecs fallback (#416) and by #524's
`signed_codestream_refusal`, so none of them can disagree about what a
mismatch is.

**One decoder, behind one door (#453).** `decode_declared_frames` decodes
exactly the frames its caller asks for, because that caller --
`io_handlers._decode_with_imagecodecs`, behind `_decode_pixels` -- has
already counted the table and decided (#418's truncation, which a refusal
here would turn into a rejected file). This module had a second decoder,
`get_pixel_data`, which `Instance.get_pixel_data()` fell back to on any
exception and which had none of the fallback's checks: it read files
ingest refused. It is deleted; every door now decodes through
`_decode_pixels`. Do not add a decode entry point here that skips it.

**Signed samples (#446).** `ljpeg_decode` and `jpegls_decode` return the
masked unsigned pattern of every sample; `_decode_frame` sign-extends it
when PixelRepresentation is 1 -- from BitsStored for JPEG Lossless, from
the stream's own precision for JPEG-LS (#478) -- so the decode returns
the signed values the file stores. See `_sign_extend`.

**JPEG 2000 carries its own signedness (#460).** `jpeg2k_decode` returns
what the codestream's SIZ declares, signed or unsigned, whatever
PixelRepresentation says, so the two can contradict each other.
`_j2k_sample_layout` reads that header and `_decode_frame` acts on the
disagreement: an *unsigned* codestream under PixelRepresentation 1 is
reinterpreted by the header, through the same `_sign_extend`, at the
codestream's own precision; a *signed* codestream under
PixelRepresentation 0 is refused, ahead of any decoder, by
`signed_codestream_refusal` (#524). See `_against_pixel_representation`.

**Colour (#464, #482).** `CONVERTS_TO` is the one table of conversions
this handler makes (8-bit YBR_FULL JPEG-LS to RGB), applied through
`colour_conversion` and `convert_colour` by `_decode_with_imagecodecs`,
which every door reaches. The conversions the codec has already made
(JPEG 2000 YBR_RCT/ICT to RGB) are a relabel only, and live in
`io_handlers._FALLBACK_PHOTOMETRICS` under `_FALLBACK_DECODER_CONVERTS`.

**Its limit, stated.** An *empty* Basic Offset Table with no Extended
Offset Table is legal (PS3.5 A.4) and names no frames, and the fragments
alone do not say where one frame ends and the next begins -- one frame
may legally span several fragments. pydicom's decoder then walks the
fragments by their codestreams' end markers and returns every frame it
finds; until #620 nothing counted that walk, so a fragment beyond
NumberOfFrames came back from `Instance.get_pixel_data()` as a frame the
header does not declare, and ingest stored an array its geometry could
not reload, with no row (review of #606, F-r2-2). `_walked_excess` now
counts it with pydicom's own generator. Two shapes remain uncounted.
NumberOfFrames 1, or absent, over several fragments with no table: the
walk joins every fragment into one frame, so no excess is visible before
the decode, and none after it either. And a native 1-bit frame that is
not a whole number of bytes, where pydicom's own excess arithmetic
raises rather than return the frames (`_native_excess`).
"""
import struct
import sys
from io import BytesIO
from itertools import islice
from typing import Optional, Tuple, Union

import numpy as np
from pydicom.uid import UID
from pydicom.encaps import (generate_fragmented_frames, generate_frames,
                            parse_basic_offsets, parse_fragments)
from pydicom.pixels import convert_color_space
from pydicom.pixels.decoders.base import DecodeRunner

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
    as quoted. Spelled by `describe_exception`, as every recorded reason
    is (#519): a hand-spelled `f"{type(e).__name__}: {e}"` was the same
    behaviour under a second spelling, and differed from the helper
    exactly where the helper earns its place -- a message-less
    `ImportError()` rendered as `ImportError: ` with nothing after it.
    """
    if IMPORT_ERROR is None:
        return RuntimeError("imagecodecs is not available")
    return RuntimeError(
        f"imagecodecs is not available: {describe_exception(IMPORT_ERROR)}")


# UID Constants
JPEGLossless = UID("1.2.840.10008.1.2.4.57")
JPEGLosslessSV1 = UID("1.2.840.10008.1.2.4.70")
JPEG2000Lossless = UID("1.2.840.10008.1.2.4.90")
JPEG2000 = UID("1.2.840.10008.1.2.4.91")
JPEGBaseline = UID("1.2.840.10008.1.2.4.50")
JPEGExtended = UID("1.2.840.10008.1.2.4.51")
JPEGLSLossless = UID("1.2.840.10008.1.2.4.80")
JPEGLSLossy = UID("1.2.840.10008.1.2.4.81")
HTJ2KLossless = UID("1.2.840.10008.1.2.4.201")
HTJ2KLosslessRPCL = UID("1.2.840.10008.1.2.4.202")
HTJ2K = UID("1.2.840.10008.1.2.4.203")

#: The syntaxes whose frames are JPEG 2000 codestreams, and so carry a
#: SIZ marker `_j2k_sample_layout` reads, and the JPEG-LS ones, whose
#: frame header `_jpegls_precision` reads. One set each, so the decode,
#: the signedness gate and ingest's HighBit row cannot disagree about
#: which files have a stream precision.
#:
#: **HTJ2K is a member (#459).** High-Throughput JPEG 2000 (PS3.5 A.4.11)
#: is a JPEG 2000 codestream with another block coder: the same SOC and
#: SIZ, so the same signedness, precision and colour transform. openjpeg's
#: `jpeg2k_decode` reads it exactly -- mono 8/16, signed 16, RGB 8/16 with
#: and without the reversible colour transform, at imagecodecs 2024.6.1
#: and 2026.8.16 (measured). **Not `htj2k_decode`**: openjph returns an RGB
#: stream written without the transform planar, `(3, rows, cols)`, the
#: same samples in another image. Do not "use the matching codec".
J2K_SYNTAXES = frozenset({JPEG2000Lossless, JPEG2000, HTJ2KLossless,
                          HTJ2KLosslessRPCL, HTJ2K})
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
    HTJ2KLossless,
    HTJ2KLosslessRPCL,
    HTJ2K,
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


def extended_offsets(ds) -> Optional[Tuple[bytes, bytes]]:
    """The Extended Offset Table exactly as pydicom's decoder walks by it.

    ``(ExtendedOffsetTable, ExtendedOffsetTableLengths)``, the pair every
    `generate_frames` call here passes as `extended_offsets=`, or None
    when the decoder walks the fragments without one. pydicom takes the
    table when both elements are present (`pixels.utils._as_options`)
    and then **drops it when their lengths differ** (`DecodeRunner.
    _validate_options`, with a warning), so both halves are asked here.

    Why every walk takes it (review of #606, M1): a walk without the
    table finds frames by searching for each codestream's end, and on a
    file whose table and fragments disagree -- a fragment no frame names,
    which PS3.3 C.7.6.3 does not allow under an EOT but pydicom decodes
    by the table all the same -- that search reads different frames from
    the ones the decoder reads. The signed-codestream gate read no sign
    in a frame Pillow then shifted, and the fallback refused a file
    Pillow read. A pair a mismatched table would give pydicom ignores is
    just as much a second walk, which is why the length check is here
    and not left to `generate_frames`.
    """
    # Membership first, as pydicom asks it, so a stand-in dataset that
    # answers every attribute is not read as carrying a table. `ds.get`
    # hands back the raw bytes rather than a DataElement (measured,
    # pydicom 3.0.2); the `getattr` takes either. An empty table names no
    # frame, and is read as none.
    if ("ExtendedOffsetTable" not in ds
            or "ExtendedOffsetTableLengths" not in ds):
        return None
    eot = ds.get("ExtendedOffsetTable")
    lengths = ds.get("ExtendedOffsetTableLengths")
    eot = getattr(eot, "value", eot)
    lengths = getattr(lengths, "value", lengths)
    if (not isinstance(eot, (bytes, bytearray))
            or not isinstance(lengths, (bytes, bytearray))
            or not eot or len(eot) != len(lengths)):
        return None
    return bytes(eot), bytes(lengths)


def offset_table_frame_count(ds) -> Optional[FrameCount]:
    """The frames the offset table names, beside the frames declared (#418).

    Args:
        ds (pydicom.Dataset): A dataset carrying `PixelData`.

    Returns:
        ``(table_frames, declared_frames, declared_raw, table_name)``,
        or None when there is nothing to compare: the transfer syntax is
        cannot be read at all (a `force=True` read of a header-less file
        has an empty `file_meta`, #281), there is no `PixelData`, the
        table does not parse, or there is no table to count by and neither
        pydicom's walk of the fragments (`_walked_excess`) nor, for native
        data, the element's length (`_native_excess`) holds more frames
        than declared. Those two report an excess only, never fewer, and
        not the shapes the module docstring's limit names. In every None
        case the caller decodes as it did before this check existed; None
        never means "consistent".

        ``table_name`` is the table's name, or `WALKED_FRAMES` or
        `NATIVE_FRAMES` when no table counted (#620), and
        `frame_count_mismatch_words` words each by what it is.

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
        encapsulated = ds.file_meta.TransferSyntaxUID.is_encapsulated
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

    # Native: no table, and no fragments -- the element's length is what
    # pydicom reads frames from (#620).
    if not encapsulated:
        return _native_excess(ds, declared, declared_raw)

    # The EOT first: when it is present the BOT is required to be empty
    # (PS3.5 A.4), so a BOT-only count would see nothing. Eight bytes per
    # frame, one 64-bit offset each. `ds.get` hands back the raw bytes
    # here rather than a DataElement (measured, pydicom 3.0.2); the
    # `getattr` takes either, so neither shape reads as "no table".
    #
    # **Counted even when its Lengths disagree**, deliberately unlike the
    # walks (`extended_offsets`). pydicom drops such a table and walks the
    # fragments, and under NumberOfFrames 1 that walk returns frame 0 and
    # never mentions frame 1; counted from the table, ingest keeps frame 0
    # and says it discarded one (measured, dev-F1/m1_count_*.raw). Read
    # through the walks' rule instead, the row went and frame 1 was lost
    # in silence. The count decides whether to report; it walks nothing.
    #
    # **A table whose count equals the declared one is not the last word
    # when pydicom drops it** (#620). Its offsets then say nothing about
    # the frames the decoder reads: pydicom walks the fragments, and the
    # walk can find more. So that one shape -- a table pydicom will not
    # walk by, agreeing with NumberOfFrames -- is asked of the walk too.
    # A table that disagrees is reported from the table, as above; one
    # pydicom walks by names every frame it reads.
    eot = ds.get("ExtendedOffsetTable")
    if eot:
        eot_bytes = getattr(eot, "value", eot)
        counted = (len(eot_bytes) // 8, declared, declared_raw,
                   "Extended Offset Table")
        if counted[0] != declared or extended_offsets(ds) is not None:
            return counted
        return _walked_excess(ds, declared, declared_raw) or counted

    try:
        offsets = parse_basic_offsets(ds.PixelData)
    except (ValueError, struct.error, TypeError, AttributeError):
        # Unparsable -- including a `PixelData` of None, which
        # `parse_basic_offsets` meets as `AttributeError: 'NoneType' object
        # has no attribute 'read'`. Not this check's question: the decoder
        # that runs next refuses such a buffer on its own terms.
        return None
    if not offsets:
        return _walked_excess(ds, declared, declared_raw)
    return (len(offsets), declared, declared_raw, "Basic Offset Table")


#: The source `offset_table_frame_count` names when no table counted the
#: frames: pydicom's walk of the fragments by their end markers, and a
#: native element's length (#620). `frame_count_mismatch_words` gives
#: these their own verb.
WALKED_FRAMES = "Pixel Data's fragments"
NATIVE_FRAMES = "Pixel Data's length"


def _walked_excess(ds, declared, declared_raw) -> Optional[FrameCount]:
    """The frames pydicom's walk finds beyond NumberOfFrames, or None (#620).

    Asked only where no table pydicom walks by names the frames: an empty
    Basic Offset Table, or an Extended Offset Table it drops. pydicom's
    decoder then walks the fragments (`generate_fragmented_frames`), and
    with more fragments than declared frames it ends a frame at every
    fragment whose last ten bytes hold an end marker -- which can find
    more frames than NumberOfFrames, every one of them returned. Counted
    here with that generator, so the count and the decoder cannot
    disagree about how many there are.

    Excess only: a walk that finds as many or fewer is None, and the
    decode proceeds as it did. Fewer is not reported because the walk
    cannot tell a short file from frames that span fragments without end
    markers the search recognises; the decoder refuses a real shortfall
    in its own words.

    **NumberOfFrames 1 (or absent) is not walked**: pydicom joins every
    fragment into one frame there, so no excess can be found -- the
    stated limit in the module docstring. Nor is a buffer with no more
    fragments than declared frames: pydicom reads one fragment per frame.

    The walk's `warn_and_log` for a trailing fragment with no end marker
    is **not suppressed**. It is the warning pydicom's own decode of this
    file emits, and `warnings.catch_warnings` was ruled out in #472: on
    3.12 it mutates the process-global filters, and this runs on threads.

    Cost: one pass over the item headers (`parse_fragments`) on
    encapsulated data with no table to count by; the walk, which copies
    each fragment's bytes transiently, only when there are more fragments
    than declared frames and at least two are declared.
    """
    if declared < 2:
        return None
    try:
        buf = BytesIO(ds.PixelData)
        # Past the Basic Offset Table item, as pydicom's generator reads
        # it: counted on the raw bytes, the table item is a fragment too.
        parse_basic_offsets(buf)
        fragments, _offsets = parse_fragments(buf)
        if fragments <= declared:
            return None
        walked = sum(1 for _frame in generate_fragmented_frames(
            ds.PixelData, number_of_frames=declared))
    except Exception:  # pylint: disable=broad-except
        # Not this check's refusal to make: the decoder that runs next
        # meets the same buffer and refuses it in its own words.
        return None
    if walked > declared:
        return (walked, declared, declared_raw, WALKED_FRAMES)
    return None


def _native_excess(ds, declared, declared_raw) -> Optional[FrameCount]:
    """The whole frames a native element holds beyond NumberOfFrames (#620).

    pydicom's rule, read from `DecodeRunner._validate_buffer`: a native
    `PixelData` longer than its declared frames plus the one trailing pad
    byte (PS3.5 7.1.1) holds `len // frame_length` whole frames, and when
    that is more than NumberOfFrames the decoder returns every one. The
    frame length is the runner's own, so a descriptor pydicom reads one
    way cannot be counted another.

    None when pydicom would not return an excess: no more whole frames
    than declared; a frame length that is not a whole number of bytes (a
    1-bit frame such as 3x5, where pydicom's own excess arithmetic raises
    `TypeError` -- not silent, and the stated limit); `YBR_FULL_422`,
    whose longer buffer pydicom reads as a wrong label rather than as
    frames; a NumberOfFrames pydicom refuses (negative, or empty), for
    which it returns no frames at all. Never raises: a dataset pydicom
    cannot set up a runner for is refused by the decode that follows, in
    its words.
    """
    if not (declared_raw is None or isinstance(declared_raw, int)
            and declared_raw >= 0):
        return None
    try:
        runner = DecodeRunner(ds.file_meta.TransferSyntaxUID)
        runner.set_source(ds)
        frame_length = runner.frame_length(unit="bytes")
        if (not isinstance(frame_length, int) or frame_length <= 0
                or runner.photometric_interpretation == "YBR_FULL_422"):
            return None
        actual = len(ds.PixelData)
    except Exception:  # pylint: disable=broad-except
        return None
    expected = frame_length * declared
    whole = actual // frame_length
    if actual > expected + expected % 2 and whole > declared:
        return (whole, declared, declared_raw, NATIVE_FRAMES)
    return None


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
    # A table names its frames. The walk and the length do not name
    # anything, and "names" would claim a count the file wrote (#620):
    # the walk finds frames by end markers, and the length holds whole
    # frames' worth of bytes, neither of which is a statement that they
    # are frames.
    if table_name == WALKED_FRAMES:
        counted_words = (f"{table_name} hold {table_frames} frames by "
                         f"their end markers")
    elif table_name == NATIVE_FRAMES:
        counted_words = f"{table_name} holds {table_frames} whole frames"
    else:
        counted_words = f"{table_name} names {table_frames} frames"
    return f"{counted_words}; {declared_words}"


#: The declared colour spaces whose decoded samples this handler converts
#: itself, per syntax, and the label the result is in (#448, #464). A
#: JPEG-LS stream carries no colour transform, so `jpegls_decode` returns
#: the YBR samples exactly as the file stores them. The conversion is
#: pydicom's `convert_color_space`, the function pydicom's own door
#: applies (#372). pydicom with pyjpegls returns the same bytes for these
#: files and labels them RGB (measured).
#:
#: **Every door converts through this one table**, by way of
#: `io_handlers._decode_with_imagecodecs` (#453). Until #464, ingest
#: converted for itself, and the read doors
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

#: No second table of label-only relabels here (#453). `DECODER_RELABELS`
#: named the rows `jpeg2k_decode` converts itself (YBR_RCT/ICT to RGB) for
#: this module's `get_pixel_data`, which relabelled its dataset from it;
#: with that door gone its only reader went too, and the one table is
#: `io_handlers._FALLBACK_PHOTOMETRICS` under `_FALLBACK_DECODER_CONVERTS`.
#: Rows cannot be put in `CONVERTS_TO` instead: `convert_colour` would
#: hand `YBR_RCT` to `convert_color_space`, which has no such conversion.


def colour_conversion(ds) -> Optional[Tuple[str, str]]:
    """`(declared, converted)` when this handler converts `ds`'s decode.

    None when there is nothing to convert. None, too, when the frame is
    not 8-bit: `convert_color_space` refuses `uint16`, and #461's ruling
    (Q5) records 16-bit YBR_FULL as a limit rather than a conversion.
    `io_handlers._decode_with_imagecodecs` refuses such a frame before
    this is asked, naming the depth, at every door (#453).

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
    for marker, pos in _jpeg_marker_segments(data, 2):
        if marker == 0xF7:
            return data[pos + 4] if pos + 4 < len(data) else None
        if marker in (0xD9, 0xDA):
            # EOI or SOS: the scan began with no frame header before it.
            return None
    return None


def _jpeg_marker_segments(data, start, stops=(0xD9, 0xDA)):
    """`(marker, pos)` for each marker segment from `start`, by walking.

    Each step is the segment's own length, so a COM or APPn payload --
    opaque bytes that may hold any marker pair -- is stepped over, never
    read (`_jpegls_precision`'s reason, #478). Fill bytes ahead of a
    marker are skipped (ITU-T T.81 B.1.1.2). Ends at a byte that is not a
    marker, at the end of `data`, or after yielding a marker in `stops`:
    by default EOI and SOS, after which entropy-coded data follows.

    One walk for the JPEG-LS readers (`_jpegls_precision`, `_jpegls_near`),
    the JPEG frame header (`_jpeg_frame_type`) and the JPEG 2000 main header (`_j2k_irreversible`, whose segments have
    the same marker-then-length shape between SOC and the first SOT).
    """
    pos = start
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            return
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        yield marker, pos
        if marker in stops:
            return
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")


def _jpegls_near(codestream) -> Optional[int]:
    """The NEAR parameter of a JPEG-LS frame's first scan, or None (#601).

    Read from the first SOS (`FF DA`) segment, reached by walking
    (`_jpeg_marker_segments`), never by searching: `Ls` (2 bytes), `Ns`
    (1), `Ns` component specifications of 2 bytes each, then NEAR (ITU-T
    T.87 C.2.3). 0 is lossless; above 0, each sample may differ from its
    source by up to NEAR.

    **Limit: the first scan speaks for the frame.** An ILV 0 stream has
    one scan per component, each with its own NEAR; a stream whose later
    scans use another NEAR than its first is not detected. None when
    there is no SOS before the data ends.
    """
    data = bytes(codestream)
    if data[:2] != b"\xff\xd8":
        return None
    for marker, pos in _jpeg_marker_segments(data, 2):
        if marker == 0xDA:
            if pos + 4 >= len(data):
                return None
            at = pos + 5 + 2 * data[pos + 4]
            return data[at] if at < len(data) else None
    return None


#: The markers in `FF C0`-`FF CF` that are not frame headers: DHT, JPG
#: (reserved) and DAC (ITU-T T.81 Table B.1). A walk that took every `Cn`
#: for a SOF would read a Huffman table ahead of the frame as SOF4.
_JPEG_NOT_FRAME_HEADERS = frozenset({0xC4, 0xC8, 0xCC})


def _jpeg_frame_type(codestream) -> Optional[int]:
    """`n` of the first SOFn frame header in a JPEG stream, or None (#601).

    The frame header names the coding process (ITU-T T.81 Table B.1):
    SOF0, 1, 2, 5, 6, 9, 10, 13 and 14 are DCT processes, lossy by
    definition; SOF3, 7, 11 and 15 are lossless. Reached by walking
    (`_jpeg_marker_segments`), never by searching, so a COM or APPn
    payload holding `FF C0` is stepped over. None when a scan (SOS) or
    the end of the image comes before any frame header, or the data
    ends first.

    The transfer syntax does not settle it: a lossless SOF3 frame under
    `.50` or `.51` decodes bit-exact (review J2 M1), and reading the
    syntax as evidence stamped it `01`, which can never be withdrawn.

    **Limit: the first frame header speaks for the stream.** A hierarchical
    stream (DHP) can follow a DCT frame with lossless differential ones;
    it is read by its first frame. None was built or measured.
    """
    data = bytes(codestream)
    if data[:2] != b"\xff\xd8":
        return None
    for marker, _pos in _jpeg_marker_segments(data, 2):
        if 0xC0 <= marker <= 0xCF and marker not in _JPEG_NOT_FRAME_HEADERS:
            return marker - 0xC0
    return None


def _jpeg_precision(codestream) -> Optional[int]:
    """The sample precision P of the first SOFn frame header, or None (#622).

    ITU-T T.81 B.2.2: the frame header is the marker, its two-byte length
    Lf, then P, one byte -- the same layout `_jpegls_precision` reads out
    of SOF55. Reached by the walk `_jpeg_frame_type` takes, so a COM or
    APPn payload holding `FF C3` is stepped over. None when no frame
    header comes before the scan, or the data ends first.
    """
    data = bytes(codestream)
    if data[:2] != b"\xff\xd8":
        return None
    for marker, pos in _jpeg_marker_segments(data, 2):
        if 0xC0 <= marker <= 0xCF and marker not in _JPEG_NOT_FRAME_HEADERS:
            return data[pos + 4] if pos + 4 < len(data) else None
    return None


#: The syntaxes whose frames are ITU-T T.81 streams, whose SOFn frame
#: header `_jpeg_precision` reads: JPEG Baseline and Extended, and JPEG
#: Lossless.
T81_SYNTAXES = frozenset({JPEGBaseline, JPEGExtended, JPEGLossless,
                          JPEGLosslessSV1})


def _stream_precision(transfer_syntax, frame) -> Tuple[Optional[str], Optional[int]]:
    """``(stream, precision)`` the frame's own header states, or ``(None, None)`` (#622).

    One reader per family, one name per family, so ingest's HighBit row
    and its precision row name a stream the same way: a JPEG 2000
    codestream's SIZ (`_j2k_sample_layout`), a JPEG-LS SOF55
    (`_jpegls_precision`), and a T.81 SOFn (`_jpeg_precision`) -- "JPEG
    Lossless stream" under `.57`/`.70`, "JPEG stream" under `.50`/`.51`.
    ``(None, None)`` for any other syntax, an empty frame, or a header
    that does not parse.
    """
    if not frame:
        return None, None
    if transfer_syntax in J2K_SYNTAXES:
        layout = _j2k_sample_layout(frame)
        return ("JPEG 2000 codestream", layout[1]) if layout else (None, None)
    if transfer_syntax in JPEGLS_SYNTAXES:
        precision = _jpegls_precision(frame)
        stream = "JPEG-LS stream"
    elif transfer_syntax in (JPEGLossless, JPEGLosslessSV1):
        precision = _jpeg_precision(frame)
        stream = "JPEG Lossless stream"
    elif transfer_syntax in (JPEGBaseline, JPEGExtended):
        precision = _jpeg_precision(frame)
        stream = "JPEG stream"
    else:
        return None, None
    return (stream, precision) if precision is not None else (None, None)


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
    dependency this module should not have. Two arms are ours and not
    pydicom's: `get_j2k_parameters` has no guard for a JP2 box declaring
    length 0, and this returns None where that walk would not terminate
    (`test_a_jp2_box_of_zero_length_is_refused_rather_than_walked_forever`);
    and it reads LBox alone, so an XLBox `jp2c` hides from it as it hid
    from this walk until #610 (`test_an_xlbox_jp2c_is_read`).

    None when neither form parses, which no stream `jpeg2k_decode`
    accepted can reach -- the SIZ is what tells a decoder the image's
    size and depth. The caller then leaves the array exactly as the codec
    returned it, which is what every door did before this rule existed.
    """
    data = bytes(codestream)
    offset = _j2k_codestream(data)
    if offset is None:
        return None
    if data[offset:offset + 2] != b"\xff\x4f":
        return None
    if data[offset + 2:offset + 4] != b"\xff\x51":
        return None
    if offset + 42 >= len(data):
        return None
    ssiz = data[offset + 42]
    return bool(ssiz & 0x80), (ssiz & 0x7F) + 1


def _j2k_codestream(data) -> Optional[int]:
    """Where the codestream starts in `data`: 0, or past a JP2 box (#601).

    Factored out of `_j2k_sample_layout` so it and `_j2k_irreversible`
    share one unwrap, and #610's XLBox trap stays in one place. None when
    `data` is a JP2 file whose boxes do not lead to a `jp2c`. The caller
    checks for SOC itself.
    """
    offset = 0
    if data.startswith(b"\x00\x00\x00\x0c\x6a\x50\x20\x20"):
        # A JP2 file: 12-byte signature box, then boxes until `jp2c`,
        # whose payload is the codestream. A box is LBox (4 bytes), TBox
        # (4), then its payload -- unless LBox is 1, when the length is
        # the 8-byte XLBox that follows TBox and the header is 16 bytes
        # (15444-1 I.4). The walk read LBox alone until #610, so an
        # XLBox `jp2c` was stepped over by 1 byte into its own
        # codestream and never found: the frame passed this gate
        # unjudged, and a signed codestream under PixelRepresentation 0
        # decoded at every door as Pillow's flipped reading.
        offset = 12
        while offset + 8 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            header = 8
            if length == 1:
                # A file that ends inside the XLBox reads a short slice,
                # which `int.from_bytes` takes without raising. No guard:
                # on a `jp2c` box the 16-byte step overruns the end and
                # fails the SOC check; on any other box the number fails
                # the header check, or is 16 or more and steps past the end.
                length = int.from_bytes(data[offset + 8:offset + 16], "big")
                header = 16
            if data[offset + 4:offset + 8] == b"\x6a\x70\x32\x63":
                # LBox 0 means "to the end of the file", which for the
                # codestream box is exactly where the codestream runs.
                offset += header
                break
            if length < header:
                # A box claiming the rest of the file (0) that is not the
                # codestream, or a length shorter than its own header:
                # stepping by it would loop or land inside the header.
                return None
            offset += length
        else:
            return None
    return offset


def _j2k_irreversible(codestream) -> Optional[bool]:
    """Whether a JPEG 2000 codestream uses the 9-7 irreversible wavelet (#601).

    Read from the COD marker segment's SPcod transformation byte, 0 for
    the 9-7 irreversible filter and 1 for the 5-3 reversible one (ISO/IEC
    15444-1 A.6.1), `Lcod` + `Scod` + four bytes of SGcod + four of SPcod
    past the marker. The main header is **walked** from SOC to the first
    SOT (`_jpeg_marker_segments`), never searched: a COM payload can hold
    `FF 52`. A JP2 box is unwrapped first (`_j2k_codestream`).

    None when there is no COD in the main header, or the byte is neither
    value. Limits, stated: a COC that overrides the transform for one
    component and a tile-part COD are not read; and a reversible
    codestream truncated at a lossy rate is lossy in fact and cannot be
    told from a lossless one by its header (measured: `693_J2KI`,
    `SC_rgb_gdcm_KY`).
    """
    data = bytes(codestream)
    offset = _j2k_codestream(data)
    if offset is None or data[offset:offset + 2] != b"\xff\x4f":
        return None
    for marker, pos in _jpeg_marker_segments(data, offset + 2,
                                             stops=(0x90, 0x93, 0xD9)):
        if marker == 0x52:
            transform = data[pos + 13] if pos + 13 < len(data) else None
            return {0: True, 1: False}.get(transform)
    return None


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
    (#478). For JPEG Lossless it passes the stream's precision only when
    that is **wider** than BitsStored (#622) -- PS3.5 8.2.1 has the
    stream's own characteristics control the decompression where they
    contradict the Data Elements -- and nothing otherwise, so a
    conformant stream is extended from BitsStored as #446 read it:
    `max(precision, BitsStored)`. A stream narrower than BitsStored keeps
    that reading too, and whether it should extend from its own
    precision, as JPEG-LS does, is not decided here.
    `io_handlers._decode_pixels` calls this on pydicom's route for the
    same wider streams, after asking pydicom not to mask them.

    Called for .57/.70/.80/.81 unconditionally, and for JPEG 2000 in one
    case only: an unsigned codestream under PixelRepresentation 1, where
    `_against_pixel_representation` passes the codestream's own precision
    (#460). A *signed* J2K codestream never reaches here --
    `jpeg2k_decode` has already returned signed, sign-extended samples,
    which this would return unchanged (there is no dtype refusal below
    since #523: nothing that reaches here is signed). That is pydicom's
    split too: `_apply_sign_correction` shifts a J2K decode
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
    **That includes an excess no table names** (#620). A fragment beyond
    NumberOfFrames under an empty offset table, or an Extended one pydicom
    drops, is counted by pydicom's own walk, so the decode is asked with
    `allow_excess_frames=False` and a signed codestream there -- which
    Pillow would shift -- never reaches a reader. Until #620 nothing
    dropped it (review of #606, F-r2-2).

    **It never raises of its own.** A buffer `generate_frames` cannot walk,
    or a frame whose SIZ does not parse (`_j2k_sample_layout` returns
    None), has no sign to read: the decoder that runs next refuses it in
    its own words, as it did before this gate.

    **The frames the decoder reads.** Walked with the Extended Offset
    Table when pydicom's decoder walks with it (`extended_offsets`), so
    a fragment no frame names cannot join a signed codestream and hide
    its SIZ (review of #606, M1).

    Cost: one transient copy of each declared frame's bytes
    (`generate_frames` joins its fragments), and a read of its first
    43.
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
                                            number_of_frames=declared,
                                            extended_offsets=extended_offsets(ds)),
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
        # Extended from the stream's own precision where it is wider than
        # BitsStored (#622), and from BitsStored otherwise: the wider of
        # the two. From BitsStored 8 a 12-bit -2048 read 0. None, not
        # BitsStored, for the narrower case, so #446's reading and its
        # refusal words ("from BitsStored") are the ones that run.
        precision = _jpeg_precision(bitstream)
        bits_stored = int(getattr(ds, "BitsStored", 0) or 0)
        return _sign_extend(
            _in_declared_container(imagecodecs.ljpeg_decode(bitstream), ds),
            ds, precision if precision is not None
            and precision > bits_stored else None)
    if transfer_syntax in [JPEGBaseline, JPEGExtended]:
        # Reached through the fallback since #604, monochrome only
        # (`io_handlers._FALLBACK_JPEG`): 12-bit JPEG Extended, which
        # Pillow refuses. No container widening and no sign extension:
        # pydicom applies neither to these syntaxes, and a decode that
        # disagrees with BitsAllocated or PixelRepresentation is refused
        # by the fallback's dtype check.
        #
        # **A stream that does not end in EOI is refused first** (review
        # of #606, M-r2-1). libjpeg-turbo treats a premature end of data
        # as a warning and fills the rest of the image with mid-grey --
        # 128, or 2048 at 12 bits -- so a file cut short decoded to rows
        # it never held, passed every check after the decode, and was
        # stored with no row: for an 8-bit file, over Pillow's own
        # "image file is truncated". Trailing 0x00 and 0xFF are padding:
        # an item is padded to even length, and DCMTK writes `ff d9 ff`
        # (pydicom's `JPEG-lossy.dcm`). Every `.50`/`.51` file in
        # pydicom's test data and every `jpeg8_encode` stream measured
        # ends in EOI. A stream closed with EOI after data lost from its
        # middle still decodes with the fill; that is the mid-stream
        # limit `docs/installation.md` states.
        if not bytes(bitstream).rstrip(b"\x00\xff").endswith(b"\xff\xd9"):
            raise ValueError(
                "the JPEG stream ends without an EOI marker, so it was cut "
                "short: libjpeg-turbo would fill the missing rows with "
                "mid-grey")
        return imagecodecs.jpeg_decode(bitstream)
    if transfer_syntax in J2K_SYNTAXES:
        # HTJ2K included, through `jpeg2k_decode`: see `J2K_SYNTAXES`.
        #
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
    means to keep. The refusal of a mismatch is
    `_decode_with_imagecodecs`', and `Instance.get_pixel_data()`'s through
    `frame_count_mismatch`; this is not a third spelling of it.

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
                                  number_of_frames=number_of_frames,
                                  extended_offsets=extended_offsets(ds)),
                  number_of_frames)]
    return frames[0] if number_of_frames == 1 else np.stack(frames)

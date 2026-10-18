"""One answer to "which axis of this pixel array means what".

A rank-3 array is either ``(frames, rows, cols)`` or ``(rows, cols,
samples)``, and its shape alone cannot say which; a test such as
``if shape[-1] in [3, 4]`` is a guess. The descriptors in
``Instance.attributes`` settle it, and every caller that needs a layout
asks this module rather than reading ``.shape`` itself.

The rule this module implements:

    **Attributes are authoritative for layout -- which axis means what.
    The array is authoritative for magnitude -- how large each axis is.**

Handing a setter a differently-sized array is a legitimate thing to do and
stays legitimate; guessing which axis is which is the defect. When the two
genuinely contradict each other -- an instance declaring *n* samples per
pixel beside an array with no axis that can hold them -- neither statement
can be trusted, so this raises rather than picking a winner.
"""
# Import constraints, deliberate and load-bearing:
#
# - No numpy. The input is a shape tuple, not an array.
# - No `entities`, no `io_handlers`. Both import this; going the other
#   way would be a cycle.
# - No third-party import at all, so it adds no `install_requires` entry.
# - `_export_instance_worker` runs in a separate process under
#   `session.export()` on every interpreter; by default `write_tree()`
#   spawns it on a GIL build and runs it in the caller's own threads on a
#   free-threaded one (the `ExportOutcome.corrections` note in
#   `io_handlers.py`). So this has to be importable at module scope in a
#   bare child, and a dependency-free module is all that child needs.
#
# `PixelGeometry` is a `NamedTuple` so it stays picklable, though nothing
# sends one across a process boundary.
from enum import Enum
from typing import Any, NamedTuple, Optional, Sequence

TAG_SAMPLES_PER_PIXEL = "0028,0002"
TAG_PHOTOMETRIC_INTERPRETATION = "0028,0004"
TAG_PLANAR_CONFIGURATION = "0028,0006"
TAG_NUMBER_OF_FRAMES = "0028,0008"
TAG_ROWS = "0028,0010"
TAG_COLUMNS = "0028,0011"

TAG_FLOAT_PIXEL_DATA = "7fe0,0008"
TAG_DOUBLE_FLOAT_PIXEL_DATA = "7fe0,0009"

#: Where an instance records the numpy dtype of the pixel frame it is
#: holding, when no DICOM descriptor can name that dtype -- the three
#: floating-point widths and `bool`. See
#: `SIDECAR_DTYPE_NAMES` for why the integer dtypes are deliberately not
#: here.
#:
#: Not a tag key, and deliberately so. It rides `attributes_json`
#: because `DicomExporter._merge` skips `t.startswith("_")` and
#: `_split_core_and_private` keeps a non-tag key inline -- the same
#: channel `_ISOCENTER_REDACTION_HASH` already uses. A real
#: `"7fe0,0008"` key in `attributes` would be written back out as an
#: element by `_merge`, carrying this string as its value.
#:
#: It exists because **no DICOM descriptor says "float"**, and none says
#: "bool" either. `SidecarPixelLoader` derives its dtype from
#: BitsAllocated and PixelRepresentation, and a 32-bit float frame and a
#: 32-bit integer frame declare the same 32 -- so without this the
#: sidecar would hand back integers where floats went in. `bool` is the
#: same case at 8 bits: numpy `bool_` and `uint8` both declare
#: BitsAllocated 8 with PixelRepresentation 0, so a mask would come back
#: as `uint8`. Set at
#: ingest from the element the bytes came out of, and kept true by
#: `set_pixel_data()` from the dtype of the array it is handed.
#:
#: **The dtype, not the element**, and the difference is float16: it has
#: no DICOM element at any width, so an element-shaped carrier could not
#: name it and a `float16` array would round-trip through the sidecar as
#: `uint16`, and the export's float16 arm and its `DATA_LOSS` row would
#: never run. The sidecar is ours and can hold what DICOM has no element
#: for; what it must never do is hand back a different type from the one
#: it was given.
PIXEL_DTYPE_ATTR = "_ISOCENTER_PIXEL_DTYPE"

#: Where an instance, or a nested icon item, records that its Planar
#: Configuration (0028,0006) of 0 was inferred at ingest because the
#: source stated none (#1134): `"absent"` when the element was missing,
#: `"empty"` when it was present with no value. Absent from every entity
#: whose source declared a value.
#:
#: The same channel as `PIXEL_DTYPE_ATTR`, for the same reason: a
#: non-tag key rides `attributes_json` (and an item's attributes, which
#: are serialised whole), and `DicomExporter._merge` skips `_`-prefixed
#: keys, so it never reaches a file. Assigned directly, never through
#: `set_attr`, which lower-cases its key (`SOURCE_SOP_UID_ATTR`'s note).
#: The export reads it to say, at INFO, that the 0 it writes was
#: inferred; the source may be gone by then and the store reopened, so
#: the fact has to be held. `generate_report` counts it for section 5.
PLANAR_INFERRED_ATTR = "_ISOCENTER_PLANAR_CONFIGURATION_INFERRED"

#: Where an instance, or a nested icon item, records that its source
#: declared a Planar Configuration (0028,0006) other than 0 under a
#: syntax whose compressed stream fixes the layout, which was read and
#: written as 0 (#1137): `"declared"`. Absent otherwise. The same channel,
#: and the same reasons, as `PLANAR_INFERRED_ATTR`. A key of its own, not
#: a third value of that one: each of #1134's readers (the export note,
#: the report's count, the icon's write-back) treats any value under it as
#: "stated none", so a third value would make all three false at once.
PLANAR_OVERRIDDEN_ATTR = "_ISOCENTER_PLANAR_CONFIGURATION_OVERRIDDEN"

#: The dtypes this carrier may name, as an allow-list. A stored string is
#: data, and `np.dtype()` on an arbitrary one is not something a loader
#: should do.
#:
#: The three float widths are every floating-point width numpy and DICOM
#: between them produce here. `bool` is the only integer-kind dtype that
#: belongs here: `set_pixel_data()` records `PixelRepresentation` as well
#: as `BitsAllocated`, and that pair
#: names every integer dtype the sidecar can hold *exactly*, and a
#: carrier recorded as well would be a second answer to a question the
#: descriptors already answer -- and the authoritative one, so a graph
#: whose descriptors were later corrected would decode against a stale
#: carrier. `bool` is the exception because no DICOM descriptor can name
#: it: numpy `bool_` and `uint8` both declare BitsAllocated 8 with
#: PixelRepresentation 0, the same argument as for float16 and the
#: reason this carrier exists at all.
SIDECAR_DTYPE_NAMES = frozenset({"float16", "float32", "float64", "bool"})

#: Numpy dtype name for each float pixel element. PS3.3 C.7.6.24 fixes
#: (7fe0,0008) at 32-bit IEEE-754 and C.7.6.25 fixes (7fe0,0009) at
#: 64-bit.
FLOAT_DTYPE_BY_ELEMENT = {
    TAG_FLOAT_PIXEL_DATA: "float32",
    TAG_DOUBLE_FLOAT_PIXEL_DATA: "float64",
}

#: Photometric Interpretations that are `SamplesPerPixel = 1`.
#: PALETTE COLOR is in here deliberately -- it is a single sample indexing a
#: lookup table, not three interleaved ones.
MONOCHROME_PHOTOMETRIC = frozenset({"MONOCHROME1", "MONOCHROME2", "PALETTE COLOR"})

#: The sample counts an undeclared last axis is allowed to be read as. It
#: stays {3, 4} rather than narrowing to {3}: `samples == 4` guarantees a
#: file pydicom refuses, which is loud, whereas reading genuine RGBA as
#: frames produces a plausible wrong image, silently. The ambiguous case is
#: refused at the export boundary instead of being narrowed here.
_IMPLICIT_SAMPLE_COUNTS = (3, 4)


class GeometryEvidence(Enum):
    """What settled the layout question -- not a confidence level.

    Callers apply different policies to ``GUESSED``: `Instance.set_pixel_data`
    accepts it with a warning, so a hand-built graph can be given pixels
    before its attributes, while `_export_instance_worker` refuses it, so a
    guess never becomes a file on disk.
    """

    DECLARED = "declared"      # SamplesPerPixel / NumberOfFrames chose the arm
    STRUCTURAL = "structural"  # only one arm was admissible for this rank
    MATCHED = "matched"        # Rows/Columns broke the tie
    GUESSED = "guessed"        # nothing resolved it; last-axis heuristic


class PixelGeometry(NamedTuple):
    """A resolved layout for one pixel array.

    ``frames`` is **never** the declared NumberOfFrames. It is the array's
    first axis on the frames-major arms and ``1`` on the others, so
    ``frames <= shape[0]`` holds for every rank and every arm, and
    ``range(geometry.frames)`` over the array's first axis is in bounds.
    """

    frames: int
    rows: int
    cols: int
    samples: int
    evidence: GeometryEvidence


def declared_int(attributes: Any, tag: str) -> Optional[int]:
    """Read one declared descriptor as an int, or ``None`` for "not declared".

    Absent, empty, unparseable and non-scalar all read as ``None``, never
    as a default. ``"3"`` and ``3`` both read as 3.

    Args:
        attributes: The instance's attributes mapping, or anything with a
            ``.get``. A mapping whose ``.get`` raises reads as "not declared".
        tag (str): The lowercase-hex tag string, e.g. ``"0028,0008"``.

    Returns:
        Optional[int]: The declared value, or None.
    """
    # Never a default: a hand-built instance that has declared nothing is
    # precisely the case that needs deriving, and collapsing "absent" into
    # "1" would silently choose the frames arm for every fixture-generator
    # RGB image. The `str` coercion below is load-bearing: a graph can hold
    # "3" where ingest stores 3, and an object with no integer spelling
    # (a `MagicMock`) must read as "not declared".
    try:
        raw = attributes.get(tag)
    except Exception:  # pylint: disable=broad-except
        return None
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


#: A Code String's letters (PS3.5 6.2): `a`-`z` to `A`-`Z`, and nothing
#: else. Python's own upper-casing is Unicode's, which rewrites `ß` as
#: `SS`: a different value, not a spelling of the same one (#612).
_CS_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz",
                          "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


#: The encoding pydicom's writer gives a Code String, whatever the
#: dataset's Specific Character Set says: its `default_encoding`, ISO
#: 8859-1, one byte for each of U+0000 to U+00FF and nothing for anything
#: above. Written here and not imported, because this module imports
#: nothing heavy (`test_module_imports_nothing_heavy`);
#: `test_a_code_string_is_encoded_as_pydicom_encodes_it` holds the two
#: together.
_CS_ENCODING = "iso8859"


def _cs_unencodable(text: str) -> str:
    """The characters of `text` a file cannot hold in a Code String."""
    held = []
    for char in text:
        try:
            char.encode(_CS_ENCODING)
        except UnicodeEncodeError:
            held.append(char)
    return "".join(held)


def _cs_spelling(value):
    """`value` as this library spells a Code String, when it is text.

    The one spelling of a declared Photometric Interpretation: the
    resolver reads through it (`_declared_str`), the export worker writes
    through it and the judgement of the written label reads through it
    (both in `io_handlers`), so no two of them can take one declaration
    for two labels.

    - Whitespace is removed from both ends, everything Python's own strip
      removes: the space, the ASCII control characters U+0009 to U+000D
      and U+001C to U+001F, U+0085, a no-break space and the Unicode
      spaces. Over pixel data a file holding one of them at an end of
      the label is one pydicom refuses to decode, and so does this
      library's own ingest; with no pixel element, or on an icon item
      whose pixels are not carried, such a file is ingested and the
      label reaches here as the file held it.
    - `a`-`z` are upper-cased and no other letter is touched: `'rgß'` is
      `RGß`, not Unicode's `RGSS`.
    - **Unless the file cannot hold that spelling.** A character above
      U+00FF cannot be encoded in a Code String, and the instance would
      not be exported at all. Then the spelling is the one releases
      before #612 wrote, Python's own upper-casing (a dotless `ı` is `I`,
      a long `ſ` is `S`), which for those labels is one the file can
      hold; the export says so with a WARNING. Where that spelling
      cannot be encoded either (fullwidth letters) it is still what is
      returned, and the file fails, as it did.

    Text only. A `bytes` value is not spelled: it is returned as held,
    ASCII or not, and written as held, which is what releases
    before #612 did with it (owner ruling, 2026-10-11). A file can bring one
    in by stating a binary VR for (0028,0004), so respelling it would
    change what a source's own label exports as.

    Args:
        value: One value of a Code String element, as the graph holds it.

    Returns:
        The spelled `str`, or `value` itself when it is not a `str`.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    spelled = text.translate(_CS_UPPER)
    if _cs_unencodable(spelled):
        return text.upper()
    return spelled


def _declared_str(attributes: Any, tag: str) -> Optional[str]:
    """Read one declared label, spelled as `_cs_spelling` spells it."""
    try:
        raw = attributes.get(tag)
    except Exception:  # pylint: disable=broad-except
        return None
    if raw is None or raw == "":
        return None
    # Not Python's own strip and upper-casing, which this was until #612:
    # the worker writes the label by `_cs_spelling`, and a resolver that
    # spelled it otherwise answered "leave it alone" about a label the
    # worker then wrote as a different one. A value that is not text
    # (`bytes`, a number) is read as the text of its `str`, as it always
    # was: that names no label, so the resolver leaves it to the writer,
    # which writes it as held.
    try:
        text = _cs_spelling(raw if isinstance(raw, str) else str(raw))
    except Exception:  # pylint: disable=broad-except
        return None
    return text or None


def _contradiction(shape, s_d, f_d, r_d, c_d) -> ValueError:
    """Build the error for "the array and the attributes cannot both be right".

    Args:
        shape (tuple): The array's shape.
        s_d (Optional[int]): Declared SamplesPerPixel.
        f_d (Optional[int]): Declared NumberOfFrames.
        r_d (Optional[int]): Declared Rows.
        c_d (Optional[int]): Declared Columns.

    Returns:
        ValueError: The error for the caller to raise, naming the shape and
        the four declared values.
    """
    # Raising is the only outcome that neither corrupts nor lies. Trusting
    # the attributes writes descriptors that do not describe the bytes;
    # trusting the array is guessing the layout. A DATA_LOSS row would
    # misname it: nothing was dropped, two statements disagree.
    return ValueError(
        f"Pixel array shape {tuple(shape)} cannot be reconciled with the "
        f"instance's declared geometry (SamplesPerPixel={s_d}, "
        f"NumberOfFrames={f_d}, Rows={r_d}, Columns={c_d}). No axis of the "
        f"array can carry {s_d} samples per pixel. Correct the array or the "
        f"attributes; they cannot both be right.")


def _resolve_rank1(shape, s_d, f_d, r_d, c_d) -> PixelGeometry:
    """Rank 1 -- a flat buffer, reshaped from the declared descriptors.

    Args:
        shape (tuple): The array's shape, ``(n,)``.
        s_d (Optional[int]): Declared SamplesPerPixel.
        f_d (Optional[int]): Declared NumberOfFrames.
        r_d (Optional[int]): Declared Rows.
        c_d (Optional[int]): Declared Columns.

    Returns:
        PixelGeometry: DECLARED when the declared size fits in the buffer
        (the caller truncates any padding); otherwise a single
        STRUCTURAL row of ``shape[0]`` columns.
    """
    # The sidecar loader returns a 1-D array only when the stored metadata
    # is too small for the buffer, so this consults the same metadata that
    # just failed.
    rows = r_d or 0
    cols = c_d or 0
    samples = s_d if s_d is not None else 1
    frames = f_d if f_d is not None else 1

    expected = rows * cols * samples * frames
    if expected > 0 and shape[0] >= expected:
        # A buffer larger than the declared size carries DICOM's even-length
        # padding; the caller truncates.
        return PixelGeometry(frames, rows, cols, samples,
                             GeometryEvidence.DECLARED)

    # Nothing declared, or declared too small for the buffer. One axis, one
    # interpretation -- there is no arm to choose between.
    return PixelGeometry(1, 1, shape[0], 1, GeometryEvidence.STRUCTURAL)


def _resolve_rank3(shape, s_d, f_d, r_d, c_d) -> PixelGeometry:
    """Rank 3 -- the ambiguity, and the only place arms have to be eliminated.

    Arm A is ``(frames, rows, cols)`` with one sample per pixel; arm B is
    ``(rows, cols, samples)`` with one frame.

    Args:
        shape (tuple): The array's shape.
        s_d (Optional[int]): Declared SamplesPerPixel.
        f_d (Optional[int]): Declared NumberOfFrames.
        r_d (Optional[int]): Declared Rows.
        c_d (Optional[int]): Declared Columns.

    Returns:
        PixelGeometry: The chosen arm, with the evidence that chose it.

    Raises:
        ValueError: If neither arm can carry the declared SamplesPerPixel.
    """
    def arm_a(evidence):
        """Arm A, ``(frames, rows, cols)``.

        Args:
            evidence (GeometryEvidence): What chose this arm.

        Returns:
            PixelGeometry: The layout.
        """
        return PixelGeometry(shape[0], shape[1], shape[2], 1, evidence)

    def arm_b(evidence):
        """Arm B, ``(rows, cols, samples)``.

        Args:
            evidence (GeometryEvidence): What chose this arm.

        Returns:
            PixelGeometry: The layout.
        """
        return PixelGeometry(1, shape[0], shape[1], shape[2], evidence)

    # Step 1 -- admissibility. Layout evidence only; magnitudes are ignored
    # here, because a declared Rows that disagrees with the array is the
    # setter doing its job, not a contradiction.
    a_admissible = s_d is None or s_d == 1
    b_admissible = (
        (s_d is not None and s_d == shape[2])
        or (s_d is None and shape[2] in _IMPLICIT_SAMPLE_COUNTS))

    if not a_admissible and not b_admissible:
        raise _contradiction(shape, s_d, f_d, r_d, c_d)

    evidence = (GeometryEvidence.DECLARED if s_d is not None
                else GeometryEvidence.STRUCTURAL)
    if a_admissible and not b_admissible:
        return arm_a(evidence)
    if b_admissible and not a_admissible:
        return arm_b(evidence)

    # Step 2 -- NumberOfFrames as tiebreak. Note that this asks for *exactly
    # one* arm to fit: shape[0] == 1 with a declared NumberOfFrames of 1 fits
    # both, and must fall through rather than pick arbitrarily. Spelling this
    # as `if f_d > 1: A else: B` returns arm B with DECLARED evidence for a
    # (1,4,3) array -- the right arm, the wrong evidence, and it is the
    # evidence the export refusal reads.
    if f_d is not None:
        a_ok = f_d == shape[0]
        b_ok = f_d == 1
        if a_ok != b_ok:
            return arm_a(GeometryEvidence.DECLARED) if a_ok \
                else arm_b(GeometryEvidence.DECLARED)

    # Step 3 -- Rows/Columns as tiebreak.
    a_ok = ((r_d is None or r_d == shape[1])
            and (c_d is None or c_d == shape[2]))
    b_ok = ((r_d is None or r_d == shape[0])
            and (c_d is None or c_d == shape[1]))
    if (r_d is not None or c_d is not None) and a_ok != b_ok:
        return arm_a(GeometryEvidence.MATCHED) if a_ok \
            else arm_b(GeometryEvidence.MATCHED)

    # Step 4 -- the guess: the last-axis heuristic, reported as a guess so
    # each caller can apply its own policy.
    if shape[2] in _IMPLICIT_SAMPLE_COUNTS:
        return arm_b(GeometryEvidence.GUESSED)
    return arm_a(GeometryEvidence.GUESSED)


def resolve_pixel_geometry(shape: Sequence[int],
                           attributes: Any) -> PixelGeometry:
    """Decide what a pixel array's axes mean, from the attributes first.

    Args:
        shape (Sequence[int]): The array's ``.shape``.
        attributes: The instance's attributes mapping (``{"gggg,eeee": value}``).

    Returns:
        PixelGeometry: The resolved layout, with the evidence that settled it.
        **Never raises on GUESSED** -- it reports the evidence and lets
        each caller apply its own policy.

    Raises:
        ValueError: If the declared SamplesPerPixel cannot be carried by any
            axis of the array (a layout contradiction), or if the rank is not
            one this pipeline supports.
    """
    shape = tuple(shape)
    ndim = len(shape)

    s_d = declared_int(attributes, TAG_SAMPLES_PER_PIXEL)
    f_d = declared_int(attributes, TAG_NUMBER_OF_FRAMES)
    r_d = declared_int(attributes, TAG_ROWS)
    c_d = declared_int(attributes, TAG_COLUMNS)

    if ndim == 1:
        return _resolve_rank1(shape, s_d, f_d, r_d, c_d)

    if ndim == 2:
        # No ambiguity exists at rank 2, so a declared SamplesPerPixel of 3 is
        # overwritten to 1 rather than raising: that is a magnitude
        # disagreement, not a layout one.
        return PixelGeometry(1, shape[0], shape[1], 1,
                             GeometryEvidence.STRUCTURAL)

    if ndim == 3:
        return _resolve_rank3(shape, s_d, f_d, r_d, c_d)

    if ndim == 4:
        if s_d is not None and s_d != shape[3]:
            raise _contradiction(shape, s_d, f_d, r_d, c_d)
        evidence = (GeometryEvidence.DECLARED if s_d is not None
                    else GeometryEvidence.STRUCTURAL)
        return PixelGeometry(shape[0], shape[1], shape[2], shape[3], evidence)

    raise ValueError(f"Unknown shape: {shape}")


def resolve_photometric_interpretation(attributes: Any,
                                       samples: int) -> Optional[str]:
    """Decide what to write for PhotometricInterpretation (0028,0004).

    Photometric Interpretation is **not derivable from an array**: a 3-sample
    array is equally RGB, YBR_FULL, YBR_FULL_422 or YBR_RCT, so
    ``if samples >= 3: "RGB"`` would relabel every ``YBR_FULL`` instance.

    So: correct only an outright contradiction, and only to the neutral
    default for the sample count.

    Args:
        attributes: The instance's attributes mapping.
        samples (int): The resolved samples per pixel.

    Returns:
        Optional[str]: The value to write, or **None meaning "leave it
        alone"**. The None arm is what lets YBR_FULL, YBR_ICT and
        MONOCHROME1 survive a round trip.
    """
    pi = _declared_str(attributes, TAG_PHOTOMETRIC_INTERPRETATION)

    if pi is None:
        return "RGB" if samples >= 3 else "MONOCHROME2"
    if samples >= 3 and pi in MONOCHROME_PHOTOMETRIC:
        # A monochrome interpretation beside three samples is nonconformant
        # whichever half is wrong, and RGB is the neutral reading of three.
        return "RGB"
    if samples == 1 and pi not in MONOCHROME_PHOTOMETRIC:
        return "MONOCHROME2"
    return None


def planar_configuration_default(attributes: Any, samples: int) -> bool:
    """Whether to write PlanarConfiguration (0028,0006) = 0.

    Only when the instance is colour **and has not declared one**. A
    declared value, planar 1 included, is kept: forcing 0 would claim a
    layout nothing had converted. Ingest is not the only way a declared 1
    reaches the graph; a hand-built instance and a reloaded one both carry
    whatever was set.

    For `Instance.set_pixel_data()`: what descriptor the graph should carry
    when the caller supplied none. Not for an exporter, which describes the
    interleaved element it writes and so writes 0 for every colour
    instance.

    Args:
        attributes: The instance's attributes mapping.
        samples (int): The resolved samples per pixel.

    Returns:
        bool: True if the caller should write 0.
    """
    # Do not move the exporter's `_write_pixel_geometry` onto this
    # predicate: it would write interleaved bytes under a
    # `PlanarConfiguration` of 1.
    if samples < 3:
        return False
    try:
        return TAG_PLANAR_CONFIGURATION not in attributes
    except Exception:  # pylint: disable=broad-except
        return True

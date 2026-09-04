"""One answer to "which axis of this pixel array means what".

Four places in this codebase used to decide what an image looked like by
reading a numpy array's ``.shape`` and breaking the ``(frames, rows, cols)``
vs. ``(rows, cols, samples)`` tie with ``if shape[-1] in [3, 4]``. Both
readings are rank 3, so that test is a guess -- and the information that
settles it was sitting unread in ``Instance.attributes`` at every one of the
four sites (#186, #205).

The rule this module implements:

    **Attributes are authoritative for layout -- which axis means what.
    The array is authoritative for magnitude -- how large each axis is.**

Handing a setter a differently-sized array is a legitimate thing to do and
stays legitimate; guessing which axis is which is the defect. When the two
genuinely contradict each other -- an instance declaring *n* samples per
pixel beside an array with no axis that can hold them -- neither statement
can be trusted, so this raises rather than picking a winner.

Import constraints, deliberate and load-bearing:

- **No numpy.** The input is a shape tuple, not an array.
- **No `entities`, no `io_handlers`.** Both import *this*; going the other
  way would be a cycle.
- **No third-party import at all**, so `tests/test_packaging_contract.py`
  needs no new `install_requires` entry.
- `_export_instance_worker` runs in a **separate process on every
  interpreter**, so this has to be importable at module scope in a bare
  child. A module-level function in a dependency-free module is all that
  child needs.

`PixelGeometry` is a `NamedTuple` so it stays picklable, though nothing in
this design sends one across a process boundary.
"""
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
#: holding, when that dtype is floating-point (#183).
#:
#: Not a tag key, and deliberately so. It rides `attributes_json`
#: because `DicomExporter._merge` skips `t.startswith("_")` and
#: `_split_core_and_private` keeps a non-tag key inline -- the same
#: channel `_ISOCENTER_REDACTION_HASH` already uses. A real
#: `"7fe0,0008"` key in `attributes` would be written back out as an
#: element by `_merge`, carrying this string as its value.
#:
#: It exists because **no DICOM descriptor says "float"**.
#: `SidecarPixelLoader` derives its dtype from BitsAllocated and
#: PixelRepresentation, and a 32-bit float frame and a 32-bit integer
#: frame declare the same 32 -- so without this the sidecar hands back
#: integers where floats went in, silently. Set at ingest from the
#: element the bytes came out of, and kept true by `set_pixel_data()`
#: from the dtype of the array it is handed.
#:
#: **The dtype, not the element**, and the difference is float16: it has
#: no DICOM element at any width, so an element-shaped carrier could not
#: name it and a `float16` array round-tripped through the sidecar as
#: `uint16`. The export's float16 arm then never ran, and the
#: `DATA_LOSS` row that says the pixels could not be written was never
#: filed. The sidecar is ours and can hold what DICOM has no element
#: for; what it must never do is hand back a different type from the one
#: it was given.
PIXEL_DTYPE_ATTR = "_ISOCENTER_PIXEL_DTYPE"

#: The float dtypes this carrier may name, as an allow-list. A stored
#: string is data, and `np.dtype()` on an arbitrary one is not something
#: a loader should do; these three are every floating-point width numpy
#: and DICOM between them produce here.
FLOAT_DTYPE_NAMES = frozenset({"float16", "float32", "float64"})

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
    """What settled the layout question -- not how confident we are.

    The distinction matters because the callers apply different policies to
    ``GUESSED``: `Instance.set_pixel_data` accepts it with a warning, so a
    hand-built graph can still be given pixels before its attributes, while
    `_export_instance_worker` refuses it, because that is the boundary at
    which a guess would become a file on disk a recipient cannot tell apart
    from a correct one.
    """

    DECLARED = "declared"      # SamplesPerPixel / NumberOfFrames chose the arm
    STRUCTURAL = "structural"  # only one arm was admissible for this rank
    MATCHED = "matched"        # Rows/Columns broke the tie
    GUESSED = "guessed"        # nothing resolved it; legacy last-axis heuristic


class PixelGeometry(NamedTuple):
    """A resolved layout for one pixel array.

    ``frames`` is **never** the declared NumberOfFrames. It is the array's
    first axis on the frames-major arms and ``1`` on the others, so
    ``frames <= shape[0]`` holds for every rank and every arm. Callers that
    iterate ``range(geometry.frames)`` over the array's first axis are in
    bounds because of that, not by coincidence.
    """

    frames: int
    rows: int
    cols: int
    samples: int
    evidence: GeometryEvidence


def declared_int(attributes: Any, tag: str) -> Optional[int]:
    """Read one declared descriptor as an int, or ``None`` for "not declared".

    Absent, empty, unparseable and non-scalar all read as ``None``. **Never
    as a default** -- a hand-built instance that has declared nothing is
    precisely the case that needs deriving, and collapsing "absent" into "1"
    would silently choose the frames arm for every fixture-generator RGB
    image.

    The `str` coercion is load-bearing rather than defensive. `set_pixel_data`
    used to write `str(frames)` for (0028,0008) while `ingest_worker` stores
    an `int`, so a graph that has been through the buggy path once holds
    ``"3"`` where ingest had ``3``; comparing raw values would fail the frames
    check on exactly the instances the bug already touched. It also keeps the
    `MagicMock` instances in `tests/test_pixel_analysis.py` working, since
    ``int(str(MagicMock()))`` raises and so reads as "not declared".

    Args:
        attributes: The instance's attributes mapping, or anything with a
            ``.get``. A mapping whose ``.get`` raises reads as "not declared".
        tag (str): The lowercase-hex tag string, e.g. ``"0028,0008"``.

    Returns:
        Optional[int]: The declared value, or None.
    """
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


def _declared_str(attributes: Any, tag: str) -> Optional[str]:
    """Read one declared descriptor as a stripped, upper-cased string."""
    try:
        raw = attributes.get(tag)
    except Exception:  # pylint: disable=broad-except
        return None
    if raw is None or raw == "":
        return None
    try:
        text = str(raw).strip().upper()
    except Exception:  # pylint: disable=broad-except
        return None
    return text or None


def _contradiction(shape, s_d, f_d, r_d, c_d) -> ValueError:
    """Build the error for "the array and the attributes cannot both be right".

    Raising is the only outcome that neither corrupts nor lies. Trusting the
    attributes writes descriptors that do not describe the bytes. Trusting the
    array is the behaviour that produced #186. Logging a DATA_LOSS row
    misnames it: nothing was dropped, two statements disagree.
    """
    return ValueError(
        f"Pixel array shape {tuple(shape)} cannot be reconciled with the "
        f"instance's declared geometry (SamplesPerPixel={s_d}, "
        f"NumberOfFrames={f_d}, Rows={r_d}, Columns={c_d}). No axis of the "
        f"array can carry {s_d} samples per pixel. Correct the array or the "
        f"attributes; they cannot both be right.")


def _resolve_rank1(shape, s_d, f_d, r_d, c_d) -> PixelGeometry:
    """Rank 1 -- a flat buffer, reshaped from the declared descriptors.

    The sidecar loader returns a 1-D array only when the stored metadata is
    too small for the buffer, in which case this is consulting the same
    metadata that just failed. Nothing is gained or lost by trying; the
    fall-through below is the existing behaviour and stays.
    """
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
    """
    def arm_a(evidence):
        return PixelGeometry(shape[0], shape[1], shape[2], 1, evidence)

    def arm_b(evidence):
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

    # Step 4 -- the guess. This is the pre-#186 heuristic, unchanged, and it
    # is reported as a guess so each caller can apply its own policy.
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
    array is equally RGB, YBR_FULL, YBR_FULL_422 or YBR_RCT. The old
    ``if samples >= 3: "RGB"`` is what relabelled every ``YBR_FULL`` instance
    on the way through ``get_pixel_data()``.

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

    Only when the instance is colour **and has not declared one**. Forcing it
    to 0 whenever ``samples >= 3`` -- the old behaviour -- overwrote a
    declared planar-1 value with a claim about a layout nothing had
    converted (#217).

    **One caller: `Instance.set_pixel_data()`.** That is the question this
    answers -- what descriptor should the graph carry when the caller
    supplied none -- and it writes no file, so a value the caller declared
    is theirs to keep.

    It is deliberately **not** the question the exporter asks.
    `_write_pixel_geometry` describes the pixel element it has just
    written, and that element is interleaved whatever `attributes` says,
    so it writes 0 for every colour instance and spells the `samples >= 3`
    gate out itself. Leaving that site on this predicate is what let
    `write_tree` emit interleaved bytes under a `PlanarConfiguration` of 1
    (#210).

    An earlier version of this docstring said `ingest_worker`'s
    normalisation means "on every live path there is nothing here to
    correct". That was incomplete: ingest is not the only way a declared 1
    reaches the graph -- a hand-built instance and a reloaded one both
    carry whatever was set -- and #217's own narrowing is what preserves it.

    Args:
        attributes: The instance's attributes mapping.
        samples (int): The resolved samples per pixel.

    Returns:
        bool: True if the caller should write 0.
    """
    if samples < 3:
        return False
    try:
        return TAG_PLANAR_CONFIGURATION not in attributes
    except Exception:  # pylint: disable=broad-except
        return True

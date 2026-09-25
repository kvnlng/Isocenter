"""OCR over pixel data: find burned-in text and where it sits."""
import numpy as np
from typing import List, Optional, Tuple
from dataclasses import dataclass
import logging
import pydicom
from pydicom.dataset import Dataset
from pydicom.pixels import apply_voi_lut
from isocenter.entities import Instance
from isocenter.logger import describe_exception, get_logger
from isocenter.pixel_geometry import resolve_pixel_geometry

logger = logging.getLogger(__name__)

# Pillow is a hard dependency; pytesseract is the optional `ocr` extra.
# Keep them in separate imports: in one `try`, a missing pytesseract would
# also leave `Image` unbound and disable every Pillow-backed code path.
from PIL import Image

try:
    import pytesseract
    HAS_OCR = True
    #: Why `import pytesseract` failed, or `None` when it did not. Bound on
    #: both arms: `_ocr_unavailable_reason()` reads it on every machine,
    #: including CI's, which has pytesseract.
    _OCR_IMPORT_ERROR = None
except ImportError as _exc:
    # Bind the name anyway, as isocenter/imagecodecs_handler.py does. OCR is
    # a supported optional configuration, not an edge case, and a module
    # attribute that exists only sometimes is a trap for anything that
    # reaches for it -- including `mock.patch`, which raises
    # AttributeError rather than skipping.
    pytesseract = None
    HAS_OCR = False
    _OCR_IMPORT_ERROR = str(_exc)
    # No warning here, deliberately: it would fire on every import and in
    # every spawned worker, where OCR is never used. The methods that need
    # OCR refuse with the reason, this import error included, so the
    # signal lands where it can be acted on.


class OcrUnavailableError(RuntimeError):
    """OCR was asked for and cannot run; nothing was scanned.

    Raised by `Session.scan_pixel_content()` and
    `Session.discover_redaction_zones()` before any worker is dispatched,
    when `pytesseract` does not import or the `tesseract` binary does not
    answer. The frozen promise is `RuntimeError`; this subclass is tier 2,
    and lets a script that treats OCR as optional catch "OCR is missing"
    by name without matching message text, which is not frozen. `HAS_OCR`
    does not answer that question: it does not cover the binary.
    """


class PixelScanError(RuntimeError):
    """An OCR pass could read none of the instances it tried.

    Raised by `Session.scan_pixel_content()` and
    `Session.discover_redaction_zones()` **after** the pass, and after the
    warning that counts the failures, when at least one instance failed
    and none was read. Not on a partial scan: an instance read is a
    result, and the others are in `PhiReport.failures` (or, for discovery,
    which has no failure field, in the log).

    `.failures` is a list of `(entity_uid, reason)`; `.attempted` is how
    many instances the pass dispatched, which is more than the failures
    when some of them carried no pixel element (neither read nor failed).

    The frozen promise is `RuntimeError`; this subclass is tier 2. It is
    **not** an `OcrUnavailableError`, which means "refused before anything
    was scanned", so a script catching `OcrUnavailableError` to treat OCR
    as optional does not catch this one.
    """

    def __init__(self, failures, attempted):
        """Record the failures and build the message.

        Args:
            failures (Iterable[Tuple[str, str]]): `(entity_uid, reason)` per
                instance that could not be read.
            attempted (int): How many instances the pass dispatched.
        """
        self.failures = list(failures)   # [(entity_uid, reason)]
        self.attempted = attempted
        first = self.failures[0] if self.failures else ("UNKNOWN", "unknown")
        super().__init__(
            f"OCR read none of the {attempted} instance(s) it tried; "
            f"{len(self.failures)} could not be read. First: {first[0]}: "
            f"{first[1]}. Every failure is in .failures.")


def _ocr_unavailable_reason() -> Optional[str]:
    """`None` when OCR can run, otherwise why it cannot.

    "Can run" includes the binary: pytesseract must import and the
    `tesseract` binary must answer the version probe. A binary that fails
    the probe, `SystemExit` included, is reported as unusable.

    Returns:
        Optional[str]: None, or the reason OCR cannot run.
    """
    # Reads the module globals at call time, so `patch.object` on this
    # module reaches it; a caller that copied `HAS_OCR` at import would not
    # see the patch, or a later install.
    if not HAS_OCR:
        return f"pytesseract could not be imported ({_OCR_IMPORT_ERROR})"
    try:
        pytesseract.get_tesseract_version()
    # Broad on purpose, and no broader. The probe's only question is
    # "can OCR run", and pytesseract answers a too-old or unparseable
    # binary with `SystemExit`, not an Exception -- letting that through
    # would exit the caller's script from inside a scan. `BaseException`
    # would also swallow KeyboardInterrupt, which is not an answer.
    except (Exception, SystemExit) as exc:  # pylint: disable=broad-exception-caught
        return ("pytesseract is installed but the tesseract binary is "
                f"unusable ({exc})")
    return None


def _require_ocr(operation: str) -> None:
    """Raise `OcrUnavailableError` naming `operation` unless OCR can run.

    Called first thing by both Session methods that need OCR, before they
    read the graph.

    Args:
        operation (str): The method name the message names.

    Raises:
        OcrUnavailableError: If OCR cannot run; the message says why and
            how to install it.
    """
    # Before the graph is read: "this method needs OCR" holds whatever the
    # graph contains, and a scaffolded config would otherwise answer
    # "nothing to scan" without OCR and surface the missing extra later.
    reason = _ocr_unavailable_reason()
    if reason is None:
        return
    raise OcrUnavailableError(
        f"{operation} needs OCR and OCR is unavailable: {reason}. "
        "Nothing was scanned.\n"
        # Quoted: zsh globs unquoted brackets and answers
        # `zsh: no matches found: isocenter[ocr]`.
        'Install the extra with: pip install "isocenter[ocr]"\n'
        "and the tesseract binary: brew install tesseract (macOS) / "
        "apt-get install tesseract-ocr (Debian/Ubuntu).")


@dataclass
class TextRegion:
    """
    Represents a region of text detected within an image or frame.

    Attributes:
        text (str): The detected text string.
        box (Tuple[int, int, int, int]): The bounding box of the text region (x, y, w, h).
        confidence (float): The confidence score of the detection (0-100).
        frame_index (int): The index of the frame where the text was detected (default 0).
    """
    text: str
    box: Tuple[int, int, int, int]  # x, y, w, h
    confidence: float
    frame_index: int = 0


def _get_voi_lut_dataset(instance: Instance) -> Dataset:
    """
    Constructs a minimal pydicom Dataset containing only the tags required for VOI LUT operations.
    """
    ds = Dataset()

    # Critical VOI/Modality LUT tags
    tags_to_copy = [
        "0028,1050", # WindowCenter
        "0028,1051", # WindowWidth
        "0028,1052", # RescaleIntercept
        "0028,1053", # RescaleSlope
        "0028,1054", # RescaleType
        "0028,3010", # VOILUTSequence (If supported by Isocenter attributes, likely complex)
        "0028,1055", # WindowCenterWidthExplanation
    ]

    for tag in tags_to_copy:
        val = instance.attributes.get(tag)
        if val is not None:
            # DicomItem stores tags as "gggg,eeee" strings; set by
            # integer tag.
            group, elem = [int(x, 16) for x in tag.split(',')]
            # `ds.add_new` needs a VR: DS for the window and rescale
            # values, LO for the two text tags.
            try:
                vr = "DS"
                if tag == "0028,1054":
                    vr = "LO" # RescaleType
                if tag == "0028,1055":
                    vr = "LO" # Explanation
                if tag == "0028,3010":
                    continue # The VOI LUT Sequence is not mapped back from the graph

                ds.add_new(pydicom.tag.Tag(group, elem), vr, val)
            except (ValueError, TypeError) as exc:
                # A tag we cannot map back onto a Dataset is skipped, and
                # logged: OCR then runs without a windowing tag that
                # decides contrast.
                get_logger().debug(
                    "Skipping attribute %s while rebuilding dataset: %s",
                    tag, describe_exception(exc))

    return ds

def _detect_text_regions_or_raise(pixel_data: np.ndarray,
                                  frame_idx: int = 0) -> List[TextRegion]:
    """`detect_text_regions` without its catch: an OCR failure raises.

    Assumes OCR is available; `_ocr_instance` checks first.

    Args:
        pixel_data (np.ndarray): One 2D frame, scaled to uint8 if needed.
        frame_idx (int): The frame index recorded on each region.

    Returns:
        List[TextRegion]: Regions with confidence above 0 and non-empty text.
    """
    # The Session path calls this, through `_ocr_instance`, so that a frame
    # whose OCR failed can be told apart from a frame with no text on it.
    # PIL needs uint8. The VOI LUT has set contrast, but the result may
    # still be wider than 8 bits.
    if pixel_data.dtype != np.uint8:
        p_min = pixel_data.min()
        p_max = pixel_data.max()
        if p_max > p_min:
            # Linear scaling to 0-255
            norm = ((pixel_data - p_min) / (p_max - p_min)) * 255.0
            img_data = norm.astype(np.uint8)
        else:
            img_data = np.zeros(pixel_data.shape, dtype=np.uint8)
    else:
        img_data = pixel_data

    img = Image.fromarray(img_data)

    # Use image_to_data for detailed box info
    # config optimized for sparse text
    config = r'--oem 3 --psm 11'

    # Output is a dict with lists
    data = pytesseract.image_to_data(
        img, config=config, output_type=pytesseract.Output.DICT)

    regions = []
    n_boxes = len(data['text'])
    for i in range(n_boxes):
        text = data['text'][i].strip()
        conf = int(data['conf'][i])

        # Filter low confidence and empty text
        if conf > 0 and len(text) > 0:
            (x, y, w, h) = (data['left'][i], data['top'][i], data['width'][i], data['height'][i])
            regions.append(TextRegion(
                text=text,
                box=(x, y, w, h),
                confidence=float(conf),
                frame_index=frame_idx
            ))
    return regions


def detect_text_regions(pixel_data: np.ndarray, frame_idx: int = 0) -> List[TextRegion]:
    """Runs OCR on the provided pixel data and returns text regions with bounding boxes.

    Args:
        pixel_data (np.ndarray): The image data (should be 2D).
        frame_idx (int): The frame index associated with this data.

    Returns:
        List[TextRegion]: Detected text regions. Also `[]` when OCR is
            unavailable, and `[]` when OCR raised (logged at ERROR), so `[]`
            here does not mean "no text". `Session.scan_pixel_content()` and
            `discover_redaction_zones()` check availability first and refuse
            instead, and report the failures this function only logs.
    """
    if not HAS_OCR:
        return []
    try:
        return _detect_text_regions_or_raise(pixel_data, frame_idx=frame_idx)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(f"OCR failed: {describe_exception(e)}")
        return []


def detect_text(pixel_data: np.ndarray) -> str:
    """The text of every region `detect_text_regions` finds, space-joined.

    Args:
        pixel_data (np.ndarray): The image data (2D).

    Returns:
        str: The joined text; empty when OCR is unavailable, failed, or
            found nothing.
    """
    regions = detect_text_regions(pixel_data)
    return " ".join([r.text for r in regions])


@dataclass
class _InstanceOcr:
    """What one instance's OCR pass produced, failures included.

    `read` is True when at least one frame went through OCR without
    raising. `failure` is `None`, or why the instance -- or some of its
    frames -- could not be read. An instance can be both read and failed:
    a frame that failed does not cost the findings of the frames that did
    not. An instance with no pixel element is neither: `read` False and
    `failure` None.
    """
    regions: List[TextRegion]
    read: bool
    failure: Optional[str]


def _frames_for_ocr(instance: Instance, pixel_array: np.ndarray) -> List[np.ndarray]:
    """Window the array and split it into the frames OCR reads."""
    # Apply VOI LUT (Windowing) if metadata exists
    # This converts high-bit DICOM to human-viewable contrast
    try:
        ds_voi = _get_voi_lut_dataset(instance)
        # apply_voi_lut applies the windowing maths for the whole
        # array, including 3D/4D. It uses index=0, so a series whose
        # WindowWidth varies per frame is windowed by its first frame.
        pixel_array = apply_voi_lut(pixel_array, ds_voi)
    except (ValueError, TypeError, AttributeError) as exc:
        # Fall back to raw pixel data when VOI LUT cannot be applied
        # (missing or malformed windowing tags).
        get_logger().debug("VOI LUT application failed: %s", describe_exception(exc))

    # Frames vs. samples is decided from the instance's descriptors,
    # not from the array's last axis: a `shape[-1] in [3, 4]` test would
    # hand a 3-frame 8x3 grayscale image to OCR as one RGB frame, and
    # frames 1 and 2 would never be read. This runs after apply_voi_lut,
    # which preserves shape.
    #
    # `geom.frames` is the array's first axis on every frames-major
    # arm and 1 on the others, never the declared NumberOfFrames, so
    # this range is in bounds by construction rather than by luck.
    geom = resolve_pixel_geometry(pixel_array.shape, instance.attributes)
    if geom.frames > 1:
        return [pixel_array[i] for i in range(geom.frames)]
    return [pixel_array]


def _ocr_instance(instance: Instance) -> _InstanceOcr:
    """OCR every frame of one instance, and say what could not be read.

    A load failure fails the instance; a frame's OCR failure fails that
    frame only, and the frames that succeeded keep their findings. An
    instance nobody could read is reported as a failure, never as a clean
    result. An instance with no pixel element is neither read nor failed.
    A frame this call loaded is released before it returns.

    Args:
        instance (Instance): The instance to read.

    Returns:
        _InstanceOcr: The regions found, whether any frame was read, and
            the failure, if any.
    """
    # The one place an instance's pixels are loaded and read for text; the
    # Session worker and the tier-2 `analyze_pixels` both come through
    # here. Two catches, each narrow in scope and broad in type, because
    # what they guard -- a sidecar read, a file decode, a tesseract
    # subprocess -- can fail in any way, and the question each answers is
    # only "was this read".
    # No pixel element to read is neither read nor failed. Checked from
    # the instance's state, not from `get_pixel_data()`'s messages, which
    # would drift under any rewording there. A hand-built instance with
    # no array, no loader and no file raises `FileNotFoundError` from
    # `get_pixel_data()`; counting that as a failure would make a series
    # holding such an instance report a failure it did not have.
    if (instance.pixel_array is None
            and not instance._pixel_loader  # pylint: disable=protected-access
            and not instance.file_path):
        return _InstanceOcr([], False, None)

    # Free what this pass loaded, and only that. `get_pixel_data()`
    # caches the frame on the instance: under threads -- the
    # free-threaded build's default, and discovery's unless recycling is
    # set -- every scanned frame would otherwise stay resident on the live
    # graph. **The gate is what protects the caller**, not the choice of
    # `unload_pixel_data()` over `discard_pixel_data()`: a frame this pass
    # loaded came through the loader or the file, so it is never an
    # unwritten replacement and the unload refusal cannot fire on it, while a
    # frame that was resident before -- an unsaved replacement, or a
    # written frame the caller loaded and still holds -- is not ours to
    # free. `unload` is the spelling because this is "free it if it is
    # safe", not "throw it away". In a `finally`, so a failure after the
    # load still frees it.
    was_resident = instance.pixel_array is not None
    try:
        return _load_and_ocr(instance)
    finally:
        if not was_resident:
            instance.unload_pixel_data()


def _load_and_ocr(instance: Instance) -> _InstanceOcr:
    """`_ocr_instance`'s body: load, prepare, and OCR each frame."""
    try:
        pixel_array = instance.get_pixel_data()
    except Exception as e:  # pylint: disable=broad-exception-caught
        return _InstanceOcr(
            [], False, f"pixels could not be read: {describe_exception(e)}")
    # An ingested SR carries its source file and no pixel element, and
    # `get_pixel_data()` answers `None` for it: neither read nor failed.
    if pixel_array is None:
        return _InstanceOcr([], False, None)

    # After both pixel-less checks, never before them. An instance with
    # nothing to read had nothing OCR could miss, and only the load can
    # tell an ingested SR from an image; with this check first, a spawned
    # worker that could not import pytesseract would report every
    # pixel-less instance as a failure too. The cost of the order is one
    # load on a route that is failing anyway, and `_ocr_instance`'s
    # `finally` frees it.
    if not HAS_OCR:
        # A failure, not `[]`. The Session methods check availability in
        # the parent before dispatching, so reaching this is a spawned
        # worker that cannot import pytesseract when the caller could. A
        # module flag rather than
        # `_ocr_unavailable_reason()`, which would spawn a tesseract
        # subprocess per instance.
        return _InstanceOcr(
            [], False, f"pytesseract could not be imported ({_OCR_IMPORT_ERROR})")

    try:
        frames = _frames_for_ocr(instance, pixel_array)
    except Exception as e:  # pylint: disable=broad-exception-caught
        return _InstanceOcr(
            [], False,
            f"pixels could not be prepared for OCR: {describe_exception(e)}")

    regions: List[TextRegion] = []
    read = False
    frame_failures = []
    for i, frame in enumerate(frames):
        # Per frame, not around the loop: a try hoisted around it would
        # lose every frame after the first failure.
        try:
            regions.extend(_detect_text_regions_or_raise(frame, frame_idx=i))
            read = True
        except Exception as e:  # pylint: disable=broad-exception-caught
            frame_failures.append(f"OCR failed on frame {i}: {describe_exception(e)}")
    return _InstanceOcr(regions, read, "; ".join(frame_failures) or None)


def analyze_pixels(instance: Instance) -> List[TextRegion]:
    """Analyzes the pixel data of a DICOM Instance for burned-in text.

    A load or OCR failure is logged at ERROR and what was read is
    returned. A frame this call loaded is released before it returns, with
    `unload_pixel_data()`; one that was resident before the call is left as
    it was. A caller who wants the frame afterwards calls
    `instance.get_pixel_data()`.

    Args:
        instance (Instance): The instance to read.

    Returns:
        List[TextRegion]: Raw findings, not filtered against any zone. Also
            `[]` when OCR is unavailable or nothing could be read, so `[]`
            is not "no text"; the Session methods check availability first
            and report what this only logs.
    """
    # Returns a list rather than raising: it runs per instance inside
    # workers, where raising would turn one precondition into N worker
    # failures.
    if not HAS_OCR:
        return []
    result = _ocr_instance(instance)
    if result.failure is not None:
        logger.error(
            f"Failed to analyze pixels for {instance.sop_instance_uid}: {result.failure}")
    return result.regions

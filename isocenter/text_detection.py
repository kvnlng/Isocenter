"""Find burned-in text with a text-detection model (#1121, PR C).

`burned_in_text: {detector: db}` decides "is this text?" with the
PP-OCRv3 English DB detector from opencv_zoo, run by OpenCV's
`cv2.dnn.TextDetectionModel_DB`, instead of tesseract. The detector reads
no text: it returns where text is, with a score, and nothing else. So
nothing it found can be quoted, logged, stored or hashed; only boxes
leave this module.

OpenCV is the optional `textdetect` extra (owner ruling Q-C1 (a)).
It publishes no free-threaded wheel, so on 3.14t the import fails,
`HAS_DETECTOR` is False, and `detector: db` raises
`TextDetectorUnavailableError`. Every function here except
`_detect_boxes` and `_model` is numpy only, so the free-threaded gate
runs all of it with `_detect_boxes` stubbed: that function is **the
seam** the unit tests patch.

**The weights ship with the package** (Q-C2 (a), Q-C3 (a)):
`resources/text_detection_en_ppocrv3_2023may.onnx`, Apache-2.0, with its
provenance in the NOTICE beside it. The model is refused unless its
sha256 is `MODEL_SHA256`, so a file swapped or truncated after install
is "unavailable", never a different detector reading under this one's
name.

**Design 1, measured by the spike** (synthetic MIDI-B Validation,
2026-10-15: 35 of 35 answer-key boxes, 0 of 82 text-free frames with a
box, about 0.21 s per radiograph). The whole frame is read once; a frame
whose long side is over `LONG_SIDE` is read at that long side, a smaller
one at its own size, never upscaled; each side of the model's input is
rounded to a multiple of 32. OpenCV maps the quads back to the frame it
was handed, so they are in frame coordinates when they leave the seam.
Then `MAX_TEXT_BOX_FRACTION` drops a box too large to be burned-in text,
and each kept quad becomes its padded axis-aligned box.
"""
import hashlib
import os
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from isocenter.pixel_analysis import OcrUnavailableError, _padding

try:
    import cv2
    HAS_DETECTOR = True
    #: Why `import cv2` failed, or None. Bound on both arms, as
    #: `pixel_analysis` binds pytesseract's, so a patch never meets an
    #: attribute that exists only sometimes.
    _DETECTOR_IMPORT_ERROR = None
except ImportError as _exc:
    cv2 = None
    HAS_DETECTOR = False
    _DETECTOR_IMPORT_ERROR = str(_exc)

#: The shipped weights and their provenance file, under
#: `isocenter/resources/`. Both are named here so the packaging contract
#: (`test_every_shipped_resource_is_named_by_the_package`) sees a reader.
MODEL_FILE = "text_detection_en_ppocrv3_2023may.onnx"
MODEL_NOTICE = "text_detection_en_ppocrv3_2023may.NOTICE"
#: opencv_zoo's Git LFS object for the fp32 English model (owner ruling
#: Q-C2 (a)); the NOTICE names the same value, and a test holds the two
#: and the file together.
MODEL_SHA256 = "03f550c6b406fda8bf54bd8327815f6c7e2edd98cea02348c93d879254366587"
MODEL_BYTES = 2423490

#: A frame whose long side is over this is read at it (spike, design 1).
#: 736 found 31 of 35; 1280 found 35 of 35. Smaller frames are read at
#: their own size: upscaling the 128-px PT frames is what put boxes on
#: text-free frames in the spike.
LONG_SIDE = 1280

#: A box whose short side is more than this fraction of the frame's short
#: side is not text (owner ruling of 2026-10-15, fixed, no key). The box is
#: the padded straight box clipped to the frame, which is what the spike
#: measured; the quad's own short side is laxer on a tilted quad, and kept
#: a 0.88 box on an IVUS frame the spike had dropped (spike follow-up 3,
#: owner ruling of 2026-10-16). On the
#: spike's set the largest box on real text was 14.8% of the frame's
#: short side, and the false boxes on IVUS ultrasound frames over 40%,
#: scored up to 0.98, so no score threshold removes them; this does, from
#: 10 false frames of 82 to none. At 10% one answer-key box was lost.
#: Text taller than a quarter of a frame is missed; that is documented.
MAX_TEXT_BOX_FRACTION = 0.25

# The model's settings, exactly the spike's (scratchpad spike-1121c,
# scripts/dbspike.py, the run behind every number above). NOT the zoo's
# `ppocr_det.py` values (mean 123.675, 116.28, 103.53 with the scale
# divided by ImageNet's std): the spike measured only these, so change
# them only with a new measurement.
INPUT_SCALE = 1.0 / 255.0
INPUT_MEAN = (122.67891434, 116.66876762, 104.00698793)
BINARY_THRESHOLD = 0.3
UNCLIP_RATIO = 2.0
#: Our limit: a frame on which the detector returns this many regions is a
#: frame failure (`TooManyTextRegions`), so a frame denser than it is
#: withheld, never redacted part-way (owner ruling of 2026-10-16, after
#: the spike's 200 left a 300-word frame part-redacted). 2,000 is not
#: measured on real dense frames; none of the spike's sample comes near
#: 200. Counted over every candidate OpenCV returns, before
#: `min_confidence`.
MAX_TEXT_REGIONS = 2000
#: OpenCV's own limit, handed to `setMaxCandidates`: 0, which OpenCV 5.0.0
#: reads as no limit. Never capped, because OpenCV truncates its contour
#: list to this many *before* it drops contours that are too small or fail
#: to unclip: capped at 2,000, a frame of 4,000 small glyphs returned
#: 1,708 regions (2,199 uncapped), so a count of returned regions could
#: never see that it had been cut (review of #1121 PR C, re-review). Our
#: limit is checked on the full list instead.
OPENCV_MAX_CANDIDATES = 0
#: Pixels of value 0 around each frame before it is read (the frame is
#: 8-bit and min-max scaled, so 0 is its own minimum). Text touching the
#: frame's edge was missed without it (review of #1121 PR C, F4); the
#: spike's follow-up 2 measured 16 px of 0 on its set: 35 of 35 answer-key
#: boxes and 0 of 82 text-free frames as before, at the same time, where a
#: border of the frame's median put a box on an ultrasound frame. The
#: `LONG_SIDE` limit applies to the bordered frame; boxes are mapped back
#: by subtracting it, then clipped to the frame.
DETECTION_BORDER = 16


class TooManyTextRegions(RuntimeError):
    """The detector returned `MAX_TEXT_REGIONS` regions or more from one
    frame: the frame fails, and its image is withheld.

    The message is counts only, and is the reason a row gives for the
    frame (`frame_failure`)."""

    def __init__(self, count: int):
        self.count = count
        self.frame_failure = (f"{count} text regions, the detector's limit "
                              f"of {MAX_TEXT_REGIONS} per frame")
        super().__init__(self.frame_failure)


class TextDetectorUnavailableError(OcrUnavailableError):
    """`burned_in_text: {detector: db}` was asked for and the detector
    cannot run; nothing was read.

    Raised by `Session.redact()` and `export(check_burned_in=True)` before
    any task, UID, row or zone, as `OcrUnavailableError` is: OpenCV did
    not import (the `textdetect` extra is not installed, or the build is
    free-threaded, for which OpenCV publishes no wheel), or the shipped
    model is missing or not the pinned file. Tier 2. A subclass of
    `OcrUnavailableError`, so a script that treats OCR as optional
    catches this by its parent's name.
    """


def model_path() -> str:
    """Where the shipped model sits."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "resources", MODEL_FILE)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _detector_unavailable_reason() -> Optional[str]:
    """None when the detector can run, otherwise why it cannot.

    Reads the module globals at call time, so a patch on this module
    reaches it. "Can run" is: OpenCV imported, it has the DB text
    detection model, and the shipped model is the pinned file.
    """
    if not HAS_DETECTOR:
        return f"OpenCV (cv2) could not be imported ({_DETECTOR_IMPORT_ERROR})"
    if not hasattr(cv2, "dnn_TextDetectionModel_DB"):
        return (f"this OpenCV ({getattr(cv2, '__version__', '?')}) has no "
                "dnn TextDetectionModel_DB")
    path = model_path()
    if not os.path.isfile(path):
        return f"the text detection model is missing ({MODEL_FILE})"
    try:
        found = _file_sha256(path)
    except OSError as exc:
        return f"the text detection model could not be read ({type(exc).__name__})"
    if found != MODEL_SHA256:
        return (f"the text detection model is not the pinned file "
                f"({MODEL_FILE}: sha256 {found[:16]}, expected "
                f"{MODEL_SHA256[:16]})")
    return None


def _require_detector(operation: str) -> None:
    """Raise `TextDetectorUnavailableError` naming `operation` unless the
    detector can run.

    Raises:
        TextDetectorUnavailableError: The message says why and how to
            install the extra, and that the free-threaded build has none.
    """
    reason = _detector_unavailable_reason()
    if reason is None:
        return
    raise TextDetectorUnavailableError(
        f"{operation} under burned_in_text detector: db needs the text "
        f"detector and it is unavailable: {reason}. Nothing was read.\n"
        # Quoted: zsh globs unquoted brackets.
        'Install the extra with: pip install "isocenter[textdetect]"\n'
        "OpenCV publishes no free-threaded (3.14t) wheel, so detector: db "
        "cannot run there; use detector: tesseract.")


def detector_identity() -> dict:
    """What read the boxes: the model's sha256 (16 hex digits) and the
    OpenCV version, as a record holds them.

    In the record, never in the policy hash (owner ruling of 2026-10-15
    for PR B's `names_by`, applied here): plain `export()` trusts a
    current record without OpenCV installed, and only `redact()` reads an
    image again when either changes, since a different build may find
    different boxes.
    """
    return {"model": MODEL_SHA256[:16],
            "opencv": str(getattr(cv2, "__version__", "unavailable"))}


_LOCAL = threading.local()


def _model():
    """This thread's detector, built on first use.

    One per thread, never shared: a `cv2.dnn` network is not safe to call
    from two threads at once, and the free-threaded build (and a
    `:memory:` store) runs the pass in threads. A spawned process has its
    own. `cv2.setNumThreads(1)` is OpenCV's twin of `OMP_THREAD_LIMIT=1`
    for tesseract: several workers each running a thread pool is where a
    box could move between runs.
    """
    model = getattr(_LOCAL, "model", None)
    if model is None:
        cv2.setNumThreads(1)
        model = cv2.dnn_TextDetectionModel_DB(cv2.dnn.readNet(model_path()))
        model.setBinaryThreshold(BINARY_THRESHOLD)
        model.setUnclipRatio(UNCLIP_RATIO)
        model.setMaxCandidates(OPENCV_MAX_CANDIDATES)
        _LOCAL.model = model
    return model


def _detect_boxes(image: np.ndarray, input_size: Tuple[int, int],
                  min_score: float) -> List[Tuple[np.ndarray, float]]:
    """The seam: one DB inference over `image`, the only OpenCV call.

    Args:
        image (np.ndarray): The frame, uint8, three channels, at its own
            size.
        input_size (tuple): `(width, height)` the model reads at;
            OpenCV resizes to it and maps the quads back to `image`.
        min_score (float): The polygon threshold, 0 to 1.

    Returns:
        list: `(quad, score)` per region at or above `min_score`, the quad
        a `(4, 2)` array of `(x, y)` in `image`'s coordinates.
    """
    model = _model()
    model.setPolygonThreshold(float(min_score))
    model.setInputParams(INPUT_SCALE, tuple(input_size), INPUT_MEAN, True)
    quads, scores = model.detect(image)
    return [(np.asarray(quad, dtype=float).reshape(-1, 2), float(score))
            for quad, score in zip(quads, scores)]


def _round32(value: float) -> int:
    """To the nearest multiple of 32, at least 32 (the spike's `r32`)."""
    return max(32, int(round(value / 32)) * 32)


def _input_size(rows: int, cols: int) -> Tuple[int, int]:
    """The model's `(width, height)` for a frame: the frame's own size,
    or its long side brought to `LONG_SIDE` when larger, never upscaled,
    each side then rounded to a multiple of 32."""
    scale = min(1.0, LONG_SIDE / max(rows, cols))
    return _round32(cols * scale), _round32(rows * scale)


def _as_bgr8(frame: np.ndarray) -> np.ndarray:
    """A frame as the detector reads it: one grey channel scaled to uint8
    as tesseract's is (min to max, when it is not uint8 already), then
    repeated into three. A colour frame is first the mean of its
    channels, as the spike read it. No polarity flip."""
    arr = np.asarray(frame)
    if arr.ndim == 3:
        # Rounded back to uint8 when it was, so a uint8 colour frame is
        # not stretched where a uint8 grey one is not.
        mean = arr.mean(axis=-1)
        arr = (np.rint(mean).astype(np.uint8) if arr.dtype == np.uint8
               else mean)
    if arr.dtype != np.uint8:
        arr = arr.astype(float)
        low, high = arr.min(), arr.max()
        if high > low:
            arr = ((arr - low) / (high - low) * 255.0).astype(np.uint8)
        else:
            arr = np.zeros(arr.shape, dtype=np.uint8)
    return np.ascontiguousarray(np.repeat(arr[:, :, None], 3, axis=2))


def _quad_short_side(quad: np.ndarray) -> float:
    """The short side of a detected quad: the shorter of two adjacent
    edges. DB's quads are rotated rectangles, so this is the text's
    height however the line is tilted. numpy, not `cv2.minAreaRect`, so
    it runs with no OpenCV."""
    quad = np.asarray(quad, dtype=float).reshape(-1, 2)
    first = float(np.hypot(*(quad[1] - quad[0])))
    second = float(np.hypot(*(quad[2] - quad[1])))
    return min(first, second)


@dataclass(frozen=True)
class DetectedRegion:
    """One region the detector accepted, before it becomes a box.

    Kept whole for a reader that needs more than the box (PR B crops and
    deskews by the quad). Holds coordinates and a score, never text.

    Attributes:
        frame (int): The frame's index.
        quad (tuple): Four `(x, y)` points in frame coordinates.
        score (float): DB's box score, 0 to 1.
        box (tuple): `(y1, y2, x1, x2)`: the quad's axis-aligned box,
            padded by `_padding(short side)` and clipped to the frame.
    """
    frame: int
    quad: Tuple[Tuple[float, float], ...]
    score: float
    box: Tuple[int, int, int, int]


def _frame_regions(frame: np.ndarray, frame_index: int,
                   min_confidence: float) -> Tuple[List[DetectedRegion], int]:
    """The regions the detector accepts on one frame, and how many boxes
    the box-size rule dropped.

    Args:
        frame (np.ndarray): One frame, rows by columns (by samples).
        frame_index (int): Its index, carried on each region.
        min_confidence (float): The policy's threshold, 0 to 100: the DB
            box score times 100 (owner ruling Q-C5 (a)), inclusive.

    Returns:
        tuple: `(regions, oversized)`.
    """
    image = _as_bgr8(frame)
    rows, cols = image.shape[:2]
    border = DETECTION_BORDER
    bordered = np.pad(image, ((border, border), (border, border), (0, 0)),
                      mode="constant", constant_values=0)
    limit = MAX_TEXT_BOX_FRACTION * min(rows, cols)
    threshold = float(min_confidence) / 100.0
    regions = []
    oversized = 0
    # Every candidate is asked for (polygon threshold 0, and OpenCV
    # uncapped: `OPENCV_MAX_CANDIDATES`) and the policy's threshold applied
    # here, so `MAX_TEXT_REGIONS` counts the whole list, faint candidates
    # included: a frame that dense fails however strong its regions are.
    found = _detect_boxes(bordered, _input_size(*bordered.shape[:2]), 0.0)
    if len(found) >= MAX_TEXT_REGIONS:
        raise TooManyTextRegions(len(found))
    for quad, score in found:
        if score < threshold:
            continue
        quad = np.asarray(quad, dtype=float).reshape(-1, 2) - border
        pad = _padding(int(round(_quad_short_side(quad))))
        x1 = max(0, int(np.floor(quad[:, 0].min())) - pad)
        y1 = max(0, int(np.floor(quad[:, 1].min())) - pad)
        x2 = min(cols, int(np.ceil(quad[:, 0].max())) + pad)
        y2 = min(rows, int(np.ceil(quad[:, 1].max())) + pad)
        if y2 <= y1 or x2 <= x1:
            continue
        # The padded straight box, clipped, against the frame's short
        # side, as the spike measured it; exactly a quarter is kept.
        if min(y2 - y1, x2 - x1) > limit:
            oversized += 1
            continue
        regions.append(DetectedRegion(
            frame_index, tuple((float(x), float(y)) for x, y in quad),
            float(score), (y1, y2, x1, x2)))
    return regions, oversized

import numpy as np
from typing import List, Tuple
from dataclasses import dataclass
import logging
import pydicom
from pydicom.dataset import Dataset
from pydicom.pixels import apply_voi_lut
from isocenter.entities import Instance
from isocenter.logger import get_logger

logger = logging.getLogger(__name__)

# Pillow is a hard dependency; pytesseract is the optional `ocr` extra.
# These were previously imported in one `try`, so a missing pytesseract
# also left `Image` unbound and silently disabled every Pillow-backed code
# path -- an optional package taking a required one down with it.
from PIL import Image

try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    # Bind the name anyway, as isocenter/imagecodecs_handler.py does. OCR is
    # a supported optional configuration, not an edge case, and a module
    # attribute that exists only sometimes is a trap for anything that
    # reaches for it -- including `mock.patch`, which raises
    # AttributeError rather than skipping.
    pytesseract = None
    HAS_OCR = False
    logger.warning(
        "pytesseract not installed. OCR features are disabled; "
        "install with `pip install isocenter[ocr]`.")

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
            # Pydicom expects specific keywords or tags.
            # DicomItem stores tags as "GGGG,EEEE" strings.
            # We can map them to keywords or set by tag.
            # Setting by tag (int) is safer.
            group, elem = [int(x, 16) for x in tag.split(',')]
            # We need to assign valid VRs if possible, or let pydicom infer?
            # Assigning raw value directly to ds[tag] might fail if VR unknown.
            # Easiest way: ds.add_new(tag, VR, value)
            # We assume DS or LO/SH.
            try:
                # Naive assignment, pydicom might complain about VR
                # For WindowCenter/Width (DS), Rescale (DS)
                vr = "DS"
                if tag == "0028,1054":
                    vr = "LO" # RescaleType
                if tag == "0028,1055":
                    vr = "LO" # Explanation
                if tag == "0028,3010":
                    continue # Skip Sequence for now (too complex to map back from dict manually here)

                ds.add_new(pydicom.tag.Tag(group, elem), vr, val)
            except (ValueError, TypeError) as exc:
                # A tag we cannot map back onto a Dataset is skipped, but
                # silently dropping it meant OCR ran against a dataset
                # quietly missing the windowing tags that decide contrast.
                get_logger().debug(
                    "Skipping attribute %s while rebuilding dataset: %s",
                    tag, exc)

    return ds

def detect_text_regions(pixel_data: np.ndarray, frame_idx: int = 0) -> List[TextRegion]:
    """
    Runs OCR on the provided pixel data and returns text regions with bounding boxes.

    Args:
        pixel_data (np.ndarray): The image data (should be 2D).
        frame_idx (int): The frame index associated with this data.

    Returns:
        List[TextRegion]: Detected text regions.
    """
    regions = []
    if not HAS_OCR:
        return regions

    try:
        # Normalize pixel types for PIL if not already uint8
        # Note: VOI LUT should have theoretically handled contrast, but we still need
        # to ensure it fits in 8-bit for OCR.
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
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)

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

    except Exception as e:
        logger.error(f"OCR failed: {e}")

    return regions


def detect_text(pixel_data: np.ndarray) -> str:
    """Legacy wrapper for simple string return."""
    regions = detect_text_regions(pixel_data)
    return " ".join([r.text for r in regions])


def analyze_pixels(instance: Instance) -> List[TextRegion]:
    """
    Analyzes the pixel data of a DICOM Instance for burned-in text.
    Returns list of TextRegion objects (raw findings, not filtered).
    Caller is responsible for filtered results.
    """
    all_regions = []

    if not HAS_OCR:
        return all_regions

    try:
        pixel_array = instance.get_pixel_data()
        if pixel_array is None:
            return all_regions

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
            get_logger().debug("VOI LUT application failed: %s", exc)
            pass

        frames = []
        shape = pixel_array.shape

        # Heuristic to detect frames vs RGB
        # If RGB, shape is (H, W, 3). If MultiFrame, (N, H, W).
        # CAUTION: apply_voi_lut might change shape? No, usually preserves.

        if pixel_array.ndim == 2:
            frames.append(pixel_array)
        elif pixel_array.ndim == 3:
            if pixel_array.shape[-1] in [3, 4]:
                frames.append(pixel_array) # Single RGB frame
            else:
                for i in range(shape[0]):
                    frames.append(pixel_array[i])
        elif pixel_array.ndim == 4:
            # (Frames, Rows, Cols, Samples)
            for i in range(shape[0]):
                frames.append(pixel_array[i])

        for i, frame in enumerate(frames):
            regions = detect_text_regions(frame, frame_idx=i)
            all_regions.extend(regions)

    except Exception as e:
        logger.error(f"Failed to analyze pixels for {instance.sop_instance_uid}: {e}")

    return all_regions

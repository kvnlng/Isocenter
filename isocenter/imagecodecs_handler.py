import sys
import numpy as np
from pydicom.uid import UID
from pydicom.encaps import generate_fragments, generate_frames
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
        print(
            f"[isocenter_imagecodecs_handler] NOT AVAILABLE. Import Error: {IMPORT_ERROR}",
            file=sys.stderr)
        return False
    return True


# UID Constants
JPEGLossless = UID("1.2.840.10008.1.2.4.57")
JPEGLosslessSV1 = UID("1.2.840.10008.1.2.4.70")
JPEG2000Lossless = UID("1.2.840.10008.1.2.4.90")
JPEG2000 = UID("1.2.840.10008.1.2.4.91")
JPEGBaseline = UID("1.2.840.10008.1.2.4.50")
JPEGExtended = UID("1.2.840.10008.1.2.4.51")
JPEGLSLossless = UID("1.2.840.10008.1.2.4.80")
JPEGLSLossy = UID("1.2.840.10008.1.2.4.81")
RLELossless = UID("1.2.840.10008.1.2.5")

HANDLER_NAME = "isocenter_imagecodecs_handler"

DEPENDENCIES = {
    "imagecodecs": ("http://www.lfd.uci.edu/~gohlke/pythonlibs/#imagecodecs", "imagecodecs"),
}

SUPPORTED_TRANSFER_SYNTAXES = [
    JPEGLossless,
    JPEGLosslessSV1,
    JPEG2000Lossless,
    JPEG2000,
    JPEGBaseline,
    JPEGExtended,
    JPEGLSLossless,
    JPEGLSLossy,
    RLELossless
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


def needs_to_convert_to_RGB(ds):
    """
    Determines if the dataset needs RGB conversion.
    Currently returns False as we preserve original photometric interpretation where possible.
    """
    return False


def should_change_PhotometricInterpretation_to_RGB(ds):
    """
    Checks if Photometric Interpretation should be changed to RGB.
    Currently returns False.
    """
    return False


def get_pixel_data(ds):
    """
    Decodes pixel data from an encapsulated dataset using `imagecodecs`.

    Handles multiple transfer syntaxes (JPEG, JPEG2000, JPEG-LS, RLE) and
    encapsulated bitstreams (fragments).

    Args:
        ds (pydicom.Dataset): The dataset containing PixelData.

    Returns:
        np.ndarray: The decoded pixel array.

    Raises:
        RuntimeError: If imagecodecs is missing or decoding fails.
    """
    if not is_available():
        raise RuntimeError("imagecodecs is not available")

    transfer_syntax = ds.file_meta.TransferSyntaxUID
    pixel_bytes = ds.PixelData

    # Handle encapsulated data (fragments)

    try:
        num_frames = getattr(ds, 'NumberOfFrames', 1)

        # Helper to decode a single bitstream
        def decode_frame(bitstream):
            if transfer_syntax in [JPEGLossless, JPEGLosslessSV1]:
                return imagecodecs.ljpeg_decode(bitstream)
            if transfer_syntax in [JPEGBaseline, JPEGExtended]:
                return imagecodecs.jpeg_decode(bitstream)
            if transfer_syntax in [JPEG2000Lossless, JPEG2000]:
                return imagecodecs.jpeg2k_decode(bitstream)
            if transfer_syntax in [JPEGLSLossless, JPEGLSLossy]:
                return imagecodecs.jpegls_decode(bitstream)
            if transfer_syntax == RLELossless:
                return imagecodecs.rle_decode(bitstream, shape=(ds.Rows, ds.Columns))
            raise RuntimeError(f"Unsupported syntax: {transfer_syntax}")

        # Multi-Frame Handling
        if num_frames > 1 and ds.file_meta.TransferSyntaxUID.is_encapsulated:

            # generate_frames handles BOT and fragments logic
            frames = []
            for frame_bitstream in generate_frames(ds.PixelData, number_of_frames=num_frames):
                decoded = decode_frame(frame_bitstream)
                frames.append(decoded)

            return np.array(frames)

        # Single-Frame Handling
        else:
            if ds.file_meta.TransferSyntaxUID.is_encapsulated:
                codestream = b"".join(generate_fragments(pixel_bytes))
            else:
                codestream = pixel_bytes

            return decode_frame(codestream)

    except Exception as e:
        print(
            f"[isocenter_imagecodecs_handler] Decode error for {transfer_syntax}: {e}",
            file=sys.stderr)
        raise RuntimeError(f"imagecodecs failed to decode {transfer_syntax}: {e}") from e

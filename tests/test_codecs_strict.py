
import pytest
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless, JPEGLosslessSV1
import numpy as np
import os
from isocenter import Session as DicomSession
from isocenter.entities import Instance

# Import isocenter to trigger __init__ handler registration
import isocenter

def test_handler_registration():
    """Verifies that the correct handlers are registered in pydicom."""
    handlers = pydicom.config.pixel_data_handlers

    # We expect pylibjpeg to be present now
    handler_names = [h.__name__ if hasattr(h, '__name__') else str(h) for h in handlers]

    # Check for Import Strings or Function Objects
    # pydicom stores them as objects once loaded, or strings if lazy?
    # Actually pydicom.config.pixel_data_handlers is a list of modules usually after config is applied?
    # Isocenter sets them as strings in __init__.py.

    print(f"Registered Handlers: {handlers}")

    # We expect 'isocenter.imagecodecs_handler'
    has_imagecodecs = any("imagecodecs_handler" in str(h) for h in handlers)
    has_pillow = any("pillow_handler" in str(h) for h in handlers)

    assert has_imagecodecs, "isocenter.imagecodecs_handler should be registered"
    assert has_pillow, "pillow_handler should be registered"

def test_jpeg_lossless_handling_mock():
    """
    Verifies that the imagecodecs handler supports JPEG Lossless.
    """
    from isocenter import imagecodecs_handler

    assert imagecodecs_handler.is_available(), "imagecodecs should be installed and available"

    supports = imagecodecs_handler.supports_transfer_syntax(JPEGLosslessSV1)
    assert supports, "imagecodecs handler should support JPEG Lossless (1.2.840.10008.1.2.4.70)"

def test_strict_export_failure():
    """
    Verifies that export fails if decompression fails (Strict Safety).
    """
    # Create a corrupted/empty file pretending to be JPEG Lossless
    filename = "corrupt_jpl.dcm"

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = JPEGLosslessSV1
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = "9.9.9.9"
    ds.PatientName = "Test^Strict"
    ds.PatientID = "STRICT_01"

    # Invalid Pixel Data (too short, corrupted)
    # Must encapsulate for compressed syntax to allow dcmwrite to save it
    from pydicom.encaps import encapsulate
    ds.PixelData = encapsulate([b"\x00" * 10])
    ds.Rows = 512
    ds.Columns = 512
    ds.BitsAllocated = 16
    ds.SamplesPerPixel = 1
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    ds.save_as(filename, write_like_original=False)

    try:
        # Create minimal Isocenter session
        import isocenter.io_handlers
        from isocenter.entities import Instance, Patient, Study, Series

        # Manually create the graph to skip ingestion overhead
        inst = Instance("9.9.9.9", "1.2.840.10008.5.1.4.1.1.2", 1, file_path=os.path.abspath(filename))
        se = Series("1.1.1", "CT", 1, instances=[inst])
        st = Study("2.2.2", "20230101", series=[se])
        p = Patient("STRICT_01", "Test^Strict", studies=[st])

        # Try to Export
        # Should raise RuntimeError because get_pixel_data will fail to decompress,
        # and we Removed Raw Export.

        from isocenter.io_handlers import DicomExporter

        with pytest.raises(RuntimeError) as excinfo:
            DicomExporter.save_patient(p, "export_strict_test")

        # Verify it wasn't the "Raw read failed" log but a hard error
        # "Export failed for ... Failed to decompress" or "Export incomplete..."
        msg = str(excinfo.value)
        assert "Export failed" in msg or "Export incomplete" in msg, f"Unexpected error message: {msg}"
        # assert "decompress" in str(excinfo.value).lower()
        # The exact inner error might vary (pylibjpeg might raise RuntimeError or ValueError)

    finally:
        if os.path.exists(filename):
            os.remove(filename)
        import shutil
        if os.path.exists("export_strict_test"):
            shutil.rmtree("export_strict_test")

if __name__ == "__main__":
    # verification run
    test_handler_registration()
    test_jpeg_lossless_handling_mock()
    test_strict_export_failure()
    print("All strict codec tests passed.")

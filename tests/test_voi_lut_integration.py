import unittest
import numpy as np
import logging
from isocenter.entities import Instance
from isocenter.pixel_analysis import analyze_pixels, _get_voi_lut_dataset

# Mock pydicom.pixel_data_handlers.util.apply_voi_lut if needed for detailed checks,
# but better to let it run if installed to verify integration.

class TestVoiLutIntegration(unittest.TestCase):
    def setUp(self):
        logging.basicConfig(level=logging.DEBUG)

    def test_get_voi_lut_dataset(self):
        """Verify helper extracts tags correctly."""
        inst = Instance(sop_instance_uid="1.2.3", sop_class_uid="1.2.3.4")
        inst.set_attr("0028,1050", "40") # Window Center
        inst.set_attr("0028,1051", "400") # Window Width

        ds = _get_voi_lut_dataset(inst)

        # Check if tags are present in Dataset
        # WindowCenter
        wc = ds.get(0x00281050)
        self.assertIsNotNone(wc)
        self.assertEqual(wc.value, "40")

        # Check exclusion of missing tags
        self.assertIsNone(ds.get(0x00281052)) # RescaleIntercept

    def test_analyze_pixels_runs_with_voi(self):
        """Verify analyze_pixels runs (no crash) with VOI tags present."""
        # Create synthetic pixel data (100x100)
        # Gradient from 0 to 1000
        arr = np.linspace(0, 1000, 100*100).reshape(100, 100).astype(np.uint16)

        inst = Instance(sop_instance_uid="1.2.3", sop_class_uid="1.2.3.4")
        inst.pixel_array = arr

        # Set Windowing that selects a range
        # Center=500, Width=200 => range [400, 600] map to output (usually 8-bit or 16-bit)
        inst.set_attr("0028,1050", "500")
        inst.set_attr("0028,1051", "200")

        # Mocking apply_voi_lut is hard because it's imported inside the module scope if we used 'from ... import ...'
        # But we can check if it runs without raising exception.

        # Note: detect_text_regions might fail if Tesseract is missing, but analyze_pixels handles exceptions?
        # Step 220 code catches Exception in detect_text_regions (logger.error) but analyze_pixels ALSO has try/except.
        # Ideally we want to see if it calls apply_voi_lut.

        # We can spy on pydicom.pixel_data_handlers.util.apply_voi_lut if we patch it.
        from unittest.mock import patch

        with patch('isocenter.pixel_analysis.apply_voi_lut', side_effect=lambda arr, ds: arr) as mock_voi, \
             patch('isocenter.pixel_analysis.HAS_OCR', True), \
             patch('isocenter.pixel_analysis.detect_text_regions', return_value=[]):

             analyze_pixels(inst)
             # Check if called
             self.assertTrue(mock_voi.called)
             args, _ = mock_voi.call_args
             # Check 2nd arg is a Dataset with our tags
             self.assertEqual(args[1].WindowCenter, "500")

if __name__ == '__main__':
    unittest.main()

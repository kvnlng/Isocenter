import unittest
from unittest.mock import patch, MagicMock
import numpy as np
from isocenter.entities import Instance
from isocenter import pixel_analysis

class TestPixelAnalysis(unittest.TestCase):

    @patch('isocenter.pixel_analysis.HAS_OCR', True)
    @patch('isocenter.pixel_analysis.pytesseract')
    def test_detect_text_simple(self, mock_pytesseract):
        # Setup: Mock image_to_data returning a DICT
        # Keys: level, page_num, block_num, par_num, line_num, word_num, left, top, width, height, conf, text
        mock_data = {
            'text': ['DETECTED', 'TEXT'],
            'conf': [90, 90],
            'left': [0, 50],
            'top': [0, 0],
            'width': [40, 40],
            'height': [10, 10]
        }
        mock_pytesseract.image_to_data.return_value = mock_data

        # Test 2D Array
        pixel_data = np.zeros((100, 100), dtype=np.uint8)

        result = pixel_analysis.detect_text(pixel_data)

        self.assertEqual(result, "DETECTED TEXT")
        mock_pytesseract.image_to_data.assert_called_once()

    @patch('isocenter.pixel_analysis.HAS_OCR', True)
    @patch('isocenter.pixel_analysis.pytesseract')
    def test_analyze_pixels_integration(self, mock_pytesseract):
        # Setup
        mock_data = {
            'text': ['SECRET'],
            'conf': [95],
            'left': [10],
            'top': [10],
            'width': [50],
            'height': [20]
        }
        mock_pytesseract.image_to_data.return_value = mock_data

        instance = MagicMock(spec=Instance)
        instance.sop_instance_uid = "1.2.3.4"
        # Return a fake image
        instance.get_pixel_data.return_value = np.zeros((100, 100, 3), dtype=np.uint8)

        # Run
        findings = pixel_analysis.analyze_pixels(instance)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].text, "SECRET")
        # TextRegion doesn't have entity_uid, that's added later when creating PhiFinding
        # self.assertEqual(findings[0].entity_uid, "1.2.3.4")

    @patch('isocenter.pixel_analysis.HAS_OCR', False)
    def test_graceful_degradation(self):
        instance = MagicMock(spec=Instance)
        instance.get_pixel_data.return_value = np.zeros((100, 100), dtype=np.uint8)

        findings = pixel_analysis.analyze_pixels(instance)
        self.assertEqual(len(findings), 0)

if __name__ == '__main__':
    unittest.main()

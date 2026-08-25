import unittest
import numpy as np
from unittest.mock import MagicMock, patch
from isocenter.verification import RedactionVerifier, TextRegion
from isocenter.entities import Instance, Equipment
from isocenter.privacy import PhiFinding

class TestOCRFormal(unittest.TestCase):
    """
    Formal integration tests for OCR Verification Logic, including
    Advanced Reporting (Partial Leaks) and Metadata population.
    """

    def setUp(self):
        # Base setup for instances
        self.instance = MagicMock(spec=Instance)
        self.instance.sop_instance_uid = "1.2.3"
        self.instance.equipment = Equipment("TestMan", "TestModel", "SN-001")

        # Define a rule that covers [0,0,100,100]
        self.rules = [{
            "serial_number": "SN-001",
            "redaction_zones": [[0, 0, 100, 100]]
        }]

        self.verifier = RedactionVerifier(self.rules)

    @patch('isocenter.verification.analyze_pixels')
    def test_full_safety(self, mock_ocr):
        """Test that fully covered text generates NO finding."""
        # Text in 10,10,50,50 (Inside 0,0,100,100)
        mock_ocr.return_value = [
            TextRegion("SafeText", (10, 10, 50, 50), 90.0)
        ]

        findings = self.verifier.verify_instance(self.instance, self.instance.equipment)
        self.assertEqual(len(findings), 0)

    @patch('isocenter.verification.analyze_pixels')
    def test_new_leak(self, mock_ocr):
        """Test that text OUTSIDE zone generates NEW_LEAK."""
        # Text in 200,200,50,50 (Outside)
        mock_ocr.return_value = [
            TextRegion("LeakText", (200, 200, 50, 50), 90.0)
        ]

        findings = self.verifier.verify_instance(self.instance, self.instance.equipment)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.metadata.get("leak_type"), "NEW_LEAK")
        self.assertEqual(f.metadata.get("coverage_score"), 0.0)

    @patch('isocenter.verification.analyze_pixels')
    def test_partial_leak(self, mock_ocr):
        """Test that text PARTIALLY covered generates PARTIAL_LEAK."""
        # Zone is 0,0,100,100
        # Text is at 50,0,100,100 (Right half out)
        # Intersect: 50,0,50,100 (Area 5000)
        # Text Area: 100*100 = 10000
        # Coverage: 0.5

        mock_ocr.return_value = [
            TextRegion("PartialText", (50, 0, 100, 100), 90.0)
        ]

        findings = self.verifier.verify_instance(self.instance, self.instance.equipment)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.metadata.get("leak_type"), "PARTIAL_LEAK")
        self.assertAlmostEqual(f.metadata.get("coverage_score"), 0.5)
        self.assertEqual(f.metadata.get("best_zone"), [0, 0, 100, 100])

if __name__ == '__main__':
    unittest.main()

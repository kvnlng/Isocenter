import unittest
from unittest.mock import MagicMock, patch
from isocenter.verification import RedactionVerifier
from isocenter.pixel_analysis import TextRegion
from isocenter.entities import Instance, Equipment

class TestRedactionVerifier(unittest.TestCase):

    def test_get_matching_rule(self):
        rules = [
            {"serial_number": "123", "model": "A"},
            {"serial_number": "456", "model": "B"}
        ]
        verifier = RedactionVerifier(rules)

        # Match
        inst = MagicMock(spec=Instance)
        inst.equipment = Equipment(manufacturer="Man", model_name="A", device_serial_number="123")
        self.assertEqual(verifier.get_matching_rule(inst.equipment), rules[0])

        # No Match
        inst.equipment = Equipment(manufacturer="Man", model_name="C", device_serial_number="789")
        self.assertIsNone(verifier.get_matching_rule(inst.equipment))

    def test_is_covered(self):
        verifier = RedactionVerifier()

        # Text: 10, 10, 100, 20 (Area 2000)
        text_box = (10, 10, 100, 20)

        # 1. Fully Covered
        zone = (0, 0, 200, 200)
        self.assertTrue(verifier.is_covered(text_box, zone))

        # 2. No Overlap
        zone = (200, 200, 50, 50)
        self.assertFalse(verifier.is_covered(text_box, zone))

        # 3. Partial Overlap (Half) -> 50, 10, 50, 20 covers right half (1000 area) -> 0.5
        zone = (60, 0, 100, 100)
        # Default threshold is 0.50 (inclusive)
        self.assertTrue(verifier.is_covered(text_box, zone))

        # 4. Tiny Overlap
        zone = (105, 10, 10, 10)
        self.assertFalse(verifier.is_covered(text_box, zone))

    @patch('isocenter.verification.analyze_pixels')
    def test_verify_instance_filtering(self, mock_analyze):
        # Setup Rules
        rules = [{
            "serial_number": "S1",
            "redaction_zones": [
                [0, 0, 100, 100] # Top Left Zone
            ]
        }]
        verifier = RedactionVerifier(rules)

        # Setup Instance
        inst = MagicMock(spec=Instance)
        inst.sop_instance_uid = "UID1"
        inst.equipment = Equipment(manufacturer="M", model_name="Mod", device_serial_number="S1")

        # Setup OCR Findings
        # Region 1: Inside Zone (Safe)
        r1 = TextRegion(text="Safe", box=(10, 10, 20, 20), confidence=90)
        # Region 2: Outside Zone (Leak)
        r2 = TextRegion(text="Leak", box=(200, 200, 50, 50), confidence=90)

        mock_analyze.return_value = [r1, r2]

        # Execute
        findings = verifier.verify_instance(inst, inst.equipment)

        # Check
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].value, "Leak")
        self.assertEqual(findings[0].reason, "New Leak (Uncovered) (Cov: 0.00)")

if __name__ == '__main__':
    unittest.main()

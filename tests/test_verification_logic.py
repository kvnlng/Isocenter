import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from isocenter.verification import RedactionVerifier
from isocenter.pixel_analysis import TextRegion
from isocenter.pixel_geometry import resolve_pixel_geometry
from isocenter.services import RedactionService
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
        # Zones are zone space (y1, y2, x1, x2), the order every consumer
        # of redaction_zones reads; text boxes stay OCR (x, y, w, h). The
        # old fixtures encoded (x, y, w, h) zones and are re-derived (#264).
        verifier = RedactionVerifier()

        # Text: x 10..110, y 10..30 (Area 2000)
        text_box = (10, 10, 100, 20)

        # 1. Fully Covered: rows 0..200, cols 0..200
        zone = (0, 200, 0, 200)
        self.assertTrue(verifier.is_covered(text_box, zone))

        # 2. No Overlap: rows 200..250, cols 200..250
        zone = (200, 250, 200, 250)
        self.assertFalse(verifier.is_covered(text_box, zone))

        # 3. Partial Overlap (Half): rows 0..100, cols 60..160 covers the
        # text's right half -- cols 60..110, rows 10..30 (1000 area) -> 0.5
        zone = (0, 100, 60, 160)
        # Default threshold is 0.50 (inclusive)
        self.assertTrue(verifier.is_covered(text_box, zone))

        # 4. Tiny Overlap: rows 10..20, cols 105..115 -> 5x10 = 50 area -> 0.025
        zone = (10, 20, 105, 115)
        self.assertFalse(verifier.is_covered(text_box, zone))

    @patch('isocenter.verification.analyze_pixels')
    def test_verify_instance_filtering(self, mock_analyze):
        # Setup Rules
        rules = [{
            "serial_number": "S1",
            "redaction_zones": [
                [0, 100, 0, 100] # Top-left zone: rows 0..100, cols 0..100
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

class TestVerifierReadsZoneSpace(unittest.TestCase):
    """The classifier must read zones the way redaction applies them.

    `redaction_zones` is zone space -- (y1, y2, x1, x2) -- for every
    consumer that touches pixels: `apply_redaction_to_array`, both redact
    paths, and the export worker (#258). verification.py read the same
    zones as (x, y, w, h), so PARTIAL_LEAK/NEW_LEAK classification
    misread every zone: covered text classified as a leak (feeding
    automation false suggestions it converts into real zones), and
    uncovered text classified as covered -- a verification pass attesting
    coverage the pixels don't have (#264).
    """

    def _verifier(self, zone):
        rules = [{
            "serial_number": "SN-ZS",
            "redaction_zones": [list(zone)]
        }]
        return RedactionVerifier(rules)

    def _instance(self):
        inst = MagicMock(spec=Instance)
        inst.sop_instance_uid = "1.2.3.4"
        inst.equipment = Equipment("Man", "Mod", "SN-ZS")
        return inst

    @patch('isocenter.verification.analyze_pixels')
    def test_covered_text_is_not_a_leak(self, mock_ocr):
        # Zone space [100, 160, 10, 90]: rows 100..160, cols 10..90.
        # Text box (x=20, y=110, w=50, h=30): rows 110..140, cols 20..70,
        # entirely inside the zone. Read as (x, y, w, h) the same zone is
        # x 100..110, y 160..250 -- no overlap at all -- so the misread
        # turns fully covered text into a NEW_LEAK.
        zone = [100, 160, 10, 90]

        # Prove coverage against the real consumer first: redacting with
        # this zone zeroes exactly the pixels the text box describes.
        arr = np.ones((200, 200), dtype=np.uint16)
        geom = resolve_pixel_geometry(arr.shape, {})
        RedactionService.apply_redaction_to_array(arr, [zone], geometry=geom)
        self.assertTrue((arr[110:140, 20:70] == 0).all(),
                        "fixture is wrong: the zone does not cover the text")

        mock_ocr.return_value = [
            TextRegion("CoveredText", (20, 110, 50, 30), 90.0)
        ]
        inst = self._instance()
        findings = self._verifier(zone).verify_instance(inst, inst.equipment)
        self.assertEqual(
            findings, [],
            "text the zone provably redacts was classified as a leak")

    @patch('isocenter.verification.analyze_pixels')
    def test_uncovered_text_is_a_new_leak(self, mock_ocr):
        # Zone space [0, 30, 120, 200]: rows 0..30, cols 120..200.
        # Text box (x=10, y=40, w=40, h=40): rows 40..80, cols 10..50 --
        # nowhere near the zone. Read as (x, y, w, h) the same zone is
        # x 0..120, y 30..230, which contains the text completely, so the
        # misread attested coverage the pixels don't have.
        zone = [0, 30, 120, 200]

        # Prove non-coverage against the real consumer: redacting with
        # this zone leaves every text pixel untouched.
        arr = np.ones((200, 200), dtype=np.uint16)
        geom = resolve_pixel_geometry(arr.shape, {})
        RedactionService.apply_redaction_to_array(arr, [zone], geometry=geom)
        self.assertTrue((arr[40:80, 10:50] == 1).all(),
                        "fixture is wrong: the zone touches the text")

        mock_ocr.return_value = [
            TextRegion("LeakedText", (10, 40, 40, 40), 90.0)
        ]
        inst = self._instance()
        findings = self._verifier(zone).verify_instance(inst, inst.equipment)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].metadata.get("leak_type"), "NEW_LEAK")
        self.assertEqual(findings[0].metadata.get("coverage_score"), 0.0)


if __name__ == '__main__':
    unittest.main()

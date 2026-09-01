import unittest
from unittest.mock import MagicMock

import numpy as np

from isocenter.session import DicomSession
from isocenter.privacy import PhiReport, PhiFinding
from isocenter.configuration import IsocenterConfiguration
from isocenter.automation import ConfigAutomator
from isocenter.pixel_geometry import resolve_pixel_geometry
from isocenter.services import RedactionService


def _finding(metadata):
    f = PhiFinding(
        entity_uid="uid",
        entity_type="Instance",
        field_name="field",
        value="val",
        reason="reason"
    )
    f.metadata = metadata
    return f


def _session_with_rules(rules):
    session = MagicMock(spec=DicomSession)
    session.configuration = IsocenterConfiguration()
    session.configuration.rules = rules
    return session


def _redact(zones, shape=(200, 200)):
    """Run zones through the real consumer, exactly as redaction does.

    `apply_redaction_to_array` reads each zone as (y1, y2, x1, x2); this is
    the convention every consumer of `redaction_zones` speaks (#258).
    """
    arr = np.ones(shape, dtype=np.uint16)
    geom = resolve_pixel_geometry(arr.shape, {})
    RedactionService.apply_redaction_to_array(arr, zones, geometry=geom)
    return arr


class TestAutomationIntegration(unittest.TestCase):

    def test_auto_remediate(self):
        # Initial rule: rows 0-50, cols 0-50 in zone space (y1, y2, x1, x2).
        best_zone = [0, 50, 0, 50]
        session = _session_with_rules([{
            "serial_number": "SN-AUTO",
            "redaction_zones": [list(best_zone)]
        }])

        # OCR text box is (x, y, w, h) = (25, 25, 100, 100):
        # rows 25..125, cols 25..125 -> zone space [25, 125, 25, 125].
        # Union with [0, 50, 0, 50] -> [0, 125, 0, 125].
        f = _finding({
            "leak_type": "PARTIAL_LEAK",
            "text_box": (25, 25, 100, 100),
            "best_zone": best_zone,
            "rule_serial": "SN-AUTO"
        })
        report = PhiReport([f])

        suggestions = ConfigAutomator.suggest_config_updates(report, session.configuration)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]['action'], "EXPAND_ZONE")
        self.assertEqual(suggestions[0]['new_zone'], [0, 125, 0, 125])

        # Apply
        count = ConfigAutomator.apply_suggestions(session, suggestions)
        self.assertEqual(count, 1)
        self.assertEqual(session.configuration.rules[0]['redaction_zones'][0], [0, 125, 0, 125])


class TestAutomationZoneConvention(unittest.TestCase):
    """A zone automation writes must land on the pixels the OCR box described.

    Zone discovery converts OCR (x, y, w, h) boxes to (y1, y2, x1, x2) before
    anything reaches configuration (discovery.py). automation.py appended raw
    boxes, so an applied suggestion redacted rows x:y and columns w:h -- the
    wrong region, or nothing at all -- while the leak stayed in the image (#258).
    These tests push the suggestion through the real consumer end to end.
    """

    def test_add_zone_lands_on_the_ocr_box_pixels(self):
        # OCR found text at x=10, y=30, w=40, h=50: rows 30..80, cols 10..50.
        # Read as (y1, y2, x1, x2), the raw box selects rows 10..30, cols
        # 40..50 instead -- a valid rectangle, just not where the text is.
        session = _session_with_rules([{
            "serial_number": "SN-AUTO",
            "redaction_zones": []
        }])
        report = PhiReport([_finding({
            "leak_type": "NEW_LEAK",
            "text_box": (10, 30, 40, 50),
            "rule_serial": "SN-AUTO"
        })])

        suggestions = ConfigAutomator.suggest_config_updates(report, session.configuration)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]['action'], "ADD_ZONE")

        applied = ConfigAutomator.apply_suggestions(session, suggestions)
        self.assertEqual(applied, 1)

        zones = session.configuration.rules[0]['redaction_zones']
        arr = _redact(zones)
        self.assertTrue(
            (arr[30:80, 10:50] == 0).all(),
            "the pixels the OCR box described were not redacted")

    def test_expand_zone_covers_text_and_original_zone(self):
        # Config zone covers rows 0..20, cols 0..60. Text at x=40, y=10,
        # w=40, h=20 spans rows 10..30, cols 40..80 -- partially outside.
        best_zone = [0, 20, 0, 60]
        session = _session_with_rules([{
            "serial_number": "SN-AUTO",
            "redaction_zones": [list(best_zone)]
        }])
        report = PhiReport([_finding({
            "leak_type": "PARTIAL_LEAK",
            "text_box": (40, 10, 40, 20),
            "best_zone": best_zone,
            "rule_serial": "SN-AUTO"
        })])

        suggestions = ConfigAutomator.suggest_config_updates(report, session.configuration)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]['action'], "EXPAND_ZONE")

        applied = ConfigAutomator.apply_suggestions(session, suggestions)
        self.assertEqual(applied, 1)

        zones = session.configuration.rules[0]['redaction_zones']
        arr = _redact(zones)
        self.assertTrue(
            (arr[10:30, 40:80] == 0).all(),
            "the leaked text region was not redacted")
        self.assertTrue(
            (arr[0:20, 0:60] == 0).all(),
            "the expanded zone no longer covers the original zone")


if __name__ == '__main__':
    unittest.main()

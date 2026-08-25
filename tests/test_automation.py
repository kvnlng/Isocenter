import unittest
from unittest.mock import MagicMock
from isocenter.session import DicomSession
from isocenter.privacy import PhiReport, PhiFinding
from isocenter.configuration import IsocenterConfiguration

class TestAutomationIntegration(unittest.TestCase):

    def test_auto_remediate(self):
        # Setup Session Mock
        session = MagicMock(spec=DicomSession)
        session.configuration = IsocenterConfiguration()
        # Initial Rule: Small box
        session.configuration.rules = [{
            "serial_number": "SN-AUTO",
            "redaction_zones": [[0, 0, 50, 50]]
        }]

        # Setup Report with PARTIAL_LEAK
        # Text is at 25, 25, 100, 100
        # (Union with 0,0,50,50 -> 0,0,125,125)
        # Wait: union logic: min(0,25)=0, min(0,25)=0, max(50,125)=125, max(50,125)=125
        # Width: 125-0 = 125, Height: 125-0 = 125

        f = PhiFinding(
            entity_uid="uid",
            entity_type="Instance",
            field_name="field",
            value="val",
            reason="reason"
        )
        f.metadata = {
            "leak_type": "PARTIAL_LEAK",
            "text_box": (25, 25, 100, 100),
            "best_zone": [0, 0, 50, 50],
            "rule_serial": "SN-AUTO"
        }
        report = PhiReport([f])

        # Test remediation logic directly via Automator (since Session method is just valid wrapper)
        from isocenter.automation import ConfigAutomator

        suggestions = ConfigAutomator.suggest_config_updates(report, session.configuration)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]['action'], "EXPAND_ZONE")
        self.assertEqual(suggestions[0]['new_zone'], [0, 0, 125, 125])

        # Apply
        count = ConfigAutomator.apply_suggestions(session, suggestions)
        self.assertEqual(count, 1)
        self.assertEqual(session.configuration.rules[0]['redaction_zones'][0], [0, 0, 125, 125])

if __name__ == '__main__':
    unittest.main()

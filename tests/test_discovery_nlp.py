import unittest
from unittest.mock import MagicMock, patch
from isocenter.discovery import ZoneDiscoverer

class TestZoneDiscovererNLP(unittest.TestCase):

    @patch('isocenter.discovery.ZoneDiscoverer._nlp_model', None)
    @patch('isocenter.discovery.ZoneDiscoverer._nlp_model_failed', True)
    def test_classify_text_regex(self):
        # 1. Name Pattern
        self.assertEqual(ZoneDiscoverer._classify_text("Smith^John"), "NAME_PATTERN")
        self.assertEqual(ZoneDiscoverer._classify_text("Doe^Jane"), "NAME_PATTERN")
        self.assertEqual(ZoneDiscoverer._classify_text("Not^Name but Has^Caret"), "NAME_PATTERN")

        # 2. Heuristic (Capitalized Words)
        self.assertEqual(ZoneDiscoverer._classify_text("Hospital A"), "PROPER_NOUN_CANDIDATE")
        self.assertEqual(ZoneDiscoverer._classify_text("Dr. Smith"), "PROPER_NOUN_CANDIDATE")

        # 3. Text
        self.assertEqual(ZoneDiscoverer._classify_text("kvp: 120"), "TEXT") # Lowercase
        self.assertEqual(ZoneDiscoverer._classify_text("12345"), "TEXT")

    @patch('isocenter.discovery.ZoneDiscoverer._nlp_model')
    @patch('isocenter.discovery.ZoneDiscoverer._nlp_model_failed', False)
    def test_classify_text_nlp(self, mock_model):
        # Setup Mock NLP
        # doc.ents = [ent]
        # ent.label_ = "PERSON"

        mock_doc = MagicMock()
        mock_ent = MagicMock()
        mock_ent.label_ = "PERSON"
        mock_doc.ents = [mock_ent]

        # Make the model callable returning the doc
        mock_model.return_value = mock_doc

        # This text would normall be "TEXT" or "CANDIDATE" but NLP says PERSON
        text = "John Smith"
        result = ZoneDiscoverer._classify_text(text)

        self.assertEqual(result, "PROPER_NOUN")
        mock_model.assert_called_with("John Smith")

    def test_group_boxes_asymmetric(self):
        # Box 1: [0, 0, 10, 10] (Right x=10)
        # Box 2: [50, 0, 10, 10] (Left x=50). Gap = 40.

        boxes = [
            [0, 0, 10, 10],
            [50, 0, 10, 10]
        ]

        # 1. Standard padding (20) -> Gap 40 > 20 -> No Merge
        merged_std = ZoneDiscoverer.group_boxes(boxes, padding=20)
        self.assertEqual(len(merged_std), 2)

        # 2. Asymmetric padding (pad_x=100) -> Gap 40 < 100 -> Merge
        merged_asy = ZoneDiscoverer.group_boxes(boxes, pad_x=100, pad_y=10)
        self.assertEqual(len(merged_asy), 1)

if __name__ == '__main__':
    unittest.main()

import unittest
from isocenter.discovery import ZoneDiscoverer

class TestZoneDiscoverer(unittest.TestCase):

    def test_overlap_logic(self):
        # [x, y, w, h]
        b1 = [0, 0, 10, 10]

        # Overlap
        b2 = [5, 5, 10, 10] # Starts inside b1
        self.assertTrue(ZoneDiscoverer._boxes_overlap(b1, b2, 0, 0), "Boxes should overlap")

        # No overlap (Right)
        b3 = [20, 0, 10, 10]
        self.assertFalse(ZoneDiscoverer._boxes_overlap(b1, b3, 0, 0), "Boxes should NOT overlap")

        # Touching (Should be false or true depending on strictness? Code says strict inequality)
        # r1 < l2: 10 < 10 is False.
        # r1 is b1[0]+b1[2] = 10.
        # l2 is 10.
        # 10 < 10 False.
        # Logic is `not (r1 < l2 ...)`
        # If they touch, `r1 == l2`, so `r1 < l2` is MATCH False.
        # So they overlap.
        b4 = [10, 0, 10, 10]
        self.assertTrue(ZoneDiscoverer._boxes_overlap(b1, b4, 0, 0), "Touching boxes should overlap (merge edge)")

    def test_merge_simple(self):
        # Two overlapping boxes
        boxes = [
            [0, 0, 10, 10],
            [5, 5, 10, 10]  # Ends at 15, 15
        ]
        merged = ZoneDiscoverer._merge_overlapping_boxes(boxes)
        self.assertEqual(len(merged), 1)
        # Union: 0,0 to 15,15 -> [0, 0, 15, 15]
        self.assertEqual(merged[0], [0, 0, 15, 15])

    def test_merge_transitive(self):
        # A overlaps B, B overlaps C, A does NOT overlap C directly
        # A: [0,0,10,10]
        # B: [5,0,10,10] (extends to 15)
        # C: [14,0,10,10] (Starts at 14, overlaps B which ends at 15)

        boxes = [
            [0, 0, 10, 10],
            [5, 0, 10, 10],
            [14, 0, 10, 10]
        ]

        merged = ZoneDiscoverer._merge_overlapping_boxes(boxes)
        self.assertEqual(len(merged), 1, "Should merge transitively into one")
        # Union: 0 to 24 (14+10)
        self.assertEqual(merged[0], [0, 0, 24, 10])

    def test_merge_disjoint(self):
        # Two separate groups
        boxes = [
            [0, 0, 10, 10],
            [5, 0, 10, 10],

            [100, 100, 10, 10],
            [105, 105, 10, 10]
        ]

        merged = ZoneDiscoverer._merge_overlapping_boxes(boxes)
        self.assertEqual(len(merged), 2)
        # Order not guaranteed, sort by x to check
        merged.sort(key=lambda x: x[0])

        self.assertEqual(merged[0], [0, 0, 15, 10])
        self.assertEqual(merged[1], [100, 100, 15, 15])

    def test_empty(self):
        self.assertEqual(ZoneDiscoverer._merge_overlapping_boxes([]), [])

class TestDiscoveryResult(unittest.TestCase):
    def test_iteration(self):
        from isocenter.discovery import DiscoveryResult, DiscoveryCandidate

        c1 = DiscoveryCandidate("A", 0.9, [0,0,10,10], 0, "TEXT")
        c2 = DiscoveryCandidate("B", 0.8, [20,20,10,10], 1, "TEXT")

        res = DiscoveryResult([c1, c2], n_sources=2)

        # Test iteration
        items = list(res)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0], c1)
        self.assertEqual(items[1], c2)

        # Test iterator protocol
        it = iter(res)
        self.assertEqual(next(it), c1)
        self.assertEqual(next(it), c2)
        with self.assertRaises(StopIteration):
            next(it)

    def test_filter_lambda(self):
        from isocenter.discovery import DiscoveryResult, DiscoveryCandidate
        c1 = DiscoveryCandidate("Keep", 0.9, [0,0,10,10], 0, "TEXT")
        c2 = DiscoveryCandidate("Drop", 0.5, [0,0,10,10], 0, "TEXT")

        res = DiscoveryResult([c1, c2], 2)

        # Filter by text (lambda)
        filtered = res.filter(lambda c: c.text == "Keep")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered.candidates[0].text, "Keep")

        # Legacy float filter check
        filtered_conf = res.filter(0.8)
        self.assertEqual(len(filtered_conf), 1)
        self.assertEqual(filtered_conf.candidates[0].confidence, 0.9)

    def test_heatmap(self):
        from isocenter.discovery import DiscoveryResult, DiscoveryCandidate
        # A 100x100 grid concept.
        # Box at 10,10 (Top Left)
        c1 = DiscoveryCandidate("A", 1.0, [10,10,10,10], 0, "TEXT")

        res = DiscoveryResult([c1], 1)
        heatmap = res.visualize_heatmap(bins=(5, 5))
        self.assertIn("Discovery Heatmap", heatmap)
        self.assertIn("|", heatmap) # Borders
        # Should have a dot or marker
        self.assertTrue("." in heatmap or "o" in heatmap)

    def test_temporal_stability(self):
        from isocenter.discovery import DiscoveryResult, DiscoveryCandidate
        # 10 sources.
        # "Static" appears in all 10.
        candidates = []
        for i in range(10):
            candidates.append(DiscoveryCandidate("Static", 1.0, [10,10,20,20], i, "TEXT"))

        # "Noise" appears in 1 (index 0)
        candidates.append(DiscoveryCandidate("Noise", 0.5, [100,100,10,10], 0, "TEXT"))

        res = DiscoveryResult(candidates, n_sources=10)
        report = res.analyze_temporal_stability()

        # Sort by status to checks
        report.sort(key=lambda x: x['status'])

        # Expect STATIC_ALWAYS and TRANSIENT
        self.assertEqual(len(report), 2)

        static_zone = next(r for r in report if r['status'] == "STATIC_ALWAYS")
        self.assertEqual(static_zone['occurrence'], 1.0)

        transient_zone = next(r for r in report if r['status'] == "TRANSIENT")
        self.assertEqual(transient_zone['occurrence'], 0.1)

    def test_inspect_clusters(self):
        from isocenter.discovery import DiscoveryResult, DiscoveryCandidate
        c1 = DiscoveryCandidate("A", 1.0, [0,0,10,10], 0, "TEXT")
        c2 = DiscoveryCandidate("B", 1.0, [5,0,10,10], 0, "TEXT") # Overlaps A
        c3 = DiscoveryCandidate("C", 1.0, [100,0,10,10], 0, "TEXT") # Disjoint

        res = DiscoveryResult([c1, c2, c3], 1)
        clusters = res.inspect_clusters(pad_x=0)

        self.assertEqual(len(clusters), 2)
        # Verify sizes, order might vary depending on implementation (but likely stable)
        lens = sorted([len(c) for c in clusters])
        self.assertEqual(lens, [1, 2]) # One cluster of 2, one of 1

    def test_get_density_matrix(self):
        from isocenter.discovery import DiscoveryResult, DiscoveryCandidate
        c1 = DiscoveryCandidate("A", 1.0, [10,10,10,10], 0, "TEXT")
        # 10x10 bin. Box is at 10,10.
        # If max extent is small, it might map to specific bin.

        res = DiscoveryResult([c1], 1)
        matrix = res.get_density_matrix(bins=(5, 5))
        self.assertEqual(len(matrix), 5)
        self.assertEqual(len(matrix[0]), 5)

        # Check that there is a count > 0 somewhere
        total_count = sum(sum(row) for row in matrix)
        self.assertEqual(total_count, 1)

    def test_to_dataframe_pandas_missing(self):
        # Mock sys.modules to simulate pandas missing if it happens to be installed
        import sys
        from unittest.mock import patch

        from isocenter.discovery import DiscoveryResult

        with patch.dict(sys.modules, {'pandas': None}):
            res = DiscoveryResult([], 1)
            with self.assertRaises(ImportError):
                res.to_dataframe()

    def test_to_dataframe_mock_success(self):
        import sys
        from unittest.mock import MagicMock, patch
        from isocenter.discovery import DiscoveryResult, DiscoveryCandidate

        mock_pd = MagicMock()
        mock_df = MagicMock()
        mock_pd.DataFrame.return_value = mock_df

        with patch.dict(sys.modules, {'pandas': mock_pd}):
            c1 = DiscoveryCandidate("A", 1.0, [0,0,0,0], 0, "TEXT")
            res = DiscoveryResult([c1], 1)

            df = res.to_dataframe()

            self.assertEqual(df, mock_df)
            mock_pd.DataFrame.assert_called_once()
            # Verify arg passed to DataFrame was a list of dicts
            args, _ = mock_pd.DataFrame.call_args
            data = args[0]
            self.assertEqual(data[0]['text'], "A")


if __name__ == '__main__':
    unittest.main()

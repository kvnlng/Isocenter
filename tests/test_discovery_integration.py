
import unittest
import os
import shutil
import tempfile
import sys
from isocenter.session import DicomSession
# Ensure we can import the generator
sys.path.insert(0, os.path.abspath('.'))
import scripts.generate_redaction_example as gen

class TestDiscoveryIntegration(unittest.TestCase):

    def setUp(self):
        # Seed for determinism
        import random
        random.seed(42)
        try:
            from faker import Faker
            Faker.seed(42)
        except ImportError:
            pass

        if not gen.HAS_DEPS:
            self.skipTest("Requires 'pillow' and 'faker' which are not installed")

        # Create temp environment
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.session = DicomSession(self.db_path)

        # Override generator path to output to temp dir
        # The generator script is hardcoded to "test_data/redaction_examples"
        # We'll just patch where we ingest from, but we have to let it write where it wants
        # or monkeypatch it.
        # Easier: Just run it, move files, or just ingest from the hardcoded path (if safe).
        # Actually, let's just use the logic from reproduce_issue_v2.py

        gen.main(output_dir=os.path.join(self.test_dir, "data")) # Generates data in the temp dir/data

    def tearDown(self):
        self.session.close()
        shutil.rmtree(self.test_dir)

    def test_proper_noun_merging(self):
        """
        Integration test verifying that 'Hospital' + Gap + 'PatientName'
        are merged into a single PROPER_NOUN zone using asymmetric clustering.
        """
        # 1. Ingest
        self.session.ingest(os.path.join(self.test_dir, "data"))

        # 2. Identify Serial
        # We know the generator makes specific sets.
        # "SN-5506" was the problematic one (GE Revolution CT).
        # But generator is random seeded in main().
        # We should find ANY serial that has data.

        eqs = self.session.store.get_unique_equipment()

        found_merged_zone = False
        found_proper_noun = False

        for eq in eqs:
            serial = eq.device_serial_number
            print(f"Scanning {serial} ({eq.manufacturer})...")

            # Result is now a DiscoveryResult object
            # We must group it to get zones
            result = self.session.discover_redaction_zones(
                serial,
                sample_size=10,
                min_confidence=50.0
            )
            zones = result.to_zones(pad_x=100, pad_y=10)

            for z in zones:
                z_type = z.get('type')
                z_rect = z.get('zone')
                width = z_rect[3] - z_rect[2]
                print(f"  Zone: {z_rect} Type: {z_type} Width: {width} Examples: {z.get('examples')}")

                if z_type == "PROPER_NOUN":
                    found_proper_noun = True # At least one machine found a name
                    if width > 250:
                        found_merged_zone = True

            if found_merged_zone:
                break

        self.assertTrue(found_proper_noun, "Should detect at least one PROPER_NOUN zone across all machines")
        self.assertTrue(found_merged_zone, "Should detect a merged zone (Width > 250px)")

if __name__ == '__main__':
    unittest.main()

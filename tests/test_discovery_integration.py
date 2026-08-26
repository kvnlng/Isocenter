
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
        # Seed for determinism. The generator picks manufacturers, serials
        # and names at random, and the assertions below depend on at least
        # one machine producing an adjacent hospital/name pair.
        import random
        from faker import Faker

        random.seed(42)
        Faker.seed(42)

        # `faker` is a declared `tests` dependency and `pillow` is a hard
        # install_requires, so a false HAS_DEPS means a broken environment,
        # not an optional feature. Skipping here is what let this test go
        # unrun for months: the old skip named 'pillow' -- which is always
        # present -- and sent readers looking in the wrong place (#44).
        self.assertTrue(
            gen.HAS_DEPS,
            "generate_redaction_example reports missing dependencies, but "
            "pillow is in install_requires and faker is in the `tests` "
            "extra. Install with `pip install -e \'.[tests]\'`.")

        # OCR is the one genuinely optional piece. This test discovers
        # redaction zones from burned-in pixel text, so without it every
        # machine yields zero zones and the assertions below are vacuous.
        # pytesseract also needs the `tesseract` system binary, which pip
        # cannot supply -- hence a real skip rather than a failure.
        from isocenter.pixel_analysis import HAS_OCR
        if not HAS_OCR:
            self.skipTest(
                "Requires the 'ocr' extra (pytesseract) and a `tesseract` "
                "binary on PATH: this test reads burned-in pixel text.")

        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.session = DicomSession(self.db_path)

        gen.main(output_dir=os.path.join(self.test_dir, "data"))

    def tearDown(self):
        self.session.close()
        shutil.rmtree(self.test_dir)

    def test_proper_noun_merging(self):
        """
        Integration test verifying that 'Hospital' + Gap + 'PatientName'
        are merged into a single PROPER_NOUN zone using asymmetric clustering.
        """
        self.session.ingest(os.path.join(self.test_dir, "data"))

        # The generator seeds its manufacturers and serials randomly, so no
        # particular serial can be named here; any machine that produced an
        # adjacent hospital/name pair satisfies the assertions.
        eqs = self.session.store.get_unique_equipment()

        found_merged_zone = False
        found_proper_noun = False

        for eq in eqs:
            serial = eq.device_serial_number

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

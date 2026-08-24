
import unittest
import os
import shutil
import tempfile
import numpy as np
import time
from gantry.session import DicomSession
from gantry.entities import Instance, Series, Study, Patient, Equipment

class ConstantPixelLoader:
    """A picklable stand-in for a real pixel loader.

    Must be a module-level callable, NOT a closure or lambda: redaction
    runs in a ProcessPoolExecutor, which pickles each task by qualified
    name. A local lambda raises "Can't get local object ...<locals>.<lambda>",
    the redaction worker dies, and -- because execute() swallows worker
    failures (issue #48) -- the run reports success having redacted
    nothing.

    These tests previously used a local lambda and still passed in CI only
    because tests/test_export_sql.py set GANTRY_FORCE_THREADS=1 in a
    fixture and never unset it. That leaked into every later test in the
    session and silently swapped the process pool for a thread pool, which
    does not pickle. Deleting that file (it covered the removed
    generate_export_from_db) removed the leak and exposed this. So these
    tests never actually exercised the process isolation they are named
    for.
    """

    def __init__(self, size=50, fill=255):
        self.size = size
        self.fill = fill

    def __call__(self):
        return np.full((self.size, self.size), self.fill, dtype=np.uint8)


class TestRedactionParallel(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.session = DicomSession(self.db_path)

    def tearDown(self):
        # Ensure thread/db resources are released
        if hasattr(self.session, "store_backend"):
            self.session.store_backend.stop()

        # Give a momentary pause for OS file handle release (Windows/sometimes Linux)
        import time; time.sleep(0.1)
        shutil.rmtree(self.test_dir)

    def test_parallel_execution_speedup_and_safety(self):
        """
        Verifies that redaction runs in parallel (multiple threads) and produces correct results.
        We can't easily assert speedup on a small test, but we can assert correctness and lack of locking errors.
        """
        # Setup 10 machines
        machine_serials = [f"M{i}" for i in range(10)]

        for i, serial in enumerate(machine_serials):
            p = Patient(f"P{i}", f"Pat{i}")
            st = Study(f"S{i}", "20230101")
            se = Series(f"Se{i}", "CT", 1)
            se.equipment = Equipment("Man", "Mod", serial)

            inst = Instance(f"I{i}", f"1.2.3.{i}", 1)
            # inst.rows = 50
            # inst.columns = 50

            # Mock Pixel Data Loader
            # We create a unique array for each to verify modification
            # In a real threading scenario, we want to ensure no race conditions on shared resources (like the DB/Log)
            inst._pixel_loader = ConstantPixelLoader()

            se.instances.append(inst)
            st.series.append(se)
            p.studies.append(st)
            self.session.store.patients.append(p)

            # Index it manually if not using full session.ingest (RedactionService indexes on init)
            # RedactionService is created inside execute_config, so it will index current store.

        # Configure Rules
        rules = []
        for serial in machine_serials:
            rules.append({
                "serial_number": serial,
                "redaction_zones": [[0, 10, 0, 10]] # Top-Left 10x10 zeroed
            })

        self.session.configuration.rules = rules

        # Execute
        self.session.redact()

        # Verify Results
        # Each instance should have the top-left 10x10 region black (0)
        # And the rest white (255)
        for p in self.session.store.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        arr = inst.get_pixel_data()
                        # ROI: 0:10, 0:10
                        roi = arr[0:10, 0:10]
                        rest = arr[10:, 10:]

                        self.assertTrue(np.all(roi == 0), f"Instance {inst.sop_instance_uid}: ROI not redacted")
                        # We can't strictly assert 'rest' is all 255 if ROI overlaps, but here it doesn't.
                        # Actually 'rest' is not the full complement.
                        # Just check a pixel outside.
                        self.assertEqual(arr[40, 40], 255, "Instance modified outside ROI")

    def test_single_machine_parallel_execution(self):
        """
        Verifies that redaction runs in parallel for a SINGLE machine with many instances.
        This tests the granular task splitting.
        """
        # Setup 1 machine with 50 instances
        serial = "SingleMach"
        p = Patient("P1", "PatientOne")
        st = Study("S1", "20230101")
        se = Series("Se1", "CT", 1)
        se.equipment = Equipment("Man", "Mod", serial)

        for i in range(50):
            inst = Instance(f"I{i}", f"1.2.3.{i}", 1)

            inst._pixel_loader = ConstantPixelLoader()
            se.instances.append(inst)

        st.series.append(se)
        p.studies.append(st)
        self.session.store.patients.append(p)

        # 1 Rule
        self.session.configuration.rules = [{
            "serial_number": serial,
            "redaction_zones": [[0, 10, 0, 10]]
        }]

        # Execute
        self.session.redact()

        # Verify
        count_redacted = 0
        for inst in se.instances:
            arr = inst.get_pixel_data()
            if np.all(arr[0:10, 0:10] == 0):
                count_redacted += 1

        self.assertEqual(count_redacted, 50, "All 50 instances should be redacted")

if __name__ == '__main__':
    unittest.main()

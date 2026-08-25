import os
import unittest
import threading
import concurrent.futures
from isocenter.persistence import SqliteStore

class TestVerticalTable(unittest.TestCase):
    def setUp(self):
        self.db_path = "test_vertical.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

        # Create store
        self.store = SqliteStore(self.db_path)
        # Create dummy instance/patient hierarchy to satisfy FKs if needed?
        # instance_attributes FK references instances(sop_instance_uid).
        # We need to insert a dummy instance first.
        with self.store._get_connection() as conn:
            conn.execute("INSERT INTO patients (patient_id, patient_name) VALUES ('P1', 'TestPatient')")
            # We need IDs to link.
            p_id = conn.execute("SELECT id FROM patients").fetchone()[0]
            conn.execute("INSERT INTO studies (patient_id_fk, study_instance_uid) VALUES (?, 'S1')", (p_id,))
            s_id = conn.execute("SELECT id FROM studies").fetchone()[0]
            conn.execute("INSERT INTO series (study_id_fk, series_instance_uid) VALUES (?, 'SE1')", (s_id,))
            se_id = conn.execute("SELECT id FROM series").fetchone()[0]
            conn.execute("INSERT INTO instances (series_id_fk, sop_instance_uid) VALUES (?, 'I1')", (se_id,))

    def tearDown(self):
        self.store.stop() # Stop audit thread
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        if os.path.exists(self.db_path.replace(".db", "_pixels.bin")):
            os.remove(self.db_path.replace(".db", "_pixels.bin"))

    def test_vertical_roundtrip(self):
        """Verify that we can save and load attributes correctly."""
        attrs = {
            ("0010", "1001"): "PrivateName",
            ("0010", "1002"): ["Value1", "Value2"] # VM > 1
        }

        self.store.save_vertical_attributes("I1", attrs)

        loaded = self.store.load_vertical_attributes("I1")

        self.assertIn(("0010", "1001"), loaded)
        self.assertEqual(loaded[("0010", "1001")], "PrivateName")

        self.assertIn(("0010", "1002"), loaded)
        self.assertEqual(loaded[("0010", "1002")], ["Value1", "Value2"])

    def test_vertical_update_serialization(self):
        """
        Verify that sequential/concurrent updates don't cause corruption (duplicate rows).
        The Delete-Insert strategy should strictly replace the value.
        """
        # Initial Write
        self.store.save_vertical_attributes("I1", {("0099", "9999"): "Initial"})

        def update_task(val):
            # Create a separate store instance per thread to simulate real concurrency?
            # Or use same store? SqliteStore handles connections per thread.
            store = SqliteStore(self.db_path) # separate connection logic
            store.save_vertical_attributes("I1", {("0099", "9999"): val})

        # Run concurrent updates
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(update_task, f"Update_{i}") for i in range(10)]
            concurrent.futures.wait(futures)

        # Verify final state
        # We don't know WHICH one won (LWW), but we must ensure:
        # 1. Only ONE value exists (VM=1) for this tag.
        # 2. It is one of the "Update_X" strings.

        loaded = self.store.load_vertical_attributes("I1")
        val = loaded.get(("0099", "9999"))

        print(f"Final resolved value: {val}")

        # Check integrity by querying DB directly for duplicates
        with self.store._get_connection() as conn:
            rows = conn.execute("SELECT * FROM instance_attributes WHERE group_id='0099' AND element_id='9999'").fetchall()
            self.assertEqual(len(rows), 1, f"Found {len(rows)} rows for tag, expected 1. Delete-Insert failed?")

        self.assertTrue(val.startswith("Update_"))

if __name__ == "__main__":
    unittest.main()

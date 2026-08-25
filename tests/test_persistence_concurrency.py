
import pytest
import threading
import concurrent.futures
import time
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.persistence import SqliteStore

class TestPersistenceConcurrency:

    @pytest.fixture
    def store(self):
        """Creates an in-memory store for testing."""
        return SqliteStore(":memory:")

    def test_smoke_single_thread(self, store):
        """Basic smoke test to ensure memory DB works at all."""
        p = Patient("SMOKE", "Smoke Test")
        store.save_all([p])
        loaded = store.load_all()
        assert len(loaded) == 1
        assert loaded[0].patient_id == "SMOKE"

    def test_concurrent_writes(self, store):
        """Verifies that multiple threads can write simultaneously without locking errors."""

        def worker(idx):
            try:
                # Each worker creates a unique patient and saves it
                p = Patient(f"P_{idx}", f"Patient_{idx}")
                # Simulate some work
                time.sleep(0.001)
                store.save_all([p])
                print(f"Worker {idx} saved P_{idx}")
                return p.patient_id
            except Exception as e:
                print(f"Worker {idx} failed: {e}")
                raise

        # Use 10 threads writing continuously
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, i) for i in range(50)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Verify all saved
        loaded_patients = store.load_all()
        assert len(loaded_patients) == 50

        saved_ids = {p.patient_id for p in loaded_patients}
        expected_ids = {f"P_{i}" for i in range(50)}
        assert saved_ids == expected_ids

    def test_mixed_read_write(self, store):
        """Verifies correct behavior with mixed readers and writers."""

        # Seed initial data
        initial_p = Patient("SEED", "Seed Patient")
        store.save_all([initial_p])

        stop_event = threading.Event()
        errors = []

        def reader_worker():
            while not stop_event.is_set():
                try:
                    p_list = store.load_all()
                    # Just verify we can read
                    assert len(p_list) >= 1
                except Exception as e:
                    errors.append(f"read error: {e}")
                    break

        def writer_worker(start_idx):
            for i in range(start_idx, start_idx + 20):
                try:
                    p = Patient(f"W_{i}", f"Writer_{i}")
                    store.save_all([p])
                except Exception as e:
                    errors.append(f"write error: {e}")
                    break

        # Start 2 readers and 2 writers
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            readers = [executor.submit(reader_worker) for _ in range(2)]
            writers = [executor.submit(writer_worker, 0), executor.submit(writer_worker, 100)]

            # Wait for writers to finish
            for w in writers:
                w.result()

            stop_event.set()
            for r in readers:
                r.result()

        if errors:
            pytest.fail(f"Concurrency errors occurred: {errors}")

        # Verify total count (1 seed + 20 + 20)
        final_list = store.load_all()
        assert len(final_list) == 41

    def test_concurrent_updates_same_patient(self, store):
        """Verifies row locking/integrity when updating the same entity."""

        p = Patient("SHARED_P", "Initial Name")
        store.save_all([p])

        def update_worker(name_suffix):
            # Create a FRESH object to simulate separate session usage or race
            # Since persistence is decoupled from object identity in memory for this test
            # (we pass object to save_all)
            p_update = Patient("SHARED_P", f"Name_{name_suffix}")
            p_update._dirty = True
            store.save_all([p_update])

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(update_worker, i) for i in range(20)]
            concurrent.futures.wait(futures)

        # Success = No crash.
        # State = Unknown last write wins, but should be valid.
        loaded = store.load_patient("SHARED_P")
        assert loaded is not None
        assert loaded.patient_name.startswith("Name_")

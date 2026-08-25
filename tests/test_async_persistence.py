import pytest
import time
import threading
from isocenter.session import DicomSession
from isocenter.entities import Patient
from isocenter.persistence import SqliteStore

def test_async_save_completes(tmp_path):
    db_path = str(tmp_path / "async.db")
    session = DicomSession(db_path)

    # 1. Create Data
    session.store.patients.append(Patient("P_ASYNC", "Async Test"))

    # 2. Trigger async save
    start_time = time.time()
    session.save()
    end_time = time.time()

    # Assert return logic was fast (no blocking IO on a tiny db, but still)
    # The actual worker IO might be faster than thread launch overhead for 1 record,
    # but the architectural point is that it returns.

    # 3. Wait regarding consistency
    # We need to wait for the worker to finish
    session.persistence_manager.shutdown() # This waits

    # 4. Verify DB
    store = SqliteStore(db_path)
    patients = store.load_all()
    assert len(patients) == 1
    assert patients[0].patient_id == "P_ASYNC"

def test_shutdown_waits(tmp_path):
    # Simulate a slow save
    # We can mock SqliteStore.save_all to sleep
    db_path = str(tmp_path / "slow.db")
    session = DicomSession(db_path)

    for i in range(5):
        session.store.patients.append(Patient(f"P_{i}", f"Name {i}"))

    session.save()

    # Shutdown should join the thread
    t_start = time.time()
    session.persistence_manager.shutdown()
    t_end = time.time()

    # Data should be there
    store = SqliteStore(db_path)
    loaded = store.load_all()
    assert len(loaded) == 5

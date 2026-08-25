
import pytest
import threading
import time
import random
from queue import Queue
from isocenter.persistence_manager import PersistenceManager
from isocenter.persistence import SqliteStore
from isocenter.entities import Patient

@pytest.fixture
def pm_stress(tmp_path):
    """
    Fixture providing a PersistenceManager backed by a real SQLite DB
    to test actual locking behavior.
    """
    db_path = str(tmp_path / "stress_test.db")
    store = SqliteStore(db_path)
    pm = PersistenceManager(store)
    yield pm
    pm.shutdown()

def test_concurrent_persistence_writes(pm_stress):
    """
    Spawns multiple producer threads to hammer the PersistenceManager.
    Verifies that all queued saves are eventually persisted without data loss.
    """
    NUM_THREADS = 10
    ITEMS_PER_THREAD = 50
    TOTAL_ITEMS = NUM_THREADS * ITEMS_PER_THREAD

    errors = []

    def producer(thread_id):
        try:
            for i in range(ITEMS_PER_THREAD):
                # Unique ID: T{thread_id}_I{i}
                pid = f"T{thread_id}_I{i}"
                p = Patient(pid, f"Name_{pid}")
                # We save a LIST of 1 patient
                pm_stress.save_async([p])
                # Tiny random sleep to vary arrival times
                if random.random() < 0.1:
                    time.sleep(0.001)
        except Exception as e:
            errors.append(e)

    threads = []
    for t_id in range(NUM_THREADS):
        t = threading.Thread(target=producer, args=(t_id,))
        threads.append(t)
        t.start()

    # Wait for producers
    for t in threads:
        t.join()

    assert not errors, f"Producer threads encountered errors: {errors}"

    # Flush queue to DB
    pm_stress.flush()

    # Verify DB contents
    saved_patients = pm_stress.store_backend.load_all()
    # Note: load_all() returns a list of Patient objects.
    # We need to check we have TOTAL_ITEMS unique IDs.

    saved_ids = set(p.patient_id for p in saved_patients)

    assert len(saved_ids) == TOTAL_ITEMS, f"Expected {TOTAL_ITEMS} unique patients, found {len(saved_ids)}"

    # Check a few random IDs to be sure
    assert "T0_I0" in saved_ids
    assert f"T{NUM_THREADS-1}_I{ITEMS_PER_THREAD-1}" in saved_ids


def test_persistence_chaos(pm_stress):
    """
    Simulates a 'Chaos Monkey' scenario where the worker thread is
    randomly killed/stopped while work is flooding in.
    Relies on the new auto-recovery logic in flush() to save the day.
    """
    RUN_TIME_SECONDS = 3
    PRODUCER_DELAY = 0.001 # 1ms

    chaos_running = True
    producer_running = True

    produced_count = 0
    lock = threading.Lock()

    def chaos_monkey():
        while chaos_running:
            time.sleep(random.uniform(0.1, 0.5))
            # Randomly kill execution
            if pm_stress.thread and pm_stress.thread.is_alive():
                 # We assume shutdown() is graceful, but let's try to be meaner:
                 # Just set running=False to break loop?
                 # Or call shutdown() which joins.
                 # Let's use shutdown() as it simulates "Operator Stop" or "System Shutdown" signal
                 # But we also want to test the 'dead thread' recovery.
                 # pm_stress.shutdown() clears queue? No, it processes pending.

                 # Let's simulates a semi-crash by setting running=False
                 # and waiting for thread to exit, but NOT clearing the queue
                 # before we restart or before flush is called.
                 pm_stress.running = False
                 # We don't join here, just let it die.

            # Note: The system doesn't auto-restart periodically,
            # it auto-restarts on save_async() or flush().

    def producer():
        nonlocal produced_count
        while producer_running:
            with lock:
                pid = f"C_{produced_count}"
                produced_count += 1

            p = Patient(pid, "Chaos")
            # save_async should trigger restart if needed
            pm_stress.save_async([p])
            time.sleep(PRODUCER_DELAY)

    # Start threads
    t_chaos = threading.Thread(target=chaos_monkey)
    t_prod = threading.Thread(target=producer)

    t_chaos.start()
    t_prod.start()

    # Let run
    time.sleep(RUN_TIME_SECONDS)

    # Stop Chaos
    chaos_running = False
    master_stop = True
    t_chaos.join()

    # Stop Producer
    producer_running = False
    t_prod.join()

    print(f"\nProduced {produced_count} items during chaos.")

    # FLUSH - This is the critical test.
    # Can it recover if the monkey left it dead?
    try:
        pm_stress.flush()
    except Exception as e:
        pytest.fail(f"Flush failed during chaos recovery: {e}")

    # Verify
    saved = pm_stress.store_backend.load_all()
    # We rely on lists being appended.
    # Note: If save_async is called while worker is dead, it restarts it.
    # If worker dies with items in queue, flush restarts it.
    # The only risk is if an item was popped but not saved when thread died?
    # Our _worker uses: try: save() finally: task_done()
    # If thread dies inside save(), exception is caught.
    # If thread loop breaks (running=False), it finishes current item?
    # If running set to False, loop condition fails at next iteration.
    # Currently processing item finishes.

    # So we should expect 100% persistence reliability here for SqliteStore
    # (since SQLite is ACID and we save one by one or batch).

    assert len(saved) == produced_count, f"Expected {produced_count}, got {len(saved)}. Chaos caused data loss!"

"""
Tests for the PersistenceManager class.
"""
import time
import os
import pytest
from isocenter.persistence_manager import PersistenceManager
from isocenter.persistence import SqliteStore
from isocenter.entities import Patient

class MockStore(SqliteStore):
    """Mock store for testing."""
    def __init__(self, db_path):
        super().__init__(db_path)
        self.db_path = db_path
        self.saved_patients = []

    def save_all(self, patients, prune_absent_patients=False):
        # Signature mirrors SqliteStore.save_all: the manager passes
        # prune_absent_patients, and a double that cannot accept it fails
        # inside the worker thread, where the error becomes a log line.
        self.saved_patients.extend(patients)

@pytest.fixture
def pm(tmp_path):
    db_path = str(tmp_path / "test_pm.db")
    store = MockStore(db_path)
    pm = PersistenceManager(store)
    yield pm
    pm.shutdown()

def test_basic_lifecycle(pm):
    """Test start, save, flush."""
    assert pm.running
    assert pm.thread.is_alive()

    p = Patient("P1", "Test")
    pm.save_async([p])

    pm.flush()

    assert len(pm.store_backend.saved_patients) == 1
    assert pm.store_backend.saved_patients[0].patient_id == "P1"

def test_restart_behavior(pm):
    """Test that save_async restarts the worker if it was shut down."""
    p1 = Patient("P1", "Test1")
    pm.save_async([p1])
    pm.flush()
    assert len(pm.store_backend.saved_patients) == 1

    # Needs to be called explicitly to stop the thread
    pm.shutdown()
    assert not pm.running
    assert not pm.thread.is_alive()

    # Now try to save again - should trigger restart
    p2 = Patient("P2", "AutoRestart")
    pm.save_async([p2])

    # Flush should work (if restart worked)
    pm.flush()

    assert pm.running
    assert pm.thread.is_alive()
    assert len(pm.store_backend.saved_patients) == 2
    assert pm.store_backend.saved_patients[1].patient_id == "P2"

def test_stale_sentinel(pm):
    """
    Simulate a scenario where a stale 'None' (sentinel) is in the queue
    but the worker is supposed to be running.
    """
    # 1. Manually inject poison pill
    pm.queue.put(None)

    # 2. Add legitimate work behind it
    p1 = Patient("P1", "Survivor")
    pm.save_async([p1])

    # 3. Wait for flush - if the bug existed, worker would die on None and flush would hang/timeout
    # or P1 would never be processed.
    pm.flush()

    # Verify worker is still alive
    assert pm.running
    assert pm.thread.is_alive()

    # Verify P1 was processed
    assert len(pm.store_backend.saved_patients) == 1
    assert pm.store_backend.saved_patients[0].patient_id == "P1"

def test_flush_recover_from_crash(pm):
    """
    Test that flush() detects if the worker is dead but queue has items,
    and restarts it to ensure data is saved.
    """
    # 1. Kill the worker
    pm.running = False
    # Wait for thread to die naturally or just proceed since running=False breaks the loop
    # But we want to simulate a CRASH where the thread is dead but running might be True or False depending on how we track it.
    # If the thread died unexpectedly, pm.running might still be True?
    # Actually, let's simulate the thread being dead.
    if pm.thread.is_alive():
        # Stop it gracefully first to ensure clean state
        pm.shutdown()

    # 2. Inject work directly into queue (simulating work queued just before/during crash)
    p = Patient("P_CRASH", "Crash Test")
    # Queue items are (patients, prune_absent_patients), as save_async
    # builds them; this test injects one directly to simulate a crash
    # that left work outstanding.
    pm.queue.put(([p], False))

    # 3. Call flush()
    # WITHOUT FIX: This should just return immediately because !is_alive(), leaving item in queue.
    # WITH FIX: This should restart worker, process item, and then return.
    pm.flush()

    # 4. Verification
    assert pm.queue.empty(), "Queue should be empty after flush"
    assert len(pm.store_backend.saved_patients) == 1
    assert pm.store_backend.saved_patients[0].patient_id == "P_CRASH"



def test_a_save_after_running_went_false_reuses_the_live_worker(pm):
    """A live worker is never joined by a second one (#250).

    `_worker` is a `while True`. Setting `running = False` does not end
    it -- only the sentinel does -- so a live worker with `running is
    False` is an ordinary state, not a crashed one. While `_start_worker`
    guarded on the flag, every such toggle started an ADDITIONAL consumer
    on the same queue and rebound `self.thread`. `test_persistence_chaos`
    toggles it nine times; the run ended with nine live workers, and
    since `shutdown()` posts exactly one sentinel and joins only
    `self.thread`, whichever of the nine ate the sentinel was usually not
    that one and the `join(timeout=30)` ran to the full 30 seconds --
    once at teardown and again from `atexit` after pytest's summary line.

    Structural, not timed: the assertion is thread identity.
    """
    before = pm.thread
    assert before.is_alive()

    pm.running = False
    pm.save_async([Patient("P_REUSE", "Reuse^Test")])

    assert pm.thread is before, (
        "save_async started a second worker on the same queue while the "
        "first was still alive; both then compete for one sentinel and "
        "shutdown() joins whichever thread it happens to hold (#250)")
    assert pm.running, (
        "the flag was left False under a live worker, so the next save "
        "would spawn yet another consumer")


def test_shutdown_after_running_went_false_actually_stops_the_worker(pm):
    """One sentinel is enough because there is only one consumer (#250).

    The other half of the guard: having reused the live worker rather
    than spawning a second, `shutdown()`'s single sentinel reaches the
    thread it then joins. A leaked second consumer is what used to eat
    the sentinel and leave `self.thread` alive for the full 30-second
    join.
    """
    pm.running = False
    pm.save_async([Patient("P_STOP", "Stop^Test")])
    pm.flush()

    pm.shutdown()

    assert not pm.thread.is_alive(), (
        "shutdown() returned with its worker still running: the sentinel "
        "was consumed by a thread other than the one being joined (#250)")

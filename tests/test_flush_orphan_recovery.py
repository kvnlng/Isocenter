"""A flush that finds an orphaned save must drain it, not wait forever (#309).

`PersistenceManager._worker` takes an item off the queue with `get()` and
reaches `task_done()` from the `finally` of the inner `try`. That covers
every ordinary exception and every `BaseException` -- `KeyboardInterrupt`,
`SystemExit`, pytest's own `Failed`/`Skipped` all unwind through it. What
it does not cover is the item itself: once `get()` has returned, the
payload is out of the deque and `unfinished_tasks` is 1, and if the worker
never reaches the `finally` the two facts stay that way forever.

The resulting state is what makes this a hang rather than a lost save:

    queue.empty() is True      -- the item is out of the deque
    queue.unfinished_tasks == 1 -- nobody called task_done() for it

`flush()` guarded its worker restart on `not self.queue.empty()`, so in
exactly this state it skipped the restart and went straight to
`queue.join()`, which cannot return while a task is unfinished. And a
restart on its own does not help: a fresh worker has nothing left to
`get()`, so the count stays at 1. The queued save is *also* lost, which
is why the fix reconciles -- puts the payload back and balances the count
-- rather than merely counting the orphan down.

These tests are deterministic and construct the state; they do not race to
produce it. The worker is stopped cleanly first, so the test thread can
stand in for a worker that died between `get()` and `task_done()` with no
timing involved and no thread killed. Every wait on `flush()` is bounded
on a helper thread: this issue is about a hang, and a test for it must not
add one -- unfixed code fails in ten seconds instead of wedging the suite.
"""
import threading

import pytest

from isocenter.entities import Patient
from isocenter.persistence import SqliteStore
from isocenter.persistence_manager import PersistenceManager


class RecordingStore(SqliteStore):
    """Records what `save_all` was handed, without touching the database.

    Local rather than imported from `tests/test_persistence_manager.py`:
    `tests/` is not a package and nothing in the suite imports across test
    modules. The signature mirrors `SqliteStore.save_all` because the
    manager passes `prune_absent_patients`, and a double that cannot
    accept it fails inside the worker thread, where the error becomes a
    log line rather than a failure.
    """

    def __init__(self, db_path):
        super().__init__(db_path)
        self.saved_patients = []

    def save_all(self, patients, prune_absent_patients=False):
        self.saved_patients.extend(patients)


@pytest.fixture
def pm(tmp_path):
    manager = PersistenceManager(RecordingStore(str(tmp_path / "flush.db")))
    yield manager
    manager.shutdown()


def _flush_on_helper(manager):
    """Run `flush()` on a helper thread and return its completion Event.

    A bare `manager.flush()` on unfixed code never returns, so every wait
    in this module is bounded. The helper is a daemon so a wedged flush
    cannot keep the interpreter alive past the run.
    """
    done = threading.Event()

    def run():
        try:
            manager.flush()
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


def _make_orphan(manager, patient):
    """Put the manager into the #309 state, deterministically.

    The worker is shut down first and its death asserted, so the test
    thread's `get()` is not racing anything: it *is* the consumer that
    took the item. `_inflight` is then set by hand, because that is the
    trace a real worker leaves -- it records the item immediately after
    `get()` returns and clears it in the `finally` before `task_done()`.
    Plain attribute assignment with no lock is correct here precisely
    because the assertion above establishes that no worker exists to
    contend for it. On unfixed code the attribute is simply never read,
    so the red these tests produce is the hang itself and not an
    `AttributeError`.
    """
    manager.shutdown()
    assert not manager.thread.is_alive(), (
        "the worker outlived shutdown(), so the item below would be "
        "consumed by a live worker and no orphan would exist")

    manager.queue.put(([patient], False))
    orphan = manager.queue.get()
    manager._inflight = orphan

    assert manager.queue.empty(), (
        "the item is still in the deque, so this is an ordinary pending "
        "save and not the #309 state")
    assert manager.queue.unfinished_tasks == 1, (
        "nothing is outstanding, so flush() has nothing to recover")
    return orphan


def test_flush_returns_and_the_orphaned_save_reaches_the_store(pm):
    """`flush()` drains an orphan *and* the save it was carrying (#309).

    Both halves matter and the second is the one that separates a fix
    from a regression. A `flush()` that noticed the imbalance and simply
    counted it down would return promptly and silently discard a queued
    save -- worse than the hang, because nothing would say so. So the
    payload has to arrive at the store.
    """
    patient = Patient("P_ORPHAN", "Orphan^Test")
    _make_orphan(pm, patient)

    done = _flush_on_helper(pm)

    assert done.wait(10), (
        "flush() never returned: the worker took an item and died before "
        "task_done(), so queue.join() waits on a count nothing will ever "
        "decrement (#309)")
    assert [p.patient_id for p in pm.store_backend.saved_patients] == [
        "P_ORPHAN"], (
        "flush() returned without the orphaned save reaching the store: "
        "the recovery dropped the payload instead of re-queueing it, "
        "which trades a visible hang for a silent data loss")


def test_flushing_twice_recovers_the_orphan_exactly_once(pm):
    """Recovery balances the count; it must not over-balance it (#309).

    One extra `task_done()` is how this fix breaks. The queue raises
    `ValueError: task_done() called too many times` when the count goes
    negative, and a second recovery of the same payload would also write
    it to the store twice. The second `flush()` finds a *live* worker --
    the first restarted one -- and the reconciliation is gated on a dead
    worker, so it takes no recovery path at all.
    """
    patient = Patient("P_ORPHAN", "Orphan^Test")
    _make_orphan(pm, patient)

    first = _flush_on_helper(pm)
    assert first.wait(10), "the first flush() never returned (#309)"

    second = _flush_on_helper(pm)
    assert second.wait(10), (
        "the second flush() never returned; recovery left the queue in a "
        "state the next flush cannot get out of")

    assert [p.patient_id for p in pm.store_backend.saved_patients] == [
        "P_ORPHAN"], (
        "the orphaned payload was saved more than once (or not at all): "
        "recovery must re-queue it exactly once")


def test_flush_still_waits_for_a_save_that_is_genuinely_running(pm):
    """The guarantee the recovery must not trade away (#309).

    `flush()` exists so that callers -- `audit()`, `redact()`, `close()`
    -- can be sure the queue is drained before they read or shut down.
    A recovery that returned early whenever it could not see progress
    would satisfy the two tests above and destroy this one. So a save
    genuinely in flight must still hold `flush()` open.

    **This test passes on unfixed code, deliberately.** Its job is to
    stay green across the fix, not to go red before it.

    The handshake is an Event pair rather than a sleep: the save parks
    inside `save_all` until this test releases it. The negative
    assertion (`not done.wait(0.5)`) can only fail in one direction --
    towards a *false pass*, if a machine is slow enough that a correct
    `flush()` had not yet returned within the window. It cannot produce
    a false failure, because the only way `done` gets set inside those
    0.5 seconds is a `flush()` that returned while the save was
    provably still parked.
    """
    started, release = threading.Event(), threading.Event()
    real_save_all = pm.store_backend.save_all

    def parked_save_all(patients, prune_absent_patients=False):
        started.set()
        release.wait(10)
        return real_save_all(
            patients, prune_absent_patients=prune_absent_patients)

    pm.store_backend.save_all = parked_save_all

    patient = Patient("P_INFLIGHT", "Parked^Test")
    pm.save_async([patient])

    assert started.wait(5), (
        "the save never entered save_all, so the window this test "
        "measures never opened")

    done = _flush_on_helper(pm)
    assert not done.wait(0.5), (
        "flush() returned while a save was still parked inside save_all: "
        "callers that flush before reading or closing would see a "
        "half-written store")

    release.set()
    assert done.wait(10), (
        "flush() never returned after the save was released")
    assert [p.patient_id for p in pm.store_backend.saved_patients] == [
        "P_INFLIGHT"], "the released save never reached the store"


def test_the_worker_records_and_releases_the_item_it_is_holding(pm):
    """The recording half, which the other three do not exercise (#309).

    Tests 1 and 2 set `_inflight` by hand -- that is what makes them
    deterministic, and it is also why they say nothing about the two
    lines in `_worker` that do it for real. Deleting either leaves those
    tests green while the production path stops leaving anything for a
    recovery to find, which is the #309 hang back again.

    Same Event handshake as the test above, so nothing new is invented:
    the save parks inside `save_all`, which is precisely the window in
    which a worker holds an item, and the assertions read the field while
    it is provably parked. Verified one mutation at a time: dropping the
    record leaves the first assertion red, dropping the clear in the
    `finally` leaves the second red, and no other test in the suite moves.
    """
    started, release = threading.Event(), threading.Event()
    real_save_all = pm.store_backend.save_all

    def parked_save_all(patients, prune_absent_patients=False):
        started.set()
        release.wait(10)
        return real_save_all(
            patients, prune_absent_patients=prune_absent_patients)

    pm.store_backend.save_all = parked_save_all

    patient = Patient("P_HELD", "Held^Test")
    pm.save_async([patient])
    assert started.wait(5), "the save never entered save_all"

    assert pm._inflight == ([patient], False), (
        "the worker is inside save_all but recorded nothing: an item taken "
        "off the queue and not recorded is invisible to flush(), which is "
        "the #309 hang with no way out")

    release.set()
    done = _flush_on_helper(pm)
    assert done.wait(10), "flush() never returned after the save finished"

    assert pm._inflight is None, (
        "the finished item is still recorded: a later flush against a dead "
        "worker would re-queue a payload that was already saved and count "
        "off a task that no longer exists")

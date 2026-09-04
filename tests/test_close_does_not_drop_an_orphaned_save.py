"""`close()` must not throw away a save it is the last chance to write (#314).

`Session.close()` -- which is what `__exit__` calls -- reaches
`PersistenceManager.shutdown()`, and `shutdown()` used to return on its
first line whenever the worker was already dead. That is precisely the
state #309 describes: a worker that stopped holding an item leaves the
payload in `_inflight` and `unfinished_tasks` above zero, and a save
still sitting in the deque is in the same position. Both were dropped,
silently, by the one call a user makes specifically so that their work is
on disk. #309's own changelog entry had to retract the claim that
`close()` was covered; this is the exposure it named.

Making `close()` flush was rejected. `flush()` documents that it never
returns early, and a context manager that does not exit is worse than the
bug. So `shutdown()` reconciles instead, bounded at **one `save_all`**
and never a `queue.join()`: it reaps what a dead worker left, drains what
the deque still holds, writes it on the caller's thread, and reports
anything it cannot write.

Two things it must not do, both pinned below. It must not touch a *live*
worker's state -- saving inline there would double-write and race the
worker -- and it must not eat the sentinel out from under one, which
would leave a `while True` worker with nothing left to stop it: #250's
leak, reintroduced by its own fix.
"""
import logging
import threading
import time

import pytest

from isocenter.entities import Patient
from isocenter.persistence import SqliteStore
from isocenter import persistence_manager as pm_module
from isocenter.persistence_manager import PersistenceManager


class RecordingStore(SqliteStore):
    """Records what `save_all` was handed, without touching the database.

    Local rather than imported from another test module: `tests/` is not
    a package and nothing in the suite imports across test modules.
    """

    def __init__(self, db_path):
        super().__init__(db_path)
        self.saved_patients = []

    def save_all(self, patients, prune_absent_patients=False):
        self.saved_patients.extend(patients)


@pytest.fixture
def pm(tmp_path):
    manager = PersistenceManager(RecordingStore(str(tmp_path / "close.db")))
    yield manager
    manager.shutdown()


def _kill_worker(manager):
    """Stop the worker cleanly, so the test thread can stand in for one."""
    manager.shutdown()
    assert not manager.thread.is_alive(), (
        "the worker outlived shutdown(), so the state below would be "
        "consumed by a live worker and no orphan would exist")


def _make_orphan(manager, patient):
    """Leave the manager in #309's state, deterministically.

    Same construction as `tests/test_flush_orphan_recovery.py`: the item
    is taken off the queue by the test thread and recorded under the dead
    worker's key, which is the trace a worker that died between `get()`
    and `task_done()` leaves.

    **Nothing is queued afterwards.** Since #315 a worker restart reaps
    the orphan by itself, so a save queued here would make these tests
    pass for a reason that has nothing to do with `shutdown()`.
    """
    _kill_worker(manager)
    manager.queue.put(([patient], False))
    orphan = manager.queue.get()
    manager._inflight[manager.thread] = orphan
    assert manager.queue.empty() and manager.queue.unfinished_tasks == 1


def _on_helper(fn):
    done = threading.Event()

    def run():
        try:
            fn()
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


def test_shutdown_saves_a_save_its_dead_worker_orphaned(pm):
    """The payload a dead worker was carrying reaches the store (#314)."""
    _make_orphan(pm, Patient("P_ORPHAN", "Orphan^Test"))

    pm.shutdown()

    assert [p.patient_id for p in pm.store_backend.saved_patients] == [
        "P_ORPHAN"], (
        "shutdown() returned without writing the save its worker died "
        "holding: close() is the last call that could have written it, "
        "and it dropped it silently (#314)")
    assert pm.queue.unfinished_tasks == 0, (
        "the orphan was written but never counted off, so a manager "
        "that save_async restarts would hang the next flush() forever")


def test_shutdown_drains_a_queued_save_the_dead_worker_never_took(pm):
    """A save still in the deque is in the same position (#314).

    The dead worker never got as far as `get()`, so there is no
    `_inflight` entry to reap -- only an item nobody will ever consume.
    """
    _kill_worker(pm)
    pm.queue.put(([Patient("P_QUEUED", "Queued^Test")], False))

    pm.shutdown()

    assert [p.patient_id for p in pm.store_backend.saved_patients] == [
        "P_QUEUED"], (
        "shutdown() left a queued save in the deque of a manager nobody "
        "will look at again (#314)")
    assert pm.queue.unfinished_tasks == 0


def test_a_stale_sentinel_does_not_crash_shutdowns_drain(pm):
    """A `None` in the deque is a sentinel, not a payload (#314).

    Reachable on the ordinary path: a `shutdown()` whose join timed out
    has already posted a sentinel, and if that worker then dies without
    consuming it, the next `shutdown()` finds it. A drain that unpacks
    it runs `patients, prune = None` and raises `TypeError` out of
    `close()` -- which then replaces whatever exception the user's
    `with` body was unwinding with a persistence one.
    """
    _kill_worker(pm)
    pm.queue.put(None)
    pm.queue.put(([Patient("P_AFTER", "After^Test")], False))

    pm.shutdown()

    assert [p.patient_id for p in pm.store_backend.saved_patients] == [
        "P_AFTER"], (
        "the drain stopped at the stale sentinel (or raised on it) and "
        "never reached the real save behind it (#314)")
    assert pm.queue.unfinished_tasks == 0


def test_shutdown_is_still_bounded_when_a_save_is_genuinely_running(pm,
                                                                   caplog):
    """The guarantee the reconciliation must not trade away (#314).

    **The boundedness half passes on unfixed code, deliberately.** A
    `shutdown()` that waited for a running save would be `flush()`,
    which `close()` must not become. So a genuinely in-flight save is
    left alone -- never saved inline, which would double-write and race
    the worker still inside `save_all`. The *reporting* half is new and
    is red before the fix: giving up on a save and saying nothing about
    it is the same silence #314 is about, one state further along.

    The second half is the regression this fix could ship: the sentinel
    `shutdown()` posted is still in the deque under a live worker, and a
    drain that swallows it leaves a `while True` worker with nothing left
    to stop it. So the worker must still exit once its save is released.
    """
    started, release = threading.Event(), threading.Event()
    real_save_all = pm.store_backend.save_all

    def parked_save_all(patients, prune_absent_patients=False):
        started.set()
        release.wait(20)
        return real_save_all(
            patients, prune_absent_patients=prune_absent_patients)

    pm.store_backend.save_all = parked_save_all
    pm.save_async([Patient("P_INFLIGHT", "Parked^Test")])
    assert started.wait(5), "the save never entered save_all"

    with caplog.at_level(logging.WARNING):
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                pm_module, "_SHUTDOWN_JOIN_TIMEOUT_S", 1.0)
            done = _on_helper(pm.shutdown)
            assert done.wait(15), (
                "shutdown() did not return: close() now waits on a "
                "running save, which is flush()'s contract and not "
                "close()'s (#314)")

    assert any("P_INFLIGHT" in r.message or "in flight" in r.message.lower()
               or "still running" in r.message.lower()
               for r in caplog.records), (
        "shutdown() gave up on an unfinished save and said nothing: a "
        "silently dropped save is what #314 is about")

    release.set()
    deadline = time.monotonic() + 15.0
    while pm.thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pm.thread.is_alive(), (
        "the worker never exited: the drain swallowed the sentinel "
        "shutdown() posted, and a `while True` worker with no sentinel "
        "left is #250's leak reintroduced by its own fix (#314)")
    assert [p.patient_id for p in pm.store_backend.saved_patients] == [
        "P_INFLIGHT"], (
        "the parked save was written twice (or not at all): shutdown() "
        "must never save a live worker's item inline")


def test_a_session_that_loses_its_worker_still_writes_on_exit(tmp_path):
    """The sentence #309's entry had to retract, now true (#314).

    `with DicomSession(...) as s:` is the documented shape, and the
    thing a user gets from it is that their work is on disk when the
    block ends. A worker that stopped holding the save used to make that
    false with no diagnostic anywhere.
    """
    from isocenter.session import DicomSession

    db = str(tmp_path / "session_close")
    with DicomSession(db) as session:
        patient = Patient("P_SESSION", "Session^Test")
        session.store.patients.append(patient)

        manager = session.persistence_manager
        _make_orphan(manager, patient)

    with DicomSession(db) as reopened:
        assert [p.patient_id for p in reopened.store.patients] == [
            "P_SESSION"], (
            "the session exited its `with` block without writing the "
            "save its worker died holding, so the user's work is gone "
            "and nothing said so (#314)")

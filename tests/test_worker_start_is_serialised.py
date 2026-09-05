"""Two callers that both find a dead worker must still start one (#313).

`PersistenceManager._start_worker` is reached from two places --
`save_async` (unlocked, on the ordinary save path) and
`_recover_orphaned_item` (under `_recover_lock`) -- and both used to do
their own check-then-act on `self.thread.is_alive()`. Two threads can
therefore both observe the same dead worker and both construct one, which
leaves two consumers on one queue: `shutdown()` posts exactly one
sentinel, whichever worker eats it is usually not the one `self.thread`
names, and the `join(timeout=30)` burns the full thirty seconds. That is
#250's shape, reached through the restart path rather than through the
`running` flag.

The window is opened deliberately rather than raced for. Both callers are
gated on a `Barrier` placed *around* `_start_worker` -- outside whatever
serialisation the method itself has -- so they arrive together, and the
first construction is then held up until the second caller has done its
own liveness check. Unfixed code constructs twice, every time. Fixed
code cannot reach the second construction at all, because the second
caller is blocked on the method's own lock and finds a live worker when
it is let through; the first caller simply waits out its bounded 2.0s
gate and proceeds alone. Timing is one-directional: a slow machine makes
the unfixed race *more* reliable and can never fail the fixed code, whose
exclusion is a lock rather than a race.
"""
import threading

import pytest

from isocenter.entities import Patient
from isocenter.persistence import SqliteStore
from isocenter import persistence_manager as pm_module
from isocenter.persistence_manager import PersistenceManager

WORKER_NAME = "PersistenceWorker"


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
    manager = PersistenceManager(RecordingStore(str(tmp_path / "worker.db")))
    yield manager
    manager.shutdown()


class _Gate:
    """Hold two callers at the door of `_start_worker`, then slow the first.

    The `Thread` patch is process-global -- `pm_module.threading` *is*
    the `threading` module -- so the factory keys on the worker's own
    target and delegates every other construction (notably `flush()`'s
    waiter thread) to the real class untouched.

    **The target is `pm_module._persistence_worker_loop`, not
    `manager._worker`.** Since #318 the worker is started with a
    module-level function over a weakref rather than a bound method.
    `_worker` was *deleted* with that change rather than left behind, so
    the old spelling does not quietly match nothing -- it raises
    `AttributeError: 'PersistenceManager' object has no attribute
    '_worker'` inside the factory, on the thread `_start_worker` is
    constructing, and both tests here go red. Measured, by putting the
    old comparison back: `2 failed`.

    That is the benign direction, and it is worth saying which direction
    it is, because the dangerous one is one small edit away. Had #318
    kept a `_worker` shim -- or had this keyed on a *name* rather than
    on the object, `kwargs.get("target").__name__ != "_worker"` -- the
    comparison would match nothing, every construction including both
    racing workers would be delegated straight past the gate, and the
    test would report `_start_worker` as serialised without ever having
    held two callers at its door. Keying on the module attribute is what
    rules that out: it is the same object `_start_worker` passes, so it
    survives a rename of the loop and cannot silently stop matching.
    """

    def __init__(self, manager, monkeypatch, parties=2):
        self.constructed = []
        self._count = 0
        self._lock = threading.Lock()
        self.second_reached = threading.Event()
        self.barrier = threading.Barrier(parties)
        real_thread = threading.Thread

        def factory(*args, **kwargs):
            if kwargs.get("target") is not pm_module._persistence_worker_loop:
                return real_thread(*args, **kwargs)
            with self._lock:
                self._count += 1
                nth = self._count
            if nth == 1:
                # Held open until the second caller has made its own
                # liveness check, which is the whole check-then-act
                # window. Bounded, because fixed code never sets this.
                self.second_reached.wait(timeout=2.0)
            else:
                self.second_reached.set()
            thread = real_thread(*args, **kwargs)
            with self._lock:
                self.constructed.append(thread)
            return thread

        monkeypatch.setattr(pm_module.threading, "Thread", factory)

        real_start = manager._start_worker

        def gated():
            self.barrier.wait(timeout=10.0)
            real_start()

        monkeypatch.setattr(manager, "_start_worker", gated)

    def live(self):
        with self._lock:
            return [t for t in self.constructed if t.is_alive()]


def _on_helper(fn):
    done = threading.Event()

    def run():
        try:
            fn()
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


def test_a_save_and_a_flush_racing_a_dead_worker_start_only_one(pm):
    """`save_async` and `flush()` are the two callers, and they collide.

    The ordinary sequence: a worker dies holding nothing, one caller
    saves and another flushes. Both find a dead worker; only one may
    start a replacement.
    """
    patient = Patient("P_RACE", "Race^Test")
    pm.shutdown()
    assert not pm.thread.is_alive(), (
        "the worker outlived shutdown(), so neither caller below would "
        "reach the restart this test is about")

    # An outstanding item so `flush()`'s recovery reaches its restart.
    pm.queue.put(([patient], False))

    with pytest.MonkeyPatch.context() as monkeypatch:
        gate = _Gate(pm, monkeypatch)

        saved = _on_helper(lambda: pm.save_async([patient]))
        flushed = _on_helper(pm.flush)

        assert saved.wait(20), "save_async never returned"
        assert flushed.wait(20), "flush() never returned"

        live = gate.live()
        assert len(live) == 1, (
            f"{len(live)} worker threads were started for one manager: "
            "save_async and flush() both checked thread liveness and both "
            "acted on it, so one queue now has two consumers competing "
            "for a single shutdown sentinel (#313)")
        assert pm.thread.name == WORKER_NAME, (
            f"the manager's worker is named {pm.thread.name!r}, so the "
            "stall census in tests/conftest.py reports it as Thread-N "
            "and a leaked worker cannot be told from any other thread "
            "(#313). Asserted on this manager's own thread rather than "
            "on a process-wide count of named workers: a count reads "
            "state every other test in the run contributes to, which is "
            "the shape that made test_the_abandoned_worker_exits fail "
            "on 3.14t while its fix was working perfectly (#316).")


def test_two_concurrent_saves_against_a_dead_worker_start_only_one(pm):
    """The same collision through one caller, twice (#313).

    `save_async`'s pre-check is unlocked by design -- it is a fast path,
    not the authority. Two saves arriving together must still leave one
    consumer, which is only true if `_start_worker` serialises itself.
    """
    pm.shutdown()
    assert not pm.thread.is_alive()

    with pytest.MonkeyPatch.context() as monkeypatch:
        gate = _Gate(pm, monkeypatch)

        first = _on_helper(lambda: pm.save_async([Patient("P_A", "A^T")]))
        second = _on_helper(lambda: pm.save_async([Patient("P_B", "B^T")]))

        assert first.wait(20) and second.wait(20), (
            "a save_async never returned")

        live = gate.live()
        assert len(live) == 1, (
            f"two concurrent saves started {len(live)} workers on one "
            "queue (#313)")

"""An abandoned manager must be collectable, worker and all (#318).

`PersistenceManager._start_worker` starts its thread with
`target=self._worker`, and a running `Thread` holds its target while a
bound method holds `self`. So a manager with a live worker is immortal --
and with it its `SqliteStore`, that store's sqlite handles, its
audit-writer thread and its sidecar file descriptors. #316 fixed exactly
this shape one level down, for the store's own audit writer; this is the
other half, and #250's `atexit` fix named it in passing as the reason it
could weaken its registration and lose nothing.

**The population is bounded, and was checked against CPython rather than
assumed.** The `finally` in `threading.Thread.run` does `del
self._target, self._args, self._kwargs`, so a worker that has *finished*
does not pin its manager; only a running one does. That is the
population #318 names.

**What this changes, said plainly.** A manager collected with saves still
queued loses them. Today the running worker would drain them; after this
it exits on a dead weakref. The loss is inherent rather than a choice:
the worker cannot write without `manager.store_backend`, and holding the
store strongly is the exact pin being removed. A second weakref to the
store would be added surface for a report that would almost never resolve
-- a `Session` holds both and they die together -- so the report is a log
line only, the same conclusion `_report_abandoned_audit_rows` reached for
audit rows. The newly-lossy population is a `Session` dropped mid-flight
without `close()`, which CLAUDE.md already documents as unsupported.
`close()`, `with`, and a `Session` still referenced at interpreter exit
(where `_flush_at_exit`'s weakref resolves and `shutdown()` runs) are
untouched.
"""
import gc
import logging
import queue
import threading
import time
import weakref

from isocenter.persistence import SqliteStore
from isocenter.persistence_manager import PersistenceManager


def _persistence_workers():
    return [t for t in threading.enumerate()
            if t.name == "PersistenceWorker"]


def _poll(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        gc.collect()
        time.sleep(0.05)
    return predicate()


def test_a_dropped_manager_is_collectable(tmp_path):
    """The manager itself, not merely its thread (#318).

    Modelled on `tests/test_audit_worker_does_not_pin_its_store.py`, with
    the same deliberate difference it records: **`shutdown()` is never
    called.** A shut-down manager is already collectable -- that is what
    `tests/test_persistence_manager.py`'s `atexit` test measures -- and
    it is not the population this is about. An abandoned one is.

    Polled rather than asserted once, because the worker holds a
    transient strong reference between resolving its weakref and dropping
    it again; a single assertion would be flaky in the false-failure
    direction.
    """
    manager = PersistenceManager(SqliteStore(str(tmp_path / "drop.db")))
    ref = weakref.ref(manager)

    del manager
    gc.collect()

    assert _poll(lambda: ref() is None), (
        "the manager is still alive after its last reference was "
        "dropped: its worker holds it through target=self._worker, so "
        "every manager with a running worker stays resident with its "
        "store, that store's sqlite handles, its audit-writer thread "
        "and its sidecar descriptors (#318)")


def test_the_abandoned_worker_exits(tmp_path):
    """A leaked manager must not become a leaked spinning thread (#318).

    Without this the fix could trade one for the other and the test above
    would still pass.

    **The assertion is thread identity, not a count.** #316's changelog
    records that a `len(...) == baseline` comparison failed on 3.14t
    while the fix was working perfectly, because every *other* abandoned
    worker in the run is now also free to exit and the baseline had gone
    to zero by the time the poll ran. 3.14t is a required gate check, so
    that is not a curiosity. Naming this manager's own worker cannot
    drift that way.

    The queue is left empty on purpose: an idle worker is parked in
    `queue.get(timeout=1.0)` and reaches its `queue.Empty` arm, which is
    the arm that must also notice a dead weakref. A loop that resolved
    only after a successful `get()` would spin here forever.
    """
    before = set(_persistence_workers())

    manager = PersistenceManager(SqliteStore(str(tmp_path / "exits.db")))
    started = set(_persistence_workers()) - before
    assert len(started) == 1, (
        "the manager started no named PersistenceWorker of its own, so "
        f"this test would measure nothing: {started}")
    worker = started.pop()

    del manager
    gc.collect()

    assert _poll(lambda: not worker.is_alive()), (
        "the persistence worker is still running with no manager to "
        "serve, so the manager it was holding cannot be collected "
        "either (#318)")


def test_saves_queued_at_collection_are_reported(tmp_path, caplog):
    """What is now lost is said out loud rather than silently (#318).

    A manager collected with saves queued loses them, and today that
    cannot happen only because the manager is immortal. So this is a new
    loss and it needs a channel. Holding the queue strongly -- which is
    safe, and load-bearing: it carries `(list(patients), bool)` tuples,
    entity graphs and never the manager, so there is no cycle back -- is
    what lets the exit path count what it abandons. Same argument
    `_audit_worker_loop` makes for its own queue.

    **The item is kept out of the worker's hands by suppressing its
    *blocking* wait**, rather than by racing it: an idle worker is parked
    in `get()` and consumes anything put behind it. This is the analogue
    of `tests/test_audit_worker_does_not_pin_its_store.py` suppressing
    `_audit_wakeup.set`.

    Two details of that suppression are load-bearing, and the first
    draft got both wrong. **Only the blocking call is suppressed**, so
    the report's own `get_nowait()` drain still sees the item --
    `Queue.get_nowait` is `self.get(block=False)` and would otherwise
    resolve to the patch and count nothing. And **the test waits until
    the patched call has actually been entered** before putting
    anything: the worker is already inside the *real* `get()` when the
    patch is installed, and that in-progress call returns the item
    normally. Without the wait, whether the worker saves the item or
    abandons it is a race, and the draft passed on the abandon side by
    luck.

    It also means the report here comes from the `queue.Empty` arm,
    which is the only place an abandoned *idle* manager is noticed at
    all.
    """
    with caplog.at_level(logging.WARNING):
        manager = PersistenceManager(SqliteStore(str(tmp_path / "lost.db")))
        work_queue = manager.queue
        real_get = work_queue.get
        entered = threading.Event()

        def only_nonblocking(block=True, timeout=None):
            if block:
                entered.set()
                raise queue.Empty
            return real_get(block=False)

        work_queue.get = only_nonblocking
        assert entered.wait(10), (
            "the worker never reached the patched get(), so the item "
            "below would be consumed by its in-progress real one")
        work_queue.put(([], False))

        del manager
        gc.collect()

        assert _poll(lambda: any("#318" in r.message
                                 for r in caplog.records)), (
            "the manager was collected with saves still queued and "
            "nothing said so: close() is the call that promises the "
            "work is on disk, and a session dropped without it now "
            "loses these silently (#318)")

    reported = [r.message for r in caplog.records if "#318" in r.message]
    # Matched against the phrase, not the bare digit. `"1" in m` is
    # satisfied by the `#318` the message ends with, so it would hold for
    # a report of seven saves just as happily as for one -- an assertion
    # about the count that cannot see the count. #316's first draft wrote
    # exactly that.
    assert any("1 queued save" in m for m in reported), (
        f"the report does not say how many saves were dropped: {reported}")

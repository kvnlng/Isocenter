"""An abandoned store must be collectable, worker and all (#316).

`SqliteStore.__init__` starts its audit-writer thread with
`target=self._audit_worker`, and a running `Thread` holds its target. A
bound method holds `self`. So every `SqliteStore` ever constructed was
immortal for as long as its worker ran -- and the worker's only exit
condition is `stop()`, which nothing calls on a store its owner simply
dropped. Measured over one full suite run while #250 was being
instrumented: 149 threads alive at interpreter exit, 147 of them audit
writers, each holding a store, its sqlite handles and its sidecar
descriptors.

#250 fixed the same shape one level up, for `PersistenceManager`, by
weakening the `atexit` registration -- and that fix's own argument names
this one as the remaining half: "a manager with a live worker cannot be
collected anyway, because the worker thread holds `self`".

**Why the exit condition is safe, in one line.** `flush_audit_queue()`
calls `_drain_and_write()` on the *caller's* thread, and
`_drain_and_write` takes `_audit_write_lock` itself. The read barrier
therefore does not depend on the worker existing at all -- the worker
only removes background latency. A worker that exits when its weakref
resolves to `None` cannot weaken the barrier, cannot invert
`_audit_write_lock` -> `_memory_lock` (same locks, same order), and does
not touch the untimed wait's semantics.
"""
import gc
import logging
import threading
import time
import weakref

from isocenter.persistence import SqliteStore


def _audit_workers():
    return [t for t in threading.enumerate() if t.name == "AuditWorker"]


def _poll(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        gc.collect()
        time.sleep(0.05)
    return predicate()


def test_a_dropped_store_is_collectable(tmp_path):
    """The store itself, not merely its thread (#316).

    Modelled on `tests/test_persistence_manager.py`'s
    `test_a_closed_sessions_manager_can_be_collected`, with one
    deliberate difference: **`stop()` is never called.** A stopped store
    is not the population the 149 came from -- an abandoned one is.

    Polled rather than asserted once, because the worker holds a
    transient strong reference between resolving its weakref and
    dropping it again; a single assertion would be flaky in the
    false-failure direction.
    """
    store = SqliteStore(str(tmp_path / "collectable.db"))
    ref = weakref.ref(store)

    del store
    gc.collect()

    assert _poll(lambda: ref() is None), (
        "the store is still alive after its last reference was dropped: "
        "its audit worker holds it through target=self._audit_worker, so "
        "every store ever built stays resident with its sqlite handles "
        "and sidecar descriptors (#316)")


def test_the_abandoned_worker_exits(tmp_path):
    """A leaked store must not become a leaked spinning thread (#316).

    Without this, the fix could trade one for the other and the test
    above would still pass.

    **The assertion is thread identity, not a count**, and the first
    draft got that wrong: a `len(...) == baseline` comparison reads
    process-global state, and every *other* abandoned store in the run
    is now also free to exit. Under the GIL the timing hid it; on
    3.14t the baseline of 5 leftover workers had gone to 0 by the time
    the poll ran, and the test failed while the fix was working
    perfectly. Naming this store's own worker cannot drift that way.
    """
    before = set(_audit_workers())

    store = SqliteStore(str(tmp_path / "exits.db"))
    started = set(_audit_workers()) - before
    assert len(started) == 1, (
        "the store started no named AuditWorker of its own, so this "
        f"test would measure nothing: {started}")
    worker = started.pop()

    del store
    gc.collect()

    assert _poll(lambda: not worker.is_alive()), (
        "the audit worker is still running with no store to serve, so "
        "the store it was holding cannot be collected either (#316)")


def test_rows_queued_at_collection_are_reported(tmp_path, caplog):
    """What is now lost is said out loud rather than silently (#316).

    A store collected with rows still queued loses them. Today that
    cannot happen only because the store is immortal, so this is a new
    loss and it needs a channel. Holding the queue strongly -- which is
    safe, since `threading.Event` references nothing and audit rows are
    plain string tuples carrying no entity graph and no store -- is what
    lets the exit path count what it is dropping.
    """
    with caplog.at_level(logging.WARNING):
        store = SqliteStore(str(tmp_path / "reported.db"))
        # Suppressed so the worker cannot drain the row before the
        # store is dropped; the row is in the queue either way.
        store._audit_wakeup.set = lambda *a, **k: None
        store.log_audit("ERROR", "u", "boom")

        del store
        gc.collect()

        assert _poll(lambda: any("#316" in r.message
                                 for r in caplog.records)), (
            "the store was collected with audit rows still queued and "
            "nothing said so: a silent loss is exactly what the audit "
            "log exists to prevent (#316)")

    reported = [r.message for r in caplog.records if "#316" in r.message]
    assert any("1" in m for m in reported), (
        f"the report does not say how many rows were dropped: {reported}")

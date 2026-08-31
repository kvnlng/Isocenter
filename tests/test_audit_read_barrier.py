"""The audit log must be read-your-writes (#218).

`log_audit()` queues a row and a background worker writes it. The worker
used to `get()` rows into a local `batch` and write them later, so
between those two points a row was in neither the queue nor the table.
`get_audit_errors()` and `get_audit_losses()` "settled" the log by
calling `flush_audit_queue()`, which drained the *queue* and nothing
else -- it could not see the worker's local batch. Both readers could
therefore miss a row that had genuinely been recorded.

`get_audit_losses()` is the one that carries the compliance grade: a
loss scoped `PRIVATE` takes `validation_status` to `REVIEW_REQUIRED`
(#146). A missed row does not merely omit a line from the report, it
awards a passing grade to a run that dropped a private tag.

The fix makes `flush_audit_queue()` a real barrier, backed by a lock the
worker holds across dequeue-and-write. **Every test here builds its own
store or session.** A shared one would recreate the accident these tests
exist to remove: `get_audit_summary()` used to settle the log as a side
effect, so any reader called after it in the same store looked correct
for reasons of its own that had nothing to do with it.
"""

import os
import pickle
import threading
import time

import pytest

from isocenter import Session
from isocenter.persistence import SqliteStore
from isocenter.io_handlers import LOSS_SCOPE_PRIVATE


#: How long an amplified `log_audit_batch` keeps a row in flight. The
#: margin the probes in T2/T3/T5 rest on is this against a barrier-less
#: drain-and-SELECT measured at 0.00 s -- roughly three orders of
#: magnitude. It is a margin, not a proof; T1 is the deterministic test.
IN_FLIGHT_SECONDS = 1.0


def _store(tmp_path, name="audit.db"):
    """A fresh file-backed store. Never shared between tests."""
    return SqliteStore(str(tmp_path / name))


class _Amplifier:
    """Hold one `log_audit_batch` open for `delay` seconds.

    Wraps the store's own method rather than patching shipped code, and
    signals `entered` when the write has *begun*. Tests wait on that
    Event, so the row is provably in flight when the read happens --
    causal, not a `sleep` racing a `sleep`.
    """

    def __init__(self, store, delay=IN_FLIGHT_SECONDS):
        self.store = store
        self.delay = delay
        self.entered = threading.Event()
        self._original = store.log_audit_batch

    def __enter__(self):
        def slow(entries):
            self.entered.set()
            time.sleep(self.delay)
            return self._original(entries)

        self.store.log_audit_batch = slow
        return self

    def __exit__(self, *exc):
        # Restore before the assertions run, so a failing test does not
        # leave a store whose writes take a second.
        self.store.log_audit_batch = self._original
        return False

    def wait(self, timeout=10.0):
        assert self.entered.wait(timeout), (
            "the batch write never started; the amplifier did not take effect")


# --------------------------------------------------------------------------
# T1 -- deterministic. No timing dependence in the failure direction.
# --------------------------------------------------------------------------

def test_flush_blocks_while_the_write_lock_is_held(tmp_path):
    """The barrier waits on `_audit_write_lock`, and then returns rows.

    Both halves are asserted, and both are needed. "It blocked" alone
    would pass against a barrier that blocks and then returns nothing.

    This does not depend on timing in the direction that matters:
    remove the barrier and there is nothing for the reader to block on,
    so it finishes and `is_alive()` is False. The 0.5 s bounds only the
    false-negative side. Coupling to the private attribute name is
    deliberate -- renaming the lock without preserving the guarantee
    should break this loudly.
    """
    store = _store(tmp_path)
    try:
        store.stop()  # no worker racing us for the queue
        store.log_audit("ERROR", "u", "boom")

        store._audit_write_lock.acquire()
        result = []
        reader = threading.Thread(
            target=lambda: result.append(store.get_audit_errors()))
        reader.start()
        try:
            reader.join(timeout=0.5)
            assert reader.is_alive(), (
                "the reader did not wait on the audit write lock; "
                "flush_audit_queue() is not a barrier")
        finally:
            store._audit_write_lock.release()

        reader.join(timeout=10)
        assert not reader.is_alive(), "the reader never unblocked"
        assert len(result[0]) == 1, (
            f"the barrier returned without the row it waited for: {result}")
    finally:
        store.stop()


# --------------------------------------------------------------------------
# T2 -- the amplified probe, one fresh store per reader.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reader_name", [
    "get_audit_errors",
    "get_audit_losses",
    "get_audit_summary",
])
def test_reader_sees_a_row_that_is_still_being_written(tmp_path, reader_name):
    """Each reader is safe *on its own*, with no other reader in front.

    The parametrization is the mechanical enforcement of that: one
    store per case, one read per case. A shared-store version would
    rebuild the very accident it exists to kill -- `generate_report()`
    was correct only because `get_audit_summary()` happened to settle
    the log one line above the two racy reads.

    `get_audit_summary` was already correct here before the fix (it
    joined the worker); it is a guard, not a reproduction. See T5 for
    the case where the join was not enough.
    """
    store = _store(tmp_path, name=f"{reader_name}.db")
    try:
        with _Amplifier(store) as amp:
            if reader_name == "get_audit_losses":
                store.log_audit("DATA_LOSS", "u", "dropped", LOSS_SCOPE_PRIVATE)
            else:
                store.log_audit("ERROR", "u", "boom")
            amp.wait()
            rows = getattr(store, reader_name)()

        assert len(rows) == 1, (
            f"{reader_name}() missed a row that was provably mid-write: {rows}")
    finally:
        store.stop()


# --------------------------------------------------------------------------
# T3 -- the compliance grade, with the accidental settling neutralised.
# --------------------------------------------------------------------------

def test_report_grades_a_private_loss_without_the_summary_settling_it(tmp_path,
                                                                     monkeypatch):
    """A `PRIVATE` loss must reach `REVIEW_REQUIRED` on its own merits.

    `generate_report()` reads the summary, then the errors, then the
    losses. Before #218 the first line joined the worker, so the two
    racy reads below it were protected by a side effect of the line
    above -- which nothing stated and nothing enforced. Reordering or
    deleting that line silently re-opened the window on the grade, and
    every test still passed.

    So the summary is replaced by a stand-in. It must return a
    **non-empty** dict: the grade is `PASS if audit_summary and not
    exceptions and not graded_losses`, so an empty one yields
    `REVIEW_REQUIRED` for the wrong reason and proves nothing.

    This pins the property worth pinning -- that the two reads below
    are correct without the line above -- rather than the line order,
    which the fix makes irrelevant.
    """
    session = Session(str(tmp_path / "grade.db"))
    try:
        store = session.store_backend
        monkeypatch.setattr(store, "get_audit_summary", lambda: {"EXPORT": 1})

        with _Amplifier(store) as amp:
            store.log_audit("DATA_LOSS", "SOP1",
                            "Dropped private tag (0009,0010)",
                            LOSS_SCOPE_PRIVATE)
            store.log_audit("ERROR", "SOP1", "Write failed: boom")
            amp.wait()
            report_path = tmp_path / "report.md"
            session.generate_report(str(report_path))

        text = report_path.read_text()
        assert "REVIEW_REQUIRED" in text, (
            "a run that dropped a private tag was graded PASS:\n" + text)
        assert "Dropped private tag (0009,0010)" in text, (
            "the loss detail never reached the report")
        assert "Write failed: boom" in text, (
            "the error detail never reached the report")
    finally:
        session.close()


# --------------------------------------------------------------------------
# T4 -- the pickle round trip. The only pin on __getstate__ when redact()
# runs in threads; one of nine when it runs in processes.
# --------------------------------------------------------------------------

def test_store_still_pickles_and_the_copy_logs_and_reads(tmp_path):
    """`__getstate__` must drop every audit primitive.

    A lock and an Event both raise `TypeError: cannot pickle
    '_thread.lock' object`, and `__getstate__`/`__setstate__` exist so
    a store can cross a process boundary. Whether anything *else*
    catches a missing `keys_to_remove` entry depends on the build, and
    both halves matter because the CI gate runs both:

    - **On a GIL build (3.12, 3.13, 3.14)** `redact()` reaches
      `_run_on_new_executor`, which builds a `ProcessPoolExecutor`, so
      the bound method `service.execute_redaction_task` pickles
      `RedactionService.store_backend` into every worker. Dropping the
      two entries turns eight other tests red as well as this one.
    - **On the free-threaded build (3.14t)** `_use_threads`
      (`parallel.py:133`) returns True, `redact()` runs in threads, and
      nothing is pickled. Those eight tests pass with the entries
      missing. **This test is the only thing that fails.**

    So it is never redundant: on one of the two gate legs it is the
    sole pin. Measured 2026-08-31 on 3.14.7t -- mutation applied, 19
    passed, this one failed.

    Passes before the fix as well (the baseline pickles today): its
    polarity is against the mutation, not against the parent commit.
    """
    store = _store(tmp_path)
    try:
        revived = pickle.loads(pickle.dumps(store))
    finally:
        store.stop()

    try:
        revived.log_audit("ERROR", "u", "boom")
        rows = revived.get_audit_errors()
        assert len(rows) == 1, (
            f"the round-tripped store does not log and read: {rows}")
    finally:
        revived.stop()


# --------------------------------------------------------------------------
# T5 -- no restart, no leaked worker.
# --------------------------------------------------------------------------

def _audit_workers():
    return [t for t in threading.enumerate() if t.name == "AuditWorker"]


def test_summary_neither_loses_the_row_nor_leaks_a_worker(tmp_path):
    """`get_audit_summary()` past the old two-second join.

    It used to `stop()` the worker: when the batch write outlasted
    `join(timeout=2.0)` the read returned `{}` for a store with an
    `ERROR` row recorded, and the `finally` cleared `_stop_event` and
    started a *second* worker while the first was still alive. The old
    one then re-checked a stop event that had just been cleared and
    never exited. One leaked thread per timed-out read.

    The leak is fixed by deleting the restart, not by improving the
    join -- and the delay here is deliberately longer than that join,
    so an implementation that kept it would still fail.
    """
    store = _store(tmp_path)
    try:
        before = len(_audit_workers())
        with _Amplifier(store, delay=3.0) as amp:
            store.log_audit("ERROR", "u", "boom")
            amp.wait()
            summary = store.get_audit_summary()

        assert summary.get("ERROR") == 1, (
            f"the summary lost a row it had waited out: {summary}")
        assert len(_audit_workers()) == before, (
            "get_audit_summary() started an extra AuditWorker: "
            f"{before} -> {len(_audit_workers())}")
    finally:
        store.stop()


# --------------------------------------------------------------------------
# T6 -- concurrent readers. Deterministic; asserts on counts, not timing.
# --------------------------------------------------------------------------

def test_eight_concurrent_readers_each_see_the_whole_backlog(tmp_path):
    """Concurrent barriers serialise on the lock; none returns short.

    Whichever thread arrives second finds the queue empty *under the
    lock*, which by the invariant means the rows are already in the
    database. Nothing here depends on an operation being atomic by
    virtue of the GIL -- the free-threaded build has none.
    """
    store = _store(tmp_path)
    try:
        store.stop()
        for i in range(500):
            store.log_audit("ERROR", f"u{i}", "boom")

        counts = [None] * 8

        def read(slot):
            counts[slot] = len(store.get_audit_errors())

        threads = [threading.Thread(target=read, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert all(not t.is_alive() for t in threads), "a reader deadlocked"
        assert counts == [500] * 8, counts
    finally:
        store.stop()


# --------------------------------------------------------------------------
# The :memory: shape. #164 was a :memory: deadlock; the barrier adds a
# second lock in front of `_memory_lock` and must not reintroduce one.
# --------------------------------------------------------------------------

def test_memory_store_reads_through_the_barrier(tmp_path):
    """Lock order is `_audit_write_lock` -> `_memory_lock`, never the
    reverse.

    `log_audit_batch` reaches `_get_connection`, which takes
    `_memory_lock` on a `:memory:` store, and the worker calls it while
    holding `_audit_write_lock`. Inverting that -- flushing from inside
    a `_get_connection` block -- would deadlock, which is why all three
    readers keep the flush *above* the `with`.
    """
    store = SqliteStore(":memory:")
    try:
        with _Amplifier(store) as amp:
            store.log_audit("DATA_LOSS", "u", "dropped", LOSS_SCOPE_PRIVATE)
            amp.wait()
            losses = store.get_audit_losses()
        assert len(losses) == 1, losses
        assert losses[0][3] == LOSS_SCOPE_PRIVATE, losses

        store.log_audit("ERROR", "u", "boom")
        assert len(store.get_audit_errors()) == 1
        assert store.get_audit_summary().get("ERROR") == 1
    finally:
        store.stop()
        if os.path.exists(store.sidecar_path):
            os.unlink(store.sidecar_path)

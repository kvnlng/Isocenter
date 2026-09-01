"""A dropped audit row must be loud where a compliance reader looks (#219).

Since #218 the audit worker does not retry a failed batch write: rows
that left the queue and hit an exception in `log_audit_batch` are gone.
That is deliberate -- the old retry held rows in a worker-local `batch`
no reader could see, which is exactly the state #218's barrier exists to
make impossible, and re-enqueueing risks an infinite loop on a
permanently failing write. What was *not* acceptable is the drop being a
`logger.error` line: a compliance report that under-counts the actions
taken must say so where its reader looks, not in a log nobody audits
(#146/#181/#218 lineage).

So the store counts every row a failed `log_audit_batch` dropped --
whatever the exception, including the `sqlite3.Error` the method used to
swallow into a log line on its own (#219's second half) -- and
`generate_report()` files a non-zero count under Exceptions & Errors,
which takes `validation_status` to `REVIEW_REQUIRED`.

Every test builds its own store or session, per the rule stated in
`test_audit_read_barrier.py`: a shared one lets a reader look correct
because of another reader's side effects.
"""

import contextlib
import sqlite3

import pytest

from isocenter import Session
from isocenter.persistence import SqliteStore


@contextlib.contextmanager
def _broken_connection(exc):
    """A `_get_connection` stand-in that fails on entry with `exc`.

    Patching the connection rather than `log_audit_batch` itself keeps
    the shipped method -- and the accounting inside it -- on the tested
    path.
    """
    raise exc
    yield  # pylint: disable=unreachable


class _FailWrites:
    """Make every `_get_connection` on `store` raise `exc` while active."""

    def __init__(self, store, exc):
        self.store = store
        self.exc = exc

    def __enter__(self):
        self.store._get_connection = (
            lambda: _broken_connection(self.exc))
        return self

    def __exit__(self, *exc_info):
        # Restore the bound method by deleting the instance shadow.
        del self.store.__dict__['_get_connection']
        return False


@pytest.mark.parametrize("exc", [
    OSError("disk gone mid-write"),
    sqlite3.OperationalError("database is locked, permanently"),
], ids=["non-sqlite", "sqlite"])
def test_a_failed_batch_write_is_counted_not_merely_logged(tmp_path, exc):
    """Both halves of #219 ride one mechanism.

    The non-sqlite case is the worker path the issue names: the
    exception used to escape `log_audit_batch`, the worker's `except`
    logged, and the rows were silently gone. The sqlite case is the
    spec's §9 item 1: `log_audit_batch` caught `sqlite3.Error` itself
    and only logged -- the same under-report one layer down. After the
    fix neither failure is distinguishable from the outside: the batch
    is dropped, and the drop count says so.
    """
    store = SqliteStore(str(tmp_path / "drops.db"))
    try:
        with _FailWrites(store, exc):
            store.log_audit("ERROR", "u1", "boom")
            store.log_audit("ERROR", "u2", "boom")
            # The barrier drains both rows into the failing write.
            store.flush_audit_queue()

        assert store.get_audit_drops() == 2, (
            "two rows failed to insert and the store does not say so")

        # Dropped means dropped: the write path is healthy again and the
        # rows do not reappear -- a retry would reopen #218's defect.
        store.flush_audit_queue()
        assert store.get_audit_errors() == [], (
            "a dropped row reappeared; the worker is retrying again")
        assert store.get_audit_drops() == 2, (
            "the count moved with no further failed write")
    finally:
        store.stop()


def test_the_write_path_survives_a_failing_batch(tmp_path):
    """A failing write must not wedge the barrier or kill the worker.

    Spec §9 item 2 argued the lock is released on the way out; this
    pins it, together with the accounting: a later row lands, and the
    drop count still names exactly the rows that did not.
    """
    store = SqliteStore(str(tmp_path / "survive.db"))
    try:
        with _FailWrites(store, OSError("disk gone")):
            store.log_audit("ERROR", "u-lost", "boom")
            store.flush_audit_queue()

        store.log_audit("ERROR", "u-kept", "boom")
        rows = store.get_audit_errors()
        assert len(rows) == 1 and "u-kept" not in rows[0][2], rows
        assert store.get_audit_drops() == 1
    finally:
        store.stop()


def test_a_drop_reaches_the_report_and_fails_the_grade(tmp_path):
    """A non-zero drop count must cost the run its PASS.

    The discriminating pair: the same store with the same successful
    `EXPORT` row grades `PASS` when nothing was dropped, and
    `REVIEW_REQUIRED` -- with the drop named under Exceptions & Errors
    -- when one row failed to insert. A `logger.error` line satisfies
    neither assertion, which is the point.
    """
    # Control: no drop, and the grade is PASS -- so the flip below is
    # attributable to the drop alone.
    control = Session(str(tmp_path / "control.db"))
    try:
        control.store_backend.log_audit("EXPORT", "u", "written")
        control_path = tmp_path / "control.md"
        control.generate_report(str(control_path))
        assert "PASS" in control_path.read_text()
    finally:
        control.close()

    session = Session(str(tmp_path / "graded.db"))
    try:
        store = session.store_backend
        with _FailWrites(store, OSError("disk gone mid-write")):
            store.log_audit("EXPORT", "u-lost", "never lands")
            store.flush_audit_queue()
        store.log_audit("EXPORT", "u", "written")

        report_path = tmp_path / "graded.md"
        session.generate_report(str(report_path))
        text = report_path.read_text()

        assert "REVIEW_REQUIRED" in text, (
            "a run whose audit trail is missing a row graded PASS:\n" + text)
        assert "AUDIT_DROP" in text, (
            "the drop never reached the Exceptions & Errors section:\n" + text)
    finally:
        session.close()


def test_a_pickled_store_starts_accounting_afresh(tmp_path):
    """The drop lock must not break pickling (#218's `__getstate__` trap).

    Adding an audit primitive without adding it to `keys_to_remove`
    breaks *every* pickle of a store. The round-tripped copy must both
    write and count.
    """
    import pickle

    store = SqliteStore(str(tmp_path / "pickled.db"))
    try:
        revived = pickle.loads(pickle.dumps(store))
    finally:
        store.stop()

    try:
        with _FailWrites(revived, OSError("disk gone")):
            revived.log_audit("ERROR", "u", "boom")
            revived.flush_audit_queue()
        assert revived.get_audit_drops() == 1
    finally:
        revived.stop()

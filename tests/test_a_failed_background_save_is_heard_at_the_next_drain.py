"""A background `save()` that fails is heard: the next call that drains
the persistence manager runs the save itself and raises what it raises,
and a session that ends with the failure unhealed writes one `ERROR` row
(#941, owner rulings Q3 A and Q4 A, 2026-10-08).

`save()` without `sync=True` queues the write and returns. Measured on
`main` at fd359eb3, on 3.12.14 and 3.14.7t, when that write then failed:

- the worker logged `Background save failed: ...` at ERROR, and that was
  all. `save()`, `persistence_manager.flush()`, `audit()` and `close()`
  returned; no exception, no audit row, nothing at interpreter exit.
- a session whose every background save failed ran `save()`, `audit()`,
  `anonymize(report)`, `save()`, `generate_report()` and `close()` with
  no error, and **graded `PASS`** over a store still holding the source
  Patient ID.
- `save(sync=True)`, `export()` and `compact()` were never silent: each
  runs a save of its own and raises or recovers.

Now `isocenter.persistence_manager` remembers the newest failed background
save. `flush()` -- and so `audit()`, `redact()` and the identity lock,
which drain on entry -- waits for the queue as before and then runs that
save on the caller's thread: a failure that has passed heals there with
nothing said, and one that has not is raised, as the save's own exception.
`save()` and `close()` still do not raise. A session that ends, by
`close()` or at interpreter exit, with the failure never followed by a
successful save writes one `ERROR` audit row keyed `SESSION`, so a later
session's report on that store grades `REVIEW_REQUIRED`.

The failure used here is a value the store refuses by name (#775): a
`set` in an attribute, which fails every time until it is replaced; and
`save_all` made to raise `OSError` once, for the failure that passes.
"""
import datetime
import os
import sqlite3
import subprocess
import gc
import sys
import textwrap
import threading
import weakref

import pytest

from isocenter import Session
from isocenter import persistence_manager as manager_module
from isocenter.builders import DicomBuilder

from support.ct_small_files import write_ct

PID = "PID-941"
TAG = "0018,1150"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path):
    """One CT, ingested and saved."""
    write_ct(tmp_path / "in" / "a.dcm", PID, 941)
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    session.save(sync=True)
    return session


def _instance(session):
    [instance] = [i for p in session.store.patients for st in p.studies
                  for se in st.series for i in se.instances]
    return instance


def _background_save(session):
    """`save()`, and wait until the worker has finished with it. The
    queue's own `join`, not `flush()`: `flush()` is under test."""
    assert session.save() is None
    session.persistence_manager.queue.join()


def _fail_persistently(session):
    """An edit the store refuses every time (#775), saved in the
    background."""
    _instance(session).set_attr(TAG, {1, 2})
    _background_save(session)


class _Counted:
    """`save_all`, counted, raising `OSError` for its first `failures` calls."""

    def __init__(self, store, failures):
        self.real, self.failures, self.calls = store.save_all, failures, 0
        store.save_all = self

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError(28, "No space left on device")
        return self.real(*args, **kwargs)


def _rows(db):
    with sqlite3.connect(str(db)) as conn:
        return conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log "
            "WHERE action_type IN ('ERROR', 'WARNING')").fetchall()


def _stored(db):
    with sqlite3.connect(str(db)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT json_extract(attributes_json, '$.\"0018,1150\"') "
            "FROM instances")]


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    line = next(line for line in path.read_text().splitlines()
                if "Validation Status" in line)
    return next(g for g in ("REVIEW_REQUIRED", "PASS", "FAIL") if f"**{g}**" in line)


def _lock(session):
    session.enable_reversible_anonymization(
        os.path.join(os.path.dirname(session.persistence_file), "isocenter.key"))


def _rule(session):
    """A rule for a machine the session does not hold: `redact()` with no
    rule at all returns before it drains, and this one matches nothing."""
    session.configuration.add_rule("SN-NOT-HERE-941",
                                   redaction_zones=[[0, 8, 0, 8]])


DOORS = [
    pytest.param(lambda s: s.persistence_manager.flush(), None, id="flush"),
    pytest.param(lambda s: s.audit(), None, id="audit"),
    pytest.param(lambda s: s.redact(show_progress=False), _rule, id="redact"),
    pytest.param(lambda s: s.lock_identities(PID), _lock, id="lock"),
    pytest.param(lambda s: s.lock_identities_batch([PID]), _lock, id="lock-batch"),
]


# ---------------------------------------------------------------------------
# Q3 A: the next draining call runs the save and raises what it raises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("door, prepare", DOORS)
def test_a_save_that_still_fails_is_raised_by_the_next_draining_call(
        tmp_path, door, prepare):
    """Red on main: each door returned. The exception is the save's own
    (#775's named `TypeError`), not a wrapper, and no row is written while
    the session lives."""
    session = _session(tmp_path)
    try:
        if prepare is not None:
            prepare(session)
        uid = _instance(session).sop_instance_uid
        _fail_persistently(session)
        with pytest.raises(TypeError) as raised:
            door(session)
        assert f"instance {uid} holds a set at {TAG}" in str(raised.value)
        assert _instance(session).has_unsaved_changes
        assert _rows(tmp_path / "s.db") == []
        # And again: the failure is not forgotten by having been raised.
        with pytest.raises(TypeError):
            session.persistence_manager.flush()
    finally:
        _instance(session).set_attr(TAG, 7)
        session.close()


def test_a_failure_that_has_passed_heals_at_the_drain_with_nothing_said(tmp_path):
    """`save_all` fails once. On main `audit()` returned with the edit
    still unsaved; now its drain runs the save: one call, no exception,
    the edit in the store, no row. Kills a drain that raises without
    retrying (the ruling's option B)."""
    session = _session(tmp_path)
    try:
        counted = _Counted(session.store_backend, failures=1)
        _instance(session).set_attr(TAG, 7)
        _background_save(session)
        assert counted.calls == 1
        assert _stored(tmp_path / "s.db") != [7]
        session.audit()
        assert counted.calls == 2, "the drain ran the failed save, once"
        assert _stored(tmp_path / "s.db") == [7]
        session.persistence_manager.flush()
        assert counted.calls == 2, "a healed failure is not run again"
        assert _rows(tmp_path / "s.db") == []
    finally:
        session.close()
    assert _rows(tmp_path / "s.db") == []


# ---------------------------------------------------------------------------
# Owner ruling of 2026-10-09: the retry saves the session's list as it
# stands at the drain, not the copy the failed save was handed
# ---------------------------------------------------------------------------

OTHER = "PID-941-B"


def _patient_ids(db):
    with sqlite3.connect(str(db)) as conn:
        return sorted(row[0] for row in conn.execute(
            "SELECT patient_id FROM patients"))


def _built(pid):
    """A patient built by hand, never saved."""
    return (DicomBuilder.start_patient(pid, "Added^Later")
            .add_study(f"1.2.941.{len(pid)}", datetime.date(2023, 1, 1))
            .add_series(f"1.2.941.{len(pid)}.1", "CT", 1)
            .add_instance(f"1.2.941.{len(pid)}.1.1", "1.2.840.10008.5.1.4.1.1.2", 1)
            .end_instance().end_series().end_study().build())


def _remove(session, patient):
    session.store.patients.remove(patient)


def _rebind(session, patient):
    """The ordinary way to drop a patient: a new list, assigned."""
    session.store.patients = [p for p in session.store.patients
                              if p is not patient]


@pytest.mark.parametrize("drop", [_remove, _rebind], ids=["remove", "rebind"])
@pytest.mark.parametrize("door, prepare", DOORS)
def test_dropping_the_refused_patient_heals_at_the_next_draining_call(
        tmp_path, door, prepare, drop):
    """Two patients; the second holds a value the store refuses, and the
    background save fails. The caller answers by dropping that patient
    from the session. The next draining call saves the session as it now
    stands: no exception, and the dropped patient's rows are pruned.

    Red while the retry saved the copy the failed save was handed: that
    copy still held the dropped patient, so every door raised its
    `TypeError` for a patient the session no longer had. And red, for
    `rebind`, while the retry read the list *object* `save()` was handed:
    a caller who assigned `session.store.patients` a new list was refused
    for the dropped patient at every call (review of #1015, finding 1)."""
    write_ct(tmp_path / "in" / "b.dcm", OTHER, 942)
    session = _session(tmp_path)
    try:
        if prepare is not None:
            prepare(session)
        [other] = [p for p in session.store.patients if p.patient_id == OTHER]
        [bad] = [i for st in other.studies for se in st.series
                 for i in se.instances]
        bad.set_attr(TAG, {1, 2})
        _background_save(session)
        assert _patient_ids(tmp_path / "s.db") == [PID, OTHER]

        drop(session, other)
        door(session)

        stored = _patient_ids(tmp_path / "s.db")
        assert len(stored) == 1 and OTHER not in stored
        session.persistence_manager.flush()
    finally:
        session.close()
    assert _rows(tmp_path / "s.db") == []


def test_a_patient_added_after_the_failed_save_is_saved_by_the_retry(tmp_path):
    """`save_all` fails once; a patient is then added to the session. The
    drain's save holds it. Red while the retry saved the earlier copy."""
    session = _session(tmp_path)
    try:
        counted = _Counted(session.store_backend, failures=1)
        _instance(session).set_attr(TAG, 7)
        _background_save(session)
        session.store.patients.append(_built("PID-941-ADDED"))
        session.persistence_manager.flush()
        assert counted.calls == 2
        assert _patient_ids(tmp_path / "s.db") == [PID, "PID-941-ADDED"]
        assert _stored(tmp_path / "s.db").count(7) == 1
    finally:
        session.close()
    assert _rows(tmp_path / "s.db") == []


def test_the_retry_is_handed_a_copy_and_prunes_as_the_failed_save_asked(tmp_path):
    """The store is handed a list of its own, never the session's list
    object (a save walks it while the caller may edit theirs), with the
    failed save's `prune_absent_patients`."""
    session = _session(tmp_path)
    try:
        counted = _Counted(session.store_backend, failures=1)
        seen = []
        real = counted.real

        def recording(patients, prune_absent_patients=False):
            seen.append((patients, list(patients), prune_absent_patients))
            return real(patients, prune_absent_patients=prune_absent_patients)

        counted.real = recording
        _instance(session).set_attr(TAG, 7)
        _background_save(session)
        session.persistence_manager.flush()
        [(handed, held, prune)] = seen
        assert handed is not session.store.patients
        assert (held, prune) == (list(session.store.patients), True)
    finally:
        session.close()


def test_a_manager_with_no_session_retries_the_list_object_it_was_handed(tmp_path):
    """A manager built by hand holds no session. What its `flush()`
    retries is the list object `save_async` was handed, read again at the
    retry. A session's own manager reads `session.store.patients`
    instead, which the tests above pin."""
    session = _session(tmp_path)
    bare = manager_module.PersistenceManager(session.store_backend)
    try:
        counted = _Counted(session.store_backend, failures=1)
        mine = list(session.store.patients)
        bare.save_async(mine, prune_absent_patients=True)
        bare.queue.join()
        mine.append(_built("PID-941-MINE"))
        bare.flush()
        assert counted.calls == 2
        assert _patient_ids(tmp_path / "s.db") == [PID, "PID-941-MINE"]
        assert len(session.store.patients) == 1
    finally:
        bare.shutdown()
        session.close()


# ---------------------------------------------------------------------------
# The calls that begin with `audit()` or `redact()` (owner ruling of
# 2026-10-09 on the review's finding 2: they raise, and everything says so)
# ---------------------------------------------------------------------------

CALLERS = [
    pytest.param(lambda s, t: s.anonymize(), id="anonymize-no-argument"),
    pytest.param(lambda s, t: s.redact_by_machine("SN-NOT-HERE-941", [0, 8, 0, 8]),
                 id="redact_by_machine"),
    pytest.param(lambda s, t: s.export(str(t / "out"), check_burned_in=True,
                                       show_progress=False),
                 id="export-check_burned_in"),
    pytest.param(lambda s, t: s.lock_identities([PID]), id="lock-a-list"),
]


@pytest.mark.parametrize("call", CALLERS)
def test_a_call_that_begins_with_a_draining_call_raises_the_failed_save(
        tmp_path, call):
    """`anonymize()` with no findings calls `audit()`,
    `redact_by_machine()` calls `redact()`, the pre-export scan calls
    `audit()` before the export's own save, and `lock_identities()` given
    a list is the batch lock. Each raises the failed save from that inner
    call, before it has written anything. On `main` each returned."""
    session = _session(tmp_path)
    try:
        _lock(session)
        _fail_persistently(session)
        with pytest.raises(TypeError) as raised:
            call(session, tmp_path)
        assert f"holds a set at {TAG}" in str(raised.value)
        assert not os.path.exists(tmp_path / "out")
        assert _rows(tmp_path / "s.db") == []
    finally:
        _instance(session).set_attr(TAG, 7)
        session.close()


def test_anonymize_handed_its_findings_does_not_raise_the_failed_save(tmp_path):
    """`anonymize(report)` makes no draining call of its own, so it
    returns over a standing failure; the next `audit()` raises it."""
    session = _session(tmp_path)
    try:
        report = session.audit()
        _fail_persistently(session)
        assert session.anonymize(report) > 0
        with pytest.raises(TypeError):
            session.audit()
    finally:
        _instance(session).set_attr(TAG, 7)
        session.close()


@pytest.mark.parametrize("lock", [
    lambda s: s.lock_identities(PID),
    lambda s: s.lock_identities_batch([PID]),
], ids=["lock", "lock-batch"])
def test_a_lock_that_raises_at_its_drain_creates_no_key_file(tmp_path, lock):
    """The key file is written only by a lock that writes a token (#813).
    The batch form committed its key and then drained, so a failed
    background save left a new `isocenter.key` beside a session with
    nothing locked (review of #1015, finding 3)."""
    session = _session(tmp_path)
    key = tmp_path / "isocenter.key"
    try:
        _lock(session)
        assert not key.exists()
        _fail_persistently(session)
        with pytest.raises(TypeError):
            lock(session)
        assert not key.exists()
    finally:
        _instance(session).set_attr(TAG, 7)
        session.close()


def test_a_report_generated_before_close_does_not_see_the_failure(tmp_path):
    """A limit, pinned so the entry's sentence is a measurement:
    `generate_report()` is not one of the calls that run the failed save,
    and the row is written when the session ends. So a report generated
    in the same session, with every save failing and no draining call
    after the last one, grades as if the saves had been written."""
    session = _session(tmp_path)
    try:
        report = session.audit()
        _Counted(session.store_backend, failures=10 ** 6)
        session.anonymize(report)
        _background_save(session)
        assert _grade(session, tmp_path) == "PASS"
    finally:
        session.close()
    [(kind, key, _)] = _rows(tmp_path / "s.db")
    assert (kind, key) == ("ERROR", "SESSION")


# ---------------------------------------------------------------------------
# The manager's own order and bookkeeping (review of #1015, finding 6)
# ---------------------------------------------------------------------------

def test_flush_waits_for_the_queue_before_it_runs_the_failed_save(tmp_path):
    """A failed save is remembered, and a second background save is in
    the worker's hands when `flush()` is called. `flush()` must wait for
    it first: it succeeds, which heals the failure, and `flush()` then
    runs nothing. A `flush()` that retried before waiting would call
    `save_all` a third time, beside the worker's."""
    session = _session(tmp_path)
    store = session.store_backend
    real = store.save_all
    calls, parked, release = [], threading.Event(), threading.Event()

    def save_all(*args, **kwargs):
        calls.append(threading.current_thread() is threading.main_thread())
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        if len(calls) == 2:
            parked.set()
            assert release.wait(30)
        return real(*args, **kwargs)

    store.save_all = save_all
    try:
        _instance(session).set_attr(TAG, 7)
        _background_save(session)
        session.save()
        assert parked.wait(30)
        # An Event, not `Thread.is_alive()` after a timed join.
        returned = threading.Event()

        def flush():
            session.persistence_manager.flush()
            returned.set()

        threading.Thread(target=flush, daemon=True).start()
        assert not returned.wait(0.5), "flush() returned with a save still running"
        assert len(calls) == 2, "flush() ran the failed save before it waited"
        release.set()
        assert returned.wait(30)
        assert len(calls) == 2
        assert _stored(tmp_path / "s.db") == [7]
    finally:
        release.set()
        store.save_all = real
        session.close()
    assert _rows(tmp_path / "s.db") == []


def test_the_manager_does_not_keep_a_closed_sessions_graph_alive(tmp_path):
    """The manager outlives its session through its exit handler. Its way
    of reading the session's patients at a retry holds the store weakly,
    so a closed session's patients are not kept until the process ends."""
    session = _session(tmp_path)
    manager = session.persistence_manager
    store = weakref.ref(session.store)
    assert manager._current_patients() is session.store.patients
    session.close()
    del session
    gc.collect()
    assert store() is None
    assert manager._current_patients() is None


def test_a_queued_save_written_at_shutdown_heals_the_failure(tmp_path):
    """`shutdown()` writes a save its worker left on the queue
    (`_drain_queued_saves`). When that write returns it is a later save
    that succeeded, so the session does not end over an unhealed failure
    and no row is written. Driven through the manager's private parts:
    the worker is stopped and an item put on the queue by hand."""
    session = _session(tmp_path)
    manager = session.persistence_manager
    _Counted(session.store_backend, failures=1)
    _instance(session).set_attr(TAG, 7)
    _background_save(session)
    assert manager._failed_save is not None
    manager._shutdown_worker()
    manager.queue.put((list(session.store.patients), True))
    session.close()
    assert _stored(tmp_path / "s.db") == [7]
    assert _rows(tmp_path / "s.db") == []


def test_a_newer_failure_is_not_overwritten_by_the_retrys_own(tmp_path):
    """While the retry of one failed save is running, the worker records
    another. The retry then raises too. What stays remembered is the
    newer failure, not the retry's."""
    session = _session(tmp_path)
    manager = session.persistence_manager
    real = session.store_backend.save_all
    newer = OSError(5, "newer")
    try:
        _Counted(session.store_backend, failures=1)
        _instance(session).set_attr(TAG, 7)
        _background_save(session)

        def retry(*args, **kwargs):
            manager._remember_failed_save(([], False), newer)
            raise OSError(28, "the retry's own")

        session.store_backend.save_all = retry
        with pytest.raises(OSError, match="the retry's own"):
            manager.flush()
        assert manager._failed_save[1] is newer
    finally:
        session.store_backend.save_all = real
        manager._forget_failed_save()
        session.close()


def test_a_failure_already_reported_is_not_reported_again_after_a_retry(tmp_path):
    """One row per failure. `shutdown()` writes the row; the manager can
    be used again afterwards; a `flush()` then runs the save, which fails
    again, and a second `shutdown()` writes no second row for it."""
    session = _session(tmp_path)
    manager = session.persistence_manager
    try:
        _fail_persistently(session)
        manager.shutdown()
        assert len(_rows(tmp_path / "s.db")) == 1
        with pytest.raises(TypeError):
            manager.flush()
        manager.shutdown()
        assert len(_rows(tmp_path / "s.db")) == 1
    finally:
        _instance(session).set_attr(TAG, 7)
        session.close()
    assert len(_rows(tmp_path / "s.db")) == 1


def test_a_later_synchronous_save_clears_the_failure(tmp_path):
    """After `save(sync=True)` returns, a drain runs nothing and raises
    nothing. Kills a memory that is never cleared. And the synchronous
    save runs one save of its own, not the failed one and then its own."""
    session = _session(tmp_path)
    try:
        _fail_persistently(session)
        _instance(session).set_attr(TAG, 7)
        counted = _Counted(session.store_backend, failures=0)
        session.save(sync=True)
        assert counted.calls == 1
        session.audit()
        session.persistence_manager.flush()
        assert counted.calls == 1
        assert _stored(tmp_path / "s.db") == [7]
    finally:
        session.close()
    assert _rows(tmp_path / "s.db") == []


def test_a_later_background_save_that_succeeds_clears_the_failure(tmp_path):
    session = _session(tmp_path)
    try:
        _fail_persistently(session)
        _instance(session).set_attr(TAG, 7)
        counted = _Counted(session.store_backend, failures=0)
        _background_save(session)
        session.persistence_manager.flush()
        assert counted.calls == 1
    finally:
        session.close()
    assert _rows(tmp_path / "s.db") == []


def test_a_synchronous_save_over_a_standing_failure_raises_its_own_save(tmp_path):
    """Control, green on main: `save(sync=True)` raises the save's
    exception from the save it runs itself, once."""
    session = _session(tmp_path)
    try:
        _fail_persistently(session)
        counted = _Counted(session.store_backend, failures=0)
        with pytest.raises(TypeError):
            session.save(sync=True)
        assert counted.calls == 1
    finally:
        _instance(session).set_attr(TAG, 7)
        session.close()


def test_save_and_close_still_do_not_raise(tmp_path):
    """`save()` returns None before the write, and `close()` must release
    the pool and the threads whatever the store holds."""
    session = _session(tmp_path)
    _fail_persistently(session)
    assert session.save() is None
    session.close()


# ---------------------------------------------------------------------------
# Q4 A: a session that ends with the failure unhealed writes one row
# ---------------------------------------------------------------------------

ROW_START = ("A background save() failed and no later save succeeded before "
             "the session ended, so the store does not hold what that save "
             "was asked to write: ")


def test_a_session_closed_over_an_unhealed_failure_writes_one_row(tmp_path):
    """Red on main: no row. One row, keyed `SESSION`, naming the cause;
    a second `close()` writes no second one."""
    session = _session(tmp_path)
    uid = _instance(session).sop_instance_uid
    _fail_persistently(session)
    assert _rows(tmp_path / "s.db") == []
    session.close()
    [(kind, key, details)] = _rows(tmp_path / "s.db")
    assert (kind, key) == ("ERROR", "SESSION")
    assert details.startswith(ROW_START + "TypeError: ")
    assert f"instance {uid} holds a set at {TAG}" in details
    session.close()
    assert len(_rows(tmp_path / "s.db")) == 1


def test_a_failure_raised_at_a_drain_and_never_healed_still_writes_the_row(tmp_path):
    """Hearing the exception is not a successful save."""
    session = _session(tmp_path)
    _fail_persistently(session)
    with pytest.raises(TypeError):
        session.audit()
    session.close()
    [(kind, key, _)] = _rows(tmp_path / "s.db")
    assert (kind, key) == ("ERROR", "SESSION")


@pytest.mark.parametrize("fails", [True, False], ids=["failed", "control"])
def test_a_later_sessions_report_on_that_store_grades_review_required(
        tmp_path, fails):
    """What the row is for. A de-identified, saved store grades `PASS`
    when reopened (the control). With one more background save that
    failed and was never healed before `close()`, the reopened store
    holds none of that save and its report says so by its grade."""
    session = _session(tmp_path)
    session.anonymize(session.audit())
    session.save(sync=True)
    if fails:
        _Counted(session.store_backend, failures=10 ** 6)
        _instance(session).set_attr(TAG, 7)
        _background_save(session)
    session.close()
    with Session(str(tmp_path / "s.db")) as reopened:
        assert _stored(tmp_path / "s.db") != [7]
        assert _grade(reopened, tmp_path) == ("REVIEW_REQUIRED" if fails else "PASS")
    rows = [row for row in _rows(tmp_path / "s.db") if row[0] == "ERROR"]
    assert len(rows) == (1 if fails else 0)
    if fails:
        assert rows[0][2] == ROW_START + "OSError: No space left on device."


CHILD = textwrap.dedent("""
    import sys
    from isocenter import Session

    session = Session(sys.argv[1])
    [instance] = [i for p in session.store.patients for st in p.studies
                  for se in st.series for i in se.instances]
    instance.set_attr("0018,1150", {1, 2})
    session.save()
    keep = session          # still referenced at exit, and never closed
    print("leaving", flush=True)
""")


def test_a_process_that_exits_without_close_writes_the_row(tmp_path):
    """The case no draining call can reach (the spec's 5.1 D): a failed
    background save, then the interpreter exits with no `close()`. Red on
    main: exit code 0, no row. The manager's exit handler runs the same
    shutdown `close()` does."""
    _session(tmp_path).close()
    assert _rows(tmp_path / "s.db") == []
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    done = subprocess.run([sys.executable, str(script), str(tmp_path / "s.db")],
                          capture_output=True, text=True, env=env,
                          cwd=str(tmp_path), timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "leaving" in done.stdout
    [(kind, key, details)] = _rows(tmp_path / "s.db")
    assert (kind, key) == ("ERROR", "SESSION")
    assert details.startswith(ROW_START + "TypeError: ")


# ---------------------------------------------------------------------------
# The manager, directly
# ---------------------------------------------------------------------------

def test_two_failures_are_one_row_and_the_newer_is_the_one_run(tmp_path):
    session = _session(tmp_path)
    _fail_persistently(session)
    _background_save(session)
    session.close()
    assert len(_rows(tmp_path / "s.db")) == 1


def test_the_module_keeps_no_public_name_for_any_of_this():
    """No new public surface: `flush()` and `shutdown()` are the doors."""
    public = {name for name in vars(manager_module.PersistenceManager)
              if not name.startswith("_")}
    assert public == {"flush", "save_async", "has_pending_saves", "shutdown"}

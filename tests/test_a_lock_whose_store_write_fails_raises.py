"""A lock whose store write fails raises, and the audit log says so (#599).

`SqliteStore.update_attributes` caught `sqlite3.Error` and logged it, and
nothing else. So `lock_identities(persist=True)` embedded the token in
memory, failed to write it, and returned `<LockingResult: 1 instances
secured>`: no raise, no audit row, and a reopen found no token (measured
on be0752e, `dev-I2/step0/p599.py`, with a trigger refusing the UPDATE).
The batch forms said the same for every chunk.

It now logs, records one `ERROR` row best-effort, and re-raises the
`sqlite3.Error` (coordinator ruling on triage-098 Q8). **Partial
persistence is not rolled back:** a batch writes patient by patient (or
chunk by chunk), so what was written before the failure stays written,
the failing write stores none of its instances (they hold their tokens in
memory, marked modified, so a later `save()` writes them), and patients
after it are not locked. The tests pin that shape rather than promise a transaction the
chunked API cannot keep.

The store is made to refuse with a `BEFORE UPDATE ON instances` trigger:
a real `sqlite3.Error` from the real UPDATE, not a patched connection,
and one that leaves the audit table writable, so the row can be read.

**Why this file imports what it does.** `isocenter.session` and
`isocenter.persistence` are named, so their probe rows are charged.
"""
import sqlite3

import pytest

from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession

from support.ct_small_files import study_uid, write_ct

SEQ = "0400,0500"
REFUSAL = "refused by the #599 test"


def row_text(count, error):
    """The ERROR row's details, and the log line's text. "This write stored
    none of them", not "held in memory only": under `persist=True` with
    `auto_persist_chunk_size` each instance is written twice, and where
    only the chunk write fails the store already holds what the row would
    have called memory-only (review of #640, P-1)."""
    return (f"update_attributes could not write {count} instance(s) to the "
            f"store, so this write stored none of them: {error}")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _refuse_updates(db, sop_uid=None):
    when = f"WHEN OLD.sop_instance_uid = '{sop_uid}'" if sop_uid else ""
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TRIGGER refuse BEFORE UPDATE ON instances {when} "
                     f"BEGIN SELECT RAISE(ABORT, '{REFUSAL}'); END")


def _allow_updates(db):
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER refuse")


def _stored_tokens(db):
    """{SOP Instance UID: whether its stored attributes carry a token}."""
    with sqlite3.connect(db) as conn:
        return {uid: SEQ in attrs for uid, attrs in conn.execute(
            "SELECT sop_instance_uid, attributes_json FROM instances")}


def _errors(session):
    return [details for _, action, details in session.store_backend.get_audit_errors()
            if action == "ERROR"]


def _ingested(tmp_path, *pids):
    for n, pid in enumerate(pids, start=1):
        write_ct(tmp_path / "in" / f"{n}.dcm", pid, f"599{n}")
    db = str(tmp_path / "s.db")
    session = DicomSession(db)
    session.ingest(str(tmp_path / "in"))
    session.save(sync=True)
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return db, session


def _uid(n):
    return f"{study_uid(f'599{n}')}.1.1"


def test_a_single_lock_whose_write_fails_raises_and_records_it(tmp_path):
    """The `sqlite3.Error` leaves `lock_identities(persist=True)`, one
    ERROR row names the count and the error and nothing of the patient,
    the store holds no token, and memory holds it marked modified, so a
    `save()` once the store accepts writes puts it there. Kills M34 (the
    re-raise dropped: success returned) and M34b (the row dropped)."""
    db, session = _ingested(tmp_path, "PAT-599")
    with session:
        _refuse_updates(db)
        with pytest.raises(sqlite3.Error) as raised:
            session.lock_identities("PAT-599", persist=True)
        assert REFUSAL in str(raised.value)
        errors = _errors(session)
        assert errors == [row_text(1, f"IntegrityError: {REFUSAL}")]
        for secret in ("PAT-599", "Test^PAT-599", _uid(1)):
            assert secret not in errors[0]
        assert _stored_tokens(db) == {_uid(1): False}

        [inst] = [i for st in session.store.patients[0].studies
                  for se in st.series for i in se.instances]
        assert SEQ in inst.sequences and inst.has_unsaved_changes
        _allow_updates(db)
        session.save(sync=True)
    assert _stored_tokens(db) == {_uid(1): True}


SHAPES = {
    # how the batch writes: (the patient whose row is refused, 1-3,
    # stored tokens after, instances in the failed write)
    "persist_per_patient": (2, {_uid(1): True, _uid(2): False, _uid(3): False}, 1),
    "chunks_of_one": (2, {_uid(1): True, _uid(2): False, _uid(3): False}, 1),
    "one_chunk_of_two": (2, {_uid(1): False, _uid(2): False, _uid(3): False}, 2),
    "final_chunk_of_one": (3, {_uid(1): True, _uid(2): True, _uid(3): False}, 1),
}

#: The chunk size each chunked shape passes; `persist_per_patient` passes
#: `persist=True` instead.
CHUNK = {"chunks_of_one": 1, "one_chunk_of_two": 2, "final_chunk_of_one": 2}

BATCH = ["PAT-599-A", "PAT-599-B", "PAT-599-C"]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_batch_whose_write_fails_part_way_keeps_what_was_written(tmp_path, shape):
    """Three patients, the store refusing one patient's row only: the
    error leaves the batch. `persist=True` writes per patient and
    `auto_persist_chunk_size=1` per chunk, so with the middle patient
    refused the first patient's token is in the store and the second's is
    not -- written stays written, nothing is rolled back across writes. One
    write is one transaction, so a chunk of two that fails on its second
    row writes neither, which is what the row's "this write stored none of
    them" says. The error leaves at the failed write, so the third patient,
    after it in Patient ID order, is not locked at all: no token in memory
    and none in the store. With two patients the refused one was last and
    nothing came after it, so R2 and R3 of the review of #640 (the store
    error caught and raised at the end of the batch, in the chunk arm and
    in the per-patient arm) locked and wrote a patient after the failure
    and passed (F-2).

    `final_chunk_of_one` refuses the third patient under chunks of two:
    `[A, B]` is written in the loop and `[C]` is left for the write after
    it, which fails. Every patient holds its token in memory, and the
    store holds A's and B's. No other shape fails that last write, so its
    error swallowed passed every test (K6 of the review of #640, round
    2)."""
    refused, stored, failed = SHAPES[shape]
    db, session = _ingested(tmp_path, *BATCH)
    with session:
        _refuse_updates(db, _uid(refused))
        with pytest.raises(sqlite3.Error):
            if shape == "persist_per_patient":
                session.lock_identities(BATCH, persist=True)
            else:
                session.lock_identities_batch(
                    BATCH, auto_persist_chunk_size=CHUNK[shape])
        memory = {pid: [SEQ in i.sequences for st in p.studies
                        for se in st.series for i in se.instances]
                  for p in session.store.patients for pid in [p.patient_id]}
        assert memory == {pid: [n <= refused]
                          for n, pid in enumerate(BATCH, start=1)}
        assert _stored_tokens(db) == stored
        assert _errors(session) == [row_text(failed, f"IntegrityError: {REFUSAL}")]


def test_the_store_error_escapes_even_when_its_row_cannot_be_written(
        tmp_path, monkeypatch):
    """The row is best-effort. Where recording it fails too, the caller
    still gets the `sqlite3.Error` that failed the write, not the row's
    exception. Kills M34c (the row written outside a `try`)."""
    db, session = _ingested(tmp_path, "PAT-599")
    with session:
        _refuse_updates(db)

        def no_row(*_args, **_kwargs):
            raise OSError("the audit queue is gone")

        monkeypatch.setattr(session.store_backend, "log_audit", no_row)
        with pytest.raises(sqlite3.Error) as raised:
            session.lock_identities("PAT-599", persist=True)
        assert REFUSAL in str(raised.value)
        monkeypatch.undo()


def test_a_working_write_raises_nothing_and_records_no_error(tmp_path):
    """The control: the same lock against a store that accepts it returns
    the instances and leaves no ERROR row."""
    db, session = _ingested(tmp_path, "PAT-599")
    with session:
        result = session.lock_identities("PAT-599", persist=True)
        assert len(result) == 1
        assert _errors(session) == []
    assert _stored_tokens(db) == {_uid(1): True}


def test_update_attributes_raises_what_sqlite_raised(tmp_path):
    """At the store itself, with nothing of the session around it: the
    exception is sqlite's own object, re-raised bare, so nothing is chained
    in front of it."""
    db, session = _ingested(tmp_path, "PAT-599")
    with session:
        _refuse_updates(db)
        instances = [i for st in session.store.patients[0].studies
                     for se in st.series for i in se.instances]
        assert isinstance(session.store_backend, SqliteStore)
        with pytest.raises(sqlite3.IntegrityError) as raised:
            session.store_backend.update_attributes(instances)
        assert raised.value.__cause__ is None

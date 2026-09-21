"""A persisted lock that finds no store row for an instance raises, and the
audit log says so (#641).

`lock_identities(persist=True)` writes through
`SqliteStore.update_attributes`, an `UPDATE ... WHERE sop_instance_uid = ?`.
An instance whose current SOP Instance UID the store holds no row for
matches nothing, which is not an `sqlite3.Error`, so #599's raise could not
see it. Measured on 63a64158: a patient built by hand and never saved
returned `<LockingResult: 1 instances secured>` with no row, no audit row
and no raise; so did an ingested, saved instance whose UID was
regenerated (as `redact()` does) and not yet saved.

The owner ruled #599's shape (#586's comments, Q2 (a) and Q2' (i)):
`update_attributes` counts the rows its write matched and, when that falls
short of the instances given, rolls the write back, writes one `ERROR` row
(counts only) and raises a bare `RuntimeError`. The tokens stay embedded in
memory, marked modified, so a later `save()` stores them; the write stored
none of its instances, those with a row included; in a batch, the writes
before it stay written and the patients after it are not locked. Nothing is
marked persisted (#398).

`test_a_lock_whose_store_write_fails_raises.py` is the `sqlite3.Error`
half and is unchanged: that error still leaves as itself.

**Why this file imports what it does.** `isocenter.session`,
`isocenter.persistence` and `isocenter.entities` are named, so their probe
rows are charged.
"""
import sqlite3
import time
from datetime import date

import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession

from support.ct_small_files import study_uid, write_ct

SEQ = "0400,0500"
TAGS = ["0010,0010", "0010,0020"]


def row_text(count, total):
    """The ERROR row's details, the log line and the exception's text."""
    return (f"update_attributes: {count} of {total} instance(s) have no row in "
            "the store under their SOP Instance UID, so this write stored none "
            "of them; save(sync=True) writes an instance the store does not "
            "hold yet")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _stored_tokens(db):
    """{SOP Instance UID: whether its stored attributes carry a token}."""
    with sqlite3.connect(db) as conn:
        return {uid: SEQ in attrs for uid, attrs in conn.execute(
            "SELECT sop_instance_uid, attributes_json FROM instances")}


def _errors(session):
    return [details for _, action, details in session.store_backend.get_audit_errors()
            if action == "ERROR"]


def _uid(n):
    return f"{study_uid(f'641{n}')}.1.1"


def _ingested(tmp_path, *pids, save=True):
    for n, pid in enumerate(pids, start=1):
        write_ct(tmp_path / "in" / f"{n}.dcm", pid, f"641{n}")
    db = str(tmp_path / "s.db")
    session = DicomSession(db)
    session.ingest(str(tmp_path / "in"))
    if save:
        session.save(sync=True)
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return db, session


def _hand_built_instance(uid, pid):
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = None
    inst.set_attr("0010,0010", f"Test^{pid}")
    inst.set_attr("0010,0020", pid)
    return inst


def _hand_built(session, pid, uid):
    """A patient of one instance, appended to the graph and never saved."""
    patient = Patient(pid, f"Test^{pid}")
    study = Study(f"ST_{pid}", date(2023, 1, 1))
    series = Series(f"SE_{pid}", "CT", 1)
    inst = _hand_built_instance(uid, pid)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return inst


def _instances(session, pid):
    [patient] = [p for p in session.store.patients if p.patient_id == pid]
    return [i for st in patient.studies for se in st.series for i in se.instances]


def _value_free(text, *secrets):
    for secret in secrets:
        assert secret not in text, text


def test_a_hand_built_patient_persisted_raises(tmp_path):
    """A patient never saved has no row. The lock raises `RuntimeError`
    with the count, one ERROR row says the same and nothing of the patient,
    the token is in memory marked modified, and a `save()` puts it in the
    store. Kills M641-1 (no check: success returned), M641-2 (the row
    dropped) and M641-3 (the shortfall logged and not raised)."""
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        inst = _hand_built(session, "PAT-641", "SOP_641")
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities("PAT-641", tags_to_lock=TAGS, persist=True)
        assert str(raised.value) == row_text(1, 1)
        assert _errors(session) == [row_text(1, 1)]
        _value_free(str(raised.value), "PAT-641", "Test^PAT-641", "SOP_641")
        assert SEQ in inst.sequences and inst.has_unsaved_changes
        session.save(sync=True)
    assert _stored_tokens(db) == {"SOP_641": True}


def test_a_regenerated_uid_persisted_raises(tmp_path):
    """An ingested, saved instance whose UID was regenerated, as `redact()`
    does, and not saved since: the live UID has no row and the old one's
    row is not the instance's any more. Raised, and the old row holds no
    token -- the case the issue did not measure."""
    db, session = _ingested(tmp_path, "PAT-641")
    with session:
        [inst] = _instances(session, "PAT-641")
        inst.regenerate_uid()
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities("PAT-641", tags_to_lock=TAGS, persist=True)
        assert str(raised.value) == row_text(1, 1)
        assert _errors(session) == [row_text(1, 1)]
        assert _stored_tokens(db) == {_uid(1): False}


def test_a_shortfall_stores_none_of_the_write(tmp_path):
    """One patient, one series: the ingested instance (which has a row)
    and a hand-built one beside it (which has none). The write is one
    transaction, so the ingested instance's row takes no token either.
    Kills M641-4 (the check after `commit()`: the matched row kept)."""
    db, session = _ingested(tmp_path, "PAT-641")
    with session:
        [ingested] = _instances(session, "PAT-641")
        series = session.store.patients[0].studies[0].series[0]
        series.instances.append(_hand_built_instance("SOP_641_B", "PAT-641"))
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities("PAT-641", tags_to_lock=TAGS, persist=True)
        assert str(raised.value) == row_text(1, 2)
        assert _stored_tokens(db) == {ingested.sop_instance_uid: False}


def test_an_ingested_patient_persists_without_raising(tmp_path):
    """The ordinary path: `ingest()` writes the rows itself, so a lock
    straight after it, with no explicit save, persists and raises nothing.
    Kills M641-5 (a loose or inverted condition that refuses a write every
    row of which matched)."""
    db, session = _ingested(tmp_path, "PAT-641", save=False)
    with session:
        result = session.lock_identities("PAT-641", tags_to_lock=TAGS, persist=True)
        assert len(result) == 1
        assert _errors(session) == []
    assert _stored_tokens(db) == {_uid(1): True}


def test_the_shortfall_escapes_even_when_its_row_cannot_be_written(
        tmp_path, monkeypatch):
    """The row is best-effort. Where recording it fails too, the caller
    still gets the `RuntimeError`, not the row's exception. Kills the row
    written outside a `try`."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        _hand_built(session, "PAT-641", "SOP_641")

        def no_row(*_args, **_kwargs):
            raise OSError("the audit queue is gone")

        monkeypatch.setattr(session.store_backend, "log_audit", no_row)
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities("PAT-641", tags_to_lock=TAGS, persist=True)
        assert str(raised.value) == row_text(1, 1)
        monkeypatch.undo()


BATCH = ["PAT-641-A", "PAT-641-B", "PAT-641-C"]

SHAPES = {
    # how the batch writes: (stored tokens after, instances in the failed write)
    "persist_per_patient": ({_uid(1): True, _uid(2): False}, 1),
    "chunks_of_one": ({_uid(1): True, _uid(2): False}, 1),
    "one_chunk_of_two": ({_uid(1): False, _uid(2): False}, 2),
}

#: The chunk size each chunked shape passes; `persist_per_patient` passes
#: `persist=True` instead.
CHUNK = {"chunks_of_one": 1, "one_chunk_of_two": 2}


@pytest.mark.parametrize("shape", SHAPES)
def test_a_batch_raises_at_the_patient_without_a_row(tmp_path, shape):
    """A and C ingested and saved, B built by hand between them in Patient
    ID order. #599's shape: the error leaves at B's write; A's own write,
    when it was its own, stays stored; a chunk holding A and B stores
    neither; C is not locked at all. Kills M641-6 (the batch catching the
    raise and going on to lock and write C)."""
    stored, failed = SHAPES[shape]
    db, session = _ingested(tmp_path, BATCH[0], BATCH[2])
    with session:
        _hand_built(session, BATCH[1], "SOP_641_B")
        with pytest.raises(RuntimeError) as raised:
            if shape == "persist_per_patient":
                session.lock_identities(BATCH, tags_to_lock=TAGS, persist=True)
            else:
                session.lock_identities_batch(
                    BATCH, auto_persist_chunk_size=CHUNK[shape], tags_to_lock=TAGS)
        assert str(raised.value) == row_text(1, failed)
        memory = {pid: [SEQ in i.sequences for i in _instances(session, pid)]
                  for pid in BATCH}
        assert memory == {BATCH[0]: [True], BATCH[1]: [True], BATCH[2]: [False]}
        assert _stored_tokens(db) == stored
        assert _errors(session) == [row_text(1, failed)]


def test_a_memory_store_rolls_back_too(tmp_path):
    """`:memory:` commits and rolls back on its one shared connection, not
    a connection per write, so the rollback is pinned there too: the
    ingested instance beside a hand-built one takes no token. Kills M641-4
    on the `_memory_conn` branch."""
    write_ct(tmp_path / "in" / "1.dcm", "PAT-641", "6411")
    with DicomSession(":memory:") as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        series = session.store.patients[0].studies[0].series[0]
        series.instances.append(_hand_built_instance("SOP_641_B", "PAT-641"))
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities("PAT-641", tags_to_lock=TAGS, persist=True)
        assert str(raised.value) == row_text(1, 2)
        assert _errors(session) == [row_text(1, 2)]
        with session.store_backend._get_connection() as conn:
            rows = {uid: SEQ in attrs for uid, attrs in conn.execute(
                "SELECT sop_instance_uid, attributes_json FROM instances")}
        assert rows == {_uid(1): False}


def test_update_attributes_counts_rows_written_not_rows_that_exist(tmp_path):
    """At the store itself: two saved instances, and a trigger that skips
    the UPDATE of the second (`RAISE(IGNORE)`, which sqlite does not
    count). Both rows exist; one write did not land. Raised, and the first
    row is unchanged. Kills M641-7 (the shortfall judged by a query for
    which rows exist, which this trigger defeats)."""
    db, session = _ingested(tmp_path, "PAT-641-A", "PAT-641-B")
    with session:
        with sqlite3.connect(db) as conn:
            before = dict(conn.execute(
                "SELECT sop_instance_uid, attributes_json FROM instances"))
            conn.execute(
                "CREATE TRIGGER skip BEFORE UPDATE ON instances "
                f"WHEN OLD.sop_instance_uid = '{_uid(2)}' "
                "BEGIN SELECT RAISE(IGNORE); END")
        instances = _instances(session, "PAT-641-A") + _instances(session, "PAT-641-B")
        for inst in instances:
            inst.set_attr("0010,0010", "Changed^Name")
        assert isinstance(session.store_backend, SqliteStore)
        with pytest.raises(RuntimeError) as raised:
            session.store_backend.update_attributes(instances)
        assert str(raised.value) == row_text(1, 2)
        with sqlite3.connect(db) as conn:
            after = dict(conn.execute(
                "SELECT sop_instance_uid, attributes_json FROM instances"))
        assert after == before


FORMS = {
    # how the lock is asked to persist
    "single": lambda s, pid: s.lock_identities(pid, tags_to_lock=TAGS, persist=True),
    "list": lambda s, pid: s.lock_identities([pid], tags_to_lock=TAGS, persist=True),
    "chunked": lambda s, pid: s.lock_identities_batch(
        [pid], auto_persist_chunk_size=10, tags_to_lock=TAGS),
}


@pytest.mark.parametrize("form", FORMS)
def test_a_lock_after_a_queued_save_waits_for_it(tmp_path, monkeypatch, form):
    """`save()` returns with its write still queued on the persistence
    manager's thread. A persisted lock straight after it found no rows for
    the instances that save was about to write, raised, and recorded an
    ERROR row saying they had none -- a durable, false row (review of #732,
    finding 2: 5 raises in 5 on 20d8e562). The lock now drains a queued
    save before it embeds anything, as `audit()` and `redact()` do (#297),
    so the rows are there: no raise, no ERROR row, and every token in the
    store. The queued save is held for half a second before it writes, so
    the window is not left to scheduling: without the drain the lock
    writes inside it every time. Kills M641-9 (no drain) in each form: one
    patient, the list form (the batch with `persist=True`) and the chunk
    flushes."""
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient = Patient("PAT-641", "Test^PAT-641")
        study = Study("ST_641", date(2023, 1, 1))
        series = Series("SE_641", "CT", 1)
        for n in range(20):
            series.instances.append(_hand_built_instance(f"SOP_641_{n}", "PAT-641"))
        study.series.append(series)
        patient.studies.append(study)
        session.store.patients.append(patient)

        save_all = session.store_backend.save_all

        def held(*args, **kwargs):
            time.sleep(0.5)
            return save_all(*args, **kwargs)

        monkeypatch.setattr(session.store_backend, "save_all", held)
        session.save()
        FORMS[form](session, "PAT-641")
        assert _errors(session) == []
        monkeypatch.undo()
    assert _stored_tokens(db) == {f"SOP_641_{n}": True for n in range(20)}

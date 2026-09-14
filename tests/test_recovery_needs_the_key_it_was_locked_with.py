"""Recovery never creates a key, raises what it cannot do, and prints
nothing (#539, #550); the lock creates the key, exclusively, at 0600 (P2).

Measured on 57400d1 (3.12 and 3.14t, `probes-E/p539.py`):

- a mistyped `key_path` **created** a fresh key at that path, then
  recovery printed "No encrypted identity token found or decryption
  failed.", logged `InvalidToken`, and returned None -- the same answer as
  a wrong key and as a patient that was never locked;
- an unknown ID printed `Patient <the ID given> not found.`, which is
  normally a pseudonym, onto the console (#550).

The owner's ruling on #539: recovery raises when the key file does not
exist and does not create one; locking keeps creating a key when none
exists. So `enable_reversible_anonymization()` no longer writes a key --
it is called before both lock and recovery and cannot know which follows
-- and the first lock does.

Every case asserts nothing reached stdout during the call and that no
message and no log record carries the patient's original or pseudonymous
Patient ID.
"""
import logging
import os
import stat

import pytest
from cryptography.fernet import Fernet

from isocenter import Session
from isocenter.crypto import KeyManager
from isocenter.entities import Patient

from support.ct_small_files import write_ct

LOCKED = "PAT-539"
UNLOCKED = "PAT-NOLOCK"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture(name="store")
def _store(tmp_path):
    """A saved store: LOCKED locked under `real.key` and anonymized,
    UNLOCKED anonymized without a lock. Returns (db, key, locked pseudonym,
    unlocked pseudonym)."""
    write_ct(tmp_path / "in" / "a.dcm", LOCKED, "5391", name="Secret^Name")
    write_ct(tmp_path / "in" / "b.dcm", UNLOCKED, "5392", name="Other^Name")
    db, key = str(tmp_path / "s.db"), str(tmp_path / "real.key")
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(key)
        report = session.audit()
        session.lock_identities(LOCKED)
        session.anonymize(report)
        session.save(sync=True)
        by_token = {("0400,0500" in p.studies[0].series[0].instances[0].sequences): p.patient_id
                    for p in session.store.patients}
    return db, key, by_token[True], by_token[False]


def _identifiers(store):
    return [LOCKED, UNLOCKED, store[2], store[3]]


def _assert_quiet(capsys, caplog, message, store):
    """Nothing printed by the call, and no Patient ID in the message or the log."""
    assert capsys.readouterr().out == ""
    for pid in _identifiers(store):
        assert pid not in message, message
        for record in caplog.records:
            assert pid not in record.getMessage(), record.getMessage()


def _recover(session, capsys, caplog, patient_id, **kwargs):
    capsys.readouterr()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        return session.recover_patient_identity(patient_id, **kwargs)


def test_a_missing_key_path_raises_and_creates_nothing(store, tmp_path, capsys, caplog):
    """(a) Kills recovery calling `load_or_generate_key`, and enable generating."""
    db, _, locked, _ = store
    typo = str(tmp_path / "typo.key")
    with Session(db) as session:
        session.enable_reversible_anonymization(typo)
        assert not os.path.exists(typo), "enable created a key file"
        with pytest.raises(FileNotFoundError) as caught:
            _recover(session, capsys, caplog, locked, restore=False)
        assert typo in str(caught.value)
        _assert_quiet(capsys, caplog, str(caught.value), store)
    assert not os.path.exists(typo), "recovery created a key file"


def test_the_key_is_checked_before_the_patient(store, tmp_path, capsys, caplog):
    """(b) An unknown ID under a missing key is still the missing key. Kills
    the key check moved after the lookup."""
    db, _, _, _ = store
    typo = str(tmp_path / "typo.key")
    with Session(db) as session:
        session.enable_reversible_anonymization(typo)
        with pytest.raises(FileNotFoundError) as caught:
            _recover(session, capsys, caplog, "ANON_NOT_A_REAL_ONE")
        _assert_quiet(capsys, caplog, str(caught.value), store)
        assert "ANON_NOT_A_REAL_ONE" not in str(caught.value)
    assert not os.path.exists(typo)


def test_the_first_lock_creates_the_key_at_0600_and_it_recovers(tmp_path, capsys, caplog):
    """(c) + P2. Enable creates nothing; the lock creates the key, mode 0600
    under a permissive umask; a new session under that key restores. Kills
    enable generating, the lock not ensuring a key, and a key written with
    the umask's mode."""
    write_ct(tmp_path / "in" / "a.dcm", LOCKED, "5393", name="Secret^Name")
    db, key = str(tmp_path / "s.db"), str(tmp_path / "later.key")
    previous = os.umask(0o022)
    try:
        with Session(db) as session:
            session.ingest(str(tmp_path / "in"))
            session.enable_reversible_anonymization(key)
            assert not os.path.exists(key)
            report = session.audit()
            session.lock_identities(LOCKED)
            assert os.path.exists(key)
            assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
            session.anonymize(report)
            session.save(sync=True)
            pseudonym = session.store.patients[0].patient_id
    finally:
        os.umask(previous)
    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        assert _recover(session, capsys, caplog, pseudonym, restore=True) is None
        assert capsys.readouterr().out == ""
        assert session.store.patients[0].patient_id == LOCKED


def test_a_lock_of_no_patient_creates_no_key(tmp_path):
    """A lock that found nobody to lock leaves the disk as it was. Kills the
    single-patient ensure placed before the lookup."""
    write_ct(tmp_path / "in" / "a.dcm", LOCKED, "5394")
    key = str(tmp_path / "k.key")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(key)
        assert list(session.lock_identities("NO-SUCH-ID")) == []
    assert not os.path.exists(key)


def test_a_malformed_key_raises_at_enable(tmp_path):
    """(d) Kills an engine left fully lazy, with nothing built at enable."""
    key = tmp_path / "bad.key"
    key.write_bytes(b"not a fernet key")
    with Session(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError):
            session.enable_reversible_anonymization(str(key))


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_a_malformed_key_at_lock_is_not_reported_as_a_refusal(tmp_path, batch):
    """R1: a key file that is malformed by the time the lock runs raises the
    key's own `ValueError`. Built inside the plan, it was caught as "a value
    no token can hold" (single) or folded into the batch's refusal. Kills
    the lock entry ensuring the key without building the engine."""
    write_ct(tmp_path / "in" / "a.dcm", LOCKED, "5395")
    key = tmp_path / "late.key"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(str(key))
        key.write_bytes(b"not a fernet key")
        with pytest.raises(ValueError):
            session.lock_identities([LOCKED] if batch else LOCKED)
        assert "0400,0500" not in session.store.patients[0].studies[0].series[0].instances[0].sequences


def test_a_wrong_key_raises(store, tmp_path, capsys, caplog):
    """(e) Kills the strict read swallowed back to None."""
    db, _, locked, _ = store
    other = tmp_path / "other.key"
    other.write_bytes(Fernet.generate_key())
    with Session(db) as session:
        session.enable_reversible_anonymization(str(other))
        with pytest.raises(RuntimeError, match="does not decrypt") as caught:
            _recover(session, capsys, caplog, locked, restore=True)
        _assert_quiet(capsys, caplog, str(caught.value), store)
        uid = session.store.patients[0].studies[0].series[0].instances[0].sop_instance_uid
        assert uid not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        assert sorted(p.patient_id for p in session.store.patients) == sorted(store[2:])


def test_a_patient_never_locked_raises(store, capsys, caplog):
    """(f) Kills the strict read swallowed back to None."""
    db, key, _, unlocked = store
    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        with pytest.raises(RuntimeError, match="no encrypted identity token") as caught:
            _recover(session, capsys, caplog, unlocked)
        _assert_quiet(capsys, caplog, str(caught.value), store)


def test_an_unknown_patient_raises_value_error(store, capsys, caplog):
    """(g) Kills the not-found print restored (#550)."""
    db, key, _, _ = store
    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        with pytest.raises(ValueError) as caught:
            _recover(session, capsys, caplog, "ANON_NOT_A_REAL_ONE")
        _assert_quiet(capsys, caplog, str(caught.value), store)
        assert "ANON_NOT_A_REAL_ONE" not in str(caught.value)


def test_a_patient_with_no_instances_raises(tmp_path, capsys, caplog):
    """A hand-built patient with no instance holds no token to read."""
    key = tmp_path / "k.key"
    key.write_bytes(Fernet.generate_key())
    with Session(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(key))
        session.store.patients.append(Patient("ANON_EMPTY", "ANONYMIZED"))
        with pytest.raises(RuntimeError, match="no instances") as caught:
            _recover(session, capsys, caplog, "ANON_EMPTY")
        assert "ANON_EMPTY" not in str(caught.value)
        assert capsys.readouterr().out == ""


def test_restore_false_checks_and_prints_nothing(store, capsys, caplog):
    """The success path under the real key: no exception, no output, and the
    graph untouched."""
    db, key, locked, unlocked = store
    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        assert _recover(session, capsys, caplog, locked, restore=False) is None
        _assert_quiet(capsys, caplog, "", store)
        assert sorted(p.patient_id for p in session.store.patients) == sorted([locked, unlocked])


def test_a_key_created_by_another_process_is_loaded_not_overwritten(tmp_path, monkeypatch):
    """(h) Exclusive create: a key that appears between the check and the
    write is loaded, never overwritten. Kills a create that is not
    exclusive (an existence test followed by `open(..., "wb")`)."""
    path = tmp_path / "race.key"
    manager = KeyManager(str(path))
    theirs = Fernet.generate_key()
    path.write_bytes(theirs)
    monkeypatch.setattr("isocenter.crypto.os.path.exists", lambda _p: False)
    assert manager.load_or_generate_key() == theirs
    assert path.read_bytes() == theirs


def test_load_key_never_creates(tmp_path):
    """`KeyManager.load_key()` raises naming the path and writes nothing."""
    path = tmp_path / "absent.key"
    with pytest.raises(FileNotFoundError, match="absent.key"):
        KeyManager(str(path)).load_key()
    assert not path.exists()


def test_no_lock_or_recovery_log_line_names_a_patient(tmp_path, caplog):
    """Coordinator's extension of P6 to the log file: every record the lock
    and recovery paths write -- missing, locked (single, batch, verbose,
    persisted), refused (single and batch), restored -- carries no Patient
    ID, original or pseudonym. Kills an ID interpolated into any of them."""
    pid = "PAT-LOG-539"
    write_ct(tmp_path / "in" / "a.dcm", pid, "5396", name="Log^Name")
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        with Session(db) as session:
            session.ingest(str(tmp_path / "in"))
            session.enable_reversible_anonymization(key)
            report = session.audit()
            session.lock_identities("NO-SUCH-ID")
            session.lock_identities(["NO-SUCH-ID", pid], persist=True, verbose=True)
            session.lock_identities(pid, persist=True, verbose=True)
            session.anonymize(report)
            pseudonym = session.store.patients[0].patient_id
            with pytest.raises(RuntimeError):
                session.lock_identities(pseudonym)
            with pytest.raises(RuntimeError):
                session.lock_identities([pseudonym, "NO-SUCH-ID"])
            session.recover_patient_identity(pseudonym, restore=True)
    records = [r.getMessage() for r in caplog.records if r.name.startswith("isocenter")]
    assert any("matched no patient" in m for m in records), records
    assert any("Preserving identity" in m for m in records), records
    assert any("Restored identity" in m for m in records), records
    for message in records:
        for identifier in (pid, pseudonym, "NO-SUCH-ID"):
            assert identifier not in message, message


def _mode_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "key file" in r.getMessage()]


def test_a_key_readable_beyond_its_owner_warns_and_keeps_its_mode(tmp_path, caplog):
    """Every key 0.9.7 wrote is typically 0644. The library does not chmod a
    file it did not create; it says so, once, naming the mode and not the
    path (review of #615, P-6). Kills the warning removed, and a chmod."""
    write_ct(tmp_path / "in" / "a.dcm", LOCKED, "5395")
    key = tmp_path / "old.key"
    key.write_bytes(Fernet.generate_key())
    os.chmod(key, 0o644)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        with caplog.at_level(logging.DEBUG, logger="isocenter"):
            session.enable_reversible_anonymization(str(key))
            session.lock_identities(LOCKED)
            session.recover_patient_identity(LOCKED, restore=False)
    [warning] = _mode_warnings(caplog)
    assert "mode 0644" in warning and "chmod 600" in warning, warning
    assert str(tmp_path) not in warning and "old.key" not in warning, warning
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o644


def test_a_key_at_0600_loads_without_a_warning(tmp_path, caplog):
    """The key the first lock creates is loaded by a later session without
    the warning. Kills the warning raised whatever the mode."""
    write_ct(tmp_path / "in" / "a.dcm", LOCKED, "5396")
    key = str(tmp_path / "k.key")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        with caplog.at_level(logging.DEBUG, logger="isocenter"):
            session.enable_reversible_anonymization(key)
            session.lock_identities(LOCKED)
            session.enable_reversible_anonymization(key)
            session.recover_patient_identity(LOCKED, restore=False)
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert _mode_warnings(caplog) == []

"""`lock_identities()` creates `isocenter.key` only when it writes a token
(#813, owner ruling Q1 A, 2026-10-01).

Measured at 7579d4df: four doors created a key file and locked nothing.
`lock_identities(report)` after `anonymize()` (the report names the
source ID, which no patient holds any more); a list of IDs matching no
patient (the single-ID form created none, so the two forms disagreed); an
empty report; and any refused lock, because the key was created before
the plan. After any of them, every later `Session()` in that directory
turned reversible anonymization on by itself (the frozen cwd-key rule).

Now the key is made in memory for planning and written only once every
plan has succeeded and at least one carries a token. If another session
writes a key between the plan and the write, its key is the one on disk,
and every patient is planned again under it, so no token is embedded
under a key nobody holds.
"""
import os
import stat

import pytest
from cryptography.fernet import Fernet

from isocenter import Session
from isocenter.crypto import KeyManager
from isocenter.entities import Patient

from support.ct_small_files import write_ct


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


KEY = "isocenter.key"


def _session(tmp_path, patients=("PA",)):
    for n, pid in enumerate(patients):
        write_ct(tmp_path / "in" / f"{pid}.dcm", pid, 8130 + n,
                 name=f"Name^{pid}")
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    return session


def _no_key_and_no_reversible_session(tmp_path):
    assert not os.path.exists(KEY)
    with Session(str(tmp_path / "other.db")) as other:
        assert other.reversibility_service is None


def _another_session_writes_a_key(path):
    """What a second session's first lock leaves: a key file, created
    exclusively at mode 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(Fernet.generate_key())


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def test_a_report_lock_after_anonymize_creates_no_key(tmp_path):
    with _session(tmp_path) as session:
        report = session.audit()
        session.anonymize(report)
        session.enable_reversible_anonymization()
        assert len(session.lock_identities(report)) == 0
    _no_key_and_no_reversible_session(tmp_path)


def test_a_list_of_unknown_ids_creates_no_key(tmp_path):
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization()
        assert len(session.lock_identities(["NOT-A-PATIENT"])) == 0
    _no_key_and_no_reversible_session(tmp_path)


def test_an_empty_report_creates_no_key(tmp_path):
    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        session.enable_reversible_anonymization()
        report = session.audit()
        assert len(report.findings) == 0
        assert len(session.lock_identities(report)) == 0
    _no_key_and_no_reversible_session(tmp_path)


def test_a_refused_single_lock_creates_no_key(tmp_path):
    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        session.enable_reversible_anonymization()
        pseudonym = session.store.patients[0].patient_id
        with pytest.raises(RuntimeError):
            session.lock_identities(pseudonym)
    _no_key_and_no_reversible_session(tmp_path)


def test_a_refused_batch_creates_no_key(tmp_path):
    """One patient's copies blanked (#761's refusal), one clean."""
    with _session(tmp_path, patients=("PA", "PB")) as session:
        session.enable_reversible_anonymization()
        pa = next(p for p in session.store.patients if p.patient_id == "PA")
        for st in pa.studies:
            for se in st.series:
                for inst in se.instances:
                    inst.set_attr("0010,0020", "")
        with pytest.raises(RuntimeError, match=r"\[1 of 2\]"):
            session.lock_identities(["PA", "PB"])
    _no_key_and_no_reversible_session(tmp_path)


def test_a_patient_with_no_instances_creates_no_key(tmp_path):
    with _session(tmp_path) as session:
        session.store.patients.append(Patient("EMPTY", "Empty^Patient"))
        session.enable_reversible_anonymization()
        assert len(session.lock_identities("EMPTY")) == 0
    _no_key_and_no_reversible_session(tmp_path)


def test_a_lock_that_writes_a_token_creates_the_key(tmp_path):
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization()
        assert len(session.lock_identities("PA")) == 1
        assert stat.S_IMODE(os.stat(KEY).st_mode) == 0o600
        key = open(KEY, "rb").read()
        [inst] = _instances(session)
        token = session.reversibility_service.token_of_ours(inst)
        assert token
        Fernet(key).decrypt(token)
        session.anonymize(session.audit())
        pseudonym = session.store.patients[0].patient_id
        session.recover_patient_identity(pseudonym, restore=True)
        assert [p.patient_id for p in session.store.patients] == ["PA"]


def test_a_key_written_between_plan_and_commit_wins_and_the_lock_replans(
        tmp_path, monkeypatch):
    """Another session links its key into place after this one planned.
    Every token embedded opens under the key on disk."""
    original = KeyManager._commit_planned_key

    def racing(self):
        _another_session_writes_a_key(self.key_path)
        return original(self)

    monkeypatch.setattr(KeyManager, "_commit_planned_key", racing)
    with _session(tmp_path, patients=("PA", "PB")) as session:
        session.enable_reversible_anonymization()
        assert len(session.lock_identities(["PA", "PB"])) == 2
        on_disk = open(KEY, "rb").read()
        tokens = [session.reversibility_service.token_of_ours(i)
                  for i in _instances(session)]
        assert len(tokens) == 2 and all(tokens)
        for token in tokens:
            Fernet(on_disk).decrypt(token)


def test_a_single_lock_racing_a_key_write_replans(tmp_path, monkeypatch):
    original = KeyManager._commit_planned_key

    def racing(self):
        _another_session_writes_a_key(self.key_path)
        return original(self)

    monkeypatch.setattr(KeyManager, "_commit_planned_key", racing)
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization()
        assert len(session.lock_identities("PA")) == 1
        on_disk = open(KEY, "rb").read()
        [inst] = _instances(session)
        Fernet(on_disk).decrypt(session.reversibility_service.token_of_ours(inst))

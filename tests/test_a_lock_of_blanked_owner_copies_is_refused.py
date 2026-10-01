"""A lock whose every instance copy of the Patient ID, or of the Patient's
Name, was blanked while the patient holds one is refused (#761, owner
ruling Q2 A, 2026-10-01).

Measured at 7579d4df: two CT_small files of patient `PA`, each instance's
`0010,0020` set to `''` before the lock. The lock reported
`2 instances secured`; each token held `0010,0020: ''`, because a token
keeps what each instance held (the L8 ruling); the original ID `PA` was
unrecoverable; the restore wrote `''` and the patient became ID-less;
and only a false pre-1.0 WARNING row on the reopen kept the next run
from PASS.

Now the lock raises `RuntimeError`, naming the tag and no value, and
writes no token. Mixed copies (some blank) still lock: recovery takes
the first non-blank token. An ID-less subject still locks: its blank is
the truth.
"""
import pydicom
import pytest

from isocenter import Session

from support.ct_small_files import write_ct

REFUSAL = ("lock_identities: this patient holds a {what}, and every "
           "instance's copy of {tag} is blank")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, patients=(("PA", "Alpha^Ann"),)):
    suffix = 761
    for pid, name in patients:
        for _ in range(2):
            suffix += 1
            write_ct(tmp_path / "in" / f"{suffix}.dcm", pid, suffix, name=name)
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    session.enable_reversible_anonymization()
    return session


def _instances(patient):
    return [i for st in patient.studies for se in st.series for i in se.instances]


def _patient(session, pid):
    return next(p for p in session.store.patients if p.patient_id == pid)


def _tokens(session):
    service = session.reversibility_service
    return [service.token_of_ours(i) for p in session.store.patients
            for i in _instances(p)]


def test_every_patient_id_copy_blanked_is_refused(tmp_path):
    with _session(tmp_path) as session:
        for inst in _instances(_patient(session, "PA")):
            inst.set_attr("0010,0020", "")
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities("PA")
        message = str(raised.value)
        assert REFUSAL.format(what="Patient ID", tag="0010,0020") in message
        assert "PA" not in message.replace("Patient", "")
        assert "ANON_" not in message
        assert "Alpha" not in message
        assert _tokens(session) == [None, None]


def test_every_patient_name_copy_blanked_is_refused(tmp_path):
    with _session(tmp_path) as session:
        for inst in _instances(_patient(session, "PA")):
            inst.set_attr("0010,0010", "")
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities("PA")
        assert REFUSAL.format(what="Patient's Name", tag="0010,0010") in str(raised.value)
        assert "Alpha" not in str(raised.value)
        assert _tokens(session) == [None, None]


def test_one_copy_blanked_still_locks_and_recovers_the_id(tmp_path):
    with _session(tmp_path) as session:
        first = _instances(_patient(session, "PA"))[0]
        first.set_attr("0010,0020", "")
        assert len(session.lock_identities("PA")) == 2
        session.anonymize(session.audit())
        pseudonym = session.store.patients[0].patient_id
        session.recover_patient_identity(pseudonym, restore=True)
        assert [p.patient_id for p in session.store.patients] == ["PA"]


def test_an_id_less_subject_still_locks(tmp_path):
    path = write_ct(tmp_path / "in" / "a.dcm", "X", 7610, name="Alpha^Ann")
    ds = pydicom.dcmread(path)
    ds.PatientID = ""
    ds.save_as(path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization()
        [patient] = session.store.patients
        assert len(session.lock_identities(patient.patient_id)) == 1


def test_a_batch_with_one_such_patient_locks_none(tmp_path):
    with _session(tmp_path, patients=(("PA", "Alpha^Ann"),
                                      ("PB", "Beta^Bob"))) as session:
        for inst in _instances(_patient(session, "PA")):
            inst.set_attr("0010,0020", "")
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(["PA", "PB"])
        message = str(raised.value)
        assert "1 of 2 patients cannot be locked" in message
        assert "[1 of 2] " + REFUSAL.format(what="Patient ID", tag="0010,0020") in message
        assert _tokens(session) == [None] * 4

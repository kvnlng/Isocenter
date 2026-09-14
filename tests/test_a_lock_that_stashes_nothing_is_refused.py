"""A lock that would stash nothing is refused, not reported as secured (#638).

`lock_identities(pid, tags_to_lock=[<a tag no instance carries>])` built
an empty record; `generate_identity_token({})` returns `b""`, and
`embed_identity_token` embeds nothing for it -- yet the lock returned
`<LockingResult: 1 instances secured>` and logged `Secured identity
(tags: []) in 1 instances of one patient.` (measured on be0752e,
`dev-I2/step0/p638.py`). A re-lock of that shape left the earlier token in
place under the same report, and a batch reported the patient secured
beside the ones it did lock. Nothing recoverable was written, and the
session's story said it was (review of #633 round 3, P-4).

The plan now refuses it, in P6 words -- the tags, never a patient or a
value -- and the batch numbers it and locks nobody, as it does every
refusal (#537). Two boundaries, both pinned here:

- **A patient with no instances is not refused.** Its report is already
  `0 instances secured`, which is true whatever the record holds.
- **A record that holds a blank is not empty.** A tag the instance
  carries as `''` is stashed as `''`, as before; blanks are the loss
  checks' concern, not this one's.

The default `tags_to_lock` cannot build an empty record: `0010,0020`
falls back to the patient's own ID where the instance has none (#495),
and a Patient ID is always a string.

**Why this file imports what it does.** `isocenter.session` is named, so
its probe row is charged.
"""
from datetime import date

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

PID_A, PID_B = "PAT-638-A", "PAT-638-B"
NAME_A, NAME_B = "Secret^A", "Secret^B"
SEQ = "0400,0500"
ABSENT = "0010,1000"   # Other Patient IDs: no hand-built instance here carries it


def nothing_to_stash(tags):
    """The #638 refusal for `tags_to_lock`. "This patient's first
    instance", not "this patient": the plan captures from the first
    instance only, so a patient whose later study carries the tag was told
    it held none, and advised to name a tag its instances carry -- the tag
    it had named (review of #640, P-2)."""
    if not tags:
        return ("lock_identities: tags_to_lock names no tag, so there is nothing "
                "to stash and the lock would secure nothing. Name a tag this "
                "patient's first instance carries; the token this call would have "
                "written is unchanged.")
    return ("lock_identities: this patient's first instance holds no value in "
            f"{', '.join(tags)}, every tag tags_to_lock names, so there is "
            "nothing to stash and the lock would secure nothing. Name a tag this "
            "patient's first instance carries; the token this call would have "
            "written is unchanged.")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _hand_patient(session, pid=PID_A, name=NAME_A, instances=1, extra=None):
    patient = Patient(pid, name)
    study = Study(f"ST_{pid}", date(2023, 1, 1))
    series = Series(f"SE_{pid}", "CT", 1)
    for n in range(instances):
        inst = Instance(f"SOP_{pid}_{n}", "1.2.840.10008.5.1.4.1.1.2", n + 1)
        inst.file_path = None
        inst.set_attr("0010,0010", name)
        inst.set_attr("0010,0020", pid)
        for tag, value in (extra or {}).items():
            inst.set_attr(tag, value)
        inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return patient


def _instances(patient):
    return [i for st in patient.studies for se in st.series for i in se.instances]


def _session(tmp_path):
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return session


@pytest.mark.parametrize("tags", [[ABSENT], [ABSENT, "0008,1030"], []],
                         ids=["one_absent_tag", "two_absent_tags", "no_tags"])
def test_a_lock_with_nothing_to_stash_is_refused_and_writes_nothing(tmp_path, tags):
    """The single form raises the exact text, writes no token on any
    instance, and names neither the patient nor its values. Kills M33
    (the refusal dropped, today's "1 instances secured") and M33b (the
    `tags_to_lock` names dropped from the message)."""
    with _session(tmp_path) as session:
        patient = _hand_patient(session, instances=3)
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID_A, persist=True, tags_to_lock=tags)
        assert str(raised.value) == nothing_to_stash(tags)
        assert all(SEQ not in inst.sequences for inst in _instances(patient))
        for secret in (PID_A, NAME_A):
            assert secret not in str(raised.value)


def test_a_patient_whose_later_study_carries_the_tag_is_told_of_its_first_instance(
        tmp_path):
    """The shape P-2 of the review of #640 measured: study 1's instance
    lacks the tag and study 2's carries it. The plan reads the first
    instance, so the lock is refused, and the refusal says what it read --
    the first instance -- rather than that the patient holds no value.
    Nothing is written. Per-instance capture is #583."""
    with _session(tmp_path) as session:
        patient = _hand_patient(session)
        later = _hand_patient(session, pid="PAT-638-LATER", extra={ABSENT: "OTHER-A"})
        session.store.patients.remove(later)
        patient.studies.extend(later.studies)
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID_A, tags_to_lock=[ABSENT])
        assert str(raised.value) == nothing_to_stash([ABSENT])
        assert all(SEQ not in inst.sequences for inst in _instances(patient))
        assert "OTHER-A" not in str(raised.value)


def test_a_relock_with_nothing_to_stash_leaves_the_earlier_token(tmp_path):
    """A re-lock naming only an absent tag, over a token the first lock
    wrote: refused, and the token is the first lock's, byte for byte.
    Before the fix this reported success and left that token in place,
    so the report described a lock that had not happened."""
    with _session(tmp_path) as session:
        patient = _hand_patient(session)
        session.lock_identities(PID_A)
        [inst] = _instances(patient)
        held = bytes(inst.sequences[SEQ].items[0].attributes["0400,0510"])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID_A, tags_to_lock=[ABSENT])
        assert str(raised.value) == nothing_to_stash([ABSENT])
        assert bytes(inst.sequences[SEQ].items[0].attributes["0400,0510"]) == held


def test_a_batch_with_one_patient_stashing_nothing_locks_nobody(tmp_path):
    """B carries the tag, A does not: one numbered refusal, and no token
    for either. Kills the refusal placed in the single path only (outside
    the plan the batch shares)."""
    with _session(tmp_path) as session:
        a = _hand_patient(session)
        b = _hand_patient(session, pid=PID_B, name=NAME_B, extra={ABSENT: "OTHER-B"})
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities([PID_B, PID_A], tags_to_lock=[ABSENT])
        assert str(raised.value) == (
            "lock_identities: 1 of 2 patients cannot be locked as asked, so no "
            "patient was locked. Each is numbered by its place among the patients "
            "found, in Patient ID order. Lock the others without these, and each of "
            f"these as its message says:\n[1 of 2] {nothing_to_stash([ABSENT])}")
        assert all(SEQ not in inst.sequences
                   for inst in _instances(a) + _instances(b))
        for secret in (PID_A, PID_B, NAME_A, NAME_B, "OTHER-B"):
            assert secret not in str(raised.value)


@pytest.mark.parametrize("form", ["single", "batch"])
def test_a_patient_with_no_instances_is_not_refused(tmp_path, form):
    """Nothing to secure is not a false report: a patient with no
    instances locks as `0 instances secured`, whatever its record would
    hold, and a batch beside it locks the other patient. Kills M33c (the
    refusal applied without asking whether there are instances)."""
    with _session(tmp_path) as session:
        hollow = Patient("PAT-638-HOLLOW", "Hollow^P")
        session.store.patients.append(hollow)
        b = _hand_patient(session, pid=PID_B, name=NAME_B, extra={ABSENT: "OTHER-B"})
        if form == "single":
            result = session.lock_identities("PAT-638-HOLLOW", tags_to_lock=[ABSENT])
            assert len(result) == 0
        else:
            result = session.lock_identities(["PAT-638-HOLLOW", PID_B],
                                             tags_to_lock=[ABSENT])
            assert len(result) == 1
            assert session.reversibility_service.recover_original_data(
                _instances(b)[0]) == {ABSENT: "OTHER-B"}


def test_a_tag_a_pass_removed_keeps_its_own_refusal(tmp_path):
    """Placement: the refusal is the plan's last. A lock naming only a tag
    a remediation removed also has nothing to stash, and the more specific
    message -- the pass emptied or removed it, which says why -- is the
    one raised. Kills M33e (the same refusal placed first among the
    plan's checks)."""
    with _session(tmp_path) as session:
        patient = _hand_patient(session)
        [inst] = _instances(patient)
        inst.record_remediation("0008,0050", None)
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID_A, tags_to_lock=["0008,0050"])
        assert str(raised.value) == (
            "lock_identities: this patient holds no value in 0008,0050, which "
            "anonymize() emptied or removed, so there is no original left to "
            "stash. tags_to_lock names no other tag, so there is nothing else to "
            "lock; the token this call would have written is unchanged.")
        assert SEQ not in inst.sequences


def test_a_record_holding_a_blank_is_not_empty(tmp_path):
    """A tag the instance carries as `''` is stashed as `''` and locked,
    as before. The refusal is for a record with no entry at all. Kills
    M33d (the test widened to "every stashed value blank")."""
    with _session(tmp_path) as session:
        patient = _hand_patient(session, extra={ABSENT: ""})
        result = session.lock_identities(PID_A, tags_to_lock=[ABSENT])
        [inst] = _instances(patient)
        assert len(result) == 1
        assert session.reversibility_service.recover_original_data(inst) == {ABSENT: ""}


def test_the_default_tags_still_lock_a_patient_whose_instance_has_none_of_them(
        tmp_path):
    """The default lock falls back to the patient's own ID where the
    instance carries no copy (#495), so its record is never empty."""
    with _session(tmp_path) as session:
        patient = _hand_patient(session)
        [inst] = _instances(patient)
        for tag in ("0010,0010", "0010,0020"):
            del inst.attributes[tag]
        result = session.lock_identities(PID_A)
        assert len(result) == 1
        assert session.reversibility_service.recover_original_data(inst) == {
            "0010,0010": NAME_A, "0010,0020": PID_A}

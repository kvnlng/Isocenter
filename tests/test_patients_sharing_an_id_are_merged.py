"""Two patients that end up with one Patient ID become one patient (#548).

`ingest()` matches patients on the Patient ID in the file, so a new study
arriving under a patient's *original* ID, after that patient was
anonymized, becomes a second `Patient`. `anonymize()` then gives it the
keyed pseudonym the first one already carries, and the graph holds two
objects for one row of the store. The save's scoped deletes turned that
into data loss (`tests/test_save_keeps_rows_memory_holds.py` pins the
save-side guard); this file pins the graph-side half: the session merges
the pair, so memory says what the store will say.

`DicomStore._merge_patients_sharing_an_id()` runs at the two places a
duplicate can be made -- after `apply_remediation` in `anonymize()`, and
after `recover_patient_identity(restore=True)` puts an original ID back
while a raw patient holds it. The patient that was in the session first
survives, keeps its object identity, and takes the other's studies in
order; the other is removed from `store.patients` with its `studies`
emptied, so a caller still holding it sees a detached patient rather
than a second parent of the same studies.

Every test that reaches the merge through a session keeps its rows
through the save guard whether or not the merge ran, so the assertions
here are on the graph. That is the point: the rows cannot see this layer.
"""
import logging

import pytest

from isocenter import Session
from isocenter.entities import (JITTER_SCHEME_KEYED, JITTER_SCHEME_UNKEYED,
                                Instance, Patient, PhiStatus, Series, Study)
from isocenter.store import DicomStore

from support.ct_small_files import row_counts, study_uid, write_ct

MODES = ["threads", "processes"]


@pytest.fixture
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _study(uid):
    st = Study(uid, "20230101")
    se = Series(f"{uid}.1", "CT", 1)
    se.instances.append(Instance(f"{uid}.1.1", "1.2.840.10008.5.1.4.1.1.2", 1))
    st.series.append(se)
    return st


def _hand_pair(first_status=None, second_status=None, pid="X-548",
               names=("Doe^Jane", "Doe^Jane")):
    """A store holding two patients with one ID, each with one study.

    Each status is recorded and then persisted, as a reload leaves it.
    """
    store = DicomStore()
    pair = []
    for name, status, uid in zip(names, (first_status, second_status),
                                 ("S1", "S3")):
        p = Patient(pid, name)
        p.studies.append(_study(uid))
        if status is not None:
            p.record_phi_status(status)
        p.mark_subtree_persisted()
        pair.append(p)
    store.patients.extend(pair)
    return store, pair[0], pair[1]


def _two_passes_in_one_session(tmp_path):
    """#548's same-session flow up to pass 2's audit.

    Returns (session, report, stored patient, re-ingested patient).
    """
    write_ct(tmp_path / "first" / "a.dcm", "PAT-001", "1")
    write_ct(tmp_path / "first" / "b.dcm", "PAT-002", "2")
    write_ct(tmp_path / "second" / "c.dcm", "PAT-001", "3")
    session = Session(str(tmp_path / "store.db"))
    session.ingest(str(tmp_path / "first"))
    session.anonymize(session.audit())
    session.save(sync=True)
    session.ingest(str(tmp_path / "second"))

    def holding(suffix):
        return next(p for p in session.store.patients
                    if study_uid(suffix) in
                    [s.study_instance_uid for s in p.studies])

    stored, arrived = holding("1"), holding("3")
    assert stored is not arrived
    report = session.audit()
    return session, report, stored, arrived


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_anonymize_merges_a_reingested_patient_into_the_stored_one(
        tmp_path, mode):
    session, report, stored, arrived = _two_passes_in_one_session(tmp_path)
    try:
        session.anonymize(report)

        ids = [p.patient_id for p in session.store.patients]
        assert len(ids) == len(set(ids)) == 2
        survivor = next(p for p in session.store.patients
                        if p.patient_id == stored.patient_id)
        assert survivor is stored
        assert [s.study_instance_uid for s in survivor.studies] == [
            study_uid("1"), study_uid("3")]
        assert not any(p is arrived for p in session.store.patients)
        assert arrived.studies == []
    finally:
        session.close()


@pytest.mark.parametrize("statuses, merged", [
    ((PhiStatus.CLEARED, PhiStatus.REMEDIATED), PhiStatus.REMEDIATED),
    ((PhiStatus.REMEDIATED, PhiStatus.IDENTIFIED), PhiStatus.IDENTIFIED),
    ((PhiStatus.CLEARED, None), PhiStatus.UNSCANNED),
    ((PhiStatus.IDENTIFIED, None), PhiStatus.IDENTIFIED),
    ((PhiStatus.REMEDIATED, PhiStatus.CLEARED), PhiStatus.REMEDIATED),
])
def test_the_merged_status_is_the_most_conservative_member(statuses, merged):
    """IDENTIFIED > UNSCANNED > REMEDIATED > CLEARED.

    The merged patient holds both members' identifiers-or-not, so it can
    claim no more than the least-assured of them. `None` leaves a member
    never scanned, which reads UNSCANNED.
    """
    store, survivor, _ = _hand_pair(*statuses)
    store._merge_patients_sharing_an_id()
    assert survivor.phi_status is merged


def test_the_merged_status_is_recorded_so_the_save_writes_it():
    """A merged status the survivor did not already carry is a change.

    Read back at once -- not UNSCANNED, which is what a status recorded at
    a revision the survivor then left would read -- and it leaves the
    survivor with unsaved changes, so the row learns it.
    """
    store, survivor, _ = _hand_pair(PhiStatus.CLEARED, PhiStatus.REMEDIATED)
    assert not survivor.has_unsaved_changes
    store._merge_patients_sharing_an_id()
    assert survivor.phi_status is PhiStatus.REMEDIATED
    assert survivor.has_unsaved_changes


def test_a_merge_that_changes_no_status_leaves_the_survivor_clean():
    """Moving studies is D2's job on the rows; the patient row is unchanged."""
    store, survivor, _ = _hand_pair(PhiStatus.REMEDIATED, PhiStatus.CLEARED)
    store._merge_patients_sharing_an_id()
    assert not survivor.has_unsaved_changes


def test_a_merge_across_jitter_schemes_refuses_and_changes_nothing():
    """A patient row holds one date-offset scheme; either choice re-offsets.

    Unreachable from `anonymize()` -- a keyed pseudonym and an unkeyed one
    are different lengths -- and reachable through a restore onto an ID
    a patient of the other scheme holds.
    """
    store, first, second = _hand_pair(PhiStatus.REMEDIATED,
                                      PhiStatus.IDENTIFIED)
    first._jitter_scheme = JITTER_SCHEME_UNKEYED
    second._jitter_scheme = JITTER_SCHEME_KEYED
    drained = []

    with pytest.raises(RuntimeError) as raised:
        store._merge_patients_sharing_an_id(drain=lambda: drained.append(1))

    message = str(raised.value)
    assert message == (
        "2 patients in this session share a Patient ID but were "
        "de-identified under different date-offset schemes; merging them "
        "would give their dates two offsets")
    assert "X-548" not in message
    assert store.patients == [first, second]
    assert [s.study_instance_uid for s in first.studies] == ["S1"]
    assert [s.study_instance_uid for s in second.studies] == ["S3"]
    assert first.phi_status is PhiStatus.REMEDIATED
    assert second.phi_status is PhiStatus.IDENTIFIED
    assert drained == []


def test_the_merge_drains_pending_saves_before_it_mutates(tmp_path,
                                                          monkeypatch):
    """A queued save snapshots the *list*, not the objects in it.

    Without the drain, a snapshot still holding the duplicate would walk
    it after its studies were moved, and upsert its name and status over
    the survivor's row. So the flush has to see the graph as it was.
    """
    session, report, stored, arrived = _two_passes_in_one_session(tmp_path)
    try:
        before = [(p, list(p.studies)) for p in session.store.patients]
        seen = []
        real_flush = session.persistence_manager.flush

        def recording_flush():
            seen.append([(p, list(p.studies))
                         for p in session.store.patients] == before)
            return real_flush()

        monkeypatch.setattr(session.persistence_manager, "flush",
                            recording_flush)
        session.anonymize(report)

        assert seen, "the merge never drained the persistence queue"
        assert seen[0] is True, "the graph was mutated before the drain"
        assert arrived.studies == []
    finally:
        session.close()


def test_no_duplicate_no_drain(tmp_path, monkeypatch):
    """`anonymize(findings)` did not drain before, and without a duplicate
    it still does not."""
    write_ct(tmp_path / "in" / "a.dcm", "PAT-001", "1")
    with Session(str(tmp_path / "store.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        calls = []
        real_flush = session.persistence_manager.flush
        monkeypatch.setattr(session.persistence_manager, "flush",
                            lambda: (calls.append(1), real_flush())[1])
        session.anonymize(report)
        assert calls == []


def test_names_that_disagree_keep_the_survivors_and_log_no_ids(caplog):
    store, survivor, dropped = _hand_pair(
        PhiStatus.REMEDIATED, PhiStatus.REMEDIATED,
        names=("Survivor^Name", "Dropped^Name"))

    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        store._merge_patients_sharing_an_id()

    assert store.patients == [survivor]
    assert survivor.patient_name == "Survivor^Name"
    assert [s.study_instance_uid for s in survivor.studies] == ["S1", "S3"]
    assert dropped.studies == []
    messages = [r.getMessage() for r in caplog.records]
    assert [r for r in caplog.records if r.levelno == logging.WARNING], messages
    assert any("Merged 1 patient" in m and "1 study moved" in m
               for m in messages), messages
    for m in messages:
        for secret in ("X-548", "Survivor^Name", "Dropped^Name"):
            assert secret not in m, m


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_restore_onto_an_id_a_raw_patient_holds_merges_them(tmp_path, mode):
    """A raw study for PAT-001 ingested before PAT-001's identity is restored.

    Measured on 1c41e5e: (2, 2, 2, 2) after the ingest and (1, 0, 0, 0)
    after the restore and `save()`, with the reopened patient holding no
    studies. The two are one subject by construction -- the decrypted
    original equals the raw ID -- so they merge.
    """
    write_ct(tmp_path / "first" / "a.dcm", "PAT-001", "1")
    write_ct(tmp_path / "second" / "c.dcm", "PAT-001", "3")
    db = str(tmp_path / "store.db")
    key = str(tmp_path / "isocenter.key")
    with Session(db) as session:
        session.ingest(str(tmp_path / "first"))
        session.enable_reversible_anonymization(key)
        report = session.audit()
        session.lock_identities("PAT-001")
        session.anonymize(report)
        session.save(sync=True)

    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        [stored] = session.store.patients
        pseudonym = stored.patient_id
        session.ingest(str(tmp_path / "second"))
        assert len(session.store.patients) == 2
        session.recover_patient_identity(pseudonym, restore=True)

        assert len(session.store.patients) == 1
        assert session.store.patients[0] is stored
        assert stored.patient_id == "PAT-001"
        assert [s.study_instance_uid for s in stored.studies] == [
            study_uid("1"), study_uid("3")]
        session.save(sync=True)

    assert row_counts(db) == (1, 2, 2, 2)
    with Session(db) as session:
        [patient] = session.store.patients
        assert patient.patient_id == "PAT-001"
        assert len(patient.studies) == 2


def test_a_restore_across_jitter_schemes_refuses_before_it_restores(
        tmp_path, monkeypatch):
    """The refusal comes before the restore writes anything, not after.

    The merge itself refuses a mixed group, but by the time it runs in
    `recover_patient_identity` every instance already holds the original
    identifiers and the patient its original ID: a refusal there leaves
    the graph holding two patients with one ID under two schemes. So the
    restore asks first, as if the patient already held the ID it is about
    to get back. The stored patient is re-classed unkeyed by hand, as a
    pre-0.9.7 store would load it.
    """
    write_ct(tmp_path / "first" / "a.dcm", "PAT-001", "1")
    write_ct(tmp_path / "second" / "c.dcm", "PAT-001", "3")
    db = str(tmp_path / "store.db")
    key = str(tmp_path / "isocenter.key")
    with Session(db) as session:
        session.ingest(str(tmp_path / "first"))
        session.enable_reversible_anonymization(key)
        report = session.audit()
        session.lock_identities("PAT-001")
        session.anonymize(report)
        session.save(sync=True)

    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        [stored] = session.store.patients
        pseudonym = stored.patient_id
        session.ingest(str(tmp_path / "second"))
        stored._jitter_scheme = JITTER_SCHEME_UNKEYED
        [inst] = [i for st in stored.studies for se in st.series
                  for i in se.instances]
        revisions = (stored._revision, inst._revision)
        attributes = dict(inst.attributes)
        drained = []
        monkeypatch.setattr(session.persistence_manager, "flush",
                            lambda: drained.append(1))

        with pytest.raises(RuntimeError) as raised:
            session.recover_patient_identity(pseudonym, restore=True)

        assert str(raised.value) == (
            "2 patients in this session share a Patient ID but were "
            "de-identified under different date-offset schemes; merging "
            "them would give their dates two offsets")
        assert stored.patient_id == pseudonym
        untouched = inst.attributes == attributes
        assert untouched
        assert (stored._revision, inst._revision) == revisions
        assert len(session.store.patients) == 2
        assert drained == []

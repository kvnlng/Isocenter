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
while a raw patient holds it -- and at `audit()`'s entry, for a duplicate
built in user code, which the scan cannot tell apart (#563, at the end). The patient that was in the session first
survives, keeps its object identity, and takes the other's studies in
order; the other is removed from `store.patients` with its `studies`
emptied, so a caller still holding it sees a detached patient rather
than a second parent of the same studies.

Every test that reaches the merge through a session keeps its rows
through the save guard whether or not the merge ran, so the assertions
here are on the graph. That is the point: the rows cannot see this layer.
"""
import logging
import sqlite3

import pytest

from isocenter import Session
from isocenter.entities import (JITTER_SCHEME_KEYED, JITTER_SCHEME_UNKEYED,
                                Instance, Patient, PhiStatus, Series, Study)
from isocenter.privacy import _replacement_uid_for
from isocenter.store import DicomStore

from support.ct_small_files import row_counts, study_uid, write_ct
from support.store_secret import secret_of


def _replaced(db, suffix):
    """Study `suffix`'s UID as `anonymize()` replaced it (#544)."""
    return _replacement_uid_for(study_uid(suffix), secret_of(db))

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

    def holding(uid):
        return next(p for p in session.store.patients
                    if uid in [s.study_instance_uid for s in p.studies])

    # Study 1 carries its replacement since pass 1 (#544); study 3 has
    # not been through a pass yet.
    stored = holding(_replaced(tmp_path / "store.db", "1"))
    arrived = holding(study_uid("3"))
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
        db = tmp_path / "store.db"
        assert [s.study_instance_uid for s in survivor.studies] == [
            _replaced(db, "1"), _replaced(db, "3")]
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
    # The leg that is a privacy claim: a patient made partly of data
    # never scanned, or edited since, does not read REMEDIATED.
    ((PhiStatus.REMEDIATED, None), PhiStatus.UNSCANNED),
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
    assert any("Merged 1 patient into" in m and "1 study moved" in m
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
        # The stored study was replaced by the pass; the raw one was never
        # anonymized, and a restore puts back no UID.
        assert [s.study_instance_uid for s in stored.studies] == [
            _replaced(db, "1"), study_uid("3")]
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


@pytest.mark.parametrize("collision", [False, True],
                         ids=["plain", "onto-a-raw-patient"])
def test_a_restore_drains_pending_saves_before_it_writes(tmp_path,
                                                         monkeypatch,
                                                         collision):
    """The restore writes onto every instance and the patient; a queued
    save could be walking them while it does (#297, as `audit()` and
    `redact()` drain on entry).

    The merge drains too, but only on a collision and only after the
    restore has written, so it cannot stand in for this one: the first
    flush has to see the patient still under its pseudonym.
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
        if collision:
            session.ingest(str(tmp_path / "second"))
        [inst] = [i for st in stored.studies for se in st.series
                  for i in se.instances]
        attributes = dict(inst.attributes)
        seen = []
        real_flush = session.persistence_manager.flush

        def recording_flush():
            seen.append((stored.patient_id, inst.attributes == attributes))
            return real_flush()

        monkeypatch.setattr(session.persistence_manager, "flush",
                            recording_flush)
        session.recover_patient_identity(pseudonym, restore=True)

        assert seen, "the restore never drained the persistence queue"
        assert seen[0] == (pseudonym, True), (
            "the restore wrote before the drain")
        assert stored.patient_id == "PAT-001"
        assert inst.attributes != attributes


# `audit()` merges too (#563). Two `Patient` objects with one ID reached
# `anonymize()` unmerged: `_rehydrate_findings` binds a patient finding by
# Patient ID, so every finding raised on either landed on the last object,
# and both shared the dedup key `(uid, path, attr)`, so one pair applied.
# Measured on 57400d1 (threads and processes, 3.12 and 3.14t,
# `probes-E/p563.py`): `[('X-DUP', 'identified'), ('ANON_...', 'remediated')]`,
# and a reopen kept both.
def _identified_study(uid, pid):
    st = _study(uid)
    inst = st.series[0].instances[0]
    inst.set_attr("0010,0010", "Doe^Jane")
    inst.set_attr("0010,0020", pid)
    return st


def _session_with_a_duplicate_pair(db, pid="X-DUP"):
    session = Session(db)
    first, second = Patient(pid, "Doe^Jane"), Patient(pid, "Doe^Jane")
    first.studies.append(_identified_study("1.2.3.563.1", pid))
    second.studies.append(_identified_study("1.2.3.563.2", pid))
    session.store.patients.extend([first, second])
    return session, first, second


def _secret_rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT count(*) FROM project_secret").fetchone()[0]


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_audit_merges_patients_sharing_an_id(tmp_path, mode):
    """Kills the merge removed from `audit()`: without it the first object
    keeps X-DUP and reads IDENTIFIED, and a reopen holds two patients."""
    db = str(tmp_path / "s.db")
    session, first, second = _session_with_a_duplicate_pair(db)
    try:
        report = session.audit()
        assert session.store.patients == [first]
        assert [s.study_instance_uid for s in first.studies] == [
            "1.2.3.563.1", "1.2.3.563.2"]
        assert second.studies == []
        assert all(f.entity is first for f in report.findings
                   if f.entity_type == "Patient")
        session.anonymize(report)
        [patient] = session.store.patients
        assert patient.patient_id.startswith("ANON_")
        assert patient.phi_status is PhiStatus.REMEDIATED
        session.save(sync=True)
    finally:
        session.close()
    with Session(db) as session:
        assert [len(p.studies) for p in session.store.patients] == [2]


def test_audit_refuses_a_merge_across_schemes_before_the_secret(tmp_path):
    """The merge's own refusal, raised before a project secret is created
    and before the audited policy is recorded, with the graph untouched.
    Kills the merge placed after `_project_secret_for_use()` (a secret row)
    and after `_audited_phi_tags` is assigned (the lock would then judge a
    blank name by a policy no audit resolved)."""
    db = str(tmp_path / "s.db")
    session, first, second = _session_with_a_duplicate_pair(db)
    try:
        first._jitter_scheme = JITTER_SCHEME_UNKEYED
        second._jitter_scheme = JITTER_SCHEME_KEYED
        with pytest.raises(RuntimeError) as raised:
            session.audit()
        assert str(raised.value) == (
            "2 patients in this session share a Patient ID but were "
            "de-identified under different date-offset schemes; merging them "
            "would give their dates two offsets")
        assert session.store.patients == [first, second]
        assert _secret_rows(db) == 0
        assert session._audited_phi_tags is None
    finally:
        session.close()


def test_an_invalid_policy_is_refused_before_the_merge(tmp_path):
    """`validate_phi_policy`'s refusal leaves the pair as it was. Kills the
    merge placed before the policy is validated."""
    db = str(tmp_path / "s.db")
    session, first, second = _session_with_a_duplicate_pair(db)
    try:
        session.configuration.phi_tags = {"0010,0020": {"action": "REMOVE"}}
        with pytest.raises(ValueError):
            session.audit()
        assert session.store.patients == [first, second]
        assert [len(p.studies) for p in (first, second)] == [1, 1]
    finally:
        session.close()


def test_a_safe_export_merges_through_its_audit(tmp_path):
    """`export(check_burned_in=True)` runs `audit()`, so a duplicate pair is
    merged there, before the plan is built; the export withholds the
    identified instances and does not trip over the emptied duplicate."""
    write_ct(tmp_path / "in" / "a.dcm", "PAT-563A", "5631")
    write_ct(tmp_path / "in" / "b.dcm", "PAT-563B", "5632")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        first, second = session.store.patients
        second.patient_id = first.patient_id
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 check_burned_in=True)
        assert session.store.patients == [first]
        assert len(first.studies) == 2
        assert summary.written == 0

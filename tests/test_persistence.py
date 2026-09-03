import pytest
import os
import sqlite3
from isocenter.persistence import SqliteStore
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.session import DicomSession

@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "test_persistence.db"
    s = SqliteStore(str(db_file))
    yield s
    # Cleanup happens automatically by pytest tmp_path (creates new dir per test)


def test_schema_init(store):
    with sqlite3.connect(store.db_path) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]

    assert "patients" in table_names
    assert "studies" in table_names
    assert "series" in table_names
    assert "instances" in table_names
    assert "audit_log" in table_names

def test_crud_hierarchy(store):
    # Create Hierarchy
    p = Patient("P1", "Patient One")
    st = Study("S1", "20230101")
    se = Series("SE1", "CT", 1)
    inst = Instance("I1", "1.2.3", 1, file_path="/tmp/test.dcm")

    p.studies.append(st)
    st.series.append(se)
    se.instances.append(inst)

    # Save
    store.save_all([p])

    # Load
    loaded_patients = store.load_all()
    assert len(loaded_patients) == 1
    p2 = loaded_patients[0]

    assert p2.patient_id == "P1"
    assert len(p2.studies) == 1
    st2 = p2.studies[0]

    assert st2.study_instance_uid == "S1"
    assert len(st2.series) == 1
    se2 = st2.series[0]

    assert len(se2.instances) == 1
    inst2 = se2.instances[0]
    assert inst2.sop_instance_uid == "I1"
    assert inst2.file_path == "/tmp/test.dcm"

def test_audit_log(store):
    store.log_audit("TEST_ACTION", "UID_123", "Details here")

    # Flush Async Queue
    store.stop()

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT * FROM audit_log").fetchone()

    assert row is not None
    assert row[2] == "TEST_ACTION"
    assert row[3] == "UID_123"
    assert "Details here" in row[4]

def test_session_integration(tmp_path):
    # Verify DicomSession uses the store
    db_path = tmp_path / "test_session.db"
    sess = DicomSession(str(db_path))
    # Simulate adding data (Session usually relies on Import, but let's manipulate internal store)
    p = Patient("PX", "Test")
    sess.store.patients.append(p)

    # Save
    sess.save()
    sess.persistence_manager.shutdown()

    # Verify DB
    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute("SELECT count(*) FROM patients").fetchone()[0]

    assert count == 1

    # Cleanup auto by tmp_path

def test_remediation_audit(store):
    from isocenter.remediation import RemediationService
    from isocenter.privacy import PhiFinding, PhiRemediation

    # Create finding
    finding = PhiFinding("PID_123", "Patient", "patient_name", "John Doe", "Names",
                        entity=Patient("PID_123", "John Doe"),
                        remediation_proposal=PhiRemediation("REPLACE_TAG", "patient_name", "ANONYMIZED"))

    svc = RemediationService(store_backend=store)
    svc.apply_remediation([finding])

    # Verify Audit Log
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT * FROM audit_log WHERE action_type='REMEDIATION_REPLACE'").fetchone()

    assert row is not None
    assert row[3] == "PID_123"
    assert "ANONYMIZED" in row[4]


def test_an_instance_reloaded_after_remediation_still_needs_a_save_for_the_next_tag(store):
    """A second remediation after a real save/load round trip (#173).

    #173's scope note established by *reading* that a hydrated entity
    sits at REMEDIATED with nothing to save, and that from there the
    `mark_modified()` calls in remediation.py are the only thing left
    advancing the revision. Nobody had run it. This does: the
    precondition below is produced by `save_all` and `load_all` rather
    than by `mark_persisted()` standing in for them, so a change to how
    hydration records a stored status shows up here.

    It also kills the same mutant as
    `test_removing_a_second_tag_after_a_reload_still_needs_a_save` in
    tests/test_remediation_invariants.py -- the `mark_modified()` in the
    `REMOVE_TAG` attributes arm. That test names the line; this one
    checks that the state the line matters in is the state a reload
    actually leaves behind.
    """
    from isocenter.entities import PhiStatus
    from isocenter.privacy import PhiFinding, PhiRemediation
    from isocenter.remediation import RemediationService

    def remove(entity, tag):
        return PhiFinding(
            entity_uid=entity.sop_instance_uid, entity_type="Instance",
            field_name=tag, value=entity.attributes.get(tag), reason="test",
            tag=tag, entity=entity,
            remediation_proposal=PhiRemediation(
                action_type="REMOVE_TAG", target_attr=tag))

    patient = Patient("P1", "Patient One")
    study = Study("S1", "20230101")
    series = Series("SE1", "CT", 1)
    inst = Instance("I1", "1.2.3", 1, file_path="/tmp/test.dcm")
    inst.set_attr("0010,0010", "DOE^JOHN")
    inst.set_attr("0008,0080", "MERCY GENERAL")
    patient.studies.append(study)
    study.series.append(series)
    series.instances.append(inst)

    RemediationService().apply_remediation([remove(inst, "0010,0010")])
    store.save_all([patient])

    hydrated = store.load_all()[0].studies[0].series[0].instances[0]
    assert hydrated.phi_status is PhiStatus.REMEDIATED, \
        "the store did not carry the first remediation's conclusion back"
    assert not hydrated.has_unsaved_changes, \
        "a freshly loaded instance has nothing the store has not got"

    RemediationService().apply_remediation([remove(hydrated, "0008,0080")])

    assert "0008,0080" not in hydrated.attributes
    assert hydrated.has_unsaved_changes, (
        "the reloaded instance reports no unsaved changes after its PHI "
        "was stripped, so the next save skips it and the value stays in "
        "the database")

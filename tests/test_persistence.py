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

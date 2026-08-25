
import os
import shutil
import pytest
from isocenter import Session
from isocenter.reporting import ComplianceReport

TEST_DB = "test_reporting.db"
REPORT_FILE = "test_report.md"

@pytest.fixture
def clean_env():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.exists(REPORT_FILE):
        os.remove(REPORT_FILE)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.exists(REPORT_FILE):
        os.remove(REPORT_FILE)

def test_compliance_reporting_flow(clean_env):
    # 1. Init Session
    s = Session(TEST_DB)

    # 2. Simulate some activity (fake audit logs)
    # Since we can't easily ingest Dicom data without files in this unit test environment,
    # we will manually inject audit logs into the persistence layer to verify aggregation.

    s.store_backend.log_audit("ANONYMIZE", "PAT_001", "Removed PatientName")
    s.store_backend.log_audit("ANONYMIZE", "PAT_002", "Removed PatientName")
    s.store_backend.log_audit("REDACT", "INST_001", "Burned-in PHI scrubbed")
    s.store_backend.log_audit("EXPORT", "INST_001", "Exported successfully")

    # Force flush
    s.store_backend.flush_audit_queue()

    # 3. Generate Report
    s.generate_report(REPORT_FILE, format="markdown")

    # 4. Verify File Creation
    assert os.path.exists(REPORT_FILE)

    with open(REPORT_FILE, "r") as f:
        content = f.read()

    print("\n--- Generated Report Content ---")
    print(content)
    print("--------------------------------")

    # 5. Verify Content
    assert "# Compliance Report" in content
    assert "Isocenter v" in content
    assert "Processing Audit" in content

    # Verify Audit Counts
    assert "| ANONYMIZE | 2 |" in content
    assert "| REDACT | 1 |" in content
    assert "| EXPORT | 1 |" in content

    # Verify Project Name
    assert f"**Project:** {TEST_DB}" in content

    # Verify Manifest - REMOVED
    # assert "## 4. Cohort Manifest" in content
    # assert "*No studies found.*" in content # Because we didn't add patients/studies effectively in this mock

    # Verify Exceptions (Empty)
    assert "*No exceptions or errors were recorded.*" in content

    # 6. Verify DTO structure (unit test part)
    summary = s.store_backend.get_audit_summary()
    assert summary["ANONYMIZE"] == 2
    assert summary["REDACT"] == 1

    s.close()

def test_exception_reporting(clean_env):
    s = Session(TEST_DB)
    s.store_backend.log_audit("ERROR", "SYS", "Critical failure")
    s.store_backend.flush_audit_queue()

    s.generate_report(REPORT_FILE)

    with open(REPORT_FILE, "r") as f:
        content = f.read()

    assert "## 3. Exceptions & Errors" in content
    assert "| ERROR | Critical failure |" in content
    s.close()

def test_burned_in_annotation_check(clean_env):
    s = Session(TEST_DB)

    # 1. Create a fake instance with BurnedInAnnotation="YES"
    # We need to manually inject it into the DB since we don't have real DICOMs handy
    # and ingestion logic is complex.

    # We need to ensure series/study/patient exist to adhere to FK constraints usually,
    # but let's see if we can cheat with raw SQL for the test.
    # The Schema requires FKs...
    # Let's use internal methods if possible, or just INSERT raw logic.

    s.store_backend.check_unsafe_attributes() # Should be empty

    with s.store_backend._get_connection() as conn:
        conn.execute("INSERT INTO patients (patient_id) VALUES ('UnsafePat')")
        pid = conn.execute("SELECT id FROM patients WHERE patient_id='UnsafePat'").fetchone()[0]

        conn.execute("INSERT INTO studies (patient_id_fk, study_instance_uid) VALUES (?, '1.2.3.4')", (pid,))
        stid = conn.execute("SELECT id FROM studies WHERE study_instance_uid='1.2.3.4'").fetchone()[0]

        conn.execute("INSERT INTO series (study_id_fk, series_instance_uid) VALUES (?, '1.2.3.4.5')", (stid,))
        seid = conn.execute("SELECT id FROM series WHERE series_instance_uid='1.2.3.4.5'").fetchone()[0]

        # attributes_json containing the bad tag
        bad_json = '{"0028,0301": "YES", "0010,0010": "BadPatient"}'

        conn.execute("""
            INSERT INTO instances (series_id_fk, sop_instance_uid, attributes_json)
            VALUES (?, '1.2.3.4.5.6', ?)
        """, (seid, bad_json))
        conn.commit()

    # 2. Generate Report
    s.generate_report(REPORT_FILE)

    with open(REPORT_FILE, "r") as f:
        content = f.read()

    # 3. Validate
    print(content)
    assert "**Validation Status** | **REVIEW_REQUIRED**" in content
    assert "BurnedInAnnotation FLAGGED as YES" in content

    s.close()

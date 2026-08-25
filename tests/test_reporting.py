
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


# --- What the report is allowed to claim -----------------------------
#
# The compliance report ends with a Data Protection Officer signature
# line, so every claim above it is one somebody signs. `deid_method`
# used to be a dataclass default of "Safe Harbor (Basic Profile)" that
# nothing ever assigned, and the methodology paragraph hardcoded "the
# Isocenter Safe Harbor pipeline" -- both printed unconditionally, on a
# bare session whose PHI scan covers six tags, and while issue #57
# leaves nested-sequence rules unfired. The report may describe what was
# configured and what was recorded. It may not assert a standard.

from isocenter.profiles import BASIC_PROFILE


def _render_report(session, tmp_path):
    """Generate a markdown report and hand back its text."""
    output = tmp_path / "compliance.md"
    session.generate_report(str(output), format="markdown")
    return output.read_text(encoding="utf-8")


def test_a_default_session_report_claims_no_compliance_standard(tmp_path):
    """A bare session scans six tags. That is not Safe Harbor.

    The standard may still be *named* -- the methodology section points at
    it as the open question the signer has to answer. What it may not do
    is appear in the summary rows, which are read as findings.
    """
    with Session(str(tmp_path / "bare.db")) as session:
        content = _render_report(session, tmp_path)

    claims = [line for line in content.splitlines()
              if line.startswith(("| Privacy Profile", "| De-ID Method"))]
    assert claims, "the summary rows this test guards have been renamed"
    for row in claims:
        assert "Safe Harbor" not in row, (
            f"the report asserts a compliance standard it did not verify: {row}")

    assert "determination for the data steward" in content, (
        "the report drops the standard entirely instead of handing the "
        "question to the person signing it")


def test_the_report_names_the_profile_that_was_actually_applied(tmp_path):
    """'See Config' told the reader nothing; name the profile."""
    config = tmp_path / "config.yaml"
    config.write_text("privacy_profile: basic\nmachines: []\n", encoding="utf-8")

    with Session(str(tmp_path / "profiled.db")) as session:
        session.load_config(str(config))
        content = _render_report(session, tmp_path)

    assert "basic" in content
    assert f"{len(BASIC_PROFILE)} tag rules" in content, (
        "the report does not state how many tag rules were in force")


def test_the_report_states_when_no_profile_was_applied(tmp_path):
    """Silence reads as 'a profile was applied'. Say the opposite."""
    with Session(str(tmp_path / "bare.db")) as session:
        content = _render_report(session, tmp_path)

    assert "session defaults" in content.lower()
    assert "6 tag rules" in content


def test_an_unresolvable_profile_is_not_reported_as_applied(tmp_path):
    """A misspelled profile name is warned about and ignored at load.

    Reporting it anyway would describe protection that never ran -- the
    most dangerous line the report could carry.
    """
    config = tmp_path / "config.yaml"
    config.write_text("privacy_profile: no_such_profile\nmachines: []\n",
                      encoding="utf-8")

    with Session(str(tmp_path / "unknown.db")) as session:
        session.load_config(str(config))
        content = _render_report(session, tmp_path)

    assert "no_such_profile" not in content, (
        "the report names a profile that failed to resolve and was never "
        "applied")

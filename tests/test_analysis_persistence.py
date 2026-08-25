import pytest
import os
import sqlite3
from isocenter.session import DicomSession
from isocenter.privacy import PhiFinding, PhiRemediation, PhiReport

def test_persist_phi_findings(tmp_path):
    db_path = str(tmp_path / "phi.db")
    session = DicomSession(db_path)

    # Create manual findings
    findings = [
        PhiFinding(
            entity_uid="P1", entity_type="Patient", field_name="patient_name", value="John Doe", reason="PHI",
            patient_id="P1",
            remediation_proposal=PhiRemediation("REPLACE_TAG", "patient_name", "ANON", "John Doe")
        ),
        PhiFinding(
            entity_uid="S1", entity_type="Study", field_name="study_date", value="20230101", reason="PHI",
            patient_id="P1",
            remediation_proposal=None
        )
    ]

    report = PhiReport(findings)

    # Save
    session.save_analysis(report)

    # Verify via SqliteStore load
    loaded = session.store_backend.load_findings()
    assert len(loaded) == 2

    # Sort or check order (load_findings orders by ID)
    f1 = loaded[0]
    f2 = loaded[1]

    assert f1.entity_uid == "P1"
    assert f1.remediation_proposal.action_type == "REPLACE_TAG"
    assert f1.remediation_proposal.new_value == "ANON"

    assert f2.entity_uid == "S1"
    assert f2.remediation_proposal is None

    # Verify via raw SQL
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute("SELECT * FROM phi_findings").fetchall()
    assert len(rows) == 2
    conn.close()

def test_persist_empty(tmp_path):
    db_path = str(tmp_path / "empty.db")
    session = DicomSession(db_path)
    session.save_analysis([])
    loaded = session.store_backend.load_findings()
    assert len(loaded) == 0

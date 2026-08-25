import pytest
from isocenter.entities import Patient, Study
from isocenter.privacy import PhiInspector

def test_phi_detection():
    # Setup
    pat = Patient("MRN123", "John Doe")
    study = Study("1.2.3.4", "20230101")
    pat.studies.append(study)

    inspector = PhiInspector()
    findings = inspector.scan_patient(pat)

    # Assert
    assert len(findings) == 3

    names = [f.field_name for f in findings]
    assert "patient_name" in names
    assert "patient_id" in names
    assert "study_date" in names

    # Validate reason
    name_finding = next(f for f in findings if f.field_name == "patient_name")
    assert "Names are PHI" in name_finding.reason
    assert name_finding.tag == "0010,0010"

    id_finding = next(f for f in findings if f.field_name == "patient_id")
    assert id_finding.tag == "0010,0020"

    date_finding = next(f for f in findings if f.field_name == "study_date")
    assert date_finding.tag == "0008,0020"

def test_no_phi():
    # Setup a patient with no PHI (sanitized)
    pat = Patient("UNKNOWN", "Unknown")
    # No studies

    inspector = PhiInspector()
    findings = inspector.scan_patient(pat)

    assert len(findings) == 0

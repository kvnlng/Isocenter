import pytest
from isocenter.entities import Patient, Study
from isocenter.privacy import PhiInspector

from support.project_secret import FIXED_A

def test_phi_detection():
    # Setup
    pat = Patient("MRN123", "John Doe")
    study = Study("1.2.3.4", "20230101")
    pat.studies.append(study)

    inspector = PhiInspector(project_secret=FIXED_A)
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
    # Setup a patient with no PHI (sanitized): its ID is already a
    # replacement. This used `"UNKNOWN"`, which the scan exempted by name
    # until #584; that string is a Patient ID a file can carry, and is
    # pseudonymized like any other now.
    pat = Patient("ANON_0123456789abcdef01234567", "Unknown")
    # No studies

    inspector = PhiInspector(project_secret=FIXED_A)
    findings = inspector.scan_patient(pat)

    assert len(findings) == 0

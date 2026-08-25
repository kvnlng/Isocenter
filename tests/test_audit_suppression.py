
import pytest
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.privacy import PhiInspector, PhiFinding, PhiReport
from isocenter.remediation import RemediationService

def test_audit_suppresses_shifted_dates():
    """
    Verifies that if an instance has 'date_shifted=True',
    the PhiInspector does NOT report SHIFT/JITTER tags as findings.
    """
    # 1. Setup Instance with a Date that requires shifting
    inst = Instance("I1", "SOP1", 1)
    inst.attributes["0008,0020"] = "20230101" # Study Date

    # 2. Setup Inspector with policy to JITTER dates
    config_tags = {
        "0008,0020": {"name": "Study Date", "action": "JITTER"} # Action is SHIFT/JITTER
    }

    inspector = PhiInspector(config_tags=config_tags)

    # 3. Initial Scan -> Should find it
    findings = inspector._scan_instance(inst, "P1")
    assert len(findings) == 1
    assert findings[0].tag == "0008,0020"
    assert findings[0].remediation_proposal.action_type == "SHIFT_DATE"

    # 4. Simulate Remediation
    # We set date_shifted manually to simulate successful jitter
    inst.date_shifted = True

    # 5. Rescan -> Should NOT find it
    findings_after = inspector._scan_instance(inst, "P1")
    assert len(findings_after) == 0

def test_remediation_service_sets_flag():
    """
    Verifies that RemediationService actually sets the date_shifted flag.
    """
    # 1. Setup Entity
    inst = Instance("I1", "SOP1", 1)
    inst.attributes["0008,0020"] = "20230101"

    # 2. Create Finding regarding this entity
    from isocenter.privacy import PhiRemediation
    proposal = PhiRemediation(
        action_type="SHIFT_DATE",
        target_attr="0008,0020",
        original_value="20230101",
        metadata={"patient_id": "P1"}
    )
    finding = PhiFinding(
        entity_uid="I1", entity_type="Instance", field_name="StudyDate",
        value="20230101", reason="PHI", entity=inst, remediation_proposal=proposal
    )

    # 3. Run Remediation
    service = RemediationService(store_backend=None)
    # We mock _get_date_shift to be deterministic/simple
    service._get_date_shift = lambda pid: 10

    service.apply_remediation([finding])

    # 4. Verify Flag
    assert inst.date_shifted is True
    assert inst.attributes["0008,0020"] == "20230111" # Shifted by 10 days

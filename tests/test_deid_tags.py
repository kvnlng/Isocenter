
import pytest
from isocenter.entities import Instance, DicomSequence, DicomItem
from isocenter.remediation import RemediationService

def test_deid_tags_stamping():
    """
    Verifies that (0012,0063) and (0012,0064) are correctly stamped on an entity.
    """
    # 1. Setup Instance
    inst = Instance("I1", "SOP1", 1)

    # 2. Apply Tags
    svc = RemediationService()
    svc.add_global_deid_tags(inst)

    # 3. Verify (0012,0063) De-identification Method
    method_val = inst.attributes.get("0012,0063")
    assert method_val is not None
    assert "Isocenter Privacy Profile" in method_val

    # 4. Verify (0012,0064) Code Sequence
    seq = inst.sequences.get("0012,0064")
    assert seq is not None
    assert len(seq.items) >= 1

    # Check for specific code 113100
    found_code = False
    for item in seq.items:
        code = item.attributes.get("0008,0100")
        if code == "113100":
            found_code = True
            assert item.attributes.get("0008,0104") == "Basic Application Confidentiality Profile"
            break

    assert found_code is True

def test_deid_tags_idempotency():
    """
    Verifies that applying tags multiple times does not duplicate them.
    """
    inst = Instance("I1", "SOP1", 1)
    svc = RemediationService()

    # Run twice
    svc.add_global_deid_tags(inst)
    svc.add_global_deid_tags(inst)

    # Verify Method list length (should be 1 if starting empty)
    method_val = inst.attributes.get("0012,0063")
    assert len(method_val) == 1
    assert method_val[0] == "Isocenter Privacy Profile"

    # Verify Sequence length (should be 1)
    seq = inst.sequences.get("0012,0064")
    # We only check for our specific code item count, assuming we are the only one adding it
    # But for a fresh instance it should be 1
    assert len(seq.items) == 1

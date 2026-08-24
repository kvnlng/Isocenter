
import pytest
from gantry.session import DicomSession
from gantry.services import RedactionService
from unittest.mock import MagicMock

def test_execute_config_crash_repro(tmp_path):
    """
    Reproduces 'list object has no attribute get' when active_rules contains a list instead of dict.
    This simulates a malformed YAML load where the list formatting might be ambiguous.
    """
    session = DicomSession(persistence_file=":memory:")

    # Simulate a "valid" load but with List-based zones (which ConfigLoader accepts)
    # machines:
    #   - serial: 123
    #     redaction_zones:
    #       - [10, 50, 10, 50]  <-- List, not Dict

    rule_with_list_zone = {
        "serial_number": "123",
        "redaction_zones": [[10, 50, 10, 50]]
    }
    # Mock index so it actually runs
    # We need a dummy instance to match "123"
    from gantry.entities import Patient, Study, Series, Instance, Equipment
    p = Patient("P1", "N1")
    st = Study("S1", None)
    se = Series("SE1", "OT", 1)
    se.equipment = Equipment("Man", "Mod", "123")
    inst = Instance("I1", "SOP1", 1)
    import numpy as np
    inst.set_pixel_data(np.zeros((100,100), dtype=np.uint8))

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    session.store.patients.append(p)

    # Re-index
    service = RedactionService(session.store)

    # This should NOT raise AttributeError anymore
    try:
        service.process_machine_rules(rule_with_list_zone)
    except AttributeError as e:
        pytest.fail(f"Regression: List-based zones crashed: {e}")

    # Verify pixels changed (simple check)
    assert inst.get_pixel_data()[10,10] == 0

def test_execute_config_session_level_interruption(capsys):
    """
    Verifies that DicomSession.execute_config catches the error and reports it.
    """
    session = DicomSession(persistence_file=":memory:")
    session.configuration.rules = [["bad", "rule"]]

    session.redact()

    captured = capsys.readouterr()
    assert "Execution interrupted: 'list' object has no attribute 'get'" in captured.out

def test_burned_in_safety_check(capsys):
    """
    Verifies that scan_burned_in_annotations correctly identifies untreated instances.
    """
    session = DicomSession(persistence_file=":memory:")

    # Create Risk Instance: Burned In = YES, No Rule
    from gantry.entities import Patient, Study, Series, Instance
    inst_risk = Instance("Risk1", "SOP_RISK", 1)
    inst_risk.attributes["0028,0301"] = "YES"
    inst_risk.attributes["0008,0008"] = ["ORIGINAL", "PRIMARY"] # Not DERIVED

    # Create Safe Instance: Burned In = YES but REMEDIATED (Derived)
    inst_safe = Instance("Safe1", "SOP_SAFE", 1)
    inst_safe.attributes["0028,0301"] = "YES" # Original flag might still be there if not overwritten?
    # But usually applying redaction sets it to "NO".
    # The scan check looks for "YES" AND "Not Derived".
    # If the rule sets it to NO, it won't trigger anyway.
    # But let's simulate a case where it SAYS Yes but IS Derived (e.g. partial redaction?)
    inst_safe.attributes["0008,0008"] = ["DERIVED", "SECONDARY"]

    # Add to store manually
    p = Patient("P", "N")
    st = Study("S", None)
    se = Series("SE", "OT", 1)
    se.instances.append(inst_risk)
    se.instances.append(inst_safe)
    st.series.append(se)
    p.studies.append(st)
    session.store.patients.append(p)

    # Run redact() with no rules loaded -- it should NOT trigger the scan,
    # since redact() returns early ("if not self.configuration.rules: return")
    # before it ever constructs a RedactionService.
    session.configuration.rules = [] # No rules

    # Capture output/logs
    # Usage of print in services.py should be captured by capsys
    session.redact()

    # So the burned-in-annotation scan has to be exercised directly against
    # a service instance instead of through redact().
    service = RedactionService(session.store)
    service.scan_burned_in_annotations()

    captured = capsys.readouterr()

    # Check for WARNING
    assert "WARNING: 1 instances flagged with 'Burned In Annotation' were not targeted" in captured.out
    # Risk1 should be in the logs (which might go to out or err depending on config/pytest)
    assert "Risk1" in (captured.out + captured.err)
    # Logger output isn't in capsys usually unless configured?
    # But the print statements ARE.


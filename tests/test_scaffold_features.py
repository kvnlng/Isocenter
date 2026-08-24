
import pytest
import os
import json
from gantry.session import DicomSession
from gantry.config_manager import ConfigLoader
from gantry.entities import Instance, Patient, Study, Series
from gantry.privacy import PhiInspector, PhiFinding, PhiRemediation
from gantry.remediation import RemediationService

def test_scaffold_config_structure(tmp_path):
    """Verify that scaffold_config produces valid JSON with new fields."""
    session = DicomSession(persistence_file=":memory:")
    output_path = tmp_path / "unified.yaml"
    session.create_config(str(output_path))

    assert output_path.exists()

    # Load and check
    with open(output_path, 'r') as f:
        import yaml
        data = yaml.safe_load(f)

    assert "version" in data
    assert "machines" in data
    assert "phi_tags" in data
    assert "date_jitter" in data
    assert "remove_private_tags" in data

    assert data["date_jitter"]["min_days"] == -365
    assert data["date_jitter"]["max_days"] == -1
    assert data["remove_private_tags"] is True

    # Check if research tags are present (e.g. Study Date with JITTER)
    tags = data["phi_tags"]
    assert "0008,0020" in tags
    assert tags["0008,0020"]["action"] == "JITTER"

def test_private_tag_removal():
    """Verify private tag removal logic."""
    inspector = PhiInspector(remove_private_tags=True)

    inst = Instance("1.2.3", "dcm_file")
    # Odd group = Private
    inst.attributes["0011,1010"] = "PrivateData"
    # Even group = Public
    inst.attributes["0010,0010"] = "PublicData"
    # Whitelisted Private
    inst.attributes["0099,0010"] = "GANTRY_SECURE"

    findings = inspector._scan_instance(inst, "PAT1")

    private_findings = [f for f in findings if "Private Tag" in f.field_name]
    assert len(private_findings) == 1
    assert private_findings[0].tag == "0011,1010"
    assert private_findings[0].remediation_proposal.action_type == "REMOVE_TAG"

    # Check whitelist ignored
    whitelist_finding = [f for f in findings if f.tag == "0099,0010"]
    assert len(whitelist_finding) == 0

def test_date_jitter_service():
    """Verify that remediation service respects jitter config."""
    # Config: ONLY shift by -10 days
    config = {"min_days": -10, "max_days": -10}
    svc = RemediationService(date_jitter_config=config)

    # With deterministic hashing, the offset is usually (hash % span) + min.
    # Span = (-10) - (-10) + 1 = 1.
    # Offset = (hash % 1) + (-10) = 0 - 10 = -10.
    # So it should ALWAYS be -10.

    shift = svc._get_date_shift("PAT123")
    assert shift == -10

    shift2 = svc._get_date_shift("OTHER_PAT")
    assert shift2 == -10

    # Test Application
    # 20230111 shifted by -10 days -> 20230101
    new_date = svc._shift_date_string("20230111", shift)
    assert new_date == "20230101"

def test_scaffold_comments(tmp_path):
    """Verify that comment keys are converted to # comments in the output file."""
    session = DicomSession(persistence_file=":memory:")

    # Needs matching equipment to trigger comment generation
    # Inject a known equipment
    # session.store.equipment.append(Equipment("GE MEDICAL", "GENESIS", "SN1")) # Invalid in current Store model
    pass

    # We also need a fake KB rule or CTP rule to trigger auto-match?
    # Or rely on the fallback logic: "Auto-matched from Model Knowledge Base" or similar?
    # Actually, scaffold_config fallback generates "Auto-matched from Model Knowledge Base" if unknown?
    # Let's check logic:
    # If not matched_rule, it checks Model KB (kb_machines). If not there, it creates empty scaffold (redaction_zones=[])
    # Empty scaffold does NOT have a comment in the code currently?
    # Wait, looking at code:
    # else: missing_configs.append({... serial_number... }) -> No comment added for completely unknown key.

    # So we need to match a rule.
    # Method: Create a fake CTP rule file so it matches SN1.
    ctp_path = "gantry/resources/ctp_rules.yaml"
    import yaml
    # Back up existing if needed, but in test env we might mock?
    # Since we can't easily overwrite source in a safe way for parallel tests,
    # we might just rely on the fact that CTP parser puts comments in `phi_tags`?
    # No, CTP parser logic is for machines.

    # Alternative: Inject a rule into `session.configuration.rules` MANUALLY with a comment, then scaffold.
    # create_config (aka "scaffold_config") includes `session.configuration.rules` in its output.

    session.configuration.rules.append({
        "serial_number": "SN-MANUAL",
        "redaction_zones": [],
        "comment": "This is a manual comment"
    })

    output_path = tmp_path / "comment_test.yaml"
    session.create_config(str(output_path))

    with open(output_path, "r") as f:
        content = f.read()

    # Check that "comment:" is NOT present (as a key)
    assert "comment: This is a manual comment" not in content
    # Check that "# This is a manual comment" IS present
    assert "# This is a manual comment" in content

def test_scaffold_multiline_comments(tmp_path):
    """Verify that multiline comments (like from CTP) are handled correctly and don't break YAML."""
    session = DicomSession(persistence_file=":memory:")

    # Inject a rule with a MULTILINE comment (simulating CTP condition)
    # The snippet from user had explicit "\n" and extra spaces
    multiline_text = 'Auto-matched from CTP. Condition: Modality.equals("US") *\n    Manufacturer.containsIgnoreCase("\n    SIEMENS")'

    session.configuration.rules.append({
        "serial_number": "SN-MULTI",
        "redaction_zones": [],
        "comment": multiline_text
    })

    output_path = tmp_path / "multiline_test.yaml"
    session.create_config(str(output_path))

    # 1. Verify content
    with open(output_path, "r") as f:
        content = f.read()

    # Should be commented out
    assert "SN-MULTI" in content
    assert "# Auto-matched" in content

    # 2. CRITICAL: Verify it parses as valid YAML
    import yaml
    try:
        with open(output_path, "r") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        pytest.fail(f"Generated YAML is invalid: {e}")

    # Verify data integrity
    loaded_rule = next(r for r in data["machines"] if r["serial_number"] == "SN-MULTI")
    # Comment should NOT be in the loaded dict (as it's a comment now)
    assert "comment" not in loaded_rule

def test_scaffold_burned_in_warning(tmp_path):
    """Verify that machines with 'Burned In Annotation' flag get a warning in scaffold."""
    session = DicomSession(persistence_file=":memory:")

    # Create Risk Instance
    from gantry.entities import Equipment
    # Inject unknown equipment -> Falls back to empty scaffold
    # But now we expect a comment
    # session.store.equipment.append(Equipment("RISK_MAN", "RISK_MOD", "SN-RISK")) # Invalid
    pass

    # Inject instance into session (need to link to patient/study/series)
    # Actually, scaffold checks `service.index.get_by_machine`.
    # So we need full object graph.
    from gantry.entities import Patient, Study, Series, Instance
    p = Patient("P1", "N1")
    st = Study("S1", None)
    se = Series("SE1", "OT", 1)
    se.equipment = Equipment("RISK_MAN", "RISK_MOD", "SN-RISK")
    inst = Instance("I_RISK", "SOP_RISK", 1)
    inst.attributes["0028,0301"] = "YES" # The Flag

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    session.store.patients.append(p)

    output_path = tmp_path / "burned_in_test.yaml"
    session.create_config(str(output_path))

    with open(output_path, "r") as f:
        content = f.read()

    # Check for Warning Comment
    assert "SN-RISK" in content
    # The fix we made adds a comment.
    # Because of our comment post-processor, it should be converted to # ...
    # We now explicitly unescape '' -> ' in the post-processor to look nice.
    assert "# WARNING: 1 images have 'Burned In Annotation' flag" in content

def test_scaffold_flow_style(tmp_path):
    """Verify that redaction_zones are formatted as flow-style lists (brackets)."""
    session = DicomSession(persistence_file=":memory:")

    # Add a machine with redaction zones
    zones = [[10, 20, 30, 40], [50, 60, 70, 80]]
    session.configuration.rules.append({
        "serial_number": "SN-FLOW",
        "redaction_zones": zones
    })

    output_path = tmp_path / "flow_test.yaml"
    session.create_config(str(output_path))

    with open(output_path, "r") as f:
        content = f.read()

    print(content) # For debugging if fails

    # We expect brackets: [[10, 20, 30, 40], [50, 60, 70, 80]]
    # or with spaces.
    # checking strict substring might be fragile due to spacing, but let's try typical yaml flow output
    assert "[[10, 20, 30, 40], [50, 60, 70, 80]]" in content or "[[10, 20, 30, 40],[50, 60, 70, 80]]" in content


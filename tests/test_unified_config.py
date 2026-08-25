import pytest
import os
import json
from isocenter import Session

@pytest.fixture
def session_db(tmp_path):
    return str(tmp_path / "test_unified.db")

def test_unified_workflow(session_db, tmp_path, dummy_patient):
    """
    Verifies that the unified config workflow functions end-to-end.
    """
    # 1. Init Session
    session = Session(session_db)
    session.store.patients.append(dummy_patient)
    session.save()

    # 2. Scaffold Config (Action: Setup)
    config_path = tmp_path / "unified_config.yaml"
    session.create_config(str(config_path))

    assert os.path.exists(config_path)

    with open(config_path, 'r') as f:
        import yaml
        data = yaml.safe_load(f)

    assert "version" in data and data["version"] == "2.0"
    assert "phi_tags" in data
    assert "machines" in data
    # Check that scalar defaults are present (Tag IDs)
    # With basic profile, 0010,0010 is implied via profile, not explicit in phi_tags
    assert data.get("privacy_profile") == "basic"
    # assert "0010,0010" in data["phi_tags"]

    # 3. Edit Config (Simulated User Action)
    # Let's add a custom tag to look for (Protocol Name = 0018,1030)
    data["phi_tags"]["0018,1030"] = "Protocol Name"
    # And a machine rule
    data["machines"].append({
        "serial_number": "SN-999", # Matches dummy_patient
        "redaction_zones": [{"roi": [0, 10, 0, 10]}]
    })

    with open(config_path, 'w') as f:
        yaml.dump(data, f)

    # 4. Load Config
    session.load_config(str(config_path))
    assert len(session.configuration.rules) >= 1
    assert "0018,1030" in session.configuration.phi_tags

    # SETUP: Inject a value for the custom tag
    # The dummy patient provided by fixture might need this set specifically on the Instance
    inst = dummy_patient.studies[0].series[0].instances[0]
    inst.set_attr("0018,1030", "SensitiveProtocol")

    # 5. Verify Audit (Target)
    report = session.audit()
    assert report is not None

    # Check for the custom finding
    found_custom = False
    for f in report:
        if f.entity_type == "Instance" and f.field_name == "Protocol Name":
            found_custom = True
            assert f.value == "SensitiveProtocol"
            break

    assert found_custom, "Audit failed to find the custom 'Protocol Name' tag on the instance."

    # 6. Verify Remediation (Anonymize)
    session.anonymize(report)

    # Verify the instance in memory is updated
    assert inst.attributes["0018,1030"] == "ANONYMIZED"

    print("\nUnified Config Workflow Verified: Custom Tag found and remediated.")

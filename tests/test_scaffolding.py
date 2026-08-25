import pytest
import json
from isocenter.session import DicomSession
from isocenter.entities import Patient, Equipment
from isocenter.builders import DicomBuilder
from datetime import date

def test_scaffold_config_creates_new_entries(tmp_path):
    # 1. Setup Session with Dummy Data
    session = DicomSession(str(tmp_path / "scaffold.pkl"))

    # Create Patient with two Series from different machines
    # Machine 1: "SN-OLD" (Will be configured)
    # Machine 2: "SN-NEW" (Will be missing)

    # We can invoke DicomImporter, or just manually inject into store for speed
    p = DicomBuilder.start_patient("P1", "Test") \
        .add_study("1.1", date(2023,1,1)) \
            .add_series("1.1.1", "CT", 1) \
                .set_equipment("MfgA", "ModelA", "SN-OLD") \
                .add_instance("1.1.1.1", "1.2", 1).end_instance() \
            .end_series() \
            .add_series("1.1.2", "CT", 2) \
                .set_equipment("MfgB", "ModelB", "SN-NEW") \
                .add_instance("1.1.2.1", "1.2", 1).end_instance() \
            .end_series() \
        .end_study() \
        .build()

    session.store.patients.append(p)

    # 2. Load partial config
    # Only "SN-OLD" is known
    config_data = {
        "version": "1.0",
        "machines": [{"serial_number": "SN-OLD", "redaction_zones": []}]
    }
    config_file = tmp_path / "existing.yaml"
    import yaml
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    session.load_config(str(config_file))

    # 3. Validation: SN-NEW is not in active rules
    assert len(session.configuration.rules) == 1
    assert session.configuration.rules[0]["serial_number"] == "SN-OLD"

    # 4. Run Scaffold
    out_file = tmp_path / "todo.yaml"
    session.create_config(str(out_file))

    # 5. Verify Output
    assert out_file.exists()
    with open(out_file, "r") as f:
        new_conf = yaml.safe_load(f)

    # The unified config scaffold keeps existing rules AND adds new ones.
    # So we expect 2 machines (SN-OLD and SN-NEW).
    assert len(new_conf["machines"]) == 2

    # Extract just the new one for validation
    new_machine = next(m for m in new_conf["machines"] if m["serial_number"] == "SN-NEW")

    assert new_machine["serial_number"] == "SN-NEW"
    assert new_machine["model_name"] == "ModelB"
    assert new_machine["manufacturer"] == "MfgB"
    assert new_machine["redaction_zones"] == []

    # Ensure instructions are removed (replaced by comments)
    assert "_instructions" not in new_conf

import os
import shutil

from isocenter.session import DicomSession
from isocenter.configuration import IsocenterConfiguration

# Cleanup previous runs
if os.path.exists("test_config_api.db"):
    os.remove("test_config_api.db")

def test_configuration_api():
    print("Initialize Session...")
    s = DicomSession("test_config_api.db")

    # 1. Test Initial State
    assert isinstance(s.configuration, IsocenterConfiguration)
    assert s.configuration.rules == []
    assert s.configuration.phi_tags == {}

    # 2. Test Add Rule
    print("Testing Add Rule...")
    s.configuration.add_rule("SERIAL_123", "TestMan", "TestModel", zones=[[10, 10, 100, 100]])

    assert len(s.configuration.rules) == 1
    rule = s.configuration.get_rule("SERIAL_123")
    assert rule is not None
    assert rule["manufacturer"] == "TestMan"

    # 3. Test Update Rule
    print("Testing Update Rule...")
    s.configuration.update_rule("SERIAL_123", {"manufacturer": "UpdatedMan"})
    rule = s.configuration.get_rule("SERIAL_123")
    assert rule["manufacturer"] == "UpdatedMan"

    # 4. Test Delete Rule
    print("Testing Delete Rule...")
    s.configuration.delete_rule("SERIAL_123")
    assert len(s.configuration.rules) == 0

    # 5. Test PHI Tag Setting
    print("Testing PHI Tag...")
    s.configuration.set_phi_tag("0010,0010", "REMOVE")
    assert "0010,0010" in s.configuration.phi_tags
    assert s.configuration.phi_tags["0010,0010"]["action"] == "REMOVE"

    # 6. Test Config Export (Round Trip check)
    print("Testing Config Export...")
    s.configuration.add_rule("SERIAL_EXPORT", "Man", "Mod", [])
    s.create_config("test_export_config.yaml")

    assert os.path.exists("test_export_config.yaml")

    # 7. Test Config Load
    print("Testing Config Load...")
    # Safe YAML modification
    import yaml
    with open("test_export_config.yaml", "r") as f:
        data = yaml.safe_load(f)

    if "machines" not in data:
        data["machines"] = []

    data["machines"].append({
        "serial_number": "LOADED_SERIAL",
        "manufacturer": "LoadedMan",
        "model_name": "LoadedMod",
        "redaction_zones": []
    })

    with open("test_import_config.yaml", "w") as f:
        yaml.dump(data, f)


    s.load_config("test_import_config.yaml")

    found = s.configuration.get_rule("LOADED_SERIAL")
    assert found is not None
    assert found["manufacturer"] == "LoadedMan"

    print("All Configuration API tests passed!")
    s.close()

if __name__ == "__main__":
    try:
        test_configuration_api()
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
        exit(1)

    # Cleanup
    if os.path.exists("test_config_api.db"):
        os.remove("test_config_api.db")
    if os.path.exists("test_export_config.yaml"):
        os.remove("test_export_config.yaml")
    if os.path.exists("test_import_config.yaml"):
        os.remove("test_import_config.yaml")

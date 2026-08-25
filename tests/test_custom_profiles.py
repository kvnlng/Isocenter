import pytest
import os
import yaml
from isocenter.config_manager import load_unified_config

def test_load_custom_privacy_profile(tmp_path):
    """
    Verifies that 'privacy_profile' can accept a file path to an external YAML file,
    and that the tags from that file are correctly loaded/merged.
    """
    # 1. Create a custom profile YAML
    custom_profile = {
        "phi_tags": {
            "0010,0010": {"action": "REMOVE", "name": "CustomName"},
            "0008,0080": {"action": "EMPTY", "name": "CustomInst"}
        }
    }
    profile_path = tmp_path / "my_custom_profile.yaml"
    with open(profile_path, "w") as f:
        yaml.dump(custom_profile, f)

    # 2. Create a main config referencing the profile
    main_config = {
        "privacy_profile": str(profile_path),
        "phi_tags": {
            "0010,0020": {"action": "REPLACE", "name": "PatientID"} # Should stay
        }
    }
    config_path = tmp_path / "main_config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(main_config, f)

    # 3. Load Unified Config
    config = load_unified_config(str(config_path))

    # 4. Verify Merging
    phi_tags = config.get("phi_tags", {})

    # Check inherited tags
    assert "0010,0010" in phi_tags
    assert phi_tags["0010,0010"]["action"] == "REMOVE"

    assert "0008,0080" in phi_tags
    assert phi_tags["0008,0080"]["action"] == "EMPTY"

    # Check local override/augmentation
    assert "0010,0020" in phi_tags
    assert phi_tags["0010,0020"]["action"] == "REPLACE"

def test_load_nonexistent_custom_profile(tmp_path, caplog):
    """
    Verifies graceful handling of missing custom profile files.
    """
    main_config = {
        "privacy_profile": "/path/to/nonexistent/profile.yaml"
    }
    config_path = tmp_path / "bad_config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(main_config, f)

    # Should log a warning but not crash
    config = load_unified_config(str(config_path))

    # Check that it didn't explode. 'phi_tags' might be None or missing if not in config.
    assert config.get("phi_tags") is None

    # Verify warning log
    assert "Unknown privacy profile reference" in caplog.text

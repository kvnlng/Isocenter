
import pytest
import os
import json
from gantry.config_manager import load_unified_config, ConfigLoader
from gantry.profiles import BASIC_PROFILE

def test_load_basic_profile(tmp_path):
    # 1. Create config using "privacy_profile": "basic"
    config_path = tmp_path / "config_basic.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({
            "version": "2.0",
            "privacy_profile": "basic",
            # No user overrides
        }, f)

    # 2. Load via top-level
    config = load_unified_config(str(config_path))

    # 3. Verify Basic Profile Rules
    tags = config["phi_tags"]
    assert tags["0010,0010"]["action"] == "REMOVE" # Patient Name
    assert tags["0008,0020"]["action"] == "REMOVE" # Study Date

def test_profile_override(tmp_path):
    # 1. Create config using "basic" but override Patient Name to KEEP
    config_path = tmp_path / "config_override.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({
            "version": "2.0",
            "privacy_profile": "basic",
            "phi_tags": {
                "0010,0010": {"action": "KEEP", "name": "Patient Name (Kept)"},
                "0008,0090": {"action": "EMPTY"} # Referring Physician overridden to EMPTY (default REMOVE)
            }
        }, f)

    config = load_unified_config(str(config_path))
    tags = config["phi_tags"]

    # Verify Override
    assert tags["0010,0010"]["action"] == "KEEP"
    assert tags["0008,0090"]["action"] == "EMPTY"

    # Verify other profile tags (not overridden) still exist
    assert tags["0010,0020"]["action"] == "REMOVE" # Patient ID

def test_legacy_loader_integration(tmp_path):
    # Verify that ConfigLoader.load_unified_config returns tuple correctly wrapped
    config_path = tmp_path / "config_legacy_adapter.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({
            "privacy_profile": "basic"
        }, f)

    phi_tags, _, _, _ = ConfigLoader.load_unified_config(str(config_path))

    assert isinstance(phi_tags, dict)
    assert phi_tags["0010,0010"]["action"] == "REMOVE"

def test_unknown_profile(tmp_path):
    config_path = tmp_path / "config_unknown.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({"privacy_profile": "super_secret_profile"}, f)

    config = load_unified_config(str(config_path))
    # Should just ignore and load empty/rules
    assert config.get("phi_tags", {}) == {}


def test_basic_profile_covers_datetime_twins_of_the_dates_it_removes():
    """Every date/time tag the profile removes must have its DT-valued twin
    covered too.

    (0008,002A) Acquisition DateTime carries the same information as
    (0008,0022) Acquisition Date, which the profile removes. Leaving the DT
    twin out means raw acquisition timing survives anonymize() while the
    plain date is stripped -- the profile looks complete and is not.
    """
    required = {
        "0008,002a": "Acquisition DateTime",
        "0040,0244": "Performed Procedure Step Start Date",
        "0040,0245": "Performed Procedure Step Start Time",
        "0040,0250": "Performed Procedure Step End Date",
        "0040,0251": "Performed Procedure Step End Time",
    }
    missing = [tag for tag in required if tag not in BASIC_PROFILE]
    assert not missing, (
        f"Basic profile is missing date/time tags: {missing}. These carry "
        "acquisition and procedure timing that survives anonymize().")


def test_basic_profile_keys_are_all_lowercase():
    """Ingested attribute keys are lowercased, so an uppercase profile key
    never matches and silently disables that tag (see #41, where
    0008,103E shipped uppercase and Series Description was never
    remediated on any documented path).
    """
    uppercase = [k for k in BASIC_PROFILE if k != k.lower()]
    assert not uppercase, f"profile keys must be lowercase: {uppercase}"

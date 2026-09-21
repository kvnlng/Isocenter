import pytest
import json
import os
from isocenter.config_manager import ConfigLoader

def test_load_valid_config(tmp_path):
    data = {
        "version": "2.0",
        "machines": [{"serial_number": "SN1", "redaction_zones": []}]
    }
    p = tmp_path / "valid.yaml"
    import yaml
    p.write_text(yaml.dump(data))

    rules = ConfigLoader.load_redaction_rules(str(p))
    assert len(rules) == 1
    assert rules[0]["serial_number"] == "SN1"

def test_missing_file():
    # Verify FileNotFoundError is raised
    with pytest.raises(FileNotFoundError):
        ConfigLoader.load_redaction_rules("/non/existent/path.yaml")

def test_invalid_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("unclosed: { brace")

    with pytest.raises(ValueError, match="Invalid YAML"):
        ConfigLoader.load_redaction_rules(str(p))

def test_validation_logic(tmp_path):
    import yaml
    # 1. Missing SN
    data = {"machines": [{"redaction_zones": []}]}
    p = tmp_path / "bs1.yaml"
    p.write_text(yaml.dump(data))
    with pytest.raises(ValueError, match="Missing 'serial_number'"):
        ConfigLoader.load_redaction_rules(str(p))

    # 2. Invalid ROI Type
    data = {"machines": [{"serial_number": "S", "redaction_zones": [{"roi": "bad"}]}]}
    p = tmp_path / "bs2.yaml"
    p.write_text(yaml.dump(data))
    with pytest.raises(ValueError, match="ROI must be a list"):
        ConfigLoader.load_redaction_rules(str(p))

    # 3. Invalid ROI Range (Start > End)
    data = {"machines": [{"serial_number": "S", "redaction_zones": [{"roi": [10, 5, 0, 10]}]}]}
    p = tmp_path / "bs3.yaml"
    p.write_text(yaml.dump(data))
    with pytest.raises(ValueError, match="Invalid ROI logic"):
        ConfigLoader.load_redaction_rules(str(p))

def test_phi_config_default():
    # With no path the default PHI policy is the floor, in Python (#495).
    # It was the shipped `phi_tags.json`, and this was #388's positive
    # control for that resource being present; the resource is deleted,
    # so the control is now that the default is the floor and not merely
    # "a mapping" -- `{}` is a mapping.
    from isocenter.profiles import FLOOR_POLICY

    tags = ConfigLoader.load_phi_config(None)
    assert tags == FLOOR_POLICY

def test_phi_config_override(tmp_path):
    data = {"phi_tags": {"0010,0010": "PatientName"}}
    p = tmp_path / "phi.yaml"
    import yaml
    p.write_text(yaml.dump(data))

    tags = ConfigLoader.load_phi_config(str(p))
    assert "0010,0010" in tags
    assert tags["0010,0010"] == "PatientName"


# --- load_phi_config validates what load_unified_config validates (#590) ---

_NOT_A_TAG = ("phi_tags key 'patient_id' is not a 'gggg,eeee' tag (four hex "
              "digits, a comma, four hex digits, such as '0010,0010'); the "
              "scan reads no tag by that key, so the rule would never run")


@pytest.mark.parametrize("shape", ["phi_tags-key", "root-mapping"])
def test_load_phi_config_refuses_a_key_the_scan_would_never_read(tmp_path,
                                                                 shape):
    """Both lax arms return `_validated_phi_tags`' answer, not the raw dict.

    `load_unified_config` refused `patient_id` while `load_phi_config`
    handed it back, so one file loaded or failed by which entry point
    read it (#590). The root-mapping arm names its source as such: the
    file has no `phi_tags` key for the message to point at.
    """
    import yaml
    rules = {"patient_id": {"action": "REPLACE"}}
    data = {"phi_tags": rules} if shape == "phi_tags-key" else rules
    p = tmp_path / "phi.yaml"
    p.write_text(yaml.dump(data))

    with pytest.raises(ValueError) as exc:
        ConfigLoader.load_phi_config(str(p))
    message = str(exc.value)
    assert _NOT_A_TAG in message, message
    source = f"{p} (root mapping)" if shape == "root-mapping" else str(p)
    assert message.startswith(f"{source}: "), message


@pytest.mark.parametrize("shape", ["phi_tags-key", "root-mapping"])
def test_load_phi_config_lowercases_keys(tmp_path, shape):
    """Killer for "return the raw mapping": the validator lowercases keys."""
    import yaml
    rules = {"0010,00A0": "PatientName"}
    data = {"phi_tags": rules} if shape == "phi_tags-key" else rules
    p = tmp_path / "phi.yaml"
    p.write_text(yaml.dump(data))

    tags = ConfigLoader.load_phi_config(str(p))
    assert tags == {"0010,00a0": "PatientName"}, tags

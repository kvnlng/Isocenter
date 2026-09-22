"""The configuration loader's basic contract.

These went through `ConfigLoader.load_redaction_rules` and
`load_phi_config` until #729 deleted both: one door reads a configuration
file now, `load_unified_config`, and the same checks are asserted there.
The tests whose only subject was a deleted door (the root-mapping arm of
`load_phi_config`, its no-path default) went with it; `PhiInspector()`'s
default is `test_one_loader_reads_a_config.py`'s.
"""
import pytest
import yaml

from isocenter.config_manager import ConfigLoader


def _write(tmp_path, data, name="c.yaml"):
    p = tmp_path / name
    p.write_text(yaml.dump(data))
    return str(p)


def test_load_valid_config(tmp_path):
    path = _write(tmp_path, {
        "version": "2.0",
        "machines": [{"serial_number": "SN1", "redaction_zones": []}]
    })
    _, rules, _, _, _ = ConfigLoader.load_unified_config(path)
    assert len(rules) == 1
    assert rules[0]["serial_number"] == "SN1"


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        ConfigLoader.load_unified_config("/non/existent/path.yaml")


def test_invalid_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("unclosed: { brace")
    with pytest.raises(ValueError, match="Invalid YAML"):
        ConfigLoader.load_unified_config(str(p))


@pytest.mark.parametrize("machine, fragment", [
    ({"redaction_zones": []}, "Missing 'serial_number'"),
    ({"serial_number": "S", "redaction_zones": [{"roi": "bad"}]}, "ROI must be a list"),
    ({"serial_number": "S", "redaction_zones": [{"roi": [10, 5, 0, 10]}]}, "Invalid ROI logic"),
], ids=["missing serial", "roi type", "roi range"])
def test_validation_logic(tmp_path, machine, fragment):
    path = _write(tmp_path, {"machines": [machine]})
    with pytest.raises(ValueError, match=fragment):
        ConfigLoader.load_unified_config(path)


_NOT_A_TAG = ("phi_tags key 'patient_id' is not a 'gggg,eeee' tag (four hex "
              "digits, a comma, four hex digits, such as '0010,0010'); the "
              "scan reads no tag by that key, so the rule would never run")


def test_a_key_the_scan_would_never_read_is_refused(tmp_path):
    """#590's refusal, on the one door that remains."""
    path = _write(tmp_path, {"privacy_profile": "none",
                             "phi_tags": {"patient_id": {"action": "REPLACE"}}})
    with pytest.raises(ValueError) as exc:
        ConfigLoader.load_unified_config(path)
    message = str(exc.value)
    assert _NOT_A_TAG in message, message
    assert message.startswith(f"{path}: "), message


def test_tag_keys_are_lowercased(tmp_path):
    """Killer for "return the raw mapping": the validator lowercases keys."""
    path = _write(tmp_path, {"privacy_profile": "none",
                             "phi_tags": {"0010,00A0": "PatientName"}})
    tags, _, _, _, _ = ConfigLoader.load_unified_config(path)
    assert tags == {"0010,00a0": "PatientName"}, tags

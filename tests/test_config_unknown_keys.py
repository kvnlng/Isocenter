"""A key the configuration schema does not have is refused by name (#712).

Measured on 63a64158: the loader read six top-level keys and ignored
every other, at every level. A misspelt `remove_private_tag: false`
loaded and the private tags the file asked to keep were removed; a
machine rule's singular `redaction_zone:` loaded a rule with no zones,
so the machine was not redacted; a `phi_tags` rule's `actoin: KEEP` left
the action at REPLACE, so the value was replaced where the file asked to
keep it; `machine_rules:` was read as `machines` (and silently dropped
beside it); and an external profile file carrying `privacy_profile:
basic` beside one rule loaded as that one rule.

Every refusal is asserted with the configuration unchanged after it (the
sentinel pattern of `test_load_config_raises.py`, #456), and the files
the library itself wrote in 0.9.x are loaded to show they still load.
"""
import os
import sqlite3

import pytest
import yaml

from isocenter.config_manager import ConfigLoader
from isocenter.configuration import IsocenterConfiguration
from isocenter.session import DicomSession

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

FIELDS = ("phi_tags", "rules", "date_jitter", "remove_private_tags",
          "privacy_profile", "config_path")

TOP_LEVEL_LIST = ("date_jitter, machines, phi_tags, privacy_profile, "
                  "remove_private_tags, version")
RULE_LIST = "comment, manufacturer, model_name, redaction_zones, serial_number"


def _set_sentinels(configuration, tmp_path):
    """A prior configuration in every field `load_config` writes, assigned
    rather than set through a method that would auto-save."""
    configuration.phi_tags = {"9999,0001": {"action": "REMOVE", "name": "sentinel"}}
    configuration.rules = [{"serial_number": "PRIOR"}]
    configuration.date_jitter = {"min_days": -7, "max_days": -7}
    configuration.remove_private_tags = False
    configuration.privacy_profile = "prior"
    configuration.config_path = str(tmp_path / "prior.yaml")
    return {field: yaml.safe_load(yaml.safe_dump(getattr(configuration, field)))
            for field in FIELDS}


def _refused(tmp_path, text, name="cfg.yaml"):
    """The message `load_config` raises for `text`; asserts `ValueError`
    and that every field kept its sentinel."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        before = _set_sentinels(session.configuration, tmp_path)
        with pytest.raises(ValueError) as caught:
            session.load_config(str(path))
        after = {field: getattr(session.configuration, field) for field in FIELDS}
    assert after == before
    return str(caught.value)


def _loaded(tmp_path, text, name="cfg.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return ConfigLoader.load_unified_config(str(path))


# --- The top level --------------------------------------------------------


def test_a_misspelt_top_level_key_is_refused(tmp_path):
    """Kills the top-level check deleted."""
    message = _refused(tmp_path, "remove_private_tag: false\n")
    assert "unknown key 'remove_private_tag' at the top level" in message, message
    assert "did you mean 'remove_private_tags'?" in message, message
    assert TOP_LEVEL_LIST in message, message
    assert "cfg.yaml" in message, message


def test_every_unknown_top_level_key_is_named(tmp_path):
    """Kills first-only reporting."""
    message = _refused(tmp_path, "rules: []\nzones: []\n")
    assert "unknown keys 'rules', 'zones'" in message, message
    assert TOP_LEVEL_LIST in message, message


def test_a_non_string_top_level_key_is_named(tmp_path):
    """YAML allows `2:` and `yes:` (the bool True) as keys; they are named,
    sorted by `str`, not a `TypeError` from sorting mixed types. (Not
    `1:` beside `yes:`: True == 1, so YAML folds them into one key.)"""
    message = _refused(tmp_path, "2: a\nyes: b\nzones: []\n")
    assert "unknown keys 2, True, 'zones'" in message, message


def test_a_file_carrying_every_top_level_key_loads(tmp_path):
    """The positive control. Kills an allowlist missing one key."""
    tags, rules, jitter, remove_private, profile = _loaded(
        tmp_path,
        'version: "2.0"\n'
        "privacy_profile: basic\n"
        "phi_tags:\n  '0018,1030': {action: REMOVE, name: Protocol}\n"
        "date_jitter: {min_days: -30, max_days: -10}\n"
        "remove_private_tags: false\n"
        "machines:\n  - serial_number: SN-1\n    redaction_zones: [[0, 10, 0, 20]]\n")
    assert profile == "basic@2026c"
    assert tags["0018,1030"] == {"action": "REMOVE", "name": "Protocol"}
    assert rules[0]["serial_number"] == "SN-1"
    assert jitter == {"min_days": -30, "max_days": -10}
    assert remove_private is False


@pytest.mark.parametrize("with_machines", [False, True])
def test_machine_rules_is_refused_with_its_rename(tmp_path, with_machines):
    """`machine_rules` was an alias `load_config` read as `machines`, and
    beside `machines: []` its rule was silently dropped. Kills the alias
    restored, and the both-present case preferring one of them."""
    text = "machine_rules:\n  - serial_number: SN1\n"
    if with_machines:
        text = "machines: []\n" + text
    message = _refused(tmp_path, text)
    assert "'machine_rules' is an old spelling of 'machines'; rename it" in message, message


# --- Machine rules and zones ----------------------------------------------


def test_a_misspelt_rule_key_is_refused(tmp_path):
    """A singular `redaction_zone:` loaded a rule with no zones. Kills the
    rule-level check deleted."""
    message = _refused(tmp_path, "machines:\n  - serial_number: SN1\n"
                                 "    redaction_zone: [[0, 50, 0, 800]]\n")
    assert "Rule #0 (SN1): unknown key 'redaction_zone'" in message, message
    assert "did you mean 'redaction_zones'?" in message, message
    assert RULE_LIST in message, message


def test_a_misspelt_serial_key_is_named_before_the_missing_serial(tmp_path):
    """Kills the key check placed after the serial check, which would
    blame a missing `serial_number` for a misspelt one."""
    message = _refused(tmp_path, "machines:\n  - serial_numbr: SN1\n")
    assert "unknown key 'serial_numbr'" in message, message
    assert "did you mean 'serial_number'?" in message, message
    assert "Missing" not in message, message


def test_a_rule_carrying_every_known_key_loads(tmp_path):
    """Kills an allowlist missing `comment` or `manufacturer`."""
    _, rules, _, _, _ = _loaded(
        tmp_path,
        "machines:\n  - serial_number: SN1\n    manufacturer: ACME\n"
        "    model_name: M1\n    comment: a note\n"
        "    redaction_zones: [[0, 10, 0, 10]]\n")
    assert rules[0]["comment"] == "a note"
    assert rules[0]["manufacturer"] == "ACME"


def test_a_zone_with_an_unknown_key_is_refused_and_note_is_allowed(tmp_path):
    """Kills the zone check deleted, and `note` missing from the
    allowlist (create_config writes it)."""
    message = _refused(tmp_path, "machines:\n  - serial_number: SN1\n"
                                 "    redaction_zones: [{roi: [0, 4, 0, 4], nte: x}]\n")
    assert "Rule #0 (SN1), Zone #0: unknown key 'nte'" in message, message
    assert "note, roi" in message, message
    _, rules, _, _, _ = _loaded(tmp_path, "machines:\n  - serial_number: SN1\n"
                                "    redaction_zones: [{roi: [0, 4, 0, 4], note: x}]\n")
    assert rules[0]["redaction_zones"][0]["note"] == "x"


def test_a_misspelt_zone_roi_is_named_not_blamed_on_the_roi(tmp_path):
    """`{rio: [...]}`: the unknown key, not "ROI must be a list of 4
    integers". Kills the zone key check placed after the ROI check."""
    message = _refused(tmp_path, "machines:\n  - serial_number: SN1\n"
                                 "    redaction_zones: [{rio: [0, 4, 0, 4]}]\n")
    assert "unknown key 'rio'" in message, message


# --- phi_tags rules -------------------------------------------------------


@pytest.mark.parametrize("tag, rule", [
    ("0008,0080", "{actoin: KEEP}"),
    # A US tag: a check after the VR checks would default the action to
    # REPLACE and refuse with #560's "cannot hold it" instead. It was a DA
    # tag until #557 gave a value-less REPLACE on a DA its dummy, which
    # loads, and the order mutant then survived.
    ("0028,0010", "{actoin: JITTER}"),
])
def test_a_misspelt_phi_rule_key_is_refused(tmp_path, tag, rule):
    """Kills the phi rule check deleted, and placed after the VR checks."""
    message = _refused(tmp_path, f"phi_tags:\n  '{tag}': {rule}\n")
    assert f"phi_tags['{tag}'] has unknown key 'actoin'" in message, message
    assert "did you mean 'action'?" in message, message
    assert "cannot hold it" not in message, message


def test_a_capitalised_phi_rule_key_is_refused(tmp_path):
    """`Action: KEEP` read as no action at all (REPLACE)."""
    message = _refused(tmp_path, "phi_tags:\n  '0008,0080': {Action: KEEP}\n")
    assert "unknown key 'Action'" in message, message


def test_replacement_keeps_its_rename_message(tmp_path):
    """`replacement` is the one known-refused key: its #538 message, not a
    generic unknown key. (Every door is covered by
    `test_a_rule_that_cannot_be_honoured_is_refused.py`'s
    `replacement-key`; this pins that the generic check does not
    pre-empt it, alongside another unknown key.)"""
    message = _refused(tmp_path,
                       "phi_tags:\n  '0008,0080': {action: REPLACE, replacement: X}\n")
    assert "the key is 'value' (0.9.8, #538), so rename it" in message, message
    assert "unknown key" not in message, message


@pytest.mark.parametrize("text", [
    "replacement: X\n",
    "machines:\n  - serial_number: SN1\n    replacement: X\n",
    "machines:\n  - serial_number: SN1\n    redaction_zones: [{roi: [0, 4, 0, 4], replacement: X}]\n",
])
def test_replacement_is_exempt_only_inside_a_phi_rule(tmp_path, text):
    """The exemption that keeps #538's message belongs to the phi rule
    alone; anywhere else `replacement` is an unknown key like any other.
    Kills the exemption applied at every level."""
    assert "unknown key 'replacement'" in _refused(tmp_path, text)


def test_a_phi_rule_with_an_unknown_key_assigned_in_code_is_refused_by_audit(tmp_path):
    """Kills the check at the loader only: `audit()` over a policy
    assigned in code refuses it too, before a project secret exists."""
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.configuration.phi_tags = {"0008,0080": {"action": "KEEP", "nam": "x"}}
        with pytest.raises(ValueError) as caught:
            session.audit()
    assert "session.configuration.phi_tags" in str(caught.value)
    assert "unknown key 'nam'" in str(caught.value)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_a_profile_rule_with_an_unknown_key_is_refused_though_the_file_overrides_it(
        tmp_path):
    """An external profile's rule is checked before it is merged, so a
    typo in it is refused even where the configuration overrides that
    tag. Kills the phi rule check only on the merged policy."""
    profile = tmp_path / "profile.yaml"
    profile.write_text("phi_tags:\n  '0008,0080': {actoin: KEEP}\n", encoding="utf-8")
    message = _refused(tmp_path, f"privacy_profile: {profile}\n"
                                 "phi_tags:\n  '0008,0080': {action: REMOVE}\n")
    assert str(profile) in message and "unknown key 'actoin'" in message, message


# --- External profile files ----------------------------------------------


def test_an_external_profile_carries_only_phi_tags(tmp_path):
    """Owner ruling Q1: a profile carries `phi_tags` and an optional
    `version`, nothing else. Kills a profile's other keys ignored."""
    profile = tmp_path / "profile.yaml"
    profile.write_text("privacy_profile: basic\nremove_private_tags: false\n"
                       "phi_tags:\n  '0010,0010': {action: REMOVE}\n", encoding="utf-8")
    message = _refused(tmp_path, f"privacy_profile: {profile}\n")
    assert str(profile) in message, message
    assert "contributes only its phi_tags" in message, message
    assert "'privacy_profile', 'remove_private_tags'" in message, message


def test_a_versioned_external_profile_still_carries_only_phi_tags(tmp_path):
    """A `version` line does not open the profile to other keys (review of
    #728, R8). Kills the key check skipped when the profile declares a
    version."""
    profile = tmp_path / "profile.yaml"
    profile.write_text('version: "2.0"\nprivacy_profile: basic\n'
                       "phi_tags:\n  '0010,0010': {action: REMOVE}\n", encoding="utf-8")
    message = _refused(tmp_path, f"privacy_profile: {profile}\n")
    assert "contributes only its phi_tags" in message, message
    assert "'privacy_profile'" in message, message


def test_an_external_profile_with_a_version_loads(tmp_path):
    """The positive control for the profile door."""
    profile = tmp_path / "profile.yaml"
    profile.write_text('version: "2.0"\nphi_tags:\n  \'0010,0010\': {action: REMOVE}\n',
                       encoding="utf-8")
    tags, _, _, _, profile_name = _loaded(tmp_path, f"privacy_profile: {profile}\n")
    assert tags == {"0010,0010": {"action": "REMOVE"}}
    assert profile_name == str(profile)


# --- What the library wrote in 0.9.x --------------------------------------


def test_what_0_9_8_wrote_still_loads():
    """The committed 0.9.8 scaffold and a committed 0.9.8 auto-save load.

    `autosaved_config_0_9_8.yaml` was captured at 63a64158 -- `isocenter`
    extracted by `git archive 63a64158 isocenter` and put first on
    `PYTHONPATH`, `isocenter.__file__` read back from it -- by
    `load_config` of a file carrying a KB-shaped rule (a `comment`, a
    zone mapping with a `note`, a list zone) under `privacy_profile:
    none`, then `configuration.add_rule("SN-ADDED-2", manufacturer="ACME",
    model="Scanner 9", zones=[[10, 50, 20, 80]])` and
    `set_phi_tag("0008,1030", "REMOVE")`; the file auto-save wrote is
    committed verbatim. The expected values below are that session's
    `rules`, `phi_tags`, `date_jitter` and `remove_private_tags` after
    the two calls, as printed at capture. Kills an allowlist that forgets
    what the library writes (`note`, `manufacturer`, `comment`).

    `scaffolded_config_0_9_8.yaml` is `scaffolded_config.golden.yaml` as
    the v0.9.8 tag has it (blob ad2f3ae0, identical at 63a64158 and
    d47f2b43), copied verbatim before #714 regenerated the golden: the
    golden is what *this* version's `create_config()` writes, so loading
    it here would prove only that 1.0 reads its own output."""
    ConfigLoader.load_unified_config(
        os.path.join(FIXTURES, "scaffolded_config_0_9_8.yaml"))

    tags, rules, jitter, remove_private, profile = ConfigLoader.load_unified_config(
        os.path.join(FIXTURES, "autosaved_config_0_9_8.yaml"))
    assert rules == [
        {"serial_number": "SN-KB-1", "model_name": "LOGIQ E9",
         "comment": "Header band across the top",
         "redaction_zones": [{"roi": [0, 60, 0, 1024], "note": "patient banner"},
                             [900, 960, 0, 300]]},
        {"serial_number": "SN-ADDED-2", "manufacturer": "ACME",
         "model_name": "Scanner 9", "redaction_zones": [[10, 50, 20, 80]]},
    ]
    assert tags == {
        "0010,0010": {"name": "Patient's Name", "action": "REPLACE"},
        "0008,0080": {"name": "Institution Name", "action": "REPLACE",
                      "value": "SITE-A"},
        "0010,0040": {"name": "Patient's Sex", "action": "KEEP"},
        "0008,1030": {"name": "Custom Tag", "action": "REMOVE"},
    }
    assert jitter == {"min_days": -30, "max_days": -10}
    assert remove_private is False
    assert profile is None


def test_a_0_9_8_autosave_with_null_metadata_still_loads():
    """Review of #728, finding 1. 0.9.8's auto-save wrote `null` for a
    rule's `manufacturer` and `model_name` after `add_rule(serial,
    eq.manufacturer, eq.model_name, zones)` on equipment without those tags,
    and for `comment` after `update_rule(serial, {"comment": None})`.
    `autosaved_config_0_9_8_null_metadata.yaml` is exactly that file,
    captured at 63a64158 (the package extracted from that commit first on
    `PYTHONPATH`, `isocenter.__file__` read back) by
    `IsocenterConfiguration(config_path=...)`, `add_rule("SN-1",
    manufacturer=None, model=None, zones=[[0, 4, 0, 4]])` and
    `update_rule("SN-1", {"comment": None})`, and committed verbatim. It
    loads, and the nulls load as the nulls it was saved from. Kills a null
    refused in any of the three rule fields."""
    _, rules, _, _, _ = ConfigLoader.load_unified_config(
        os.path.join(FIXTURES, "autosaved_config_0_9_8_null_metadata.yaml"))
    assert rules == [{"serial_number": "SN-1", "manufacturer": None,
                      "model_name": None, "redaction_zones": [[0, 4, 0, 4]],
                      "comment": None}]


# --- The in-code doors that write the file --------------------------------


def _configuration_with_a_rule(tmp_path):
    # Auto-save on: these tests read the file each door writes, and since
    # #715 the doors write only when asked.
    path = tmp_path / "project.yaml"
    configuration = IsocenterConfiguration(config_path=str(path), auto_save=True)
    configuration.add_rule("SN1", redaction_zones=[[0, 4, 0, 4]])
    return configuration, path


def test_update_rule_refuses_a_key_the_loader_would_refuse(tmp_path):
    """Before #712 the typo was stored and auto-saved, writing a file the
    session's own loader then refused. Kills `update_rule` not validating,
    and validating after the in-place `update` (the typo left in
    memory)."""
    configuration, path = _configuration_with_a_rule(tmp_path)
    rules_before = yaml.safe_load(yaml.safe_dump(configuration.rules))
    bytes_before = path.read_bytes()
    with pytest.raises(ValueError, match="unknown key 'redaction_zone'"):
        configuration.update_rule("SN1", {"redaction_zone": [[0, 8, 0, 8]]})
    assert configuration.rules == rules_before
    assert path.read_bytes() == bytes_before


def test_update_rule_keeps_the_rule_it_returns_by_reference(tmp_path):
    """`get_rule` documents a reference; an update that validates still
    updates that dict rather than replacing it."""
    configuration, _ = _configuration_with_a_rule(tmp_path)
    rule = configuration.get_rule("SN1")
    configuration.update_rule("SN1", {"model_name": "M2"})
    assert rule["model_name"] == "M2"
    ConfigLoader.load_unified_config(configuration.config_path)


def test_add_rule_refuses_a_numeric_serial(tmp_path):
    """Kills `add_rule` not validating."""
    configuration, path = _configuration_with_a_rule(tmp_path)
    rules_before = yaml.safe_load(yaml.safe_dump(configuration.rules))
    bytes_before = path.read_bytes()
    with pytest.raises(ValueError, match="'serial_number' must be a string"):
        configuration.add_rule(12345)
    assert configuration.rules == rules_before
    assert path.read_bytes() == bytes_before


def test_add_rule_refusing_a_replacement_keeps_the_rule_it_would_replace(tmp_path):
    """`add_rule` deletes the serial's existing rule first, and the delete
    auto-saves: a refusal after it would lose the rule and rewrite the
    file. Kills the validation placed after `delete_rule`."""
    configuration, path = _configuration_with_a_rule(tmp_path)
    rules_before = yaml.safe_load(yaml.safe_dump(configuration.rules))
    bytes_before = path.read_bytes()
    with pytest.raises(ValueError, match="ROI"):
        configuration.add_rule("SN1", redaction_zones=[[0, 1, 2]])
    assert configuration.rules == rules_before
    assert path.read_bytes() == bytes_before


def test_add_rule_that_passes_still_replaces_the_serials_rule(tmp_path):
    """The delete moved below the validation still runs: a valid
    `add_rule` for a serial that has a rule replaces it rather than adding
    a second. Kills the moved `delete_rule` call deleted (a probe survivor
    on this branch before this test)."""
    configuration, _ = _configuration_with_a_rule(tmp_path)
    configuration.add_rule("SN1", redaction_zones=[[0, 8, 0, 8]])
    assert [r["redaction_zones"] for r in configuration.rules] == [[[0, 8, 0, 8]]]


def test_update_rule_still_refuses_a_serial_change(tmp_path):
    """The guard ahead of the new validation. Kills it inverted or
    deleted (a probe survivor on this branch before this test)."""
    configuration, _ = _configuration_with_a_rule(tmp_path)
    with pytest.raises(ValueError, match="cannot be changed"):
        configuration.update_rule("SN1", {"serial_number": "SN2"})
    configuration.update_rule("SN1", {"serial_number": "SN1", "model_name": "M"})
    assert configuration.get_rule("SN1")["model_name"] == "M"


def test_add_rule_with_null_metadata_writes_a_file_that_loads(tmp_path):
    """`add_rule(serial, None, None)` -- an `Equipment` with no
    Manufacturer or model -- is accepted, and the file it auto-saves loads
    (review of #728, finding 1)."""
    path = tmp_path / "project.yaml"
    configuration = IsocenterConfiguration(config_path=str(path), auto_save=True)
    configuration.add_rule("SN-1", manufacturer=None, model_name=None, redaction_zones=[[0, 4, 0, 4]])
    configuration.update_rule("SN-1", {"comment": None})
    _, rules, _, _, _ = ConfigLoader.load_unified_config(str(path))
    assert rules[0]["manufacturer"] is None and rules[0]["comment"] is None


@pytest.mark.parametrize("updates, fragment", [
    ({"model_name": 5}, "'model_name' must be a string"),
    ({"redaction_zones": [[5, 1, 0, 1]]}, "Start > End"),
])
def test_update_rule_refuses_a_value_the_loader_would_refuse(tmp_path, updates, fragment):
    """The update's own values are what is judged, not the old rule's
    (review of #728, R3): the round trip spec §3.6 closes covers a wrong
    value as well as an unknown key. Kills `{**updates, **rule}`, under
    which the old values win and the check passes."""
    configuration, path = _configuration_with_a_rule(tmp_path)
    rules_before = yaml.safe_load(yaml.safe_dump(configuration.rules))
    bytes_before = path.read_bytes()
    with pytest.raises(ValueError, match=fragment):
        configuration.update_rule("SN1", updates)
    assert configuration.rules == rules_before
    assert path.read_bytes() == bytes_before

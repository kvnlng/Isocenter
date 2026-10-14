"""The `burned_in_text` configuration key: what loads, what is refused (#1108).

Measured on f31758d3: a configuration could say which fixed zones to
blank per machine and nothing about text that moves per image, and a
file carrying `burned_in_text:` was refused as an unknown top-level key.
The owner's ruling of 2026-10-14 adds one top-level key,

    burned_in_text:
      action: redact        # report | redact | withhold (required)
      min_confidence: 80    # 0..100, optional, default 80

with `null` read as absent, kept out of the PHI policy fingerprint, held
on the tier-2 attribute `IsocenterConfiguration.burned_in_text`, and
added to the unshipped 2.0 schema row rather than a 2.1 (Q7).

**Why this file imports what it does.** The loader through
`isocenter.config_manager` and the attribute through
`isocenter.configuration`, so both modules' probe rows are charged.
"""
import pytest
import yaml

from isocenter import config_manager
from isocenter.config_manager import ConfigLoader
from isocenter.configuration import IsocenterConfiguration
from isocenter.session import DicomSession as Session


def _write(tmp_path, text, name="c.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _loaded(tmp_path, text):
    return ConfigLoader.load_unified_config(str(_write(tmp_path, text)))


@pytest.mark.parametrize("action", ["report", "redact", "withhold"])
def test_each_action_loads_with_the_default_confidence_filled_in(tmp_path, action):
    """Kills an action missing from the accepted set, and the default
    left unfilled (two spellings of one policy would then hash apart)."""
    parts = _loaded(tmp_path, f"burned_in_text:\n  action: {action}\n")
    assert parts[5] == {"action": action, "min_confidence": 80}


def test_a_stated_confidence_is_kept(tmp_path):
    parts = _loaded(tmp_path,
                    "burned_in_text: {action: report, min_confidence: 62.5}\n")
    assert parts[5] == {"action": "report", "min_confidence": 62.5}


@pytest.mark.parametrize("text", ["machines: []\n", "burned_in_text:\n",
                                  "burned_in_text: null\n"])
def test_absent_and_null_are_no_policy(tmp_path, text):
    """`null` is absent, as the owner ruled; kills a null refused or read
    as a policy."""
    assert _loaded(tmp_path, text)[5] is None


@pytest.mark.parametrize("text, words", [
    ("burned_in_text: {min_confidence: 80}\n", "'burned_in_text' needs an 'action'"),
    ("burned_in_text: {action: Redact}\n", "'Redact'"),
    ("burned_in_text: {action: blank}\n", "report, redact, withhold"),
    ("burned_in_text: {action: null}\n", "needs an 'action'"),
    ("burned_in_text: {action: redact, min_confidence: true}\n", "min_confidence"),
    ("burned_in_text: {action: redact, min_confidence: '80'}\n", "min_confidence"),
    ("burned_in_text: {action: redact, min_confidence: -1}\n", "0 to 100"),
    ("burned_in_text: {action: redact, min_confidence: 101}\n", "0 to 100"),
    ("burned_in_text: {action: redact, min_confidence: .nan}\n", "0 to 100"),
    ("burned_in_text: {action: redact, min_box_height: 20}\n", "'min_box_height'"),
    ("burned_in_text: redact\n", "must be a mapping"),
    ("burned_in_text: [redact]\n", "must be a mapping"),
])
def test_a_policy_this_library_cannot_apply_is_refused_by_name(tmp_path, text, words):
    """Each refusal names the cause. `action` is not case-folded, as no
    `action` is elsewhere (#754). Kills each check deleted."""
    path = _write(tmp_path, text)
    with pytest.raises(ValueError) as caught:
        ConfigLoader.load_unified_config(str(path))
    assert words in str(caught.value), str(caught.value)
    assert str(path) in str(caught.value)


def test_a_refused_file_leaves_the_sessions_policy_as_it_was(tmp_path):
    good = _write(tmp_path, "burned_in_text: {action: withhold}\n", "good.yaml")
    bad = _write(tmp_path, "burned_in_text: {action: wipe}\n", "bad.yaml")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(good))
        with pytest.raises(ValueError):
            session.load_config(str(bad))
        assert session.configuration.burned_in_text == {
            "action": "withhold", "min_confidence": 80}


def test_load_config_assigns_it_and_a_file_without_it_clears_it(tmp_path):
    """Every load assigns the key, as it assigns `date_jitter`; a policy
    left from an earlier file would OCR a store whose file never asked."""
    with Session(str(tmp_path / "s.db")) as session:
        assert session.configuration.burned_in_text is None
        session.load_config(str(_write(
            tmp_path, "burned_in_text: {action: redact, min_confidence: 90}\n",
            "a.yaml")))
        assert session.configuration.burned_in_text == {
            "action": "redact", "min_confidence": 90}
        session.load_config(str(_write(tmp_path, "machines: []\n", "b.yaml")))
        assert session.configuration.burned_in_text is None


def test_save_writes_the_policy_and_reloads_to_it(tmp_path):
    path = _write(tmp_path, "burned_in_text: {action: report, min_confidence: 70}\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.save()
        assert yaml.safe_load(path.read_text())["burned_in_text"] == {
            "action": "report", "min_confidence": 70}
        session.load_config(str(path))
        assert session.configuration.burned_in_text == {
            "action": "report", "min_confidence": 70}


def test_save_writes_no_line_when_there_is_no_policy(tmp_path):
    """Kills a `burned_in_text: null` line written for every file, which
    a 1.0 library before this key would refuse as unknown."""
    path = _write(tmp_path, "machines: []\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.save()
    assert "burned_in_text" not in path.read_text()


def test_create_config_carries_the_policy(tmp_path):
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.burned_in_text = {"action": "withhold",
                                                "min_confidence": 85}
        out = tmp_path / "scaffold.yaml"
        session.create_config(str(out))
        session.load_config(str(out))
        assert session.configuration.burned_in_text == {
            "action": "withhold", "min_confidence": 85}


def test_create_config_refuses_a_policy_assigned_in_code_that_would_not_load(tmp_path):
    """The scaffold carries the policy, so one the loader refuses would
    make a file that does not load, as #731 refuses a bad range."""
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.burned_in_text = {"action": "blank"}
        with pytest.raises(ValueError, match="report, redact, withhold"):
            session.create_config(str(tmp_path / "scaffold.yaml"))
    assert not (tmp_path / "scaffold.yaml").exists()


def test_the_policy_is_not_in_the_phi_policy_fingerprint():
    """Zones are not in it, and neither is this (owner ruling): a
    `(0012,0063)` short hash must not move when the key is added."""
    plain = IsocenterConfiguration()
    with_policy = IsocenterConfiguration()
    with_policy.burned_in_text = {"action": "redact", "min_confidence": 80}
    assert plain._scan_policy() == with_policy._scan_policy()


def test_the_key_set_is_the_one_the_schema_row_names():
    """`tests/test_config_schema_version.py` pins the row; this pins that
    the sub-keys are one frozenset beside the others."""
    assert config_manager._BURNED_IN_TEXT_KEYS == frozenset({"action", "min_confidence"})
    assert "burned_in_text" in config_manager._TOP_LEVEL_KEYS

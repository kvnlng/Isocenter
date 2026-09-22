"""What `IsocenterConfiguration.save()` writes (#715).

Measured on 63a64158: after `load_config()` of the 7-line file below, one
`add_rule()` rewrote it as 1,872 lines -- the comments gone, all 620
rules of the basic profile pasted under `phi_tags`. A file naming no
profile came back as `privacy_profile: none` plus the 620 floor rules.
Pasting the profile froze this release's table into a file that had
asked for it by name, which is what pinning profile names (#714) exists
to prevent.

`save()` now names the profile (the pinned name, an external file's path,
`none`, or no line for the floor) and writes under `phi_tags` only the
rules that differ from that base's, compared whole. The invariant every
test here leans on: a file `save()` writes loads to the configuration it
was written from.

**Why this file imports what it does.** `IsocenterConfiguration` through
`isocenter.configuration` and the loader through `isocenter.config_manager`,
so both modules' probe rows are charged.
"""
import shutil
from pathlib import Path

import pytest
import yaml

from isocenter.config_manager import ConfigLoader
from isocenter.configuration import IsocenterConfiguration
from isocenter.profiles import BASIC_PROFILE
from isocenter.session import DicomSession as Session

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The issue's own reproduction (#715), byte for byte what the spec
#: measured: two comment lines, a quoted version, a trailing comment, a
#: flow-style mapping.
SEVEN_LINE_CONFIG = """\
# Site policy for the registry export.
# Reviewed by the privacy office.
version: "2.0"
privacy_profile: basic   # the PS3.15 basic profile
date_jitter: {min_days: -30, max_days: -10}
remove_private_tags: true
machines: []
"""


def _write(tmp_path, text, name="c.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _parsed(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _state(configuration):
    """The fields a round trip must reproduce."""
    return {
        "phi_tags": configuration.phi_tags,
        "rules": configuration.rules,
        "date_jitter": configuration.date_jitter,
        "remove_private_tags": configuration.remove_private_tags,
        "privacy_profile": configuration.privacy_profile,
        "_floor": configuration._floor,  # pylint: disable=protected-access
    }


def _reloaded(tmp_path, path, db="reload.db"):
    with Session(str(tmp_path / db)) as session:
        session.load_config(str(path))
        return _state(session.configuration)


def test_the_7_line_file_saves_small(tmp_path):
    """Kills `phi_tags` written whole (0.9.8: 1,872 lines), the profile
    diffed against the floor rather than `PRIVACY_PROFILES['basic@2026c']`
    (the three research defaults would be written too), and the `version`
    key dropped."""
    path = _write(tmp_path, SEVEN_LINE_CONFIG)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.add_rule("SN1", zones=[[0, 10, 0, 10]])
        session.configuration.set_phi_tag("0008,0080", "KEEP")
        session.configuration.save()
    text = path.read_text(encoding="utf-8")
    saved = yaml.safe_load(text)
    assert saved["privacy_profile"] == "basic@2026c"
    assert list(saved["phi_tags"]) == ["0008,0080"]
    assert saved["version"] == "2.0"
    assert len(text.splitlines()) < 40, text


def _external_profile(tmp_path, tags, name="profile.yaml"):
    return _write(tmp_path, yaml.safe_dump({"phi_tags": tags}), name)


#: One per base a policy can have (§2.4 of the L4 spec). Each returns the
#: config text to load, or None for a bare session.
def _base_cases(tmp_path):
    external = _external_profile(tmp_path, {
        "0010,0010": {"action": "REMOVE", "name": "Patient's Name"},
        "0008,0090": {"action": "EMPTY", "name": "Referring Physician's Name"}})
    empty = _external_profile(tmp_path, {}, "empty_profile.yaml")
    return {
        "basic": "privacy_profile: basic\n",
        "basic@2026c": "privacy_profile: basic@2026c\n",
        "floor file": "machines: []\n",
        "bare session": None,
        "none": "privacy_profile: none\nphi_tags:\n  '0010,0010': {action: REMOVE}\n",
        "external": f"privacy_profile: {external}\n",
        "external with no tags": f"privacy_profile: {empty}\n",
    }


@pytest.mark.parametrize("case", ["basic", "basic@2026c", "floor file", "bare session",
                                  "none", "external", "external with no tags"])
def test_every_policy_base_round_trips(tmp_path, case):
    """Kills the floor written as `none` (the reload loses the floor
    flag), `none` written with no line (the reload adds 620 rules), an
    external profile diffed against `{}` or against the built-in, and
    `_floor` not restored by the reload."""
    text = _base_cases(tmp_path)[case]
    path = tmp_path / "c.yaml"
    with Session(str(tmp_path / "s.db")) as session:
        if text is None:
            session.configuration.config_path = str(path)
        else:
            _write(tmp_path, text)
            session.load_config(str(path))
        session.configuration.add_rule("SN1", zones=[[0, 10, 0, 10]])
        session.configuration.set_phi_tag("0008,1030", "REMOVE")
        session.configuration.date_jitter = {"min_days": -20, "max_days": -5}
        session.configuration.remove_private_tags = False
        session.configuration.save()
        expected = _state(session.configuration)
    assert _reloaded(tmp_path, path) == expected


def test_the_floor_is_saved_as_no_profile_line(tmp_path):
    """Kills 0.9.8's `privacy_profile: none` plus 620 rules for a session
    that loaded no file."""
    path = tmp_path / "bare.yaml"
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.config_path = str(path)
        session.configuration.set_phi_tag("0018,1030", "REMOVE")
        session.configuration.save()
    saved = _parsed(path)
    assert "privacy_profile" not in saved
    assert list(saved["phi_tags"]) == ["0018,1030"]


def test_a_changed_value_under_the_base_action_is_written(tmp_path):
    """The diff compares whole rules. Kills an action-only diff, the one
    `create_config()`'s scaffold uses: the rule keeps the profile's action
    and name and adds a `value`, which such a diff drops."""
    path = _write(tmp_path, "privacy_profile: basic\n")
    rule = {**BASIC_PROFILE["0010,0010"], "value": "X"}
    assert rule["action"] == "REPLACE"
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.phi_tags["0010,0010"] = dict(rule)
        session.configuration.save()
    assert _parsed(path)["phi_tags"] == {"0010,0010": rule}
    assert _reloaded(tmp_path, path)["phi_tags"]["0010,0010"] == rule


@pytest.mark.parametrize("text, base", [("privacy_profile: basic\n", "basic@2026c"),
                                        ("machines: []\n", "floor")])
def test_a_rule_the_base_supplies_cannot_be_saved_away(tmp_path, text, base):
    """A rule deleted from `phi_tags` directly cannot be written over a
    base that supplies it: the reload would bring it back. Kills writing
    that file, and the check placed after the write."""
    path = _write(tmp_path, text)
    before = path.read_bytes()
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        del session.configuration.phi_tags["0010,0010"]
        with pytest.raises(ValueError) as refused:
            session.configuration.save()
    message = str(refused.value)
    assert "0010,0010" in message and base in message, message
    assert "set_phi_tag" in message, message
    # A built-in base or the floor has no profile file that can change.
    assert "load it again" not in message, message
    assert path.read_bytes() == before


@pytest.mark.parametrize("declared", ['version: "2.0"\n', "", 'version: "2.3"\n'])
def test_save_writes_the_schema_version(tmp_path, declared):
    """Every saved file has a `version` line, and it is the schema's: a
    file declaring 2.3 is saved as 2.0 (owner ruling Q5), because what
    `save()` writes is 2.0 content by construction. The literal, not
    `CONFIG_VERSION`: `test_config_schema_version.py` covers the writer
    reading the constant. Kills the line dropped, an unquoted float, and
    the declared string copied through."""
    path = _write(tmp_path, declared + "privacy_profile: basic\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.save()
    version = _parsed(path)["version"]
    assert version == "2.0"
    assert isinstance(version, str)


def test_a_rule_comment_is_kept_as_data(tmp_path):
    """Kills `save()` reusing the scaffold renderer, which turns a rule's
    `comment:` into a `#` line the reload does not read."""
    path = _write(tmp_path, "machines:\n  - {serial_number: SN1, comment: front desk,"
                            " redaction_zones: [[0, 4, 0, 4]]}\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.save()
    assert _reloaded(tmp_path, path)["rules"][0]["comment"] == "front desk"


def test_a_0_9_8_autosave_resaves_smaller_and_means_the_same(tmp_path):
    """`autosaved_basic_config_0_9_8.yaml` is what 0.9.8 wrote for
    `SEVEN_LINE_CONFIG` after `add_rule("SN1", zones=[[0, 10, 0, 10]])` and
    `set_phi_tag("0008,0080", "KEEP")`: captured from `git archive v0.9.8`
    on `sys.path` first, `isocenter.__file__` read back, and committed
    verbatim (1,872 lines). Saving it again drops the pasted rules that
    still equal `basic@2026c`'s and keeps the rest, so it loads to the
    same policy. Fewer lines, not an absolute bound: after L11 (#557) the
    rules whose actions changed stay as overrides, which is the file
    keeping its meaning. Kills a diff that keeps rules equal to the base."""
    fixture = FIXTURES / "autosaved_basic_config_0_9_8.yaml"
    copy = tmp_path / "copy.yaml"
    shutil.copyfile(fixture, copy)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(copy))
        session.configuration.save()
    fixture_lines = len(fixture.read_text(encoding="utf-8").splitlines())
    assert len(copy.read_text(encoding="utf-8").splitlines()) < fixture_lines
    assert _parsed(copy)["privacy_profile"] == "basic@2026c"
    assert _reloaded(tmp_path, copy, "a.db") == _reloaded(tmp_path, fixture, "b.db")


_A = "0010,0010"
_B = "0008,0090"
_C = "0008,1030"
_D = "0018,1030"
_E = "0008,1010"


def _loaded_external(tmp_path, session):
    profile = _external_profile(tmp_path, {
        _A: {"action": "REMOVE", "name": "Patient's Name"},
        _B: {"action": "EMPTY", "name": "Referring Physician's Name"},
        _C: {"action": "REMOVE", "name": "Study Description"}})
    path = _write(tmp_path, f"privacy_profile: {profile}\n")
    session.load_config(str(path))
    return profile, path


def test_a_changed_external_profile_is_diffed_as_it_is_now(tmp_path):
    """The external profile is re-read at save time. After the load, its
    file changes `_A`'s action and drops `_B`: the save writes `_A` and `_B`
    as overrides (memory's rules, no longer the base's), and the reload
    reproduces memory. Kills a snapshot taken at load, whose diff drops
    `_A` and `_B` and whose reload then carries the profile's new action
    and loses `_B`."""
    with Session(str(tmp_path / "s.db")) as session:
        profile, path = _loaded_external(tmp_path, session)
        _external_profile(tmp_path, {
            _A: {"action": "EMPTY", "name": "Patient's Name"},
            _C: {"action": "REMOVE", "name": "Study Description"}})
        session.configuration.set_phi_tag(_D, "REMOVE")
        session.configuration.save()
        expected = _state(session.configuration)
    assert profile.exists()
    saved = _parsed(path)
    assert saved["privacy_profile"] == str(profile)
    assert set(saved["phi_tags"]) == {_A, _B, _D}
    assert _reloaded(tmp_path, path) == expected


def test_an_external_profile_that_gained_a_rule_is_refused(tmp_path):
    """The profile file gained `_E` after the load: a file naming it would
    bring `_E` in, which memory never held. Kills the refusal applied only
    to built-in bases."""
    with Session(str(tmp_path / "s.db")) as session:
        _, path = _loaded_external(tmp_path, session)
        before = path.read_bytes()
        _external_profile(tmp_path, {
            _A: {"action": "REMOVE", "name": "Patient's Name"},
            _B: {"action": "EMPTY", "name": "Referring Physician's Name"},
            _C: {"action": "REMOVE", "name": "Study Description"},
            _E: {"action": "REMOVE", "name": "Station Name"}})
        with pytest.raises(ValueError) as refused:
            session.configuration.save()
    message = str(refused.value)
    assert _E in message and "load it again" in message, message
    assert path.read_bytes() == before


def test_zones_are_written_inline(tmp_path):
    """Kills the `FlowList` wrapping lost: a zone reads as coordinates, not
    as a list four lines tall."""
    path = tmp_path / "c.yaml"
    configuration = IsocenterConfiguration(config_path=str(path))
    configuration.add_rule("SN1", zones=[[0, 10, 0, 10]])
    configuration.save()
    assert "redaction_zones: [[0, 10, 0, 10]]" in path.read_text(encoding="utf-8")


def test_a_hand_assigned_bare_name_is_written_as_assigned(tmp_path):
    """`privacy_profile = "basic"` set in code still finds its table
    through the alias, so only the override is written, under the name as
    assigned. Kills a base lookup that skips `PROFILE_ALIASES` (the save
    would refuse `basic` as an unknown profile)."""
    path = tmp_path / "c.yaml"
    configuration = IsocenterConfiguration(config_path=str(path))
    configuration.privacy_profile = "basic"
    configuration.phi_tags = {tag: dict(rule) for tag, rule in BASIC_PROFILE.items()}
    configuration.phi_tags[_D] = {"action": "KEEP", "name": "Protocol Name"}
    configuration.save()
    saved = _parsed(path)
    assert saved["privacy_profile"] == "basic"
    assert list(saved["phi_tags"]) == [_D]
    tags, _, _, _, _ = ConfigLoader.load_unified_config(str(path))
    assert tags == configuration.phi_tags



def test_the_file_reads_top_down_in_the_schema_order(tmp_path):
    """Keys in the order the docs list them, each rule in block style.
    Kills `sort_keys` and `default_flow_style` flipped: the reload is the
    same either way, and the file is what a person reads (probe survivors
    on this branch before this test)."""
    path = tmp_path / "c.yaml"
    configuration = IsocenterConfiguration(config_path=str(path))
    configuration.privacy_profile = "basic@2026c"
    configuration.phi_tags = {tag: dict(rule) for tag, rule in BASIC_PROFILE.items()}
    configuration.phi_tags[_D] = {"action": "KEEP", "name": "Protocol Name"}
    configuration.add_rule("SN1", zones=[[0, 10, 0, 10]])
    configuration.save()
    text = path.read_text(encoding="utf-8")
    assert list(yaml.safe_load(text)) == ["version", "privacy_profile", "phi_tags",
                                          "date_jitter", "remove_private_tags", "machines"]
    assert "\nmachines:\n- serial_number: SN1\n" in text, text


def test_a_hand_assigned_none_saves_with_no_base(tmp_path):
    """`privacy_profile = "none"` assigned in code (the loader leaves None
    for a file's `none`) is a base of no rules: every rule in memory is
    written, and the file reloads to the same rules. Kills the `none` arm
    of `_policy_base_rules` removed, which refuses the name as neither a
    profile nor a file (review of #742, R2)."""
    path = tmp_path / "c.yaml"
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.config_path = str(path)
        session.configuration.privacy_profile = "none"
        session.configuration.set_phi_tag("0008,1030", "REMOVE")
        session.configuration.save()
        expected = session.configuration.phi_tags
    saved = _parsed(path)
    assert saved["privacy_profile"] == "none"
    assert saved["phi_tags"] == expected
    assert _reloaded(tmp_path, path)["phi_tags"] == expected


@pytest.mark.parametrize("profile, match", [
    (7, "must be a profile name or a path, got int"),
    ("basic@2027a", "'basic@2027a' is not a profile this isocenter ships"),
], ids=["not a string", "an unshipped edition"])
def test_a_profile_no_file_could_name_is_refused(tmp_path, profile, match):
    """A `privacy_profile` assigned in code that no file could name is a
    `ValueError` with its own reason, and nothing is written. Kills the
    type check removed (a `TypeError` from `"@" in 7`) and the `@` arm
    removed (the generic neither-profile-nor-file wording), R3 and R4 in
    the review of #742."""
    path = _write(tmp_path, SEVEN_LINE_CONFIG)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.privacy_profile = profile
        with pytest.raises(ValueError, match=match):
            session.configuration.save()
    assert path.read_text(encoding="utf-8") == SEVEN_LINE_CONFIG


def test_the_missing_rules_refusal_counts_what_it_does_not_show(tmp_path):
    """The refusal names three missing tags and counts the rest. Kills the
    count off by the three shown (review of #742, R1)."""
    path = _write(tmp_path, "privacy_profile: basic\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        for tag in list(session.configuration.phi_tags)[:5]:
            del session.configuration.phi_tags[tag]
        with pytest.raises(ValueError) as refused:
            session.configuration.save()
    assert "(and 2 more)" in str(refused.value), str(refused.value)


def test_a_tag_key_in_uppercase_is_the_base_s_tag(tmp_path):
    """`phi_tags` keys are compared and written as the loader reads them,
    lowercase. A base rule re-keyed in uppercase is still that rule, not a
    missing one, and an override keyed in uppercase is written lowercase.
    Kills the diff taken over raw keys (review of #742, finding 5)."""
    path = _write(tmp_path, "privacy_profile: basic\n")
    tag = next(t for t in BASIC_PROFILE if t != t.upper())
    changed = {**BASIC_PROFILE[tag], "action": "KEEP"}
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        del session.configuration.phi_tags[tag]
        session.configuration.phi_tags[tag.upper()] = dict(changed)
        session.configuration.save()
    assert _parsed(path)["phi_tags"] == {tag: changed}
    assert _reloaded(tmp_path, path)["phi_tags"][tag] == changed


def test_a_relative_profile_lost_to_a_chdir_says_where_it_looked(tmp_path, monkeypatch):
    """An external profile named by a relative path is looked for in the
    working directory at save time, as at load time. After a `chdir` the
    save is refused, and under auto-save so is every change method; the
    refusal names the directory it looked in, and nothing changes. Kills
    the directory left out of the message (review of #742, finding 3)."""
    _external_profile(tmp_path, {"0010,0010": {"action": "REMOVE"}}, "rel.yaml")
    path = _write(tmp_path, "privacy_profile: rel.yaml\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.auto_save = True
        monkeypatch.chdir(elsewhere)
        with pytest.raises(ValueError) as refused:
            session.configuration.set_phi_tag("0008,1030", "REMOVE")
        assert "0008,1030" not in session.configuration.phi_tags
    message = str(refused.value)
    assert "'rel.yaml'" in message and str(elsewhere) in message, message
    assert "set privacy_profile to its path" in message, message
    assert path.read_text(encoding="utf-8") == "privacy_profile: rel.yaml\n"

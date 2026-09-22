"""A configuration's `version` is read, and only version 2 loads (#711).

Measured on 63a64158: `config_manager.CONFIG_VERSION = "2.0"` existed and
nothing read it. `version: "9.9"` loaded, `version: 2.0` (a YAML float)
loaded, and so did a file with no `version` line; `create_config()` and
`save()` each wrote their own `'2.0'` literal beside the constant.

Now: an absent `version` is 2.0, permanently -- every configuration in the
documentation omits it. A present one is a quoted `"MAJOR.MINOR"` string
whose major is 2; any minor of 2 loads, and a refusal inside a file that
declares a minor newer than this library's says so. Both writers stamp
`CONFIG_VERSION`.

Every refusal here is asserted with the configuration unchanged after it,
the sentinel pattern of `test_load_config_raises.py` (#456).
"""
import sqlite3

import pytest
import yaml

from isocenter import config_manager, profiles
from isocenter.config_manager import CONFIG_VERSION, ConfigLoader
from isocenter.configuration import IsocenterConfiguration
from isocenter.session import DicomSession

FIELDS = ("phi_tags", "rules", "date_jitter", "remove_private_tags",
          "privacy_profile", "config_path")


def _set_sentinels(configuration, tmp_path):
    """A prior configuration in every field `load_config` writes, assigned
    rather than set through a method that would auto-save."""
    configuration.phi_tags = {"9999,0001": {"action": "REMOVE", "name": "sentinel"}}
    configuration.rules = [{"serial_number": "PRIOR"}]
    configuration.date_jitter = {"min_days": -7, "max_days": -7}
    configuration.remove_private_tags = False
    configuration.privacy_profile = "prior"
    configuration.config_path = str(tmp_path / "prior.yaml")
    return {field: _copy(getattr(configuration, field)) for field in FIELDS}


def _copy(value):
    return yaml.safe_load(yaml.safe_dump(value))


def _refused(tmp_path, text, name="cfg.yaml"):
    """The message `load_config` raises for `text`; asserts it raised
    `ValueError` and that every field kept its sentinel."""
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


@pytest.mark.parametrize("version", ["9.9", "3.0", "1.0", "0.9"])
def test_a_version_this_library_does_not_read_is_refused(tmp_path, version):
    """Kills: the check deleted; `major > 2` (lets 1.0 and 0.9 in);
    `major < 2` (lets 3.0 in); the minor compared instead of the major.
    `"1.0"` is the label retired in December 2025, and is refused rather
    than read as 2.0: one spelling for this schema."""
    message = _refused(tmp_path, f'version: "{version}"\nprivacy_profile: basic\n')
    assert f"version '{version}'" in message, message
    assert "reads version 2" in message, message
    assert "cfg.yaml" in message, message


@pytest.mark.parametrize("version", ["2.0", "2.7", "2.10"])
def test_any_minor_of_version_2_loads(tmp_path, version):
    """Kills `version == CONFIG_VERSION` in place of the major check."""
    tags, _, _, _, profile = _loaded(
        tmp_path, f'version: "{version}"\nprivacy_profile: basic\n')
    assert profile == "basic@2026c"
    assert len(tags) > 0


def test_a_file_with_no_version_loads_as_version_2(tmp_path):
    """The same body with and without `version: "2.0"` loads the same
    policy. Kills an absent `version` refused, which would refuse every
    configuration a user copied from the documentation."""
    body = ("privacy_profile: basic\n"
            "phi_tags:\n  '0018,1030': {action: REMOVE, name: Protocol}\n"
            "remove_private_tags: false\n"
            "machines:\n  - serial_number: SN-1\n    redaction_zones: [[0, 10, 0, 20]]\n")
    unversioned = _loaded(tmp_path, body, name="a.yaml")
    versioned = _loaded(tmp_path, 'version: "2.0"\n' + body, name="b.yaml")
    assert unversioned == versioned


@pytest.mark.parametrize("line", [
    "version: 2.0", "version: 2", "version: true", "version:",
    'version: "2"', 'version: "two"', 'version: " 2.0"', 'version: "2.0.1"',
    'version: "2.0\\n"', 'version: "02.0"',
    # Falsy but present: refused, not read as absent (review of #728, R6).
    'version: ""', "version: 0", "version: false"])
def test_a_version_that_is_not_a_quoted_string_is_refused(tmp_path, line):
    """Kills: a `str(version)` coercion (2.0 -> "2.0" would load); an
    unanchored pattern (`" 2.0"`, `"2.0.1"`, `"2.0\\n"`); a null treated
    as absent; the major compared as an int (`"02.0"`)."""
    message = _refused(tmp_path, f"{line}\nprivacy_profile: basic\n")
    assert "version" in message, message
    assert "(#711)" in message, message


def test_an_unquoted_float_version_says_to_quote_it(tmp_path):
    """The float is the trap: `version: 2.10` is the number 2.1."""
    message = _refused(tmp_path, "version: 2.10\nprivacy_profile: basic\n")
    assert "version must be a quoted string" in message, message
    assert "got 2.1 (float)" in message, message


def test_the_version_is_checked_before_the_keys(tmp_path):
    """A file written for another major may carry keys this library has
    never heard of; the version is the reason to refuse it. Kills the two
    checks swapped."""
    message = _refused(tmp_path, 'version: "3.0"\na_new_key: 1\n')
    assert "version '3.0'" in message, message
    assert "a_new_key" not in message, message


def test_a_newer_minor_names_itself_when_it_brings_an_unknown_key(tmp_path):
    """Kills the hint dropped, and the hint shown always (the ordinary typo
    message stays short)."""
    newer = _refused(tmp_path, 'version: "2.3"\na_new_key: 1\n')
    assert "a_new_key" in newer, newer
    assert f"{tmp_path / 'cfg.yaml'} declares version 2.3" in newer, newer
    assert "newer isocenter" in newer, newer
    current = _refused(tmp_path, 'version: "2.0"\na_new_key: 1\n')
    assert "a_new_key" in current, current
    assert "newer isocenter" not in current, current


def test_a_newer_minor_hint_compares_minors_as_numbers(tmp_path, monkeypatch):
    """`"2.10"` is newer than `"2.9"`; compared as strings it is not. Kills
    a string comparison of the minors."""
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", "2.9")
    message = _refused(tmp_path, 'version: "2.10"\na_new_key: 1\n')
    assert "newer isocenter" in message, message


def test_a_newer_minor_hint_reaches_a_rule_refusal(tmp_path):
    """A newer minor may add a key inside a machine rule as well as at the
    top; the hint is on every refusal of such a file. Kills the hint
    applied to the top level only."""
    message = _refused(tmp_path, 'version: "2.3"\nmachines:\n'
                       '  - serial_number: SN1\n    new_rule_key: 1\n')
    assert "new_rule_key" in message and "newer isocenter" in message, message


def test_audit_config_path_refuses_the_same_version_before_a_secret(tmp_path):
    """`audit(config_path=)` reads the file through the same loader, before
    the project secret is minted. Kills the check placed only in
    `load_config`."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text('version: "9.9"\nprivacy_profile: basic\n', encoding="utf-8")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        with pytest.raises(ValueError, match="version '9.9'"):
            session.audit(config_path=str(cfg))
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_what_the_writers_stamp_is_a_version_this_library_reads(tmp_path):
    """Both writers stamp `CONFIG_VERSION`, and both files reload. The
    major is pinned as the literal `"2"`: a check derived from
    `CONFIG_VERSION` would pass a bump to 3.0, and the promise is version
    2 for all of 1.x. Kills a writer literal drifting from the constant,
    and the constant's major bumped without the reader."""
    assert CONFIG_VERSION.split(".")[0] == "2"

    scaffold = tmp_path / "scaffold.yaml"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.create_config(str(scaffold))
    saved = tmp_path / "saved.yaml"
    IsocenterConfiguration(config_path=str(saved)).save()

    for path in (scaffold, saved):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["version"] == CONFIG_VERSION, path
        ConfigLoader.load_unified_config(str(path))


def test_the_writers_read_the_constant(tmp_path, monkeypatch):
    """The constant, not a literal that happens to equal it. Kills either
    writer keeping its own `'2.0'`."""
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", "2.4")
    scaffold = tmp_path / "scaffold.yaml"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.create_config(str(scaffold))
    saved = tmp_path / "saved.yaml"
    IsocenterConfiguration(config_path=str(saved)).save()
    for path in (scaffold, saved):
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["version"] == "2.4", path


def test_an_external_profile_declaring_another_version_is_refused(tmp_path):
    """The profile door checks the profile file's own `version`. Kills
    that door left unchecked."""
    profile = tmp_path / "profile.yaml"
    profile.write_text('version: "9.9"\nphi_tags:\n  \'0010,0010\': {action: REMOVE}\n',
                       encoding="utf-8")
    message = _refused(tmp_path, f"privacy_profile: {profile}\n")
    assert str(profile) in message, message
    assert "version '9.9'" in message, message


def _with_profile(tmp_path, main_version, profile_version, profile_rule):
    """A configuration naming an external profile; each file's `version`
    line only when its argument is not None."""
    profile = tmp_path / "profile.yaml"
    head = f'version: "{profile_version}"\n' if profile_version else ""
    profile.write_text(f"{head}phi_tags:\n  '0008,0080': {profile_rule}\n",
                       encoding="utf-8")
    head = f'version: "{main_version}"\n' if main_version else ""
    return profile, _refused(tmp_path, f"{head}privacy_profile: {profile}\n")


def test_a_newer_minor_profile_names_itself_inside_an_unversioned_config(tmp_path):
    """The profile door carries its own note (spec §11.3, review of #728,
    R1): a `2.7` profile refused for a rule key says the profile declares
    2.7. Kills the note's wrap removed from `_external_profile_tags`."""
    profile, message = _with_profile(tmp_path, None, "2.7", "{actoin: KEEP}")
    assert f"{profile} declares version 2.7" in message, message


def test_a_configurations_version_is_not_blamed_on_its_profile(tmp_path):
    """Review of #728, finding 2: a `2.5` configuration whose unversioned
    profile holds a typo said "this file declares version 2.5" about the
    profile, which declares nothing. Kills the outer wrap noting a refusal
    the profile's own wrap already judged."""
    _, message = _with_profile(tmp_path, "2.5", None, "{actoin: KEEP}")
    assert "unknown key 'actoin'" in message, message
    assert "declares version" not in message, message


def test_a_newer_profile_in_a_newer_configuration_is_noted_once(tmp_path):
    """Both files newer: one note, naming the profile, whose refusal it
    is. Kills a note per wrap."""
    profile, message = _with_profile(tmp_path, "2.5", "2.7", "{actoin: KEEP}")
    assert message.count("declares version") == 1, message
    assert f"{profile} declares version 2.7" in message, message


def test_load_phi_config_notes_a_newer_minor(tmp_path):
    """The `load_phi_config` door carries the note too (review of #728,
    R9). Kills its wrap removed."""
    path = tmp_path / "cfg.yaml"
    path.write_text('version: "2.7"\nphi_tags:\n  \'0010,0010\': {actoin: KEEP}\n',
                    encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        ConfigLoader.load_phi_config(str(path))
    assert f"{path} declares version 2.7" in str(caught.value), str(caught.value)


#: The schema, by version. A 1.x that adds a key bumps `CONFIG_VERSION` to
#: 2.1 and adds a "2.1" row here. The accept-any-2.x rule is sound only if
#: every added key comes with a minor bump -- otherwise a file using the
#: new key under a library that lacks it gets no newer-minor hint.
SCHEMA_BY_VERSION = {
    "2.0": {
        "top": {"version", "privacy_profile", "phi_tags", "date_jitter",
                "remove_private_tags", "machines"},
        "rule": {"serial_number", "manufacturer", "model_name",
                 "redaction_zones", "comment"},
        "zone": {"roi", "note"},
        "phi_rule": {"action", "name", "value"},
        # Every `privacy_profile` value a built-in resolves, pinned names
        # and bare aliases alike (#714). A later PS3.15 edition is a new
        # value, so it comes with a new minor like a new key does.
        "profiles": {"basic", "basic@2026c"},
    },
}


def test_a_key_added_to_the_schema_bumps_the_minor():
    """Kills a key added under an unchanged `CONFIG_VERSION`."""
    expected = SCHEMA_BY_VERSION[CONFIG_VERSION]
    assert set(config_manager._TOP_LEVEL_KEYS) == expected["top"]
    assert set(config_manager._RULE_KEYS) == expected["rule"]
    assert set(config_manager._ZONE_KEYS) == expected["zone"]
    assert set(config_manager._PHI_RULE_KEYS) == expected["phi_rule"]


def test_a_new_profile_name_comes_with_a_schema_minor():
    """Kills an edition (`basic@2027a`) added under an unchanged
    `CONFIG_VERSION` -- the accept-any-2.x rule is then unsound for values,
    because a 2.0 file naming it reaches an older library with no
    newer-minor hint -- and a name removed in a later minor (#714)."""
    names = set(profiles.PRIVACY_PROFILES) | set(profiles.PROFILE_ALIASES)
    assert names == SCHEMA_BY_VERSION[CONFIG_VERSION]["profiles"]
    rows = sorted(SCHEMA_BY_VERSION, key=lambda v: int(v.split(".")[1]))
    for older, newer in zip(rows, rows[1:]):
        assert (SCHEMA_BY_VERSION[older]["profiles"]
                <= SCHEMA_BY_VERSION[newer]["profiles"]), (older, newer)

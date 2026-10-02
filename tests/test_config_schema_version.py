"""A configuration's `version` is read, and only version 2 loads (#711).

Measured on 63a64158: `config_manager.CONFIG_VERSION = "2.0"` existed and
nothing read it. `version: "9.9"` loaded, `version: 2.0` (a YAML float)
loaded, and so did a file with no `version` line; `create_config()` and
`save()` each wrote their own `'2.0'` literal beside the constant.

Now: an absent `version` is 2.0, permanently -- every configuration in the
documentation omits it. A present one is a quoted `"MAJOR.MINOR"` string
whose major is 2 and whose minor is no newer than this library's
(`CONFIG_VERSION`). A newer minor is refused, naming the remedy (#784): a
minor bump means the same file is applied differently (#762), which this
library cannot do, so loading it would apply it the old way under the new
number. Both writers stamp `CONFIG_VERSION`.

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


def _newer_minor():
    """`CONFIG_VERSION`'s minor plus one, computed, so the tests follow a
    bump."""
    major, minor = config_manager.CONFIG_VERSION.split(".")
    return f"{major}.{int(minor) + 1}"


def test_this_librarys_own_minor_loads(tmp_path):
    """`"2.0"` (and so `CONFIG_VERSION`) loads. Kills `>` written `>=`."""
    tags, _, _, _, profile = _loaded(
        tmp_path, f'version: "{CONFIG_VERSION}"\nprivacy_profile: basic\n')
    assert profile == "basic@2026c"
    assert len(tags) > 0


def test_a_newer_minor_is_refused(tmp_path):
    """Owner ruling Q1 A on #784. Main: `version: '2.1'` loaded with no
    warning and no row, was applied as 2.0 applies it, and `save()` then
    wrote it back as `'2.0'`, erasing the declaration. Kills the check
    deleted."""
    newer = _newer_minor()
    message = _refused(tmp_path, f'version: "{newer}"\nprivacy_profile: basic\n')
    assert message == (
        f"{tmp_path / 'cfg.yaml'}: version '{newer}' is newer than this "
        f"isocenter's configuration version {CONFIG_VERSION}, which may apply "
        f"it differently than it was written for; upgrade isocenter, or set "
        f"version: '{CONFIG_VERSION}' to apply it as {CONFIG_VERSION} does")


def test_a_newer_minor_is_refused_by_the_loader_and_audit(tmp_path):
    """`ConfigLoader.load_unified_config` and `audit(config_path=)` read
    the same check, the latter before the project secret is minted."""
    newer = _newer_minor()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f'version: "{newer}"\nprivacy_profile: basic\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"is newer than this isocenter's .* does$"):
        ConfigLoader.load_unified_config(str(cfg))
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        with pytest.raises(ValueError, match=r"newer than this isocenter's configuration version"):
            session.audit(config_path=str(cfg))
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_a_refused_newer_file_is_not_the_one_save_writes(tmp_path):
    """The erasure measured on main: load a `2.1` file, `save()`, and the
    file said `'2.0'`. Refused at load, `config_path` never names it, so
    no save of this session rewrites it."""
    newer = _newer_minor()
    cfg = tmp_path / "cfg.yaml"
    text = f'version: "{newer}"\nprivacy_profile: basic\n'
    cfg.write_text(text, encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match=r"newer than this isocenter's configuration version"):
            session.load_config(str(cfg))
        assert session.configuration.config_path != str(cfg)
    assert cfg.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("ours, theirs, loads", [
    ("2.9", "2.10", False), ("2.9", "2.9", True), ("2.10", "2.9", True)])
def test_minors_compare_as_numbers(tmp_path, monkeypatch, ours, theirs, loads):
    """`"2.10"` is newer than `"2.9"`; compared as strings it is not.
    Kills a string comparison of the minors, either way round, and a
    `CONFIG_VERSION` read at import time."""
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", ours)
    text = f'version: "{theirs}"\nprivacy_profile: basic\n'
    if loads:
        _loaded(tmp_path, text)
    else:
        assert "is newer than this isocenter's configuration version" in \
            _refused(tmp_path, text)


def test_the_newer_minor_is_named_before_the_keys(tmp_path):
    """A newer file may carry a key this library lacks; its version is the
    reason, as for another major. Kills the check placed after the key
    check."""
    message = _refused(tmp_path, f'version: "{_newer_minor()}"\na_new_key: 1\n')
    assert "is newer than this isocenter's configuration version" in message, message
    assert "a_new_key" not in message, message


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
    'version: "2.0\\n"',
    # Falsy but present: refused, not read as absent (review of #728, R6).
    'version: ""', "version: 0", "version: false"])
def test_a_version_that_is_not_a_quoted_string_is_refused(tmp_path, line):
    """Kills: a `str(version)` coercion (2.0 -> "2.0" would load); an
    unanchored pattern (`" 2.0"`, `"2.0.1"`, `"2.0\\n"`); a null treated
    as absent. `"02.0"` was here for the major compared as an int; since
    #730 a leading zero is refused as a spelling before the major is read,
    in `test_a_config_value_means_one_thing.py`."""
    message = _refused(tmp_path, f"{line}\nprivacy_profile: basic\n")
    assert "version" in message, message
    assert "such as '2.0'" in message, message


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


def test_a_newer_external_profile_is_refused_naming_the_profile(tmp_path):
    """External profile files share `_declared_version`, so a profile
    declaring a newer minor is refused, by its own path, inside a
    configuration that declares nothing. Kept from the deleted note
    tests (#784): the refusal names the file whose version it is."""
    newer = _newer_minor()
    profile, message = _with_profile(tmp_path, None, newer, "{action: KEEP}")
    assert message.startswith(f"{profile}: version '{newer}' is newer than "
                              "this isocenter's configuration version"), message


def test_a_newer_configuration_is_refused_before_its_profile_is_read(tmp_path):
    """Both files newer: the configuration's own version is judged first,
    and the refusal names the configuration, not the profile."""
    newer = _newer_minor()
    profile, message = _with_profile(tmp_path, newer, newer, "{actoin: KEEP}")
    assert message.startswith(f"{tmp_path / 'cfg.yaml'}: version"), message
    assert str(profile) not in message and "actoin" not in message, message


def test_a_current_configuration_does_not_blame_its_profile(tmp_path):
    """Review of #728, finding 2, as it stands after #784: a configuration
    at this library's minor whose unversioned profile holds a typo is
    refused for the typo, with no word about a version."""
    _, message = _with_profile(tmp_path, CONFIG_VERSION, None, "{actoin: KEEP}")
    assert "unknown key 'actoin'" in message, message
    assert "is newer than" not in message, message


#: The schema, by version. A 1.x that adds a key bumps `CONFIG_VERSION` to
#: 2.1 and adds a "2.1" row here. A file declaring a minor newer than the
#: library's is refused (#784), which names the cause only if every added
#: key comes with a minor bump -- otherwise a file using the new key, as
#: written by a library that has it, declares a version an older library
#: reads, and is refused for the key rather than for the version.
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


# --- save() over a file declaring a newer minor (#784, owner ruling on #895) ---

def _newer_target(tmp_path):
    """A file another, newer isocenter wrote, and its text."""
    target = tmp_path / "theirs.yaml"
    text = (f'version: "{_newer_minor()}"\nprivacy_profile: basic\n'
            f'remove_private_tags: true\n')
    target.write_text(text, encoding="utf-8")
    return target, text


def _expected_refusal(target):
    return (f"{target}: declares version '{_newer_minor()}', newer than this "
            f"isocenter's configuration version {CONFIG_VERSION}; saving "
            f"would rewrite it as {CONFIG_VERSION}. Nothing was written: "
            f"upgrade isocenter, or save to another path")


def test_save_refuses_to_overwrite_a_file_declaring_a_newer_minor(tmp_path):
    """Owner ruling on #895: the load refuses a newer file, but a session
    pointed at one by assignment wrote over it on main, erasing its
    declaration. Kills the guard deleted, and the guard reading the
    session's own version instead of the file's."""
    target, text = _newer_target(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.configuration.config_path = str(target)
        with pytest.raises(ValueError) as caught:
            session.configuration.save()
    assert str(caught.value) == _expected_refusal(target)
    assert target.read_text(encoding="utf-8") == text


def test_auto_save_refuses_a_newer_target_and_leaves_memory_alone(tmp_path):
    """The same guard reached through a `set_*` call under `auto_save`:
    the trial save raises, so the rule is not applied in memory either."""
    target, text = _newer_target(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        configuration = session.configuration
        configuration.config_path = str(target)
        configuration.auto_save = True
        before = _copy(configuration.phi_tags)
        with pytest.raises(ValueError) as caught:
            configuration.set_phi_tag("0010,0010", "REMOVE")
        assert configuration.phi_tags == before
    assert str(caught.value) == _expected_refusal(target)
    assert target.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("existing", [
    None,                                         # missing
    "version: [unclosed\n",                       # not YAML
    "- a\n- list\n",                              # not a mapping
    "",                                           # empty
    "version: 2.1\n",                             # not a string
    'version: "two"\n',                           # not MAJOR.MINOR
    f'version: "{CONFIG_VERSION}"\n',             # ours
    'version: "9.9"\n',                           # another major: not a minor
])
def test_a_target_that_declares_no_newer_minor_is_saved_as_before(
        tmp_path, existing):
    """A missing or unreadable target, or one declaring no newer minor of
    this major, is written as it was before the guard. Kills the guard
    widened to refuse whatever it cannot read."""
    target = tmp_path / "target.yaml"
    if existing is not None:
        target.write_text(existing, encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.configuration.config_path = str(target)
        session.configuration.save()
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["version"] == CONFIG_VERSION


def test_an_unreadable_target_is_saved_as_before(tmp_path):
    """A target this process may write but not read (mode 0o200) saves as
    before: the guard reads what it can, and a read error is not a
    refusal."""
    import os
    target = tmp_path / "target.yaml"
    target.write_text(f'version: "{_newer_minor()}"\n', encoding="utf-8")
    os.chmod(target, 0o200)
    try:
        if os.access(target, os.R_OK):
            pytest.skip("running as a user who reads a 0o200 file")
        with DicomSession(str(tmp_path / "s.db")) as session:
            session.configuration.config_path = str(target)
            session.configuration.save()
    finally:
        os.chmod(target, 0o600)
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["version"] == CONFIG_VERSION


def test_create_config_refuses_to_overwrite_a_file_declaring_a_newer_minor(tmp_path):
    """The owner's save() ruling on #895 applied to the other writer
    (review of #895): `create_config()` onto another isocenter's newer
    file wrote over it as ours. Same refusal, same words, nothing
    written. Kills the guard missing from `create_config()`."""
    target, text = _newer_target(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError) as caught:
            session.create_config(str(target))
    assert str(caught.value) == _expected_refusal(target)
    assert target.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("existing", [None, f'version: "{CONFIG_VERSION}"\n',
                                      "version: [unclosed\n"])
def test_create_config_onto_a_target_declaring_no_newer_minor_writes(
        tmp_path, existing):
    """A missing target, one of ours, or one that is not YAML is
    scaffolded as before."""
    target = tmp_path / "scaffold.yaml"
    if existing is not None:
        target.write_text(existing, encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.create_config(str(target))
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["version"] == CONFIG_VERSION

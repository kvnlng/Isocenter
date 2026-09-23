"""A profile name is pinned to its PS3.15 edition (#714).

Until 1.0 nothing the library read recorded an edition: `basic` meant
whichever table the installed version shipped, and the floor was built
from it at import. A release adopting a later edition would have changed
what an unchanged config removes, with nothing in the file saying so --
0.9.8 did exactly that once, when `basic` went from 35 rules to 620 (#547).

Now `basic@2026c` is the name, bare `basic` means it in every 1.x, the
floor is built on it in every 1.x, and a later edition arrives as a new
name. `configuration.privacy_profile` holds the pinned name even when the
file said `basic` (owner ruling Q1), `create_config()` writes it (Q2), and
the compliance report names the edition the rules came from.

The module constants below are literals. None is computed from the code
under test: an expected value derived from `profiles` passes any change
to `profiles`.
"""
import collections
import hashlib
import json
import pathlib
import sqlite3

import pytest
import yaml

import isocenter
from isocenter import profiles
from isocenter.config_manager import ConfigLoader
from isocenter.profiles import BASIC_PROFILE, FLOOR_POLICY, RESEARCH_DEFAULTS
from isocenter.session import DicomSession as Session

from support.annex_e import derive, load_table

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

#: What a bare name means, for every 1.x.
PINNED_ALIASES = {"basic": "basic@2026c"}
#: The profile the floor is built on, and the one `create_config()` names.
PINNED_FLOOR_BASE = "basic@2026c"
#: sha256 of `json.dumps(table, sort_keys=True, separators=(",", ":"),
#: ensure_ascii=True)`. Before 1.0 a change here is allowed, with a
#: CHANGELOG entry saying what the table gained or lost (L10 #544 and L11
#: #557 will). After 1.0 it is a new name, never a new digest.
PINNED_DIGESTS = {
    # #544: the 55 `U` rows and `006a,0003` gained a value-less REPLACE,
    # the keyed UID replacement (590 -> 646 rules).
    "basic@2026c": "113e1151d676310a4c577690ddf6701c993473426d9db348625a28a664fbc6e2",
    "floor": "60d328ee0fb22c9602bb1dc7ad7d8b3d0fb1a1760c7835031e9fefcfc446108a",
}
#: The rules 1.0 ships under each pinned name: `PINNED_DIGESTS` as it
#: stood when L14 (#26, #527) filled this on `main`, before RELEASING.md's
#: "Cutting a release" step 1. Written out as literals, never as
#: `dict(PINNED_DIGESTS)`: a copy expression tracks every later edit and
#: pins nothing. Until the v1.0.0 tag, a change to `basic@2026c` or the
#: floor updates **both** dicts in the same PR, and its review checks that
#: it did; after the tag this one never changes.
FROZEN_AT_1_0 = {
    "basic@2026c": "113e1151d676310a4c577690ddf6701c993473426d9db348625a28a664fbc6e2",
    "floor": "60d328ee0fb22c9602bb1dc7ad7d8b3d0fb1a1760c7835031e9fefcfc446108a",
}

#: The one sentence both digest tests end on (owner ruling Q3, 2026-09-21).
_WHAT_MAY_CHANGE = (
    "Before 1.0, update the digest in tests/test_profile_editions.py with a "
    "CHANGELOG entry saying what the table gained or lost. After 1.0, a "
    "pinned name's rules are frozen: a different table is a new name "
    "(basic@2027a), except that a 1.x may correct a row the published 2026c "
    "standard shows was transcribed wrongly, as a Breaking entry quoting "
    "the standard's row.")


def _digest(table):
    return hashlib.sha256(json.dumps(
        table, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("ascii")).hexdigest()


def _current_digests():
    return {"basic@2026c": _digest(profiles.PRIVACY_PROFILES["basic@2026c"]),
            "floor": _digest(profiles.FLOOR_POLICY)}


def _describe(name, table):
    histogram = collections.Counter(rule.get("action") for rule in table.values())
    return f"{name}: {len(table)} rules, actions {dict(sorted(histogram.items()))}"


def _config(tmp_path, text, name="cfg.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _report_rows(session, tmp_path, name="report.md"):
    path = tmp_path / name
    session.generate_report(str(path))
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        for label in ("Privacy Profile", "De-ID Method"):
            if line.startswith(f"| {label} |"):
                rows[label] = line
    assert set(rows) == {"Privacy Profile", "De-ID Method"}, rows
    return rows


def test_the_pinned_name_loads_the_2026c_table(tmp_path):
    """Kills the pinned name not registered (red at 0.9.8, which refused
    it as an unknown profile)."""
    path = _config(tmp_path, "privacy_profile: basic@2026c\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(path)
        assert session.configuration.phi_tags == BASIC_PROFILE
        assert len(session.configuration.phi_tags) == 646
        assert session.configuration.privacy_profile == "basic@2026c"


def test_bare_basic_is_basic_at_2026c(tmp_path):
    """`basic` and `basic@2026c` load the same rules, and both leave the
    pinned name in the configuration (Q1). Kills the alias pointing at
    another table, and the field storing the file's spelling."""
    loaded = {}
    for spelling in ("basic", "basic@2026c"):
        path = _config(tmp_path, f"privacy_profile: {spelling}\n",
                       name=f"{spelling}.yaml")
        with Session(str(tmp_path / f"{spelling}.db")) as session:
            session.load_config(path)
            loaded[spelling] = session.configuration.phi_tags
            assert session.configuration.privacy_profile == "basic@2026c", spelling
    assert loaded["basic"] == loaded["basic@2026c"] == BASIC_PROFILE


def test_what_bare_basic_and_the_floor_mean_is_pinned():
    """Kills the alias or the floor's base moved to a newer edition in a
    1.x. Both are frozen at 1.0; 2.0 may move them."""
    assert profiles.PROFILE_ALIASES == PINNED_ALIASES
    assert profiles.FLOOR_BASE == PINNED_FLOOR_BASE


def test_a_pinned_name_has_the_rules_it_was_pinned_with():
    """Kills any rule, action or display name of `basic@2026c` changed --
    including by a regenerated literal after an edit to
    `tests/support/annex_e.py`'s mapping, which
    `test_basic_profile_is_derived_from_annex_e` cannot see, since it holds
    the literal to whatever the derivation now says -- and
    `RESEARCH_DEFAULTS` edited (the floor's digest)."""
    current = _current_digests()
    tables = {"basic@2026c": profiles.PRIVACY_PROFILES["basic@2026c"],
              "floor": profiles.FLOOR_POLICY}
    for name, pinned in PINNED_DIGESTS.items():
        assert current[name] == pinned, (
            f"The rules pinned as {name!r} changed ({_describe(name, tables[name])}; "
            f"digest {current[name]}, pinned {pinned}). {_WHAT_MAY_CHANGE}")


def test_a_1x_release_carries_the_rules_1_0_froze():
    """Below 1.0 this skips, so it skips on `main` and at RELEASING.md's
    "Cutting a release" step 1, where the version still reads 0.9.x. It
    first runs at step 3's release commit, which bumps the version to
    `1.0.0rc1`, and is red there for any change to a pinned name's rules
    since L14 (#26) copied `PINNED_DIGESTS` into `FROZEN_AT_1_0` on `main`.
    A change between that copy and the v1.0.0 tag, when the pin freezes,
    updates both tables in the same PR."""
    major = int(isocenter.__version__.split(".")[0])
    if major < 1:
        pytest.skip(f"isocenter {isocenter.__version__} is before 1.0; "
                    f"FROZEN_AT_1_0 is first checked at 1.0.0rc1 (L14, #26)")
    assert FROZEN_AT_1_0, (
        "FROZEN_AT_1_0 is empty in a 1.x: copy PINNED_DIGESTS into it at the "
        "v1.0.0 cut (#26)")
    current = _current_digests()
    for name, frozen in FROZEN_AT_1_0.items():
        assert current[name] == frozen, (
            f"{name!r} no longer has the rules 1.0 shipped under it "
            f"(digest {current[name]}, frozen {frozen}). {_WHAT_MAY_CHANGE}")


@pytest.mark.parametrize("name", [
    "basic@2027a", "basic@2025e", "basic@2026C", "BASIC@2026c", "basic@2026",
    "basic@", "@2026c", "basic@2026c ", "retain-longitudinal@2027a"])
def test_an_edition_this_isocenter_does_not_ship_is_refused(tmp_path, name):
    """Kills a prefix match (`startswith("basic")`), case-folding,
    `.strip()`, and an `@` check narrowed to a grammar that lets the last
    one through to `os.path.isfile`. The configuration is unchanged."""
    path = _config(tmp_path, yaml.safe_dump({"privacy_profile": name}))
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.phi_tags = {"0010,0010": {"action": "KEEP", "name": "sentinel"}}
        before = dict(session.configuration.phi_tags)
        with pytest.raises(ValueError) as caught:
            session.load_config(path)
        assert session.configuration.phi_tags == before
        assert session.configuration.privacy_profile is None
    message = str(caught.value)
    assert repr(name) in message, message
    assert "basic@2026c" in message, message
    assert "is not a profile this isocenter ships" in message, message


def test_an_edition_shaped_name_is_never_read_as_a_file(tmp_path, monkeypatch):
    """Kills the `@` check placed after `os.path.isfile`: a file named
    `basic@2027a` in the working directory would turn a refused edition
    into a silently loaded external profile.

    The second half kills a string sentinel for the floor: an external
    profile file named `floor` loads as that file, and the report does
    not call it the floor policy."""
    monkeypatch.chdir(tmp_path)
    profile_text = "phi_tags:\n  '0018,1030': {action: KEEP, name: Protocol}\n"
    (tmp_path / "basic@2027a").write_text(profile_text, encoding="utf-8")
    path = _config(tmp_path, "privacy_profile: basic@2027a\n")
    with Session(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match="is not a profile this isocenter ships"):
            session.load_config(path)
        assert session.configuration.phi_tags.get("0018,1030") != {
            "action": "KEEP", "name": "Protocol"}

    (tmp_path / "floor").write_text(profile_text, encoding="utf-8")
    path = _config(tmp_path, "privacy_profile: floor\n", name="floor.yaml")
    with Session(str(tmp_path / "f.db")) as session:
        session.load_config(path)
        assert session.configuration.phi_tags == {
            "0018,1030": {"action": "KEEP", "name": "Protocol"}}
        assert session.configuration.privacy_profile == "floor"
        rows = _report_rows(session, tmp_path)
    assert "floor policy" not in rows["Privacy Profile"], rows
    assert "floor policy" not in rows["De-ID Method"], rows
    assert "Custom profile 'floor'" in rows["De-ID Method"], rows


def test_an_unshipped_edition_from_a_newer_schema_says_so(tmp_path):
    """Kills the newer-minor hint not wired to the profile value (L1
    reviewer point 4): a `2.1` file naming an edition this version lacks
    is told a newer isocenter may read it; a `2.0` file is not."""
    newer = _config(tmp_path, 'version: "2.1"\nprivacy_profile: basic@2027a\n',
                    name="newer.yaml")
    with pytest.raises(ValueError) as caught:
        ConfigLoader.load_unified_config(newer)
    assert "declares version 2.1" in str(caught.value), str(caught.value)
    assert "needs a newer isocenter" in str(caught.value), str(caught.value)

    same = _config(tmp_path, 'version: "2.0"\nprivacy_profile: basic@2027a\n',
                   name="same.yaml")
    with pytest.raises(ValueError) as caught:
        ConfigLoader.load_unified_config(same)
    assert "declares version" not in str(caught.value), str(caught.value)


def test_the_floor_is_the_pinned_base_with_the_research_defaults(tmp_path):
    """Kills the floor built from something other than its base."""
    assert FLOOR_POLICY == {**profiles.PRIVACY_PROFILES[PINNED_FLOOR_BASE],
                            **RESEARCH_DEFAULTS}

    tags, _, _, _, base = ConfigLoader.load_unified_config(
        _config(tmp_path, "remove_private_tags: true\n"))
    assert tags == FLOOR_POLICY
    assert base is profiles.FLOOR

    with Session(str(tmp_path / "s.db")) as session:
        assert session.configuration.phi_tags == FLOOR_POLICY


def test_every_shipped_edition_has_its_table_vendored():
    """Kills an edition shipped with no table behind it, and the 2026c
    fixture replaced by a later edition's (the refresh recipe `annex_e.py`
    carried before #714, which would rewrite what `basic@2026c` means)."""
    for name, table in profiles.PRIVACY_PROFILES.items():
        edition = name.partition("@")[2]
        fixture = FIXTURES / f"ps3.15-{edition}-table-e1-1.json"
        assert fixture.is_file(), f"{name} ships with no vendored table at {fixture}"
        assert json.loads(fixture.read_text(encoding="utf-8"))["edition"] == edition
    assert derive(load_table(FIXTURES / "ps3.15-2026c-table-e1-1.json")) == \
        profiles.PRIVACY_PROFILES["basic@2026c"]


def test_the_scaffold_names_and_diffs_one_profile(tmp_path, monkeypatch):
    """The name `create_config()` writes and the table its `phi_tags` are
    diffed against come from one constant, `profiles.FLOOR_BASE`.

    `session.py` reads `profiles.FLOOR_BASE` and `profiles.PRIVACY_PROFILES`
    as attributes of the module at call time, so patching the `profiles`
    module reaches both. The patched base is a registered table differing
    from 2026c in one row (Institution Name REMOVE, not EMPTY); the
    scaffold must name that table **and** write the one row the floor has
    that it lacks. Kills the name and the diffed table read from two
    places (a literal `"basic"` beside `BASIC_PROFILE`, before #714)."""
    config = tmp_path / "scaffold.yaml"
    with Session(str(tmp_path / "s.db")) as session:
        session.create_config(str(config))
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["privacy_profile"] == "basic@2026c"
    assert data["phi_tags"] == RESEARCH_DEFAULTS
    tags, _, _, _, _ = ConfigLoader.load_unified_config(str(config))
    assert tags == FLOOR_POLICY

    other = {tag: dict(rule) for tag, rule in BASIC_PROFILE.items()}
    other["0008,0080"] = {"action": "REMOVE", "name": "Institution Name"}
    monkeypatch.setitem(profiles.PRIVACY_PROFILES, "basic@2099z", other)
    monkeypatch.setattr(profiles, "FLOOR_BASE", "basic@2099z")
    patched = tmp_path / "patched.yaml"
    with Session(str(tmp_path / "p.db")) as session:
        session.create_config(str(patched))
    data = yaml.safe_load(patched.read_text(encoding="utf-8"))
    assert data["privacy_profile"] == "basic@2099z"
    assert data["phi_tags"] == {**RESEARCH_DEFAULTS,
                                "0008,0080": BASIC_PROFILE["0008,0080"]}


def test_save_writes_the_pinned_name(tmp_path):
    """Kills `save()` writing the file's spelling. Calls `save()` directly
    rather than relying on auto-save, which #715 makes opt-in."""
    path = _config(tmp_path, "privacy_profile: basic\n")
    saved = tmp_path / "saved.yaml"
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(path)
        session.configuration.config_path = str(saved)
        session.configuration.save()
        tags = session.configuration.phi_tags
    assert yaml.safe_load(saved.read_text(encoding="utf-8"))["privacy_profile"] == "basic@2026c"
    with Session(str(tmp_path / "t.db")) as session:
        session.load_config(str(saved))
        assert session.configuration.phi_tags == tags
        assert session.configuration.privacy_profile == "basic@2026c"


def test_the_report_names_the_edition_and_tells_floor_from_none(tmp_path):
    """Kills the floor and `none` labelled alike (0.9.8's report said
    `None (session defaults)` for both), the report printing the file's
    spelling, and the edition hard-coded rather than read from the name."""
    with Session(str(tmp_path / "bare.db")) as session:
        bare = _report_rows(session, tmp_path, "bare.md")
    assert "session defaults" in bare["Privacy Profile"].lower(), bare
    assert "floor" in bare["Privacy Profile"], bare
    assert "basic@2026c" in bare["Privacy Profile"], bare
    assert "edition 2026c" in bare["De-ID Method"], bare

    with Session(str(tmp_path / "basic.db")) as session:
        session.load_config(_config(tmp_path, "privacy_profile: basic\n", "b.yaml"))
        basic = _report_rows(session, tmp_path, "basic.md")
    assert basic["Privacy Profile"] == "| Privacy Profile | basic@2026c |", basic
    assert "edition 2026c" in basic["De-ID Method"], basic
    assert "646 tag rules" in basic["De-ID Method"], basic

    with Session(str(tmp_path / "none.db")) as session:
        session.load_config(_config(tmp_path, "privacy_profile: none\n", "n.yaml"))
        none = _report_rows(session, tmp_path, "none.md")
    for row in none.values():
        assert "session defaults" not in row.lower(), none
        assert "floor" not in row, none
        assert "2026c" not in row, none
    assert none["Privacy Profile"] == "| Privacy Profile | None (no base profile) |", none
    assert none["De-ID Method"].startswith("| De-ID Method | No profile: "), none

    # The edition is read from the name: a registered name with another
    # edition is reported with that edition.
    other = {tag: dict(rule) for tag, rule in BASIC_PROFILE.items()}
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(profiles.PRIVACY_PROFILES, "basic@2099z", other)
        with Session(str(tmp_path / "other.db")) as session:
            session.load_config(_config(tmp_path, "privacy_profile: basic@2099z\n", "o.yaml"))
            rows = _report_rows(session, tmp_path, "other.md")
    assert "edition 2099z" in rows["De-ID Method"], rows
    assert "2026c" not in rows["De-ID Method"], rows


def test_each_load_resets_what_the_report_calls_the_floor(tmp_path):
    """One session: bare, then `none`, then a file with no profile line,
    then `basic`. Kills the floor flag set only when true and never
    cleared, and set at construction only."""
    with Session(str(tmp_path / "s.db")) as session:
        assert "floor" in _report_rows(session, tmp_path, "0.md")["Privacy Profile"]

        session.load_config(_config(tmp_path, "privacy_profile: none\n", "none.yaml"))
        row = _report_rows(session, tmp_path, "1.md")["Privacy Profile"]
        assert row == "| Privacy Profile | None (no base profile) |", row

        session.load_config(_config(tmp_path, "remove_private_tags: true\n", "floor.yaml"))
        row = _report_rows(session, tmp_path, "2.md")["Privacy Profile"]
        assert "floor" in row, row

        session.load_config(_config(tmp_path, "privacy_profile: basic\n", "basic.yaml"))
        row = _report_rows(session, tmp_path, "3.md")["Privacy Profile"]
        assert row == "| Privacy Profile | basic@2026c |", row


def test_a_0_9_8_scaffold_still_means_the_floor(tmp_path):
    """`scaffolded_config_0_9_8.yaml` is the scaffold golden as the v0.9.8
    tag has it, verbatim: `privacy_profile: basic` over the research
    defaults. It loads to the floor, and the configuration names the
    pinned table. Kills the bare alias dropped, and a 0.9.x file refused."""
    fixture = str(FIXTURES / "scaffolded_config_0_9_8.yaml")
    tags, _, _, _, base = ConfigLoader.load_unified_config(fixture)
    assert tags == FLOOR_POLICY
    assert base == "basic@2026c"
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(fixture)
        assert session.configuration.privacy_profile == "basic@2026c"


def test_audit_config_path_refuses_an_unshipped_edition_before_a_secret(tmp_path):
    """Kills the resolution done in `load_config` only: `audit(config_path=)`
    goes through the same loader, and refuses before a project secret is
    created."""
    db = tmp_path / "s.db"
    path = _config(tmp_path, "privacy_profile: basic@2027a\n")
    with Session(str(db)) as session:
        with pytest.raises(ValueError, match="is not a profile this isocenter ships"):
            session.audit(config_path=path)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_the_policy_base_names_what_the_policy_was_built_on(tmp_path, capsys):
    """`_policy_base` is the one-string identifier `load_config` prints and
    #555's policy record is to carry. Kills each of its three returns
    replaced (the probe's survivors at introduction)."""
    external = tmp_path / "site_profile.yaml"
    external.write_text("phi_tags:\n  '0018,1030': {action: KEEP, name: Protocol}\n",
                        encoding="utf-8")
    cases = [
        (None, "floor over basic@2026c"),
        ("privacy_profile: none\n", "none"),
        ("remove_private_tags: true\n", "floor over basic@2026c"),
        ("privacy_profile: basic\n", "basic@2026c"),
        (f"privacy_profile: {external}\n", str(external)),
    ]
    for i, (text, expected) in enumerate(cases):
        with Session(str(tmp_path / f"{i}.db")) as session:
            if text is not None:
                session.load_config(_config(tmp_path, text, f"{i}.yaml"))
                assert f" - Privacy Profile: {expected}\n" in capsys.readouterr().out
            assert session.configuration._policy_base == expected, (text, expected)


def test_the_floor_flag_is_private_state_not_part_of_the_frozen_shape():
    """`_floor` is not a constructor parameter, not in the repr, and not
    in equality, so the frozen field list and what two configurations
    compare equal on are unchanged. Kills `init=`, `repr=` or `compare=`
    flipped on it."""
    from isocenter.configuration import IsocenterConfiguration
    with pytest.raises(TypeError):
        IsocenterConfiguration(_floor=False)  # pylint: disable=unexpected-keyword-arg
    first, second = IsocenterConfiguration(), IsocenterConfiguration()
    second._floor = False
    assert first == second
    assert "_floor" not in repr(first)


def test_an_external_profile_with_no_rules_is_reported_as_no_base(tmp_path):
    """An external profile that contributes no rules names no base: the
    loader drops its path, and the report says `None (no base profile)`,
    which is true of it and of `privacy_profile: none` alike -- not
    `privacy_profile: none`, a line that file never had (review of #738,
    B1). Kills the loader keeping the empty profile's path."""
    empty = tmp_path / "empty_profile.yaml"
    empty.write_text("phi_tags: {}\n", encoding="utf-8")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(_config(tmp_path, f"privacy_profile: {empty}\n"))
        assert session.configuration.privacy_profile is None
        assert session.configuration.phi_tags == {}
        rows = _report_rows(session, tmp_path)
    assert rows["Privacy Profile"] == "| Privacy Profile | None (no base profile) |", rows
    assert rows["De-ID Method"] == (
        "| De-ID Method | No profile: 0 tag rules, 0 pixel redaction rules |"), rows


def test_a_bare_name_assigned_in_code_is_reported_as_the_built_in(tmp_path):
    """`configuration.privacy_profile` is a public field; `"basic"` assigned
    into it in code is the built-in, reported under its pinned name, not a
    custom profile named `basic` (review of #738, N2). Kills the report
    looking the field up without the aliases."""
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.privacy_profile = "basic"
        rows = _report_rows(session, tmp_path)
    assert rows["Privacy Profile"] == "| Privacy Profile | basic@2026c |", rows
    assert "Profile 'basic@2026c' (PS3.15 Annex E Table E.1-1, edition 2026c)" in rows["De-ID Method"], rows
    assert "Custom profile" not in rows["De-ID Method"], rows


def test_the_two_refusals_say_what_the_changelog_quotes(tmp_path):
    """Both refusals, whole: the file's path first, the value, the names
    this version ships, and what `basic` means (review of #738, N3). Kills
    the path prefix dropped, the alias clause dropped, and the unknown-name
    list narrowed to the pinned names."""
    unshipped = _config(tmp_path, "privacy_profile: basic@2027a\n", "u.yaml")
    with pytest.raises(ValueError) as caught:
        ConfigLoader.load_unified_config(unshipped)
    assert str(caught.value) == (
        f"{unshipped}: privacy_profile 'basic@2027a' is not a profile this "
        "isocenter ships; it ships basic@2026c ('basic' means basic@2026c). "
        "A later PS3.15 edition arrives as a new name in a newer isocenter (#714)")

    unknown = _config(tmp_path, "privacy_profile: Basic\n", "k.yaml")
    with pytest.raises(ValueError) as caught:
        ConfigLoader.load_unified_config(unknown)
    assert str(caught.value) == (
        f"{unknown}: privacy_profile 'Basic' is neither a built-in profile "
        "(basic, basic@2026c), 'none', nor an existing file")

"""The policy fingerprint over what a configuration file loads to (#754).

`test_the_policy_fingerprint.py` pins the canonical form over dicts. A
store's statuses are compared with the fingerprint of whatever
`load_config()` made of a file, so the properties a person relies on are
properties of the loader and `save()` together with the form, and the
review of #750 (finding 5) checked them by hand and found no test:

- a file `save()` writes loads to the policy it was written from;
- key order, rule order, a tag's case and a rule's `name` do not make
  another policy;
- `basic` and `basic@2026c` are one policy;
- the floor is one policy by each door that gives it.

And where the loader stands between a file and the edges of the form the
owner ruled stay as they are (Q4 A, recorded on #935): `value: null`
**is** something a file can say, and is another policy than no `value`; a
set, a rule key that is not a string and a tag that is not a string are
refused before the form sees them; `set_phi_tag` lowercases a tag and a
direct assignment to `phi_tags` does not.

No fingerprint is written here as hex: every one moves with
`CONFIG_VERSION`, which each file below reads for its `version:` line.

**Why this file imports what it does.** The loader through
`isocenter.config_manager` and the form through `isocenter.configuration`,
so both modules' probe rows are charged.
"""
from pathlib import Path

import pytest
import yaml

from isocenter import config_manager
from isocenter.config_manager import ConfigLoader
from isocenter.configuration import _scan_policy_for
from isocenter.session import DicomSession as Session

NAME = "0010,0010"
DESC = "0008,103e"
FLOOR_LABEL = "floor over basic@2026c"


def _head(profile_line="privacy_profile: none\n"):
    # Read at call time: a minor bump must not turn this file red.
    return f'version: "{config_manager.CONFIG_VERSION}"\n' + profile_line


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _loaded(path):
    """What the one loader gives for a file: `(phi_tags, fingerprint)`."""
    tags, _machines, _jitter, private, _base = (
        ConfigLoader.load_unified_config(str(path)))
    return tags, _scan_policy_for(tags, private, "base").fingerprint


def _policy_of(tmp_path, path, db):
    """The policy in force in a new session that loaded `path`."""
    with Session(str(tmp_path / db)) as session:
        session.load_config(str(path))
        return session.configuration._scan_policy(), dict(
            session.configuration.phi_tags)


RULES = (f'phi_tags:\n  "{NAME}": {{action: REPLACE, value: v}}\n'
         f'  "{DESC}": {{action: KEEP}}\n')

#: Per base: the file's text, or None for `create_config()`'s scaffold.
ROUND_TRIPS = {
    "none": lambda: _head() + RULES,
    "floor": lambda: _head("") + RULES,
    "basic": lambda: _head("privacy_profile: basic\n") + RULES,
    "scaffold": lambda: None,
}


@pytest.mark.parametrize("base", sorted(ROUND_TRIPS))
def test_a_saved_configuration_loads_to_the_same_policy(tmp_path, base):
    """Kills: `save()` writing a rule the loader reads back differently (a
    key dropped, a tag's case changed, the profile line lost or inlined
    under another name)."""
    path = tmp_path / "c.yaml"
    text = ROUND_TRIPS[base]()
    with Session(str(tmp_path / "first.db")) as session:
        if text is None:
            session.create_config(str(path))
        else:
            path.write_text(text, encoding="utf-8")
        session.load_config(str(path))
        before = session.configuration._scan_policy()
        tags_before = dict(session.configuration.phi_tags)
        session.configuration.save()
    after, tags_after = _policy_of(tmp_path, path, "second.db")
    assert tags_before, "an empty policy round-trips whatever save() does"
    assert tags_after == tags_before
    assert after.fingerprint == before.fingerprint
    assert after.base == before.base
    assert after == before


def test_a_null_value_survives_a_save(tmp_path):
    """`value: null` loads as a rule holding `value: None`, which is another
    policy than the rule with no `value`, and `save()` writes the line
    back. Kills: the loader or `save()` dropping a null `value` (every
    store whose file has the line would re-audit, or stop re-auditing)."""
    null = _write(tmp_path, "null.yaml", _head()
                  + f'phi_tags:\n  "{DESC}": {{action: REPLACE, value: null}}\n')
    absent = _write(tmp_path, "absent.yaml", _head()
                    + f'phi_tags:\n  "{DESC}": {{action: REPLACE}}\n')
    absent_tags, absent_fingerprint = _loaded(absent)
    assert absent_tags == {DESC: {"action": "REPLACE"}}

    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(null))
        assert session.configuration.phi_tags == {
            DESC: {"action": "REPLACE", "value": None}}
        before = session.configuration._scan_policy().fingerprint
        session.configuration.save()
    saved = yaml.safe_load(null.read_text(encoding="utf-8"))
    assert saved["phi_tags"] == {DESC: {"action": "REPLACE", "value": None}}
    assert "value: null" in null.read_text(encoding="utf-8")
    after, _tags = _policy_of(tmp_path, null, "again.db")
    assert after.fingerprint == before
    assert before != absent_fingerprint


#: Two files that must load to one policy, by what differs between them.
ONE_POLICY = {
    "key order": (f'phi_tags:\n  "{NAME}": {{action: REPLACE, value: v}}\n',
                  f'phi_tags:\n  "{NAME}": {{value: v, action: REPLACE}}\n'),
    "rule order": (RULES,
                   f'phi_tags:\n  "{DESC}": {{action: KEEP}}\n'
                   f'  "{NAME}": {{action: REPLACE, value: v}}\n'),
    "tag case": (f'phi_tags:\n  "{DESC}": {{action: KEEP}}\n',
                 f'phi_tags:\n  "{DESC.upper()}": {{action: KEEP}}\n'),
    "name": (f'phi_tags:\n  "{DESC}": {{action: KEEP, name: One}}\n',
             f'phi_tags:\n  "{DESC}": {{action: KEEP, name: Another}}\n'),
}


@pytest.mark.parametrize("what", sorted(ONE_POLICY))
def test_key_order_rule_order_tag_case_and_name_do_not_make_another_policy(
        tmp_path, what):
    """Kills: the loader keeping a tag's case; `name` reaching the form;
    an order reaching the form."""
    a_text, b_text = ONE_POLICY[what]
    assert a_text != b_text
    a_tags, a = _loaded(_write(tmp_path, "a.yaml", _head() + a_text))
    b_tags, b = _loaded(_write(tmp_path, "b.yaml", _head() + b_text))
    assert a_tags and b_tags
    assert a == b
    # And the two are not one policy because every file is: a third file
    # with another action is another policy.
    _tags, other = _loaded(_write(
        tmp_path, "c.yaml", _head() + f'phi_tags:\n  "{DESC}": {{action: REMOVE}}\n'))
    assert other != a


def test_a_values_surrounding_space_is_another_policy_from_a_file(tmp_path):
    """`value: " v "` loads with its spaces and writes them, so it is
    another policy than `value: "v"` (owner ruling Q2 A on #969). Kills:
    the loader or the form stripping a string value."""
    spaced_tags, spaced = _loaded(_write(
        tmp_path, "spaced.yaml",
        _head() + f'phi_tags:\n  "{DESC}": {{action: REPLACE, value: " v "}}\n'))
    bare_tags, bare = _loaded(_write(
        tmp_path, "bare.yaml",
        _head() + f'phi_tags:\n  "{DESC}": {{action: REPLACE, value: "v"}}\n'))
    assert spaced_tags == {DESC: {"action": "REPLACE", "value": " v "}}
    assert bare_tags == {DESC: {"action": "REPLACE", "value": "v"}}
    assert spaced != bare


def test_basic_and_its_pinned_name_are_one_policy(tmp_path):
    """Kills: the alias resolved to another table, or recorded under the
    bare name."""
    bare, bare_tags = _policy_of(
        tmp_path, _write(tmp_path, "bare.yaml", _head("privacy_profile: basic\n")),
        "bare.db")
    pinned, pinned_tags = _policy_of(
        tmp_path, _write(tmp_path, "pinned.yaml",
                         _head("privacy_profile: basic@2026c\n")), "pinned.db")
    assert len(bare_tags) > 100
    assert bare_tags == pinned_tags
    assert bare.fingerprint == pinned.fingerprint
    assert bare.base == pinned.base == "basic@2026c"
    # `none` is not that policy.
    _tags, none = _loaded(_write(tmp_path, "none.yaml", _head()))
    assert none != bare.fingerprint


def test_the_floor_is_one_policy_by_three_doors(tmp_path):
    """A new session, a file with no `privacy_profile` line, and
    `create_config()`'s scaffold loaded back: one fingerprint. The scaffold
    names the floor's base profile and writes the floor's differences from
    it, so its base label is the profile's and the other two say `floor`.
    Kills: a door that builds the floor its own way."""
    scaffold = tmp_path / "scaffold.yaml"
    with Session(str(tmp_path / "new.db")) as session:
        new = session.configuration._scan_policy()
        new_tags = dict(session.configuration.phi_tags)
        session.create_config(str(scaffold))
    from_file, file_tags = _policy_of(
        tmp_path, _write(tmp_path, "floor.yaml", _head("")), "file.db")
    scaffolded, scaffold_tags = _policy_of(tmp_path, scaffold, "scaffold.db")

    assert len(new_tags) > 100
    assert new_tags == file_tags == scaffold_tags
    assert new.fingerprint == from_file.fingerprint == scaffolded.fingerprint
    assert new.base == from_file.base == FLOOR_LABEL
    assert scaffolded.base == "basic@2026c"
    # The floor is not `basic` under another label.
    basic, _tags = _policy_of(
        tmp_path, _write(tmp_path, "basic.yaml", _head("privacy_profile: basic\n")),
        "basic.db")
    assert basic.fingerprint != new.fingerprint


REFUSED = {
    "a set value": f'phi_tags:\n  "{DESC}": {{action: REPLACE, value: !!set {{a, b}}}}\n',
    "an int rule key": f'phi_tags:\n  "{DESC}": {{action: REPLACE, 1: x}}\n',
    "an int tag key": "phi_tags:\n  1048592: {action: KEEP}\n",
}


@pytest.mark.parametrize("what", sorted(REFUSED))
def test_the_loader_refuses_what_the_form_cannot_order(tmp_path, what):
    """A set hashes by process, and a key that is not a string is hashed
    as its repr: neither reaches the form from a file. The class only; the
    words are the loader's to change. Kills: one of these loading."""
    # The same head with an ordinary rule loads, so the refusal below is
    # for the rule and not for the file around it.
    tags, _fingerprint = _loaded(_write(
        tmp_path, "control.yaml",
        _head() + f'phi_tags:\n  "{DESC}": {{action: REPLACE, value: a}}\n'))
    assert tags == {DESC: {"action": "REPLACE", "value": "a"}}
    path = _write(tmp_path, "refused.yaml", _head() + REFUSED[what])
    with pytest.raises(ValueError):
        ConfigLoader.load_unified_config(str(path))


def test_set_phi_tag_lowercases_and_a_direct_assignment_does_not(tmp_path):
    """The form does not fold a tag's case (the pure tests), so the doors
    do: `set_phi_tag("0008,103E", ...)` writes the lower-case rule and
    leaves the policy alone, and `phi_tags["0008,103E"] = ...` is a second
    rule and another policy. Kills: `set_phi_tag` keeping the case; the
    form lowercasing."""
    path = _write(tmp_path, "c.yaml",
                  _head() + f'phi_tags:\n  "{DESC}": {{action: KEEP}}\n')
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        loaded = session.configuration._scan_policy().fingerprint
        session.configuration.set_phi_tag(DESC.upper(), "KEEP")
        assert sorted(session.configuration.phi_tags) == [DESC]
        assert session.configuration._scan_policy().fingerprint == loaded

        session.configuration.phi_tags[DESC.upper()] = {"action": "KEEP"}
        assert sorted(session.configuration.phi_tags) == [DESC.upper(), DESC]
        assert session.configuration._scan_policy().fingerprint != loaded
    assert Path(path).read_text(encoding="utf-8").count(DESC) == 1

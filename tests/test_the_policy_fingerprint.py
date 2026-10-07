"""The fingerprint a PHI status's policy is recorded under (#555).

A status is recorded under the policy the scan that concluded it ran with,
and the store keeps that policy as `"v1:"` plus the sha256 of a canonical
form. Two policies are the same policy when their fingerprints are equal,
so the canonical form decides it:

- **What a scan reads is in it**: every rule key but `name`, the rule's
  form (a bare-string rule against a mapping), and `remove_private_tags`,
  which on `CT_small` is the difference between 183 findings and 4.
- **`name` is not**: it only labels a finding, and relabelling a tag must
  not make a store's statuses read as recorded under another policy.
- **Nothing is normalized.** `action: ""` and `action: "REPLACE"` are read
  differently by two of the inspector's sites, so they hash differently.
  The fingerprint may tell apart two policies that scan alike (a re-audit
  is the cost), and must never equate two that scan differently (#555
  again).
- **`CONFIG_VERSION` is in it (#762)**: the dict does not say how the
  library reads it, and a release that reads an unchanged dict
  differently bumps the minor.

`test_the_v1_fingerprint_is_pinned` is what makes v1 permanent: every 1.x
store carries v1 records, so a later canonical form is a v2 beside it,
never an edit of v1. The pins hold the form, not the live version: each
reads `CONFIG_VERSION` as `PINNED_VERSION`, so a legitimate minor bump
leaves them green and only an edit of the form turns them red. (#762
edited v1 once, before any release carried a v1 record.)

Pure: no session, no store.
"""
import datetime
import hashlib
import os
import subprocess
import sys

import pytest

import isocenter
from isocenter import config_manager, configuration
from isocenter.configuration import _canonical_policy_v1, _scan_policy_for

NAME = "0010,0010"
INST = "0008,0080"


def _fp(tags, remove_private_tags=True):
    return _scan_policy_for(tags, remove_private_tags, "base").fingerprint


def test_a_name_is_not_part_of_the_policy():
    """Kills: `name` hashed with the rest of the rule."""
    a = {NAME: {"action": "REPLACE", "name": "PatientName"}}
    b = {NAME: {"action": "REPLACE", "name": "Relabelled"}}
    c = {NAME: {"action": "REPLACE"}}
    assert _fp(a) == _fp(b) == _fp(c)


def test_two_bare_string_rules_differ_only_in_their_label():
    """A bare-string rule's text is its display name (`privacy.py`'s
    `description = str(config_val)`), read as REPLACE. It is `name` by
    another spelling, so it is out of the fingerprint too."""
    assert _fp({NAME: "PatientName"}) == _fp({NAME: "Anything"})


PAIRS = {
    "action": ({NAME: {"action": "REPLACE"}}, {NAME: {"action": "REMOVE"}}),
    "value": ({NAME: {"action": "REPLACE", "value": "A"}},
              {NAME: {"action": "REPLACE", "value": "B"}}),
    "a tag added": ({NAME: {"action": "REPLACE"}},
                    {NAME: {"action": "REPLACE"}, INST: {"action": "EMPTY"}}),
    "a tag removed": ({NAME: {"action": "REPLACE"}, INST: {"action": "EMPTY"}},
                      {INST: {"action": "EMPTY"}}),
    "empty action": ({NAME: {"action": ""}}, {NAME: {"action": "REPLACE"}}),
    "string form": ({NAME: "PatientName"}, {NAME: {"action": "REPLACE"}}),
    # `if not config_val: continue` at the inspector's configured-tag
    # site: an empty string rule is skipped there, a named one is not.
    "empty string": ({NAME: ""}, {NAME: "PatientName"}),
    "empty mapping": ({NAME: {}}, {NAME: {"action": "REPLACE"}}),
}


@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_what_a_scan_reads_is_part_of_the_policy(pair):
    """Kills: any field dropped; a normalization equating `""` with REPLACE,
    a string rule with a mapping, or an empty string rule with a named one."""
    a, b = PAIRS[pair]
    assert _fp(a) != _fp(b)


def test_remove_private_tags_is_part_of_the_policy():
    """Kills: the flag left out of the canonical form."""
    tags = {NAME: {"action": "REPLACE"}}
    assert _fp(tags, True) != _fp(tags, False)


def test_the_order_of_rules_does_not_matter():
    """Kills: `sort_keys` dropped."""
    a = {NAME: {"action": "REPLACE", "value": "v"}, INST: {"action": "EMPTY"}}
    b = {INST: {"action": "EMPTY"}, NAME: {"value": "v", "action": "REPLACE"}}
    assert list(a) != list(b)
    assert _fp(a) == _fp(b)


def test_a_value_json_cannot_hold_does_not_raise():
    """`_scan_policy()` runs at export with no validator in front of it,
    over a `phi_tags` code can assign. Kills: no `default=`; the inner keys
    not coerced (`TypeError` from `json.dumps` or from `sort_keys`)."""
    odd = {
        NAME: {"action": "REPLACE", "value": datetime.date(2020, 1, 2)},
        0x00100020: {"action": "REPLACE"},
        INST: {"action": "EMPTY", 7: "seven"},
    }
    fingerprint = _fp(odd)
    assert fingerprint.startswith("v1:") and len(fingerprint) == 3 + 64
    # And a date is not confused with its text, nor with another date:
    # what JSON cannot hold is hashed as what it was, not dropped.
    as_text = {NAME: {"action": "REPLACE", "value": "2020-01-02"}}
    assert _fp({NAME: odd[NAME]}) != _fp(as_text)
    other_day = {NAME: {"action": "REPLACE", "value": datetime.date(2021, 3, 4)}}
    assert _fp({NAME: odd[NAME]}) != _fp(other_day)


#: The `CONFIG_VERSION` every pin below is taken under, whatever the live
#: one is: the pins hold the form, and a minor bump is not an edit of it.
PINNED_VERSION = "2.0"


@pytest.fixture
def pinned_version(monkeypatch):
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", PINNED_VERSION)


def test_a_value_outside_ascii_is_escaped_not_refused(pinned_version):
    """The canonical form is ASCII: a non-ASCII value is escaped, so it
    hashes and never raises on the `.encode("ascii")`. Kills:
    `ensure_ascii` turned off."""
    tags = {NAME: {"action": "REPLACE", "value": "Müller"}}
    assert _canonical_policy_v1(tags, True) == (
        b'{"config_version":"2.0",'
        b'"phi_tags":{"0010,0010":{"action":"REPLACE","value":"M\\u00fcller"}},'
        b'"remove_private_tags":true}'), NEVER_CHANGE_V1


#: Computed once from the implementation and pasted (re-pasted by #762,
#: which added `config_version` before any release carried a v1 record).
#: The canonical bytes are pinned as well as the digest, so a failure says
#: which half moved.
PINNED_TAGS = {NAME: {"action": "REPLACE", "name": "x"},
               INST: {"action": "EMPTY"}}
PINNED_CANONICAL = (b'{"config_version":"2.0",'
                    b'"phi_tags":{"0008,0080":{"action":"EMPTY"},'
                    b'"0010,0010":{"action":"REPLACE"}},'
                    b'"remove_private_tags":true}')
PINNED_FINGERPRINT = (
    "v1:af588d97c17092db11531bde1e71841fe7712c2909beffbd37129ebc4108064e")

NEVER_CHANGE_V1 = (
    "The v1 canonical form is a store format: every store a 1.x wrote "
    "carries v1 records, and a 1.0 store opens in every 1.x. Never change "
    "v1. Add `_canonical_policy_v2` with a 'v2:' prefix beside it, and "
    "compare a stored v1 record against the in-force policy's v1 "
    "fingerprint (#555). A CONFIG_VERSION bump is not a change of v1: "
    "these pins read it as PINNED_VERSION (#762).")


def test_the_v1_fingerprint_is_pinned(pinned_version):
    """Kills: any change to the canonical form -- separators,
    `ensure_ascii`, the prefix, the key names `config_version`, `phi_tags`
    and `remove_private_tags`, the `__form__` spelling."""
    assert _canonical_policy_v1(PINNED_TAGS, True) == PINNED_CANONICAL, \
        NEVER_CHANGE_V1
    assert ("v1:" + hashlib.sha256(PINNED_CANONICAL).hexdigest()
            == PINNED_FINGERPRINT), NEVER_CHANGE_V1
    assert _fp(PINNED_TAGS) == PINNED_FINGERPRINT, NEVER_CHANGE_V1


def test_the_string_form_spelling_is_pinned(pinned_version):
    """The `__form__` half of the canonical form, which B5's mapping-only
    policy does not reach."""
    assert _canonical_policy_v1({NAME: "PatientName", INST: ""}, False) == (
        b'{"config_version":"2.0","phi_tags":{"0008,0080":{"__form__":"empty-string"},'
        b'"0010,0010":{"__form__":"string"}},"remove_private_tags":false}'), \
        NEVER_CHANGE_V1


def test_the_base_is_not_part_of_the_fingerprint():
    """A `create_config()` scaffold and the bare floor are one policy under
    two bases."""
    tags = {NAME: {"action": "REPLACE"}}
    a = _scan_policy_for(tags, True, "floor over basic@2026c")
    b = _scan_policy_for(tags, True, "/tmp/scaffold.yaml")
    assert a.fingerprint == b.fingerprint
    assert a != b


def test_the_label_helper_spells_each_base_once():
    """The configuration and `audit(config_path=)` read one helper, so one
    base cannot be spelled two ways."""
    from isocenter import profiles
    assert configuration._policy_base_label(profiles.FLOOR) == (
        "floor over basic@2026c")
    assert configuration._policy_base_label(None) == "none"
    assert configuration._policy_base_label("basic@2026c") == "basic@2026c"

    config = configuration.IsocenterConfiguration()
    assert config._policy_base == "floor over basic@2026c"
    # The floor flag still defaults True: a name assigned in code wins.
    config.privacy_profile = "basic@2026c"
    assert config._policy_base == "basic@2026c"
    config.privacy_profile = None
    config._floor = False
    assert config._policy_base == "none"


def test_the_policy_in_force_is_computed_on_every_call():
    """`phi_tags` can be assigned directly; a cached policy would go stale."""
    config = configuration.IsocenterConfiguration()
    before = config._scan_policy()
    config.phi_tags = {NAME: {"action": "REPLACE"}}
    after = config._scan_policy()
    assert before.fingerprint != after.fingerprint
    assert after == _scan_policy_for(config.phi_tags, True,
                                     "floor over basic@2026c")
    config.remove_private_tags = False
    assert config._scan_policy().fingerprint != after.fingerprint


# --- the configuration schema version is part of the policy (#762) --------
#
# Owner's ruling on #762: a change to what an unchanged configuration scans
# for bumps `CONFIG_VERSION`'s minor, and the v1 canonical form carries
# `CONFIG_VERSION`, so the fingerprint moves with the behaviour and the
# #555 notice says so. Found in the final review of #760: two identical
# dicts scanned differently across #556/#557 and fingerprinted the same.

def test_one_policy_under_two_config_versions_is_two_policies(monkeypatch):
    """Kills: `CONFIG_VERSION` left out of the canonical form; the version
    read from a literal, or bound by `from .config_manager import`, instead
    of `config_manager.CONFIG_VERSION` at call time."""
    tags = {NAME: {"action": "REPLACE"}, INST: {"action": "EMPTY"}}
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", "2.0")
    before = _fp(tags)
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", "2.1")
    after = _fp(tags)
    assert before != after
    assert before.startswith("v1:") and after.startswith("v1:")
    # The configuration's own door reads it the same way.
    config = configuration.IsocenterConfiguration()
    in_force_21 = config._scan_policy().fingerprint
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", "2.0")
    assert config._scan_policy().fingerprint != in_force_21


def test_one_policy_under_one_config_version_is_one_policy(monkeypatch):
    """The other half: the version adds nothing that varies between two
    calls. Kills: anything per-call (a time, an id) hashed beside it."""
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", "2.7")
    a = {NAME: {"action": "REPLACE", "value": "v"}}
    b = {NAME: {"value": "v", "action": "REPLACE"}}
    assert _fp(a) == _fp(b) == _fp(dict(a))
    assert _canonical_policy_v1(a, True) == _canonical_policy_v1(b, True)


# --- what the form does with input the loader never gives it (#754) -------
#
# The form is a store format from 1.0.0, and `_scan_policy()` reads a
# `phi_tags` code can assign, so what it does with a `None`, a key that is
# not a string, a `set` or an upper-case tag is as permanent as what it
# does with a loaded file. Owner's ruling on #754 (Q4 A, recorded on #935):
# each of these stays as measured and is pinned as it is. None is a
# recommendation: a `set` value is a policy no second process can match.
#
# One input is deliberately not pinned: a rule value that is a mapping with
# keys of two types raises `TypeError` (#962). An input that raises has no
# stored fingerprint, so it can be given one later without moving another.

def _canonical(tags, remove_private_tags=True):
    return _canonical_policy_v1(tags, remove_private_tags)


def _rules(body: bytes, private: bytes = b"true") -> bytes:
    """The canonical bytes around a `phi_tags` body, under PINNED_VERSION."""
    return (b'{"config_version":"2.0","phi_tags":{' + body
            + b'},"remove_private_tags":' + private + b'}')


def test_a_null_value_is_not_an_absent_value(pinned_version):
    """A file can say `value: null`, and it is another policy than the
    rule with no `value` (a re-audit when the line comes or goes). Kills:
    a rule key whose value is `None` dropped from the form."""
    with_null = {NAME: {"action": "REPLACE", "value": None}}
    without = {NAME: {"action": "REPLACE"}}
    assert _canonical(with_null) == _rules(
        b'"0010,0010":{"action":"REPLACE","value":null}'), NEVER_CHANGE_V1
    assert _canonical(without) == _rules(
        b'"0010,0010":{"action":"REPLACE"}'), NEVER_CHANGE_V1
    assert _fp(with_null) != _fp(without)
    assert _canonical({NAME: {"action": None}}) == _rules(
        b'"0010,0010":{"action":null}'), NEVER_CHANGE_V1
    assert _fp({NAME: {"action": None}}) != _fp({NAME: {}})


def test_a_key_that_is_not_a_string_is_its_repr(pinned_version):
    """So rule keys `1` and `"1"` are one key. Kills: a key tagged with its
    type; `str()` for `repr()` (the date tells those apart, an `int` does
    not)."""
    assert _canonical({NAME: {1: "x"}}) == _rules(
        b'"0010,0010":{"1":"x"}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {1: "x"}}) == _canonical({NAME: {"1": "x"}})
    assert 1 != "1"
    assert _canonical({0x00100010: {"action": "KEEP"}}) == _rules(
        b'"1048592":{"action":"KEEP"}'), NEVER_CHANGE_V1
    assert _canonical({(0x10, 0x10): {"action": "KEEP"}}) == _rules(
        b'"(16, 16)":{"action":"KEEP"}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {datetime.date(2020, 1, 2): "x"}}) == _rules(
        b'"0010,0010":{"datetime.date(2020, 1, 2)":"x"}'), NEVER_CHANGE_V1


def test_of_two_keys_with_one_repr_the_later_is_kept(pinned_version):
    """A rule holding both `1` and `"1"` is one key in the form, and the
    one written later is the one hashed: the same two entries in the other
    order are another policy. Kills: the first kept; the two kept apart."""
    one_first = {NAME: {1: "a", "1": "b"}}
    text_first = {NAME: {"1": "b", 1: "a"}}
    assert one_first[NAME] == text_first[NAME]
    assert _canonical(one_first) == _rules(
        b'"0010,0010":{"1":"b"}'), NEVER_CHANGE_V1
    assert _canonical(text_first) == _rules(
        b'"0010,0010":{"1":"a"}'), NEVER_CHANGE_V1


UNFOLDED = {
    # A tag assigned to `phi_tags` directly: `set_phi_tag` and the loader
    # lowercase, the form does not.
    "an upper-case tag": ({"0008,103e": {"action": "KEEP"}},
                          {"0008,103E": {"action": "KEEP"}}),
    "a tag with a space": ({NAME: {"action": "KEEP"}},
                           {"0010, 0010": {"action": "KEEP"}}),
    "an action's case": ({NAME: {"action": "KEEP"}},
                         {NAME: {"action": "keep"}}),
    "a number and its text": ({NAME: {"value": 1}}, {NAME: {"value": "1"}}),
    "a bool and its number": ({NAME: {"value": True}}, {NAME: {"value": 1}}),
    "an int and its float": ({NAME: {"value": 1}}, {NAME: {"value": 1.0}}),
    # A file can say `value: " v "`, and it writes another replacement
    # than `value: "v"`: equating them would let a stored status pass for
    # current under a policy that writes a different value (review of
    # #969, S1; owner ruling Q2 A).
    "a value's surrounding space": ({NAME: {"value": " v "}},
                                    {NAME: {"value": "v"}}),
    "a value's case": ({NAME: {"value": "Anon"}}, {NAME: {"value": "ANON"}}),
    # Memory only: the loader refuses `Action` as an unknown key.
    "a rule key's case": ({NAME: {"action": "KEEP"}},
                          {NAME: {"Action": "KEEP"}}),
}


@pytest.mark.parametrize("pair", sorted(UNFOLDED))
def test_nothing_is_normalized_in_a_tag_an_action_or_a_value(pair):
    """Kills: the tag lowercased or stripped inside the form; `action`
    case-folded; a value compared by `==` or by its text; a string value
    stripped or case-folded; a rule key case-folded."""
    a, b = UNFOLDED[pair]
    assert _fp(a) != _fp(b)


def test_the_unfolded_spellings_are_hashed_as_written(pinned_version):
    """What each side of the pairs above is: an inequality alone passes
    when either side is garbage."""
    assert _canonical({"0008,103E": {"action": "keep"}}) == _rules(
        b'"0008,103E":{"action":"keep"}'), NEVER_CHANGE_V1
    assert _canonical({"0010, 0010": {"value": True}}) == _rules(
        b'"0010, 0010":{"value":true}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {"value": 1.0}}) == _rules(
        b'"0010,0010":{"value":1.0}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {"value": 1}}) == _rules(
        b'"0010,0010":{"value":1}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {"value": "1"}}) == _rules(
        b'"0010,0010":{"value":"1"}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {"value": " Anon v "}}) == _rules(
        b'"0010,0010":{"value":" Anon v "}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {"Action": "KEEP", "VALUE": "v"}}) == _rules(
        b'"0010,0010":{"Action":"KEEP","VALUE":"v"}'), NEVER_CHANGE_V1


def test_a_list_and_a_tuple_are_one_value(pinned_version):
    """YAML gives a list; code can assign a tuple; they are one policy.
    Kills: a tuple hashed as its type and text."""
    as_list = {NAME: {"value": [1, 2]}}
    as_tuple = {NAME: {"value": (1, 2)}}
    assert as_list != as_tuple
    assert _canonical(as_tuple) == _rules(
        b'"0010,0010":{"value":[1,2]}'), NEVER_CHANGE_V1
    assert _canonical(as_list) == _canonical(as_tuple)


def test_a_value_json_cannot_hold_is_hashed_as_its_type_and_text(pinned_version):
    """Kills: either of `_tagged`'s key names; `repr` for `str`; the type's
    qualified name; a rule that is not a mapping or a string dropped.

    The set has **one** member: the text of a larger one changes with the
    process (the test below), and bytes pinned over it would be a pin of
    this process's hash seed."""
    assert _canonical({NAME: {"value": {"x"}}}) == _rules(
        b'"0010,0010":{"value":{"__str__":"{\'x\'}","__type__":"set"}}'), \
        NEVER_CHANGE_V1
    assert _canonical({NAME: {"value": frozenset({"x"})}}) == _rules(
        b'"0010,0010":{"value":{"__str__":"frozenset({\'x\'})",'
        b'"__type__":"frozenset"}}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {"value": b"x"}}) == _rules(
        b'"0010,0010":{"value":{"__str__":"b\'x\'","__type__":"bytes"}}'), \
        NEVER_CHANGE_V1
    # `str` and `repr` of a date differ, as they do not for the three above.
    assert _canonical({NAME: {"value": datetime.date(2020, 1, 2)}}) == _rules(
        b'"0010,0010":{"value":{"__str__":"2020-01-02","__type__":"date"}}'), \
        NEVER_CHANGE_V1
    assert _canonical({NAME: None}) == _rules(
        b'"0010,0010":{"__form__":{"__str__":"None","__type__":"NoneType"}}'), \
        NEVER_CHANGE_V1


#: Run in a child with a chosen hash seed: the tree it imported and the
#: fingerprint of a policy whose one value is a set of eight strings.
_SET_IN_A_CHILD = (
    "import isocenter\n"
    "from isocenter.configuration import _scan_policy_for\n"
    "value = {'alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta',"
    " 'theta'}\n"
    "print(isocenter.__file__)\n"
    "print(_scan_policy_for({'0010,0010': {'action': 'REPLACE',"
    " 'value': value}}, True, 'base').fingerprint)\n")


def _set_fingerprint_under(seed):
    # conftest's COVERAGE_FILE reaches every child; this one measures
    # nothing, and a set's order must not depend on a tracer being loaded.
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env.update(PYTHONHASHSEED=str(seed), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-P", "-c", _SET_IN_A_CHILD],
                          env=env, capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    tree, fingerprint = done.stdout.split()
    assert tree == isocenter.__file__, "the child imported another tree"
    assert fingerprint.startswith("v1:") and len(fingerprint) == 3 + 64
    int(fingerprint[3:], 16)
    return fingerprint


def test_a_set_value_is_another_policy_in_another_process():
    """A `set` is hashed as its text, and its text is in hash order, so a
    status recorded under such a policy reads as another policy's in the
    next process (it fails closed, with the #555 notice). Memory only: the
    loader refuses a set. Kills: a set sorted, or hashed as its members,
    before it is tagged.

    Deterministic: each child's `PYTHONHASHSEED` is fixed, so the order
    each one sees is the same on every run. Seed 1 is run twice, so
    "differs" is not two children that each print noise."""
    first = _set_fingerprint_under(1)
    assert _set_fingerprint_under(1) == first
    seen = {first} | {_set_fingerprint_under(seed) for seed in (2, 3)}
    assert len(seen) > 1, (
        "three hash seeds gave one fingerprint for a set of eight strings: "
        "the form no longer hashes a set as its text. " + NEVER_CHANGE_V1)


def test_no_policy_and_an_empty_policy_are_one(pinned_version):
    """Kills: `(phi_tags or {})` removed; `bool()` removed from the
    switch."""
    assert _canonical(None) == _canonical({}) == _rules(b""), NEVER_CHANGE_V1
    assert _canonical({}, 1) == _canonical({}, True) == _rules(b"", b"true")
    assert _canonical({}, None) == _canonical({}, False) == _rules(
        b"", b"false"), NEVER_CHANGE_V1
    assert _canonical({}, 0) == _rules(b"", b"false")


def test_a_nested_mapping_is_sorted_and_nan_is_written(pinned_version):
    """Kills: `allow_nan=False` (a NaN would raise at export); the sort
    not reaching a value's own keys."""
    nested = {"b": 1, "a": 2}
    assert list(nested) == ["b", "a"]
    assert _canonical({NAME: {"value": nested}}) == _rules(
        b'"0010,0010":{"value":{"a":2,"b":1}}'), NEVER_CHANGE_V1
    assert _canonical({NAME: {"value": float("nan")}}) == _rules(
        b'"0010,0010":{"value":NaN}'), NEVER_CHANGE_V1

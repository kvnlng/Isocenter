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

`test_the_v1_fingerprint_is_pinned` is what makes v1 permanent: every 1.x
store carries v1 records, so a later canonical form is a v2 beside it,
never an edit of v1.

Pure: no session, no store.
"""
import datetime
import hashlib

import pytest

from isocenter import configuration
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
    # And a date is not confused with its text.
    as_text = {NAME: {"action": "REPLACE", "value": "2020-01-02"}}
    assert _fp({NAME: odd[NAME]}) != _fp(as_text)


#: Computed once from the implementation and pasted. The canonical bytes
#: are pinned as well as the digest, so a failure says which half moved.
PINNED_TAGS = {NAME: {"action": "REPLACE", "name": "x"},
               INST: {"action": "EMPTY"}}
PINNED_CANONICAL = (b'{"phi_tags":{"0008,0080":{"action":"EMPTY"},'
                    b'"0010,0010":{"action":"REPLACE"}},'
                    b'"remove_private_tags":true}')
PINNED_FINGERPRINT = (
    "v1:b390cfc7bc2264363c68e2f4e9463f21acef78494151eb5a236913c29d7c498a")

NEVER_CHANGE_V1 = (
    "The v1 canonical form is a store format: every store a 1.x wrote "
    "carries v1 records, and a 1.0 store opens in every 1.x. Never change "
    "v1. Add `_canonical_policy_v2` with a 'v2:' prefix beside it, and "
    "compare a stored v1 record against the in-force policy's v1 "
    "fingerprint (#555).")


def test_the_v1_fingerprint_is_pinned():
    """Kills: any change to the canonical form -- separators,
    `ensure_ascii`, the prefix, the key names `phi_tags` and
    `remove_private_tags`, the `__form__` spelling."""
    assert _canonical_policy_v1(PINNED_TAGS, True) == PINNED_CANONICAL, \
        NEVER_CHANGE_V1
    assert ("v1:" + hashlib.sha256(PINNED_CANONICAL).hexdigest()
            == PINNED_FINGERPRINT), NEVER_CHANGE_V1
    assert _fp(PINNED_TAGS) == PINNED_FINGERPRINT, NEVER_CHANGE_V1


def test_the_string_form_spelling_is_pinned():
    """The `__form__` half of the canonical form, which B5's mapping-only
    policy does not reach."""
    assert _canonical_policy_v1({NAME: "PatientName", INST: ""}, False) == (
        b'{"phi_tags":{"0008,0080":{"__form__":"empty-string"},'
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

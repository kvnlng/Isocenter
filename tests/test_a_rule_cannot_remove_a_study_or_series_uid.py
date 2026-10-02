"""A REMOVE or EMPTY rule on Study or Series Instance UID, or a REPLACE
with a value, is refused at every door a policy comes in by (#877).

A Study owns `0020,000d` and a Series owns `0020,000e`, and the exporter
stamps each file's copy from its owner (#624, #544). Neither action ever
changed the owner, so the stamp wrote the **source** UID. Measured on
61c59ce3 over `CT_small` and `MR_small` under basic@2026c plus the one
rule: REMOVE exported the source UID with no row, graded PASS and wrote
`(0012,0062) YES`; EMPTY exported it too, graded REVIEW_REQUIRED, and
wrote no marker. A REPLACE with a `value:` exported the source UID as
well (REVIEW_REQUIRED, no marker), and a literal on every study would
merge them. The owner's rulings: refuse each when it is loaded, and name
REPLACE with no value, the keyed replacement, as the action to use; only
that and KEEP load.

The doors are the ones `test_a_rule_that_cannot_be_honoured_is_refused`
names: `load_config`, `audit(config_path=)`, `set_phi_tag`, `audit()`
over `configuration.phi_tags` assigned directly, and
`PhiInspector(config_tags=)`. All five go through
`config_manager._refused_phi_rule`. `create_config()` and `save()` are
writers, not doors: a rule assigned directly is written and then
refused when the file is loaded, as Patient ID's REMOVE is (#537).
"""
import sqlite3

import pytest
import yaml

from isocenter.config_manager import validate_phi_policy
from isocenter.privacy import PhiInspector
from isocenter.profiles import (BASIC_PROFILE, FLOOR_POLICY, PRIVACY_PROFILES,
                                RESEARCH_DEFAULTS)
from isocenter.session import DicomSession


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


UIDS = {"0020,000d": "Study Instance UID", "0020,000e": "Series Instance UID"}
DOORS = ("load_config", "load_config_floor", "load_config_basic",
         "audit_config_path", "set_phi_tag", "set_phi_tag_auto_save",
         "audit_assigned", "inspector")


def _yaml(tmp_path, tags, profile="none"):
    path = tmp_path / "cfg.yaml"
    data = {"phi_tags": tags}
    if profile is not None:
        data["privacy_profile"] = profile
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def _through(door, tmp_path, tag, rule):
    if door == "inspector":
        PhiInspector(config_tags={tag: rule})
        return
    with DicomSession(str(tmp_path / "s.db")) as session:
        if door == "load_config":
            session.load_config(_yaml(tmp_path, {tag: rule}))
        elif door == "load_config_floor":
            session.load_config(_yaml(tmp_path, {tag: rule}, profile=None))
        elif door == "load_config_basic":
            session.load_config(_yaml(tmp_path, {tag: rule}, profile="basic"))
        elif door == "audit_config_path":
            session.audit(config_path=_yaml(tmp_path, {tag: rule}))
        elif door == "audit_assigned":
            session.configuration.phi_tags = {tag: rule}
            session.audit()
        else:
            if not isinstance(rule, dict):
                pytest.skip("set_phi_tag cannot spell this rule")
            if door == "set_phi_tag_auto_save":
                session.configuration.config_path = str(tmp_path / "saved.yaml")
                session.configuration.auto_save = True
            session.configuration.set_phi_tag(tag, rule["action"], rule.get("value"))


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("action", ["REMOVE", "EMPTY", "remove", "empty"])
@pytest.mark.parametrize("tag", sorted(UIDS))
def test_remove_or_empty_on_an_owned_uid_is_refused(tmp_path, door, action, tag):
    """Kills the arm deleted, the arm keyed on one tag or one action, and
    the arm judging the action before it is upper-cased; across the door
    axis, a refusal wired into only some doors."""
    with pytest.raises(ValueError) as caught:
        _through(door, tmp_path, tag, {"action": action})
    message = str(caught.value)
    assert f"phi_tags['{tag}'] is {action.upper()};" in message, message
    assert UIDS[tag] in message, message
    assert "(REPLACE with no `value:` key)" in message, message
    assert message.endswith("the export would carry the source UID"), message


@pytest.mark.parametrize("door", DOORS)
# `''` is a value here (review of #881). The instance scan takes the
# keyed branch only for `rule_value is None`, so `value: ''` proposed
# `ANONYMIZED` on an instance's copy and exported a nested Study or Series
# UID as the source, while the owner scan read it as value-less. Only the
# rule with no `value:` key loads. Kills `and value` (truthiness).
@pytest.mark.parametrize("value", ["1.2.3.4", "2.25.1", ""])
@pytest.mark.parametrize("tag", sorted(UIDS))
def test_replace_with_a_value_on_an_owned_uid_is_refused(tmp_path, door, value, tag):
    """Owner ruling (a) on #877. Measured before it: the export carried
    the source UID and graded REVIEW_REQUIRED with no row naming why. A
    value the UI can hold is chosen, so #560's VR check cannot be what
    refuses it; kills the arm keyed on REMOVE and EMPTY alone."""
    if value == "" and door.startswith("set_phi_tag"):
        # `set_phi_tag(tag, "REPLACE", "")` writes no `value:` key
        # (`if value:`), so the rule it stores is the value-less one, and
        # loads: the empty string never reaches the policy by this door.
        _through(door, tmp_path, tag, {"action": "REPLACE", "value": value})
        return
    with pytest.raises(ValueError) as caught:
        _through(door, tmp_path, tag, {"action": "REPLACE", "value": value})
    message = str(caught.value)
    assert f"phi_tags['{tag}'] is REPLACE with value {value!r};" in message, message
    assert UIDS[tag] in message, message
    assert "(REPLACE with no `value:` key)" in message, message
    assert message.endswith("the export would carry the source UID"), message


@pytest.mark.parametrize("tag", ["0020,000D", "0020,000E"])
def test_an_upper_case_key_is_refused_too(tmp_path, tag):
    """The loader and `validate_phi_policy` lowercase the key before the
    arm reads it; an arm comparing the key as spelled would miss this."""
    with pytest.raises(ValueError, match=r"would carry the source UID$"):
        PhiInspector(config_tags={tag: {"action": "REMOVE"}})
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match=r"would carry the source UID$"):
            session.load_config(_yaml(tmp_path, {tag: {"action": "EMPTY"}}))


def test_the_message():
    """The exact text, which the CHANGELOG quotes."""
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0020,000d": {"action": "REMOVE"}}, "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0020,000d'] is REMOVE; Study Instance UID can "
        "only be kept (KEEP) or replaced by this project's keyed replacement "
        "UID (REPLACE with no `value:` key), because the study writes its UID "
        "on every exported file, so under REMOVE the export would carry the "
        "source UID")
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0020,000e": {"action": "EMPTY"}}, "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0020,000e'] is EMPTY; Series Instance UID can "
        "only be kept (KEEP) or replaced by this project's keyed replacement "
        "UID (REPLACE with no `value:` key), because the series writes its "
        "UID on every exported file, so under EMPTY the export would carry "
        "the source UID")
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0020,000d": {"action": "REPLACE", "value": "1.2.3"}},
                            "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0020,000d'] is REPLACE with value '1.2.3'; Study "
        "Instance UID can only be kept (KEEP) or replaced by this project's "
        "keyed replacement UID (REPLACE with no `value:` key), because "
        "the study writes its UID on every exported file, so under REPLACE "
        "with a value the export would carry the source UID")
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0020,000e": {"action": "REPLACE", "value": ""}},
                            "cfg.yaml")
    assert "is REPLACE with value ''; Series Instance UID" in str(caught.value)


ALLOWED = {
    "replace": {"action": "REPLACE"},
    # A null value is an absent one (#713): the keyed replacement.
    "replace-null-value": {"action": "REPLACE", "value": None},
    "keep": {"action": "KEEP"},
    "string-form": "Instance UID",
}


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("case", sorted(ALLOWED))
@pytest.mark.parametrize("tag", sorted(UIDS))
def test_value_less_replace_and_keep_still_load(tmp_path, door, case, tag):
    """Kills over-refusal: the arm refusing every action on the two tags,
    or reading a null `value:` as a value."""
    _through(door, tmp_path, tag, ALLOWED[case])


@pytest.mark.parametrize("tag", ["0008,0018", "0020,0052", "0008,1155"])
@pytest.mark.parametrize("rule", [{"action": "REMOVE"}, {"action": "EMPTY"},
                                  {"action": "REPLACE", "value": "1.2.3.4"}])
def test_other_uids_are_not_swept_in(tag, rule):
    """Kills the arm keyed on the UI VR rather than on the two owned tags:
    SOP Instance UID, Frame of Reference UID and Referenced SOP Instance
    UID take REMOVE, EMPTY and a valued REPLACE as they did."""
    PhiInspector(config_tags={tag: rule})


@pytest.mark.parametrize("tag", sorted(UIDS))
@pytest.mark.parametrize("action", ["SHIFT", "JITTER"])
def test_shift_on_an_owned_uid_was_and_is_refused_by_its_own_arm(tag, action):
    """SHIFT/JITTER on a UI is #559's refusal, not this one."""
    with pytest.raises(ValueError, match=r"apply only to DA and DT$"):
        validate_phi_policy({tag: {"action": action}}, "cfg.yaml")


def test_a_refused_audit_mints_no_secret(tmp_path):
    """Refused before the project secret is generated, as every other
    refused rule is."""
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.configuration.phi_tags = {"0020,000e": {"action": "REMOVE"}}
        with pytest.raises(ValueError, match=r"would carry the source UID$"):
            session.audit()
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


@pytest.mark.parametrize("tag", sorted(UIDS))
def test_set_phi_tag_leaves_the_policy_and_file_unchanged(tmp_path, tag):
    """Under auto-save: refused before memory or the file changes."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        config = session.configuration
        config.config_path = str(tmp_path / "saved.yaml")
        config.auto_save = True
        config.set_phi_tag("0008,0080", "KEEP")
        before = {t: dict(r) if isinstance(r, dict) else r
                  for t, r in config.phi_tags.items()}
        saved = (tmp_path / "saved.yaml").read_bytes()
        with pytest.raises(ValueError, match=r"would carry the source UID$"):
            config.set_phi_tag(tag, "REMOVE")
        assert config.phi_tags == before
        assert (tmp_path / "saved.yaml").read_bytes() == saved


@pytest.mark.parametrize("tag", sorted(UIDS))
def test_an_external_profiles_row_is_refused_unless_the_file_overrides_it(tmp_path, tag):
    """The merged policy is what is judged, as for Patient ID (#537)."""
    profile = tmp_path / "p.yaml"
    profile.write_text(yaml.safe_dump(
        {"phi_tags": {tag: {"action": "REMOVE"}}}), encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match=r"would carry the source UID$"):
            session.load_config(_yaml(tmp_path, {}, profile=str(profile)))
        session.load_config(_yaml(
            tmp_path, {tag: {"action": "REPLACE"}}, profile=str(profile)))
        assert session.configuration.phi_tags[tag] == {"action": "REPLACE"}


def test_no_shipped_table_carries_the_refused_rule():
    """The profiles replace both UIDs, so no shipped table is refused by
    the new arm (a profile row it refused would refuse every bare
    session)."""
    tables = {"FLOOR_POLICY": FLOOR_POLICY, "BASIC_PROFILE": BASIC_PROFILE,
              "RESEARCH_DEFAULTS": RESEARCH_DEFAULTS, **PRIVACY_PROFILES}
    for name, table in tables.items():
        for tag in UIDS:
            if tag in table:
                assert table[tag]["action"] == "REPLACE", (name, tag)
    for tag in UIDS:
        assert BASIC_PROFILE[tag]["action"] == "REPLACE"
        assert FLOOR_POLICY[tag]["action"] == "REPLACE"


@pytest.mark.parametrize("key", ["0020,xxxx", "0020,00xx", "00xx,000d"])
def test_no_mask_key_reaches_the_owned_uids(tmp_path, key):
    """A repeating-group key is 50xx or 60xx only (#556), so no mask can
    name 0020,000d or 0020,000e: these are refused as keys that are not
    tags, before any action is read."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match="is not a 'gggg,eeee' tag"):
            session.load_config(_yaml(tmp_path, {key: {"action": "REMOVE"}}))

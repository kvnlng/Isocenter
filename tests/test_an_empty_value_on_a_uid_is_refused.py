"""`REPLACE` with `value: ''` on a UI tag is refused at every door a policy
comes in by (#883).

Everywhere else `''` already reads as no value: the loader's #560 VR check
(`value or dummy`), the UI exemption (`not value`), `_is_uid_replacement`,
`_holds_owned_replacement` and `set_phi_tag` (`if value:`). The instance
scan's UI branch alone read it as a value (`rule_value is None`), proposed
`ANONYMIZED`, which no UI holds, and the proposal was declined (#560).
Measured on `main` at 7579d4df over CT_small with a `ReferencedImageSequence`
naming its own SOP UID, under `basic@2026c` plus `{REPLACE, value: ''}` on
`0008,1155` or `0020,0052`: the source UID was exported, the run graded
REVIEW_REQUIRED, and nothing said why. An honest grade, and a file that
did not do what the line seemed to ask.

Owner rulings Q2 A and Q3 A on #883: refuse it when it is loaded, naming
the value-less REPLACE (the keyed replacement, #544); and keep
`set_phi_tag(tag, "REPLACE", value="")`, which stores no `value:` key and so
is that rule. `null` and no key still load, and `''` on a tag that is not
UI still loads and writes the VR's dummy, as before.

The doors are #877's: `load_config` under `none`, the floor and `basic`,
`ConfigLoader.load_unified_config`, `audit(config_path=)`, `audit()` over
`configuration.phi_tags` assigned in code, `PhiInspector(config_tags=)`,
and an external profile carrying the row. All go through
`config_manager._refused_phi_rule`.
"""
import sqlite3

import pydicom
import pytest
import yaml
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.config_manager import (ConfigLoader, _standard_dictionary_vr,
                                      validate_phi_policy)
from isocenter.privacy import PhiInspector, _replacement_uid_for
from isocenter.profiles import FLOOR_POLICY, PRIVACY_PROFILES
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


# SOP Instance UID, Frame of Reference UID, Referenced SOP Instance UID,
# Transaction UID, and Referenced SOP Class UID (a row Table E.1-1 does not
# mark `U`).
UI_TAGS = ("0008,0018", "0020,0052", "0008,1155", "0008,0014", "0008,1150")
DOORS = ("load_config", "load_config_floor", "load_config_basic",
         "load_unified_config", "audit_config_path", "audit_assigned",
         "inspector")
EMPTY = {"action": "REPLACE", "value": ""}


def _yaml(tmp_path, tags, profile="none"):
    path = tmp_path / "cfg.yaml"
    data = {"phi_tags": tags}
    if profile is not None:
        data["privacy_profile"] = profile
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def _through(door, tmp_path, tag, rule):
    """Hand `{tag: rule}` in by `door`."""
    if door == "inspector":
        PhiInspector(config_tags={tag: rule})
        return
    if door == "load_unified_config":
        ConfigLoader.load_unified_config(_yaml(tmp_path, {tag: rule}))
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
        else:
            assert door == "audit_assigned", door
            session.configuration.phi_tags = {tag: rule}
            session.audit()


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("tag", UI_TAGS)
def test_an_empty_value_on_a_uid_is_refused(tmp_path, door, tag):
    """Main: loads at every door. Kills the arm deleted, the arm keyed on
    the owned UIDs alone, and the arm moved after the UI exemption (where
    `not value` lets `''` through first); across the door axis, a refusal
    wired into only some doors."""
    with pytest.raises(ValueError) as caught:
        _through(door, tmp_path, tag, dict(EMPTY))
    message = str(caught.value)
    assert f"phi_tags['{tag}'] is REPLACE with value '';" in message, message
    assert "Omit the value: key" in message, message
    assert "(#883)" in message, message


@pytest.mark.parametrize("door", ("load_config", "load_config_basic", "audit_config_path"))
def test_a_refused_load_leaves_the_configuration_unchanged(tmp_path, door):
    """The sentinel pattern of `test_load_config_raises.py`: a rule set
    before the refused load is still there, and nothing else moved."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.configuration.set_phi_tag("0008,0080", "KEEP")
        before = {t: dict(r) if isinstance(r, dict) else r
                  for t, r in session.configuration.phi_tags.items()}
        profile = session.configuration.privacy_profile
        with pytest.raises(ValueError, match=r"\(#883\)"):
            if door == "audit_config_path":
                session.audit(config_path=_yaml(tmp_path, {"0020,0052": dict(EMPTY)}))
            else:
                session.load_config(_yaml(
                    tmp_path, {"0020,0052": dict(EMPTY)},
                    profile="basic" if door == "load_config_basic" else "none"))
        assert session.configuration.phi_tags == before
        assert session.configuration.privacy_profile == profile


def test_the_message():
    """The exact text, which the CHANGELOG quotes."""
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0008,1155": dict(EMPTY)}, "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0008,1155'] is REPLACE with value ''; on a UI tag "
        "an empty value: is still a value, and no UID is empty, so the scan "
        "would propose nothing a UI can hold and the export would carry the "
        "source UID. Omit the value: key (or write value: null) for this "
        "project's keyed replacement UID (#544), or use EMPTY or REMOVE (#883)")


def test_the_owned_uids_keep_their_own_refusal():
    """`0020,000d` and `0020,000e` are refused by #877's arm, which comes
    first; its text does not move."""
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0020,000e": dict(EMPTY)}, "cfg.yaml")
    message = str(caught.value)
    assert "is REPLACE with value ''; Series Instance UID" in message
    assert "(#877)" in message and "(#883)" not in message


ALLOWED = {
    "replace": {"action": "REPLACE"},
    "replace-null-value": {"action": "REPLACE", "value": None},
    "keep": {"action": "KEEP"},
}


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("case", sorted(ALLOWED))
@pytest.mark.parametrize("tag", UI_TAGS)
def test_value_less_replace_null_and_keep_still_load(tmp_path, door, case, tag):
    """Kills `value == ''` widened to `not value`, which refuses the
    keyed rule itself."""
    _through(door, tmp_path, tag, dict(ALLOWED[case]))


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("tag", ["0010,0010", "0009,0010"],
                         ids=["patients-name", "private"])
def test_an_empty_value_on_a_tag_that_is_not_ui_still_loads(tmp_path, door, tag):
    """`''` on any other VR writes the VR's dummy, as no value does, and on
    a private tag (no dictionary VR) `ANONYMIZED`, as before. Kills the UI
    check removed, which refuses `''` everywhere (option C, not ruled)."""
    assert _standard_dictionary_vr(tag) != "UI"
    _through(door, tmp_path, tag, dict(EMPTY))


def _source_with_a_reference(directory):
    """CT_small with a `ReferencedImageSequence` item naming its own SOP
    Instance UID, as in the #881 review."""
    directory.mkdir(parents=True, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    item = Dataset()
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.ReferencedSOPInstanceUID = ds.SOPInstanceUID
    ds.ReferencedImageSequence = Sequence([item])
    ds.save_as(str(directory / "ct.dcm"))
    return str(ds.SOPInstanceUID)


def test_set_phi_tag_with_an_empty_value_is_the_keyed_rule(tmp_path):
    """Owner ruling Q3 A: `set_phi_tag(tag, "REPLACE", value="")` stores no
    `value:` key, so it is the value-less rule and loads, and the nested
    copy is exported under this project's keyed replacement. Its docstring
    says an empty value is no value, on every tag."""
    sop = _source_with_a_reference(tmp_path / "src")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(tmp_path / "src"))
        session.load_config(_yaml(tmp_path, {}, profile="basic"))
        session.configuration.set_phi_tag("0008,1155", "REPLACE", "")
        assert "value" not in session.configuration.phi_tags["0008,1155"]
        session.audit()
        session.anonymize()
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
    (path,) = list((tmp_path / "out").rglob("*.dcm"))
    nested = pydicom.dcmread(str(path)).ReferencedImageSequence[0]
    assert nested.ReferencedSOPInstanceUID == _replacement_uid_for(sop, FIXED_A)


def test_no_shipped_table_carries_an_empty_uid_value():
    """A shipped row the arm refused would refuse every bare session."""
    tables = {"FLOOR_POLICY": FLOOR_POLICY, **PRIVACY_PROFILES}
    for name, table in tables.items():
        for tag, rule in table.items():
            if isinstance(rule, dict) and rule.get("value") == "":
                assert _standard_dictionary_vr(tag) != "UI", (name, tag)


def test_a_refused_audit_mints_no_secret(tmp_path):
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        with pytest.raises(ValueError, match=r"\(#883\)"):
            session.audit(config_path=_yaml(tmp_path, {"0020,0052": dict(EMPTY)}))
        session.configuration.phi_tags = {"0008,0018": dict(EMPTY)}
        with pytest.raises(ValueError, match=r"\(#883\)"):
            session.audit()
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_an_external_profiles_row_is_refused_unless_the_file_overrides_it(tmp_path):
    """The merged policy is what is judged, as for #877."""
    profile = tmp_path / "p.yaml"
    profile.write_text(yaml.safe_dump(
        {"phi_tags": {"0020,0052": dict(EMPTY)}}), encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match=r"\(#883\)"):
            session.load_config(_yaml(tmp_path, {}, profile=str(profile)))
        session.load_config(_yaml(
            tmp_path, {"0020,0052": {"action": "REPLACE"}}, profile=str(profile)))
        assert session.configuration.phi_tags["0020,0052"] == {"action": "REPLACE"}

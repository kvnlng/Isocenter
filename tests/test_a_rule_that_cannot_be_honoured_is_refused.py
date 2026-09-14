"""A `phi_tags` rule the pipeline cannot honour is refused, at every door
a policy comes in by (#537, #538, #559, #560).

Each of these loaded before 0.9.8 and then did something other than it
said. Measured on ac33641:

- `0010,0020` under `REMOVE`, `EMPTY`, `JITTER` or `REPLACE` with a value:
  the patient's ID was written as the keyed pseudonym whatever the rule
  said (#537). Honouring them is not safe either: the ID is what keeps
  two patients apart, and `anonymize()` merges patients that share one.
- `REPLACE` on a DA, TM, DT, UI or AS tag exported the literal
  `ANONYMIZED`, which the VR cannot hold; on DS the element was dropped
  with a `DATA_LOSS` row; on OB the export failed with `TypeError: a
  bytes-like object is required` (#560). The string form is a REPLACE
  rule, so `"0008,0013": "Instance Creation Time"` is the same case.
- `JITTER` on a TM declined on every pass and graded the run
  REVIEW_REQUIRED (#559).
- A `value:` under `KEEP`, a non-string `value:`, and the `replacement:`
  key `set_phi_tag(replacement=)` wrote in 0.9.7: none was ever applied
  (#538).

The five doors are `load_config`, `audit(config_path=)`, `set_phi_tag`,
`audit()` over `session.configuration.phi_tags` assigned directly, and
`PhiInspector(config_tags=)`. The parametrisation is the product of the
two axes, so a check wired into only some doors is red at the others.
"""
import sqlite3

import pytest
import yaml

from isocenter.config_manager import validate_phi_policy
from isocenter.privacy import PhiInspector
from isocenter.profiles import BASIC_PROFILE, FLOOR_POLICY, RESEARCH_DEFAULTS
from isocenter.session import DicomSession


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


#: id -> (tag, rule, fragment the message must carry, set_phi_tag args or
#: None when `set_phi_tag` cannot spell the rule).
REFUSED = {
    "id-remove": ("0010,0020", {"action": "REMOVE"},
                  "Patient ID can only be kept", ("REMOVE", None)),
    "id-empty": ("0010,0020", {"action": "EMPTY"},
                 "Patient ID can only be kept", ("EMPTY", None)),
    "id-jitter": ("0010,0020", {"action": "JITTER"},
                  "Patient ID can only be kept", ("JITTER", None)),
    "id-replace-value": ("0010,0020", {"action": "REPLACE", "value": "X"},
                         "Patient ID can only be kept", ("REPLACE", "X")),
    "replace-on-da": ("0008,0012", {"action": "REPLACE"},
                      "0008,0012 is DA, which cannot hold it", ("REPLACE", None)),
    "string-form-on-tm": ("0008,0013", "Instance Creation Time",
                          "0008,0013 is TM, which cannot hold it", None),
    "replace-on-ds": ("0010,1030", {"action": "REPLACE"},
                      "0010,1030 is DS, which cannot hold it", ("REPLACE", None)),
    "replace-on-ob": ("0042,0011", {"action": "REPLACE"},
                      "0042,0011 is OB, which cannot hold it", ("REPLACE", None)),
    "replace-on-at": ("0028,0009", {"action": "REPLACE", "value": "00100010"},
                      "0028,0009 is AT, which cannot hold it", ("REPLACE", "00100010")),
    "jitter-on-tm":("0008,0030", {"action": "JITTER"},
                     "apply only to DA and DT", ("JITTER", None)),
    "value-under-keep": ("0008,0080", {"action": "KEEP", "value": "X"},
                         "has a value under KEEP", ("KEEP", "X")),
    "replacement-key": ("0008,0080", {"action": "REPLACE", "replacement": "X"},
                        "the key is 'value'", None),
    "value-not-a-string": ("0008,0080", {"action": "REPLACE", "value": 7},
                           "value must be a string, got int", None),
    # validate_value passes both of these, and both reached the file
    # (review of #574, F-4): a DA range as the Study Date, and a
    # two-valued Patient's Name.
    "date-range": ("0008,0020", {"action": "REPLACE", "value": "19000101-19010101"},
                   "a '-' in a DA is a range", ("REPLACE", "19000101-19010101")),
    "name-multi-valued": ("0010,0010", {"action": "REPLACE", "value": "A\\B"},
                          "0010,0010 holds one value", ("REPLACE", "A\\B")),
}

DOORS = ("load_config", "audit_config_path", "set_phi_tag",
         "audit_assigned", "inspector")


def _yaml(tmp_path, tags, profile="none"):
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump({"privacy_profile": profile, "phi_tags": tags}),
                    encoding="utf-8")
    return str(path)


def _through(door, tmp_path, tag, rule, setter):
    if door == "inspector":
        PhiInspector(config_tags={tag: rule})
        return
    with DicomSession(str(tmp_path / "s.db")) as session:
        if door == "load_config":
            session.load_config(_yaml(tmp_path, {tag: rule}))
        elif door == "audit_config_path":
            session.audit(config_path=_yaml(tmp_path, {tag: rule}))
        elif door == "audit_assigned":
            session.configuration.phi_tags = {tag: rule}
            session.audit()
        else:
            if setter is None:
                pytest.skip("set_phi_tag cannot spell this rule")
            session.configuration.set_phi_tag(tag, *setter)


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_rule_that_cannot_be_honoured_is_refused(tmp_path, door, case):
    """Kills, one per case, each of the six checks deleted; and, across
    the door axis, the validator wired into only some of the doors."""
    tag, rule, fragment, setter = REFUSED[case]
    with pytest.raises(ValueError) as caught:
        _through(door, tmp_path, tag, rule, setter)
    message = str(caught.value)
    assert f"phi_tags['{tag}']" in message, message
    assert fragment in message, message


def test_the_messages_are_the_ones_the_changelog_quotes():
    """The exact text, for the three a user is most likely to meet."""
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0010,0020": {"action": "REMOVE"}}, "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0010,0020'] is REMOVE; Patient ID can only be "
        "kept (KEEP) or replaced by its keyed pseudonym (REPLACE with no "
        "value), because the ID is what keeps two patients apart and "
        "anonymize() merges patients that share one (#537)")
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0008,0012": {"action": "REPLACE"}}, "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0008,0012'] is REPLACE, which writes "
        "'ANONYMIZED', and 0008,0012 is DA, which cannot hold it; use EMPTY "
        "or REMOVE, or JITTER to shift it, or give a value: that is a valid "
        "DA (#560)")
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0042,0011": {"action": "REPLACE"}}, "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0042,0011'] is REPLACE, which writes "
        "'ANONYMIZED', and 0042,0011 is OB, which cannot hold it; use EMPTY "
        "or REMOVE (#560)")
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0010,0020": {"action": "REPLACE", "value": "S1"}},
                            "cfg.yaml")
    assert "is REPLACE with value 'S1'; Patient ID" in str(caught.value)


@pytest.mark.parametrize("tag", ["0028,0106", "5400,1010"],
                         ids=["US-or-SS", "OB-or-OW"])
def test_a_compound_dictionary_vr_is_refused_when_no_arm_holds_the_value(tag):
    """`dictionary_VR` spells some tags `US or SS` / `OB or OW`, and
    pydicom's `validate_value` has no validator under those names, so it
    passes any value. Kills the compound VR handed to `validate_value`
    whole: the rule loads and the export fails on the value."""
    with pytest.raises(ValueError, match=tag) as caught:
        validate_phi_policy({tag: {"action": "REPLACE"}}, "cfg.yaml")
    # Every arm is numeric or binary, so no text value fits and the advice
    # stops at EMPTY or REMOVE, as it does for US and OB alone (review of
    # #574, F-3: it offered "give a value: that is a valid US or SS").
    assert str(caught.value).endswith("which cannot hold it; use EMPTY or REMOVE (#560)"), \
        str(caught.value)


def test_the_range_and_multi_value_messages():
    """The exact text of the two refusals F-4 added."""
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0008,0020": {"action": "REPLACE", "value": "19000101-19010101"}},
                            "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0008,0020'] is REPLACE, which writes "
        "'19000101-19010101', and a '-' in a DA is a range, which 0008,0020 "
        "cannot hold; give one DA value (#560)")
    with pytest.raises(ValueError) as caught:
        validate_phi_policy({"0010,0010": {"action": "REPLACE", "value": "A\\B"}}, "cfg.yaml")
    assert str(caught.value) == (
        "cfg.yaml: phi_tags['0010,0010'] is REPLACE, which writes 'A\\\\B', "
        "and 0010,0010 holds one value, which a '\\' would make two; give a "
        "value without one (#560)")


@pytest.mark.parametrize("tag,value", [
    ("0008,002a", "20230515104822-0500"),     # a DT's UTC offset is not a range
    ("0008,0012", "19000101"),
    ("0008,1030", "A-B"),                     # LO: a hyphen is text
    ("0020,0020", "A\\P"),                    # CS, VM 2
    ("0029,1013", "A\\B-C"),                  # private: no dictionary entry
], ids=["dt-offset", "da", "lo-hyphen", "cs-vm2", "private"])
def test_what_the_range_and_multi_value_checks_let_through(tag, value):
    """Kills over-refusal: every '-' refused, a '\\' refused on a tag whose
    VM allows several values, and a private tag judged."""
    validate_phi_policy({tag: {"action": "REPLACE", "value": value}}, "cfg.yaml")


@pytest.mark.parametrize("key", ["10000,0010", "ffff,ffff0"])
def test_an_oversized_tag_key_is_a_value_error(key):
    """The loader refuses such a key by shape; the in-code doors reached
    pydicom's `Tag` with it and raised `OverflowError` (review of #574,
    F-5). The same refusal, as `ValueError`, at the inspector door."""
    with pytest.raises(ValueError) as caught:
        PhiInspector(config_tags={key: {"action": "REPLACE", "value": "x"}})
    assert f"phi_tags key {key!r} is not a 'gggg,eeee' tag" in str(caught.value)


def test_a_refused_audit_mints_no_secret(tmp_path):
    """#456: a call that raises changes nothing, and on a fresh store the
    first `audit()` generates and commits a project secret. Kills the
    validation placed after `_project_secret_for_use`."""
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.configuration.phi_tags = {"0010,0020": {"action": "EMPTY"}}
        with pytest.raises(ValueError, match="0010,0020"):
            session.audit()
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_a_refused_audit_raises_in_the_parent_under_processes(tmp_path, monkeypatch):
    """A refused policy raises before any worker is dispatched, not inside
    `scan_worker`, where it would come back as a failure row."""
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.configuration.phi_tags = {"0008,0012": {"action": "REPLACE"}}
        with pytest.raises(ValueError, match="0008,0012"):
            session.audit()


def test_set_phi_tag_leaves_the_policy_and_file_unchanged_when_refused(tmp_path):
    """Kills validate-after-store and validate-after-save."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        config = session.configuration
        config.config_path = str(tmp_path / "saved.yaml")
        config.set_phi_tag("0008,0080", "KEEP")
        before = {tag: dict(rule) if isinstance(rule, dict) else rule
                  for tag, rule in config.phi_tags.items()}
        saved = (tmp_path / "saved.yaml").read_bytes()
        with pytest.raises(ValueError, match="0010,0020"):
            config.set_phi_tag("0010,0020", "REMOVE")
        with pytest.raises(ValueError, match="OBLITERATE"):
            config.set_phi_tag("0008,0080", "OBLITERATE")
        assert config.phi_tags == before
        assert (tmp_path / "saved.yaml").read_bytes() == saved


def test_every_shipped_profile_passes():
    """Guard: a profile refresh cannot ship a row the validator refuses,
    and `PhiInspector()` with no arguments loads the floor."""
    for name, profile in (("BASIC_PROFILE", BASIC_PROFILE),
                          ("FLOOR_POLICY", FLOOR_POLICY),
                          ("RESEARCH_DEFAULTS", RESEARCH_DEFAULTS)):
        validate_phi_policy(profile, name)
    PhiInspector()


ALLOWED = {
    "id-keep": ("0010,0020", {"action": "KEEP"}),
    "id-replace": ("0010,0020", {"action": "REPLACE"}),
    "id-string-form": ("0010,0020", "Patient ID"),
    "name-replace-value": ("0010,0010", {"action": "REPLACE", "value": "Project-X"}),
    "date-string-form": ("0008,0020", "Study Date"),
    "date-replace": ("0008,0020", {"action": "REPLACE"}),
    "date-jitter": ("0008,0020", {"action": "JITTER"}),
    "date-replace-value": ("0008,0020", {"action": "REPLACE", "value": "19000101"}),
    "dt-jitter": ("0008,002a", {"action": "JITTER"}),
    "private-replace": ("0029,1013", {"action": "REPLACE"}),
    "private-jitter": ("0029,1013", {"action": "JITTER"}),
    "cs-replace": ("0010,2203", {"action": "REPLACE"}),
    "sequence-replace": ("0008,1110", {"action": "REPLACE"}),
    "sequence-jitter": ("0008,1110", {"action": "JITTER"}),
    "unknown-standard-tag": ("0018,fff0", {"action": "REPLACE"}),
    "value-null": ("0008,0080", {"action": "KEEP", "value": None}),
}


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("case", sorted(ALLOWED))
def test_what_is_still_allowed(tmp_path, door, case):
    """Kills over-refusal: private tags not exempt, SQ not exempt, Study
    Date's REPLACE-means-shift skip missing, a DT refused for JITTER."""
    tag, rule = ALLOWED[case]
    setter = None
    if isinstance(rule, dict):
        setter = (rule["action"], rule.get("value"))
    _through(door, tmp_path, tag, rule, setter)


def test_a_user_keep_over_an_external_profiles_refused_row_loads(tmp_path):
    """The merged policy is what is checked: a user's KEEP over an
    external profile's `0010,0020: REMOVE` is honourable, and the profile
    alone is not."""
    profile = tmp_path / "p.yaml"
    profile.write_text(yaml.safe_dump(
        {"phi_tags": {"0010,0020": {"action": "REMOVE"}}}), encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match="0010,0020"):
            session.load_config(_yaml(tmp_path, {}, profile=str(profile)))
        session.load_config(_yaml(
            tmp_path, {"0010,0020": {"action": "KEEP"}}, profile=str(profile)))
        assert session.configuration.phi_tags["0010,0020"] == {"action": "KEEP"}

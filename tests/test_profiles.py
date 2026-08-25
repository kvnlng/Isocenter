
import pytest
import os
import json
from isocenter.config_manager import load_unified_config, ConfigLoader
from isocenter.profiles import BASIC_PROFILE

def test_load_basic_profile(tmp_path):
    # 1. Create config using "privacy_profile": "basic"
    config_path = tmp_path / "config_basic.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({
            "version": "2.0",
            "privacy_profile": "basic",
            # No user overrides
        }, f)

    # 2. Load via top-level
    config = load_unified_config(str(config_path))

    # 3. Verify Basic Profile Rules
    tags = config["phi_tags"]
    assert tags["0010,0010"]["action"] == "REMOVE" # Patient Name
    assert tags["0008,0020"]["action"] == "REMOVE" # Study Date

def test_profile_override(tmp_path):
    # 1. Create config using "basic" but override Patient Name to KEEP
    config_path = tmp_path / "config_override.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({
            "version": "2.0",
            "privacy_profile": "basic",
            "phi_tags": {
                "0010,0010": {"action": "KEEP", "name": "Patient Name (Kept)"},
                "0008,0090": {"action": "EMPTY"} # Referring Physician overridden to EMPTY (default REMOVE)
            }
        }, f)

    config = load_unified_config(str(config_path))
    tags = config["phi_tags"]

    # Verify Override
    assert tags["0010,0010"]["action"] == "KEEP"
    assert tags["0008,0090"]["action"] == "EMPTY"

    # Verify other profile tags (not overridden) still exist
    assert tags["0010,0020"]["action"] == "REMOVE" # Patient ID

def test_legacy_loader_integration(tmp_path):
    # Verify that ConfigLoader.load_unified_config returns tuple correctly wrapped
    config_path = tmp_path / "config_legacy_adapter.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({
            "privacy_profile": "basic"
        }, f)

    phi_tags, _, _, _, _ = ConfigLoader.load_unified_config(str(config_path))

    assert isinstance(phi_tags, dict)
    assert phi_tags["0010,0010"]["action"] == "REMOVE"

def test_unknown_profile(tmp_path):
    config_path = tmp_path / "config_unknown.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({"privacy_profile": "super_secret_profile"}, f)

    config = load_unified_config(str(config_path))
    # Should just ignore and load empty/rules
    assert config.get("phi_tags", {}) == {}


def test_basic_profile_covers_datetime_twins_of_the_dates_it_removes():
    """Every date/time tag the profile removes must have its DT-valued twin
    covered too.

    (0008,002A) Acquisition DateTime carries the same information as
    (0008,0022) Acquisition Date, which the profile removes. Leaving the DT
    twin out means raw acquisition timing survives anonymize() while the
    plain date is stripped -- the profile looks complete and is not.
    """
    required = {
        "0008,002a": "Acquisition DateTime",
        "0040,0244": "Performed Procedure Step Start Date",
        "0040,0245": "Performed Procedure Step Start Time",
        "0040,0250": "Performed Procedure Step End Date",
        "0040,0251": "Performed Procedure Step End Time",
    }
    missing = [tag for tag in required if tag not in BASIC_PROFILE]
    assert not missing, (
        f"Basic profile is missing date/time tags: {missing}. These carry "
        "acquisition and procedure timing that survives anonymize().")


def test_basic_profile_datetime_twins_are_actually_set_to_remove():
    """Membership alone does not prove a tag is remediated.

    `test_basic_profile_covers_datetime_twins_of_the_dates_it_removes`
    above only checks that these five tags are keys in `BASIC_PROFILE` --
    a tag can sit there with any action, including one that does nothing,
    and that test would still pass. This is the exact "present but not
    firing" failure this module's docstring warns about: mutating
    `"0040,0251"`'s action from `REMOVE` to `KEEP` survived the full suite
    with no test catching it. Assert the action explicitly for all five
    tags Task 1 (#38) added.
    """
    required = {
        "0008,002a": "Acquisition DateTime",
        "0040,0244": "Performed Procedure Step Start Date",
        "0040,0245": "Performed Procedure Step Start Time",
        "0040,0250": "Performed Procedure Step End Date",
        "0040,0251": "Performed Procedure Step End Time",
    }
    wrong = {
        tag: BASIC_PROFILE[tag]["action"]
        for tag in required
        if BASIC_PROFILE[tag]["action"] != "REMOVE"
    }
    assert not wrong, f"expected action REMOVE for {required}; got {wrong}"


def test_basic_profile_keys_are_all_lowercase():
    """Ingested attribute keys are lowercased, so an uppercase profile key
    never matches and silently disables that tag (see #41, where
    0008,103E shipped uppercase and Series Description was never
    remediated on any documented path).
    """
    uppercase = [k for k in BASIC_PROFILE if k != k.lower()]
    assert not uppercase, f"profile keys must be lowercase: {uppercase}"


def test_documented_basic_profile_tag_count_matches_the_code():
    """`docs/waveforms.md` states the Basic profile's tag count as a number.

    A hand-written count in prose is a second source of truth for a fact the
    code already knows, and it drifted silently once: the doc said 28 while
    the profile had grown to 34 (five date/time tags from #38, one from #39).
    Nobody noticed because nothing checked. This pins the two together so the
    next person to add a tag is told to update the sentence.

    Same failure mode as the requirements.txt/setup.py drift that broke
    `pip install isocenter`, and as docs/changelog.md (#52): the copy people
    read is not the copy people edit.

    The "effective" half of this sentence is NOT `len(BASIC_PROFILE)`: one
    entry, `(0070,0006)`, lives inside a Waveform Annotation Sequence item
    rather than at the top level of the instance, and the worker clone
    `session.audit()`/`session.anonymize()` actually scan against
    (`_make_lightweight_copy`, `isocenter/session.py`) drops the `text_index`
    nested-sequence content needs to be reached through -- so that entry's
    REMOVE/EMPTY action never fires (tracked as #57, out of scope for this
    test). A tag can sit in the profile, present in `BASIC_PROFILE`, and do
    nothing; asserting effective-count == `len(BASIC_PROFILE)` would pin
    that as true. Instead this pins the *documented* effective count to a
    value one less than the tag count, so the doc has to be updated by hand
    (not silently kept "true" by construction) if a newly added tag turns
    out to be similarly inert.
    """
    import pathlib
    import re

    from isocenter.profiles import BASIC_PROFILE

    doc = pathlib.Path(__file__).resolve().parent.parent / "docs" / "waveforms.md"
    text = doc.read_text(encoding="utf-8")

    # Matches the "**34 tags, 33 effective**" phrasing.
    match = re.search(r"\*\*(\d+) tags, (\d+)\s+effective\*\*", text)
    assert match, (
        "could not find the Basic-profile tag-count sentence in "
        "docs/waveforms.md; if the wording changed, update this test to "
        "match rather than deleting it")

    claimed, claimed_effective = int(match.group(1)), int(match.group(2))
    actual = len(BASIC_PROFILE)

    assert claimed == actual, (
        f"docs/waveforms.md says the Basic profile has {claimed} tags but "
        f"isocenter/profiles.py defines {actual}. Update the sentence in "
        "docs/waveforms.md.")
    # Known-inert tags: present in BASIC_PROFILE but not reachable by the
    # scan. Empty since 0.8.0 -- `(0070,0006)` was the only entry, inert
    # because the scan never opened sequences (#57).
    # `test_the_basic_profile_reaches_unformatted_text_value` in
    # tests/test_nested_phi_audit.py proves it fires; this only keeps the
    # doc sentence honest. Add to this set, do not weaken the assertion,
    # if another tag is ever found inert.
    known_inert = set()
    assert known_inert <= set(BASIC_PROFILE), (
        "a tag documented as known-inert is no longer in BASIC_PROFILE; "
        "update `known_inert` and the doc sentence together")
    expected_effective = actual - len(known_inert)
    assert claimed_effective == expected_effective, (
        f"docs/waveforms.md claims {claimed_effective} effective tags but "
        f"the profile defines {actual} tags with {len(known_inert)} known "
        f"inert, i.e. {expected_effective} effective.")

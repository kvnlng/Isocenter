"""Tests that fail when the de-identification decisions invert (#104, #105).

Written against `scripts/mutation_probe.py` findings rather than against
a reported defect: the code below is correct today, and nothing held it
there. Each test names the mutation it exists to kill, because a test
whose purpose is "notice if this flips" is otherwise indistinguishable
from a redundant one and gets deleted in a tidy-up.
"""
import pytest

from isocenter.entities import Instance, Patient, Study
from isocenter.privacy import PhiInspector
from isocenter.remediation import RemediationService


# --- #104: per-patient date jitter ------------------------------------
#
# The existing tests assert the shift is *stable* for a given patient. A
# constant is also stable, so they pass when jitter collapses. What was
# never asserted is that different patients get different shifts.


def _shift_for(pid, jitter_config=None):
    svc = RemediationService()
    if jitter_config is not None:
        svc.jitter_config = jitter_config
    return svc._get_date_shift(pid)


def test_different_patients_get_different_date_shifts():
    """Kills `span < 1` -> `span >= 1`, which pins every shift to min_days.

    A single offset shared by a whole cohort preserves every
    cross-patient temporal relationship and is undone by learning one
    real date, so this is the property that makes the shift a
    de-identification measure rather than a formatting change.
    """
    shifts = {_shift_for(f"PATIENT{i:04d}") for i in range(60)}

    assert len(shifts) > 1, (
        f"every patient received the same shift ({shifts}) -- per-patient "
        "jitter has collapsed to a constant")


def test_date_shifts_spread_across_the_configured_range():
    """A constant would also satisfy 'more than one distinct value' if two
    patients happened to differ, so pin the spread rather than the count."""
    shifts = [_shift_for(f"PATIENT{i:04d}") for i in range(200)]

    assert min(shifts) >= -365 and max(shifts) <= -1
    # 200 patients over a 365-day window should not land in one corner.
    assert max(shifts) - min(shifts) > 180, (
        f"shifts span only {max(shifts) - min(shifts)} days: "
        f"min={min(shifts)} max={max(shifts)}")


def test_an_inverted_jitter_config_still_produces_a_spread():
    """Kills `min_days > max_days` -> `<=`, which makes the ordering guard
    swap unconditionally: span goes negative, clamps to 1, and every
    patient is shifted by exactly the same day.

    The guard's tolerance of a reversed config is only stated in a
    comment; this is what holds it.
    """
    shifts = {_shift_for(f"PATIENT{i:04d}", {"min_days": -1, "max_days": -365})
              for i in range(60)}

    assert len(shifts) > 1, (
        f"a reversed min/max config collapsed every shift to {shifts}")


# --- #105: the private-tag decision -----------------------------------


def _instance_with(attrs):
    inst = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    for tag, value in attrs.items():
        inst.attributes[tag] = value
    return inst


def _private_tag_findings(inst):
    inspector = PhiInspector(remove_private_tags=True)
    return [f for f in inspector._scan_instance(inst, "P1", None)
            if f.tag and int(f.tag.split(',')[0], 16) % 2 != 0]


def test_a_non_whitelisted_private_tag_is_reported():
    """Half of `not in WHITELIST_TAGS`. Alone this passes when the test
    inverts, which is why the pair below matters more than either."""
    findings = _private_tag_findings(_instance_with({"0009,1001": "vendor"}))

    assert [f.tag for f in findings] == ["0009,1001"]


def test_the_legacy_identity_tags_are_not_reported_as_private_phi():
    """Kills `not in WHITELIST_TAGS` -> `in WHITELIST_TAGS`.

    (0099,0010) and (0099,1001) held the encrypted identities in `gantry`
    v0.4.1 only; v0.5.0 moved them to the Encrypted Attributes Sequence
    (0400,0500). They are exempt so `remove_private_tags` cannot strip
    the identities out of a store written by that release and leave it
    unrecoverable with its own key.

    An earlier version of this docstring called them the *reversibility
    service's* tags, which has been false since v0.5.0 -- see #113.
    Reversibility's tags are in an even group and never reach this sweep.

    Inverting the check destroys those identities while *also* letting
    every real vendor private tag through untouched.
    """
    inst = _instance_with({
        "0099,0010": "ISOCENTER",
        "0099,1001": b"encrypted",
        "0009,1001": "vendor",
    })

    reported = {f.tag for f in _private_tag_findings(inst)}

    assert "0009,1001" in reported, "a vendor private tag went unreported"
    assert reported.isdisjoint({"0099,0010", "0099,1001"}), (
        f"reversibility tags flagged for removal: {reported}")


def test_a_date_finding_carries_the_patient_id_its_shift_is_keyed_on():
    """Kills `== "SHIFT_DATE"` -> `!= "SHIFT_DATE"` in the metadata.

    This is the upstream half of #104: the shift can be computed
    correctly and still collapse if the id it is derived from never
    reaches the proposal.
    """
    inspector = PhiInspector(
        config_tags={"0008,0020": {"action": "SHIFT", "name": "Study Date"}})
    inst = _instance_with({"0008,0020": "20230101"})

    findings = inspector._scan_instance(inst, "PATIENT_XYZ", None)
    dated = [f for f in findings if f.tag == "0008,0020"]

    assert dated, "the configured date tag produced no finding"
    proposal = dated[0].remediation_proposal
    assert proposal.action_type == "SHIFT_DATE"
    assert proposal.metadata.get("patient_id") == "PATIENT_XYZ"

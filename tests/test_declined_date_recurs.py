"""A date the shift declined is raised again by the next audit (#498).

`PhiInspector._scan_instance` treats a SHIFT/JITTER tag as done once the
instance or its study was `date_shifted`. That flag said the *entity*
was
shifted, not that every value on it was, so a value the shift declined in
pass 1 -- one `SHIFT_DATE` cannot parse -- raised nothing in pass 2. A
second blind `anonymize()` then recorded CLEARED over it, and the manifest
said `anonymized: true` beside a value the pipeline never de-identified.

Measured on 49e135a (study date valid, so the study ends `date_shifted`),
for each value: pass 1 declines it and the instance is IDENTIFIED; pass 2's
audit raises nothing; the instance ends CLEARED, manifest `[True]`, value
unchanged. The four values, all hidden the same way:

- `'notadate'`, unparseable (the issue's case);
- `'20230101-20230131'`, a DA range;
- `['20230101', '20230202']`, a multi-valued DA;
- `'20230515104822+0100'`, a DT with a timezone offset.

**The rule these tests hold.** The shortcut may skip a value the shift
could apply to -- it has already been shifted, and shifting it again would
move it twice. It may not skip a value the shift cannot apply to: that one
is raised again, the decline recurs, and the instance stays IDENTIFIED.
"Cannot apply" is decided by the same parser the `SHIFT_DATE` arm uses, so
the scan re-raises exactly what the arm declines. A blank value is not
raised again: the arm skips it without a decline because there is nothing
to leave behind, and re-raising it would take a clean instance to
IDENTIFIED on every re-audit. That blank exemption is spelled in both
halves, so one test here runs the arm and compares the decline row it
actually writes with the predicate's answer: a test of either half alone
stays green while the other drifts, and an arm that declined a blank the
scan no longer raises is #498's shape again.

**Why this file imports what it does.** It reaches the scan through
`isocenter.session` and the parser through `isocenter.remediation`, so it
charges both modules' probe rows; see `test_mutation_probe_targets.py`.
The predicate is imported inside its own test so that, before the fix, the
behavioural tests above still collect and go red on what they measure
rather than on a missing name.
"""
import json
import sqlite3
from datetime import date

import pytest

from isocenter.entities import Equipment, Instance, Patient, PhiStatus, Series, Study
from isocenter.session import DicomSession
from support.project_secret import FIXED_A

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
CONTENT_DATE = "0008,0023"
ACQUISITION_DATE = "0008,0022"
ACQUISITION_DATETIME = "0008,002a"
REFERRING = "0008,0090"


def _shift(*tags):
    return {tag: {"name": tag, "action": "SHIFT"} for tag in tags}


UNSHIFTABLE = [
    pytest.param(CONTENT_DATE, "notadate", id="unparseable"),
    pytest.param(CONTENT_DATE, "20230101-20230131", id="da-range"),
    pytest.param(CONTENT_DATE, ["20230101", "20230202"], id="multi-valued-da"),
    pytest.param(ACQUISITION_DATETIME, "20230515104822+0100", id="dt-with-timezone"),
]

SHIFTABLE = [
    pytest.param(CONTENT_DATE, "20230515", id="plain-da"),
    pytest.param(ACQUISITION_DATETIME, "20230515104822.123456", id="dt-with-fraction"),
]

BLANK = [
    pytest.param(CONTENT_DATE, "", id="empty"),
    pytest.param(CONTENT_DATE, "   ", id="whitespace"),
]


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _built(tmp_path, attrs, study_date=date(2023, 1, 1)):
    session = DicomSession(str(tmp_path / "m.db"))
    patient = Patient("P498", "Orig^Name")
    # `study_date` is passed in because a caller here may hand it text no
    # parser can read, and that is a real graph: `normalize_study_date`
    # keeps an unreadable DA verbatim rather than inventing a day or
    # dropping one (#60). Such a study never shifts, which is what
    # `test_the_instances_own_shift_flag_does_not_hide_its_declined_sibling`
    # needs. In the constructor and not assigned after it: `Study` routes
    # both through the same `__setattr__`, so the two-step produced an
    # identical entity -- same value, same `date_shifted`, same revision.
    study = Study("1.2.826.0.1.498", study_date)
    series = Series("1.2.826.0.1.498.1", "OT", 1)
    series.equipment = Equipment("Acme", "M", "SN-498")
    instance = Instance("1.2.826.0.1.498.1.0", SC_SOP_CLASS, 1)
    for tag, value in attrs.items():
        instance.set_attr(tag, value)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return session, instance


def _manifest(session, tmp_path):
    out = tmp_path / "manifest.json"
    session.generate_manifest(str(out), format="json")
    return [item["anonymized"] for item in json.loads(out.read_text(encoding="utf-8"))["items"]]


def _declines(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='REMEDIATION_DECLINED'")]


def _raised(session, tag):
    return [f for f in session.audit() if f.entity_type == "Instance" and f.tag == tag]


@pytest.mark.parametrize("tag,value", UNSHIFTABLE)
def test_a_declined_value_is_raised_again_once_the_study_is_shifted(tmp_path, tag, value):
    """Red before, for every value: pass 2's audit raised nothing, and a
    second `anonymize()` recorded CLEARED over the value."""
    session, instance = _built(tmp_path, {tag: value})
    with session:
        session.configuration.phi_tags = _shift(tag)
        session.audit()
        session.anonymize()
        assert session.store.patients[0].studies[0].date_shifted
        assert instance.attributes[tag] == value
        assert instance.phi_status is PhiStatus.IDENTIFIED

        assert len(_raised(session, tag)) == 1
        session.anonymize()
        assert instance.attributes[tag] == value
        assert instance.phi_status is PhiStatus.IDENTIFIED
        assert _manifest(session, tmp_path) == [False]
    declines = _declines(tmp_path / "m.db")
    assert len(declines) == 2 and all(tag in d for d in declines), declines


def test_the_reviewers_shape_ends_identified_with_the_manifest_false(tmp_path):
    """`scratchpad/rev491/attack_r1.py` case f: a REPLACE that succeeds and
    a SHIFT that declines on one instance, `audit()` + `anonymize()` twice.
    Red before: `I=cleared`, manifest `[True]`, `'notadate'` still there."""
    session, instance = _built(tmp_path, {REFERRING: "Dr^Leak", CONTENT_DATE: "notadate"})
    with session:
        session.configuration.phi_tags = {
            REFERRING: {"name": "ReferringPhysicianName", "action": "REPLACE"},
            CONTENT_DATE: {"name": "ContentDate", "action": "SHIFT"}}
        session.anonymize(session.audit().findings)
        session.anonymize(session.audit().findings)
        assert instance.attributes[REFERRING] == "ANONYMIZED"
        assert instance.attributes[CONTENT_DATE] == "notadate"
        assert instance.phi_status is PhiStatus.IDENTIFIED
        assert _manifest(session, tmp_path) == [False]


def test_a_shifted_sibling_does_not_hide_a_declined_date(tmp_path):
    """The shortcut's other half. The study date is unparseable, so the
    study is never shifted; the instance's valid AcquisitionDate shifts,
    and that shift used to set `instance.date_shifted`, which hid its
    declined ContentDate on the same instance.

    Renamed with #510: `Instance.date_shifted` is gone, and what the
    AcquisitionDate's shift now leaves behind is a record for *that
    tag's* value. The claim is the one the old name made -- a sibling
    date shifting must not hide a declined one -- and it is now the
    mechanism's own consequence rather than a special case, so this
    asserts the record rather than the flag.
    """
    session, instance = _built(
        tmp_path, {ACQUISITION_DATE: "20230515", CONTENT_DATE: "notadate"},
        study_date="notadate")
    with session:
        session.configuration.phi_tags = _shift(ACQUISITION_DATE, CONTENT_DATE)
        session.audit()
        session.anonymize()
        assert not session.store.patients[0].studies[0].date_shifted
        shifted = instance.attributes[ACQUISITION_DATE]
        assert shifted != "20230515"
        assert instance.date_shift_vouches_for(ACQUISITION_DATE, shifted)
        assert not instance.date_shift_vouches_for(CONTENT_DATE, "notadate")

        assert [f.tag for f in session.audit() if f.entity_type == "Instance"] == [CONTENT_DATE]
        session.anonymize()
        assert instance.attributes[ACQUISITION_DATE] == shifted
        assert instance.phi_status is PhiStatus.IDENTIFIED
        assert _manifest(session, tmp_path) == [False]


@pytest.mark.parametrize("tag,value", SHIFTABLE)
def test_a_value_already_shifted_is_not_raised_or_shifted_again(tmp_path, tag, value):
    """What the shortcut is for, kept: a value the shift applied to in
    pass 1 is skipped in pass 2, so it moves once and the instance clears."""
    session, instance = _built(tmp_path, {tag: value})
    with session:
        session.configuration.phi_tags = _shift(tag)
        session.audit()
        session.anonymize()
        once = instance.attributes[tag]
        assert once != value
        assert _raised(session, tag) == []
        session.anonymize()
        assert instance.attributes[tag] == once
        assert instance.phi_status is PhiStatus.CLEARED
        assert _manifest(session, tmp_path) == [True]


def test_a_blank_value_is_not_raised_again(tmp_path):
    """The arm skips a blank date without a decline -- nothing to leave
    behind -- so re-raising it would take a clean instance to IDENTIFIED on
    every re-audit, the cry-wolf shape."""
    session, instance = _built(tmp_path, {CONTENT_DATE: ""})
    with session:
        session.configuration.phi_tags = _shift(CONTENT_DATE)
        session.audit()
        session.anonymize()
        assert session.store.patients[0].studies[0].date_shifted
        assert _raised(session, CONTENT_DATE) == []
        assert instance.phi_status is PhiStatus.CLEARED


@pytest.mark.parametrize("value,declines", [
    ("notadate", True), ("20230101-20230131", True), (["20230101", "20230202"], True),
    ("20230515104822+0100", True), ("20230515", False), ("20230515104822.123456", False),
    ("2024-05-11", False), (date(2023, 5, 15), False), ("", False), ("   ", False),
])
def test_date_shift_declines_is_the_arms_own_answer(value, declines):
    """The predicate the scan asks is the SHIFT_DATE arm's own parser: True
    exactly when that parser would leave the value unshifted, which is the
    decline the scan has to re-raise. Only that decline -- the arm's other
    one, an unresolvable PatientID, is not modelled, and it needs none:
    within a pass it cannot be reached from the scan, and across passes it
    reaches the same outcome the parser's answer already asks for. The
    predicate's docstring carries the argument."""
    from isocenter.remediation import _date_shift_declines
    assert _date_shift_declines(value) is declines


@pytest.mark.parametrize("tag,value", UNSHIFTABLE + SHIFTABLE)
def test_the_arm_declines_exactly_what_the_predicate_says_it_will(tmp_path, tag, value):
    """The two halves cannot silently disagree -- run the arm, compare.

    The exemption for a blank value is spelled twice: once in the arm,
    which returns without a decline row rather than crying wolf over a
    value with nothing left behind, and once in the predicate the scan
    asks. The test above pins the predicate's answer and
    `test_a_blank_value_is_not_raised_again` pins the scan's; neither pins
    that the *arm* still agrees. Make the arm record a decline for a blank
    and every other test in this file stays green while the scan skips a
    value the arm declined -- #498's own shape, restored for blanks: the
    decline row lands in pass 1 and the next audit reports CLEARED over it.

    So this one drives the arm for each value and asserts the decline row
    it actually writes is the one the predicate promised, which is red for
    a disagreement in either direction.

    Pinned rather than derived. Having the arm call the predicate looks
    like the one-spelling fix and is worse twice over: the arm's `else`
    branch is reached *because* the parser already answered, so it would
    re-parse to learn what it holds -- and it would re-parse with a shift
    of 0 rather than the patient's own, which is not the same question for
    a date near `date.max`.
    """
    from isocenter.remediation import _date_shift_declines
    session, _ = _built(tmp_path, {tag: value})
    with session:
        session.configuration.phi_tags = _shift(tag)
        report = session.audit()
        # Non-vacuity: the arm is only under measurement if pass 1 raised
        # the value at all. Without this, a scan that stopped raising
        # blanks would make "no decline" true for free.
        raised = [f.tag for f in report.findings if f.entity_type == "Instance"]
        assert raised == [tag], raised
        session.anonymize(report.findings)
    # Read after close, so the audit-log writer thread has flushed.
    declined = [d for d in _declines(tmp_path / "m.db") if tag in d]
    assert bool(declined) is _date_shift_declines(value), declined


@pytest.mark.parametrize("tag,value", BLANK)
def test_the_arm_declines_nothing_for_a_blank_the_scan_no_longer_raises(
        tmp_path, tag, value):
    """The blank half of the pairing above, driven through the arm itself.

    The pairing was parametrized over `BLANK` too until #510/#513, when
    shifted-ness became a per-value record: with a record every pass
    looks like pass 1, so a blank raised every pass a finding the arm
    declines to act on for ever, and #491's demotion left a clean
    instance IDENTIFIED. The scan therefore stopped raising a blank
    `SHIFT`/`JITTER` value at all -- which makes the scan-driven pairing
    *vacuous* for a blank rather than false, the non-vacuity assertion
    above being exactly what says so.

    So the blank case is handed the proposal the scan used to hand it.
    The claim it makes is unchanged and is still #498's: the arm writes
    no decline row for a blank, the predicate says it declines nothing,
    and neither half may move without the other.
    """
    from isocenter.privacy import PhiFinding, PhiRemediation
    from isocenter.remediation import RemediationService, _date_shift_declines

    session, instance = _built(tmp_path, {tag: value})
    with session:
        proposal = PhiRemediation(action_type="SHIFT_DATE", target_attr=tag,
                                  original_value=value,
                                  metadata={"patient_id": "P498"})
        finding = PhiFinding(
            entity_uid=instance.sop_instance_uid, entity_type="Instance",
            field_name=tag, value=value, reason="PHI", tag=tag,
            entity=instance, remediation_proposal=proposal)
        # Given a secret, or the arm never runs: `_get_date_shift` raises
        # before the blank guard is reached. Until #553 that raise wrote no
        # row, so "no decline" held without the arm executing at all; since
        # a raise is a decline, the missing secret is what this would read.
        service = RemediationService(store_backend=session.store_backend,
                                     project_secret=FIXED_A)
        assert service.apply_remediation([finding]) == 0
        assert instance.attributes[tag] == value
    declined = [d for d in _declines(tmp_path / "m.db") if tag in d]
    assert declined == [], declined
    assert _date_shift_declines(value) is False

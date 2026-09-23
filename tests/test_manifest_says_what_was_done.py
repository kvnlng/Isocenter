"""The manifest's `anonymized` says what was done to each instance (#486).

`ManifestItem.anonymized` defaulted to `True` and `generate_manifest()`
never passed it, so every manifest said `"anonymized": true` for every
instance. Measured on e184933: pydicom's `CT_small.dcm`, ingested and never
anonymized, produced `[True]` beside its untouched PatientID `1CT1`.

**The rule these tests hold.** An item is `anonymized` when its patient,
its study and the instance itself each carry `PhiStatus.REMEDIATED` or
`PhiStatus.CLEARED` at their current revision: the last tag-policy scan
found no identifier left unremediated on any of the three, and none has
been edited since. The series is not consulted, because nothing scans a
series (`phi_status_summary()` leaves it out for the same reason).
`REMEDIATED` is not required anywhere in the chain, because a re-audit of an
anonymized graph records `CLEARED` over it -- measured, and pinned below --
and a rule that required it would call a re-checked graph un-anonymized.

What that rule does *not* mean is stated where it is documented
(`docs/api/stability.md`): it is not "`anonymize()` ran" (a clean input
scanned by `audit()` alone reads `true`), and burned-in pixel text is not
part of it.

**Why this file imports what it does.** It reaches `ManifestItem` through
`isocenter.manifest`, the session through `isocenter.session` and the
graph through `isocenter.entities`, so it is in those three probe rows;
and it is a hand extra in `remediation.py`'s row (#441), because
`test_a_declined_finding_on_the_same_instance_says_false` kills the
pass-end demotion there without importing the module. See
`test_mutation_probe_targets.py`.
"""
import json
import os
import shutil
from datetime import date

import pytest
from pydicom.data import get_testdata_file

from isocenter.entities import Equipment, Instance, Patient, PhiStatus, Series, Study
from isocenter.manifest import ManifestItem
from isocenter.session import DicomSession

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _manifest(session, tmp_path, name="manifest.json"):
    out = tmp_path / name
    session.generate_manifest(str(out), format="json")
    return {item["sop_instance_uid"]: item["anonymized"]
            for item in json.loads(out.read_text(encoding="utf-8"))["items"]}


def _ingested(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), src / "ct.dcm")
    session = DicomSession(str(tmp_path / "m.db"))
    session.ingest(str(src))
    return session


def _built(tmp_path, study_date=date(2023, 1, 1)):
    session = DicomSession(str(tmp_path / "b.db"))
    patient = Patient("P486", "Original^Name")
    study = Study("1.2.826.0.1.486", date(2023, 1, 1))
    # Assigned after construction, as a graph loaded from a damaged store
    # would carry it: the SHIFT_DATE arm declines a value it cannot parse.
    study.study_date = study_date
    series = Series("1.2.826.0.1.486.1", "OT", 1)
    series.equipment = Equipment("Acme", "Model", "SN-486")
    instance = Instance("1.2.826.0.1.486.1.0", SC_SOP_CLASS, 1)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return session, instance


def test_a_session_that_never_anonymized_says_false(tmp_path):
    """The issue's repro. Red before #486: `[True]` beside `1CT1`.

    Kills: `generate_manifest` omitting `anonymized=`, which leaves the
    dataclass default to answer.
    """
    with _ingested(tmp_path) as session:
        patient_id = session.store.patients[0].patient_id
        answers = _manifest(session, tmp_path)
    assert patient_id == "1CT1"
    assert list(answers.values()) == [False], answers


def test_an_anonymized_session_says_true(tmp_path):
    """The other half: without it, a manifest that says `false` for
    everything passes the test above.

    Kills: `anonymized=` dropped from `generate_manifest` once the default
    is `False`, and the status membership test inverted.
    """
    with _ingested(tmp_path) as session:
        session.anonymize()
        answers = _manifest(session, tmp_path)
    assert list(answers.values()) == [True], answers


def test_the_answer_survives_a_reopened_store(tmp_path):
    """Keyed on persisted state, not on this session's memory of its verbs.

    A rule read from `_actions_performed` would say `false` here: the
    reopened session never called `anonymize()`.
    """
    with _ingested(tmp_path) as session:
        session.anonymize()
        session.save(sync=True)
    with DicomSession(str(tmp_path / "m.db")) as reopened:
        answers = _manifest(reopened, tmp_path)
    assert list(answers.values()) == [True], answers


def test_a_re_audit_of_an_anonymized_graph_still_says_true(tmp_path):
    """A re-audit records CLEARED over REMEDIATED; both mean "nothing left".

    Kills: a rule that requires REMEDIATED somewhere in the chain.
    """
    with _ingested(tmp_path) as session:
        session.anonymize()
        session.audit()
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert instance.phi_status.value == "cleared"
        answers = _manifest(session, tmp_path)
    assert list(answers.values()) == [True], answers


def test_an_edit_after_anonymize_says_false(tmp_path):
    """An edit the scan has not seen is not vouched for.

    Kills: a rule read from the recorded status without its revision
    check -- the anti-pattern `PhiStatus`'s docstring names.
    """
    session, instance = _built(tmp_path)
    with session:
        session.anonymize()
        assert _manifest(session, tmp_path, "before.json") == {
            instance.sop_instance_uid: True}
        instance.set_attr("0008,103e", "Edited after the scan")
        answers = _manifest(session, tmp_path, "after.json")
    assert answers == {instance.sop_instance_uid: False}, answers


def test_a_declined_study_remediation_says_false(tmp_path):
    """A study date remediation declined leaves the study IDENTIFIED.

    The instance's own status is REMEDIATED (its SOP Instance UID is
    replaced since #544; CLEARED before) and its patient's REMEDIATED, so
    a rule that consulted only the instance, or skipped the study, would
    say `true` over a study date that reaches the export unshifted. The
    study's own UID replacement is applied beside the declined date, and
    does not lift it out of IDENTIFIED.

    Kills: the study dropped from the chain.
    """
    session, instance = _built(tmp_path, study_date="notadate")
    with session:
        session.anonymize()
        study = session.store.patients[0].studies[0]
        assert study.phi_status.value == "identified"
        assert instance.phi_status.value == "remediated"
        answers = _manifest(session, tmp_path)
    assert answers == {instance.sop_instance_uid: False}, answers


def test_a_manifest_item_nobody_described_is_not_anonymized():
    """The default is the claim an item makes when no caller said otherwise.

    Kills: the default flipped back to `True`.
    """
    item = ManifestItem(patient_id="P", study_instance_uid="1.2",
                        series_instance_uid="1.2.3", sop_instance_uid="1.2.3.4")
    assert item.anonymized is False


# --- review round: a decline beside a success on one instance ---------------

#: One tag that remediates and one that declines, on the same instance:
#: `0008,0023` under SHIFT with a value no date parser accepts.
TAGS_WITH_A_DATE = {
    "0008,0090": {"name": "ReferringPhysicianName", "action": "REPLACE"},
    "0008,0023": {"name": "ContentDate", "action": "SHIFT"},
}


@pytest.fixture
def strategy(request, monkeypatch):
    """The threads-or-processes lever for the scan `audit()` runs.

    `audit()` does not print the strategy it resolved, unlike `redact()`,
    so this arm sets the lever and cannot assert it was honoured. The
    remediation itself is serial either way; what the processes arm varies
    is the scan whose findings are rehydrated against the live graph.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    if request.param == "processes":
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    else:
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    return request.param


@pytest.mark.parametrize("strategy", ["threads", "processes"], indirect=True)
def test_a_declined_finding_on_the_same_instance_says_false(tmp_path, strategy):
    """Two findings on one instance: one remediates, one declines.

    Red before the review round: `apply_remediation` stamped REMEDIATED
    per success and `_record_decline` recorded nothing, so the instance
    left the pass REMEDIATED with `'notadate'` still in it -- the manifest
    said `true` while the same session's report graded `REVIEW_REQUIRED`
    and named the decline in section 3.3. Measured on 686bdea.

    The rule now: an entity that declined during a pass does not leave it
    REMEDIATED. Its status is re-recorded IDENTIFIED at the pass's end,
    and the manifest reads that status, not the audit trail's declines:
    one source for the answer.

    Kills: the pass-end demotion in `apply_remediation` deleted.
    `test_a_pass_that_only_declined_leaves_the_status_alone`
    (`test_declined_remediation_is_recorded.py`) pins the other half:
    an entity that only declined keeps the status it had.
    """
    session, instance = _built(tmp_path)
    instance.set_attr("0008,0090", "Dr^Leak")
    instance.set_attr("0008,0023", "notadate")
    # `privacy_profile: none`, so TAGS_WITH_A_DATE is the whole policy.
    # This was a root-level tag mapping written as tags.json, accepted
    # only through the `audit(config_path=)` fallback #456 removed. JSON
    # text is valid YAML.
    config = tmp_path / "tags.yaml"
    config.write_text(json.dumps({"privacy_profile": "none",
                                  "phi_tags": TAGS_WITH_A_DATE}), encoding="utf-8")
    with session:
        findings = session.audit(str(config)).findings
        assert session.anonymize(findings) >= 1
        assert instance.attributes["0008,0090"] != "Dr^Leak"
        assert instance.attributes["0008,0023"] == "notadate"
        session.store_backend.flush_audit_queue()
        assert len(session.store_backend.get_audit_declines()) == 1
        assert instance.phi_status is PhiStatus.IDENTIFIED
        answers = _manifest(session, tmp_path)
    assert answers == {instance.sop_instance_uid: False}, answers


def test_a_patient_edit_after_anonymize_says_false(tmp_path):
    """The patient term of the chain (the reviewer's scenario 7).

    `test_an_edit_after_anonymize_says_false` edits the instance and
    `test_a_declined_study_remediation_says_false` holds the study, so
    a chain that dropped the patient passed both.

    Kills: the patient dropped from the chain.
    """
    session, instance = _built(tmp_path)
    with session:
        session.anonymize()
        assert _manifest(session, tmp_path, "before.json") == {
            instance.sop_instance_uid: True}
        patient = session.store.patients[0]
        patient.patient_name = "Back^Again"
        patient.mark_modified()
        assert patient.phi_status is PhiStatus.UNSCANNED
        assert instance.phi_status is not PhiStatus.UNSCANNED
        answers = _manifest(session, tmp_path, "after.json")
    assert answers == {instance.sop_instance_uid: False}, answers

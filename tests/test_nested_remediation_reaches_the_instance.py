"""A remediation inside a sequence is the instance's remediation too (#494).

`anonymize()` resolves a finding raised inside a sequence to the nested
`DicomItem` (#57) and writes the replacement there. The success block
stamped that item `REMEDIATED`, and nothing told the instance that holds
it: an item has no link to its instance, and `DicomItem.set_attr` moves
only the item's own revision. Two things followed.

- **The status.** An instance whose only findings were nested read
  `IDENTIFIED` after `anonymize()`, so `phi_status_summary()` counted it
  identified and the JSON manifest said `"anonymized": false` until the
  next `audit()`.
- **The store kept the identifier.** An instance loaded from the store,
  re-audited to the status it already carried (the #173 short-circuit),
  reported no unsaved changes after `anonymize()`, so `save()` skipped it
  with its sequences. Measured on 1c41e5e: the item held `ANONYMIZED` in
  memory and `Nested^PHI` in the store after `save()` and a reopen.

The rule now is the top-level rule, one level down. `Session.anonymize()`
hands the service the instance owning each nested finding's item, and a
nested success marks that instance modified and stamps it `REMEDIATED`;
a nested decline names the instance for #491's pass-end demotion as well
as the item. An owner is admitted only when the finding's path, followed
from that instance, reaches the finding's own item -- a hand-built graph
can give two instances one UID.

What it does not change, deliberately (#553): a findings list handed to
`anonymize(findings=...)` still speaks only for the findings in it, and a
proposal that raised still demotes nothing, at either depth.
"""
import json
from datetime import date

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.entities import (DicomItem, DicomSequence, Equipment, Instance,
                                Patient, PhiStatus, Series, Study)
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession

from support.project_secret import FIXED_A, load_fixed_secret

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
SEQ = "0040,0275"          # Request Attributes Sequence
STEP = "0040,0007"         # Scheduled Procedure Step Description
STEP_DATE = "0040,0002"    # Scheduled Procedure Step Start Date
NESTED_PHI = "Nested^PHI"
TAGS = {STEP: {"name": "StepDescription", "action": "REPLACE"}}

MODES = ["threads", "processes"]


@pytest.fixture
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def config(tmp_path):
    """A tag policy whose only rule is the nested step description."""
    path = tmp_path / "tags.yaml"  # JSON is YAML; the loader wants the suffix
    path.write_text(json.dumps({"privacy_profile": "none", "phi_tags": TAGS}))
    return str(path)


def _instance_with_items(uid, *values):
    """An instance holding one Request Attributes item per `values` entry.

    Each entry is a `{tag: value}` dict.
    """
    inst = Instance(uid, SC_SOP_CLASS, 1)
    inst.file_path = None
    seq = DicomSequence(tag=SEQ)
    for attrs in values:
        item = DicomItem()
        for tag, value in attrs.items():
            item.set_attr(tag, value)
        seq.items.append(item)
    inst.sequences[SEQ] = seq
    return inst


def _graph(*instances, pid="P1"):
    p = Patient(pid, "Original^Name")
    st = Study("1.2.3", date(2023, 1, 1))
    se = Series("1.2.3.1", "OT", 1)
    se.equipment = Equipment("Acme", "Model", "SN-494")
    se.instances.extend(instances)
    st.series.append(se)
    p.studies.append(st)
    return p


def _nested_finding(inst, index, action, tag, new_value=None, original=None,
                    metadata=None, uid=None):
    item = inst.sequences[SEQ].items[index]
    return PhiFinding(
        entity_uid=uid or inst.sop_instance_uid, entity_type="Instance",
        field_name=tag, value=original, reason="test", tag=tag, entity=item,
        entity_path=((SEQ, index),),
        remediation_proposal=PhiRemediation(
            action_type=action, target_attr=tag, new_value=new_value,
            original_value=original, metadata=metadata or {}))


def _top_finding(inst, action, tag, new_value=None, original=None):
    return PhiFinding(
        entity_uid=inst.sop_instance_uid, entity_type="Instance",
        field_name=tag, value=original, reason="test", tag=tag, entity=inst,
        remediation_proposal=PhiRemediation(
            action_type=action, target_attr=tag, new_value=new_value,
            original_value=original))


def _reload_at(inst, status):
    """The state a load from the store leaves an instance in."""
    inst.record_phi_status(status)
    inst.mark_subtree_persisted()
    assert inst.phi_status is status and not inst.has_unsaved_changes


def test_a_nested_only_instance_reads_remediated_after_anonymize(tmp_path,
                                                                 config):
    """#494's own repro: the manifest said `false` over a finished pass."""
    inst = _instance_with_items("1.2.3.1.0", {STEP: NESTED_PHI})
    patient = _graph(inst)
    with DicomSession(str(tmp_path / "hand.db")) as session:
        session.store.patients.append(patient)
        report = session.audit(config)
        session.anonymize(report.findings)

        item = inst.sequences[SEQ].items[0]
        assert item.attributes[STEP] != NESTED_PHI
        assert item.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status is PhiStatus.REMEDIATED
        summary = session.phi_status_summary()
        assert PhiStatus.IDENTIFIED not in summary["instances"]

        manifest = tmp_path / "m.json"
        session.generate_manifest(str(manifest), format="json")
        items = json.loads(manifest.read_text())["items"]
        assert [i["anonymized"] for i in items] == [True]


def _write_nested_only_ct(path):
    """CT_small whose only finding under TAGS is the nested description.

    Owners already carry replacement-shaped values, the private tags and
    StudyDate are gone, so no other finding stamps or dirties anything.
    """
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    item = Dataset()
    item.ScheduledProcedureStepDescription = NESTED_PHI
    ds.RequestAttributesSequence = Sequence([item])
    ds.remove_private_tags()
    ds.PatientID = "ANON_probe"
    ds.PatientName = "ANONYMIZED"
    if "StudyDate" in ds:
        del ds.StudyDate
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path))


def _nested_value(session):
    [patient] = session.store.patients
    inst = patient.studies[0].series[0].instances[0]
    return inst, inst.sequences[SEQ].items[0].attributes.get(STEP)


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_nested_only_remediation_after_a_reload_reaches_the_store(
        tmp_path, config, mode):
    """The store kept the identifier: measured `Nested^PHI` after reopen."""
    _write_nested_only_ct(tmp_path / "in" / "a.dcm")
    db = str(tmp_path / "store.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.audit(config)
        session.save(sync=True)

    with DicomSession(db) as session:
        report = session.audit(config)
        inst, _ = _nested_value(session)
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert not inst.has_unsaved_changes, (
            "setup: the re-audit is meant to short-circuit (#173)")
        session.anonymize(report)
        inst, value = _nested_value(session)
        assert value != NESTED_PHI
        assert inst.has_unsaved_changes
        session.save(sync=True)

    with DicomSession(db) as session:
        _, value = _nested_value(session)
        assert value != NESTED_PHI, "the store kept the nested identifier"


def test_a_nested_remediation_on_an_instance_already_remediated_is_saved(
        tmp_path):
    """Pins the owner's `mark_modified()` in the success block: #173, nested.

    The owner already reads `REMEDIATED` -- loaded that way, or stamped by
    an earlier `anonymize()` call and saved since -- so the stamp this
    finding earns is the status it already carries and short-circuits.
    Without its own `mark_modified()` the instance reports nothing to
    save, and the replacement written into its sequence never reaches the
    store: exactly the shape the five `mark_modified()` pins in
    `tests/test_remediation_invariants.py` defend one level up.
    """
    inst = _instance_with_items("1.2.3.1.0", {STEP: NESTED_PHI})
    db = str(tmp_path / "remediated.db")
    with DicomSession(db) as session:
        session.store.patients.append(_graph(inst))
        session.save(sync=True)
        _reload_at(inst, PhiStatus.REMEDIATED)

        session.anonymize([_nested_finding(inst, 0, "REPLACE_TAG", STEP,
                                           new_value="ANONYMIZED",
                                           original=NESTED_PHI)])

        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.has_unsaved_changes, (
            "the instance reports no unsaved changes after the identifier "
            "inside it was replaced, so the next save skips it and the "
            "store keeps the value")
        session.save(sync=True)

    with DicomSession(db) as session:
        _, value = _nested_value(session)
        assert value == "ANONYMIZED"


def test_a_nested_success_beside_a_nested_decline_reads_identified_and_is_saved(
        tmp_path):
    """One nested replacement applied, one nested date shift declined.

    The declined value is still in the instance, so it is not REMEDIATED;
    the applied one still has to reach the store.
    """
    inst = _instance_with_items("1.2.3.1.0", {STEP: NESTED_PHI},
                                {STEP_DATE: "notadate"})
    db = str(tmp_path / "mixed.db")
    with DicomSession(db) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.store.patients.append(_graph(inst))
        session.save(sync=True)
        _reload_at(inst, PhiStatus.IDENTIFIED)

        session.anonymize([
            _nested_finding(inst, 0, "REPLACE_TAG", STEP,
                            new_value="ANONYMIZED", original=NESTED_PHI),
            _nested_finding(inst, 1, "SHIFT_DATE", STEP_DATE,
                            original="notadate",
                            metadata={"patient_id": "P1"}),
        ])

        assert inst.sequences[SEQ].items[1].attributes[STEP_DATE] == "notadate"
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert inst.has_unsaved_changes
        session.save(sync=True)

    with DicomSession(db) as session:
        _, value = _nested_value(session)
        assert value == "ANONYMIZED"


def test_a_nested_decline_demotes_the_owner(tmp_path):
    """Pins the owner named in `_record_decline`.

    A top-level success stamps the instance REMEDIATED; a decline inside
    its sequence leaves a value behind, so the pass must end IDENTIFIED.
    """
    inst = _instance_with_items("1.2.3.1.0", {STEP_DATE: "notadate"})
    inst.set_attr("0008,0080", "Some Hospital")
    with DicomSession(str(tmp_path / "demote.db")) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.store.patients.append(_graph(inst))
        session.anonymize([
            _top_finding(inst, "REPLACE_TAG", "0008,0080",
                         new_value="ANONYMIZED", original="Some Hospital"),
            _nested_finding(inst, 0, "SHIFT_DATE", STEP_DATE,
                            original="notadate",
                            metadata={"patient_id": "P1"}),
        ])
        assert inst.attributes["0008,0080"] == "ANONYMIZED"
        assert inst.phi_status is PhiStatus.IDENTIFIED


def test_an_owner_is_only_admitted_when_the_path_resolves_to_the_findings_item(
        tmp_path):
    """Two instances with one UID; the finding's item is under the second.

    Resolving the owner by UID alone would stamp -- and dirty -- the first
    instance for a change made inside the second.
    """
    first = _instance_with_items("1.2.3.1.0", {STEP: NESTED_PHI})
    second = _instance_with_items("1.2.3.1.0", {STEP: NESTED_PHI})
    with DicomSession(str(tmp_path / "twins.db")) as session:
        session.store.patients.append(_graph(first, second))
        _reload_at(first, PhiStatus.IDENTIFIED)
        _reload_at(second, PhiStatus.IDENTIFIED)

        session.anonymize([_nested_finding(second, 0, "REPLACE_TAG", STEP,
                                           new_value="ANONYMIZED",
                                           original=NESTED_PHI)])

        assert first.sequences[SEQ].items[0].attributes[STEP] == NESTED_PHI
        assert first.phi_status is PhiStatus.IDENTIFIED
        assert not first.has_unsaved_changes
        assert second.phi_status is PhiStatus.REMEDIATED


def test_remediation_without_owners_behaves_as_before():
    """A service used directly, with no session to name owners, stamps the
    item and nothing else -- it has no graph to guess an owner from."""
    inst = _instance_with_items("1.2.3.1.0", {STEP: NESTED_PHI})
    _reload_at(inst, PhiStatus.IDENTIFIED)

    applied = RemediationService().apply_remediation(
        [_nested_finding(inst, 0, "REPLACE_TAG", STEP, new_value="ANONYMIZED",
                         original=NESTED_PHI)])

    item = inst.sequences[SEQ].items[0]
    assert applied == 1
    assert item.phi_status is PhiStatus.REMEDIATED
    assert inst.phi_status is PhiStatus.IDENTIFIED
    assert not inst.has_unsaved_changes

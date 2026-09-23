"""One replacement value per owned tag, whatever order the findings arrive in (#496).

The tags the patient and study scans own -- PatientName (0010,0010) and
PatientID (0010,0020) on the `Patient`, StudyDate (0008,0020) on the
`Study` -- can also be raised by the instance scan when the tag policy
names them. Both findings ran, the dedup key could not tell them apart
because they carry different UIDs, and whichever ran last won the
instance's copy. Measured on 09fca54, one patient, bare-name REPLACE
policy: in the scan's order the instance ended `0010,0020 = "ANONYMIZED"`
while the Patient and the exported file said `ANON_<hash>`; with the
findings reversed it ended `ANON_<hash>`. EMPTY, and a StudyDate that
differed per instance, split the same way.

**The rule these tests hold** (owner ruling, 2026-09-11): one replacement
value per tag, whichever level raised the finding; the instance copy and
the owner's value agree; the outcome does not depend on finding order.
Every case runs in both orders: the scan's, and reversed, which puts every
instance finding ahead of its study's and its patient's.

The measured cases and what each now ends at:

- A REPLACE, B EMPTY, E JITTER (dates equal or differing): the instance
  finding is **folded** into the owner's write -- it does not run, the
  instance holds the owner's value, and the owner's audit row says how many
  were folded.
- C REMOVE: ran after the owner's write, so the copy was absent in both
  orders. Since #537 the rule governs the owner too, the owner's removal
  takes the copy away, and the instance's REMOVE folds into it, as A, B
  and E do.
- D KEEP: no finding on the owner or the copy; both keep the original
  (#537; before, the owner was replaced and #492 wrote that onto them).

Since #537 Patient ID can only be kept or pseudonymised, so EMPTY and
REMOVE are exercised on the name and the date.
- F the owner's own finding declines: nothing reached the copy, so the
  instance applies its own proposal. Suppressing it would leave the real
  original date in both frozen readers -- #492's leak -- and the owner's
  DECLINED row already grades the run REVIEW_REQUIRED.
- G only the instance findings are handed in: no owner write to fold
  into, so they apply, as the caller asked.

**Why this file imports what it does.** It reaches `RemediationService`
through `isocenter.remediation` and `PhiInspector` through
`isocenter.privacy`, so it charges both modules' probe rows; see
`test_mutation_probe_targets.py`.
"""
import json
import sqlite3

import pytest

from isocenter.entities import (DicomItem, DicomSequence, Instance, Patient,
                                PhiStatus, Series, Study)
from isocenter.io_handlers import format_study_date
from isocenter.privacy import PhiInspector
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession

from support.project_secret import FIXED_A, load_fixed_secret

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
NAME = "Orig^Name"
PID = "P496"
OWNED = ("0010,0010", "0010,0020", "0008,0020")
ORDERS = ("scan-order", "reversed")
REPLACE = {"0010,0010": "Patient Name", "0010,0020": "Patient ID"}
# Patient ID may not be emptied or removed since #537 (the rule is
# refused), so EMPTY and REMOVE are exercised on the name and the date.
EMPTY = {"0010,0010": {"action": "EMPTY"}, "0008,0020": {"action": "EMPTY"}}
REMOVE = {"0010,0010": {"action": "REMOVE"}, "0008,0020": {"action": "REMOVE"}}
KEEP = {"0010,0010": {"action": "KEEP"}, "0010,0020": {"action": "KEEP"}}
JITTER = {"0008,0020": {"action": "JITTER"}}
REPLACE_DATE = {"0008,0020": "Study Date"}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _built(tmp_path, *, study_date="20240101", instance_dates=("20240101", "20240101"),
           name=NAME, pid=PID):
    tmp_path.mkdir(parents=True, exist_ok=True)
    session = DicomSession(str(tmp_path / "m.db"))
    patient = Patient(pid, name)
    study = Study("1.2.826.0.1.496", "20240101")
    # Assigned after construction, as a graph from a damaged store would
    # carry it: the SHIFT_DATE arm declines a value it cannot parse.
    study.study_date = study_date
    series = Series("1.2.826.0.1.496.1", "OT", 1)
    for i, inst_date in enumerate(instance_dates):
        instance = Instance(f"1.2.826.0.1.496.1.{i}", SC_SOP_CLASS, i + 1)
        instance.set_attr("0010,0010", name)
        instance.set_attr("0010,0020", pid)
        instance.set_attr("0008,0020", inst_date)
        series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return session


def _ordered(findings, order):
    findings = list(findings)
    return findings[::-1] if order == "reversed" else findings


def _instances(session):
    return [inst for st in session.store.patients[0].studies
            for se in st.series for inst in se.instances]


def _copies(session):
    return [{t: inst.attributes.get(t, "<absent>") for t in OWNED}
            for inst in _instances(session)]


def _anonymize(session, policy, order, *, select=None):
    session.configuration.phi_tags = policy
    findings = list(session.audit())
    if select is not None:
        findings = [f for f in findings if select(f)]
    return session.anonymize(_ordered(findings, order))


def _audit_rows(db_path, action_type):
    with sqlite3.connect(str(db_path)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type=?", (action_type,))]


# ---------------------------------------------------------------------------
# A, B, E: folded into the owner's write
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ORDERS)
def test_a_replace_policy_leaves_the_patients_value_on_every_instance(tmp_path, order):
    """Red before in the scan's order: the instance's own REPLACE ran last
    and wrote "ANONYMIZED" over the Patient's `ANON_<hash>`."""
    with _built(tmp_path) as session:
        _anonymize(session, REPLACE, order)
        patient = session.store.patients[0]
        assert patient.patient_id.startswith("ANON_")
        for inst in _instances(session):
            assert inst.attributes["0010,0010"] == patient.patient_name
            assert inst.attributes["0010,0020"] == patient.patient_id
            assert inst.phi_status is PhiStatus.REMEDIATED


@pytest.mark.parametrize("order", ORDERS)
def test_a_replace_policy_counts_the_owner_once_and_says_what_it_folded(tmp_path, order):
    """Case A's accounting: 3 applied (patient_name, patient_id, study_date),
    not 7, and 2 REPLACE rows, not 6. Nothing is lost silently: each owner
    row names the two instance findings folded into it. Nothing is counted
    twice: the folded findings have no rows of their own."""
    with _built(tmp_path) as session:
        assert _anonymize(session, REPLACE, order) == 3
    rows = _audit_rows(tmp_path / "m.db", "REMEDIATION_REPLACE")
    assert len(rows) == 2, rows
    for field in ("patient_name", "patient_id"):
        (row,) = [r for r in rows if f": {field} ->" in r]
        assert row.endswith("; written to 2 instance copies; "
                            "2 instance-level findings on this tag folded into it"), row
    assert _audit_rows(tmp_path / "m.db", "REMEDIATION_DECLINED") == []


@pytest.mark.parametrize("order", ORDERS)
def test_an_empty_policy_leaves_the_patients_value_on_every_instance(tmp_path, order):
    """The exporter stamps the owner's value on every file, so the instance
    copy agrees with the owner's. Since #537 EMPTY governs the owner too:
    both end empty (before, the owner kept `ANONYMIZED` and the copies
    were written with it)."""
    with _built(tmp_path) as session:
        _anonymize(session, EMPTY, order)
        patient = session.store.patients[0]
        study = patient.studies[0]
        assert (patient.patient_name, study.study_date) == ("", "")
        for inst in _instances(session):
            assert inst.attributes["0010,0010"] == patient.patient_name
            assert inst.attributes["0010,0020"] == patient.patient_id
            assert inst.attributes["0008,0020"] == ""


@pytest.mark.parametrize("dates", [("20240101", "20240101"), ("20240101", "20240105")],
                         ids=["equal", "differing"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_jitter_policy_leaves_the_studys_date_on_every_instance(tmp_path, order, dates):
    """Red before in the scan's order with differing dates: the second
    instance shifted its own 20240105 by the patient's offset and kept it,
    beside a study that said otherwise."""
    with _built(tmp_path, instance_dates=dates) as session:
        assert _anonymize(session, JITTER, order) == 3
        study = session.store.patients[0].studies[0]
        assert study.date_shifted
        for inst in _instances(session):
            assert inst.attributes["0008,0020"] == format_study_date(study.study_date)


@pytest.mark.parametrize("policy", [REPLACE, EMPTY, REMOVE, KEEP, JITTER],
                         ids=["replace", "empty", "remove", "keep", "jitter"])
def test_the_end_state_does_not_depend_on_finding_order(tmp_path, policy):
    """The two orders side by side, on one graph shape, for every policy."""
    states = []
    for order in ORDERS:
        with _built(tmp_path / order, instance_dates=("20240101", "20240105")) as session:
            # Two stores compared value for value, so they share a project
            # secret, said here: each would otherwise mint its own
            # pseudonym and offset (0.9.7).
            load_fixed_secret(session, tmp_path / order, FIXED_A)
            _anonymize(session, policy, order)
            states.append(_copies(session))
    assert states[0] == states[1]


# ---------------------------------------------------------------------------
# C, D: REMOVE still runs; KEEP has nothing to fold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ORDERS)
def test_a_remove_policy_folds_into_the_owners_removal(tmp_path, order):
    """C. Since #537 a REMOVE rule on the name or the date governs the
    owner, and the owner's removal deletes the instance copies
    (`_write_to_instances`, value None). The instance's own REMOVE on the
    same copy then matched nothing and recorded `REMEDIATION_DECLINED ...
    matched no applicable arm`, grading a correct outcome REVIEW_REQUIRED
    (measured on ac33641 with hand-built findings). It now folds, as
    REPLACE and EMPTY do: the copies are absent in both orders, no
    decline, and each owner row names its folds.

    Kills the REMOVE early return kept in `_folds_into_owner` (a declined
    row appears) and the pending count keyed without `removed` (see
    `test_a_remove_does_not_fold_into_a_written_owner`)."""
    with _built(tmp_path) as session:
        # patient_name and study_date removed, patient_id pseudonymised;
        # the four instance REMOVEs fold.
        assert _anonymize(session, REMOVE, order) == 3
        patient = session.store.patients[0]
        assert patient.patient_name is None
        assert patient.studies[0].study_date is None
        for copy in _copies(session):
            assert copy["0010,0010"] == "<absent>"
            assert copy["0008,0020"] == "<absent>"
        for inst in _instances(session):
            assert inst.phi_status is PhiStatus.REMEDIATED
        session.save(sync=True)
        report = tmp_path / "r.md"
        session.generate_report(str(report))
    assert _audit_rows(tmp_path / "m.db", "REMEDIATION_DECLINED") == []
    rows = _audit_rows(tmp_path / "m.db", "REMEDIATION_REMOVE")
    assert len(rows) == 2, rows
    for row in rows:
        assert row.endswith("; removed from 2 instance copies; "
                            "2 instance-level findings on this tag folded into it"), row
    assert "**Grade Basis:** PASS" in report.read_text(encoding="utf-8")


class _Rows:
    """A store backend that keeps the audit rows it is handed."""

    def __init__(self):
        self.rows = []

    def log_audit_batch(self, rows):
        self.rows.extend(rows)

    def log_audit(self, *row):
        self.rows.append(row)


def _hand_built(entity, tag, action, new_value=None, entity_type="Instance"):
    from isocenter.privacy import PhiFinding, PhiRemediation
    return PhiFinding(
        entity_uid=getattr(entity, "sop_instance_uid", "owner"),
        entity_type=entity_type, field_name=tag, value="v", reason="hand-built",
        tag=tag, entity=entity,
        remediation_proposal=PhiRemediation(action, tag, new_value=new_value))


def test_a_remove_does_not_fold_into_a_written_owner():
    """An owner that *wrote* a value does not absorb an instance REMOVE:
    the copy holds the owner's value, and the policy asked for it gone.
    Only reachable with hand-built findings (one rule feeds both levels
    on every scanned path). And the other way round, an owner that
    *removed* absorbs the REMOVE but not an instance REPLACE on the same
    copy, which declines against the absent tag. Kills `_folds_into_owner`
    ignoring `removed`, and the pending count keyed without it (the owner
    row would claim 2 folds)."""
    patient = _patient_with_one_instance(NAME, PID)
    instance = patient.studies[0].series[0].instances[0]
    backend = _Rows()
    applied = RemediationService(store_backend=backend).apply_remediation([
        _hand_built(patient, "patient_name", "REPLACE_TAG", "ANONYMIZED", "Patient"),
        _hand_built(instance, "0010,0010", "REMOVE_TAG"),
    ])
    assert applied == 2
    assert "0010,0010" not in instance.attributes
    owner_row = next(r[2] for r in backend.rows if "patient_name" in r[2])
    assert "folded" not in owner_row, owner_row
    assert not [r for r in backend.rows if r[0] == "REMEDIATION_DECLINED"]

    patient = _patient_with_one_instance(NAME, PID)
    instance = patient.studies[0].series[0].instances[0]
    backend = _Rows()
    RemediationService(store_backend=backend).apply_remediation([
        _hand_built(patient, "patient_name", "REMOVE_TAG", entity_type="Patient"),
        _hand_built(instance, "0010,0010", "REMOVE_TAG"),
        _hand_built(instance, "0010,0010", "REPLACE_TAG", "ANONYMIZED"),
    ])
    assert "0010,0010" not in instance.attributes
    owner_row = next(r[2] for r in backend.rows if "patient_name" in r[2])
    assert owner_row.endswith("; 1 instance-level finding on this tag folded into it"), owner_row


@pytest.mark.parametrize("order", ORDERS)
def test_findings_handed_in_twice_fold_once(tmp_path, order):
    """A caller that passes a report twice gets the same result as passing
    it once: duplicates are skipped, not folded or counted twice."""
    with _built(tmp_path) as session:
        session.configuration.phi_tags = REPLACE
        findings = list(session.audit())
        assert session.anonymize(_ordered(findings + findings, order)) == 3
    rows = _audit_rows(tmp_path / "m.db", "REMEDIATION_REPLACE")
    assert len(rows) == 2, rows
    assert all(row.endswith("; 2 instance-level findings on this tag folded into it")
               for row in rows), rows


@pytest.mark.parametrize("order", ORDERS)
def test_a_keep_policy_leaves_the_patients_value_on_every_instance(tmp_path, order):
    """D. KEEP raises no finding on the patient or on the copies, so the
    name and ID are kept everywhere (#537: until 0.9.8 the patient was
    anonymized whatever the rule said, and #492's write put its
    replacement on the copies). The one remediation is the study date,
    which has no rule; the scan's CLEARED on the instances survives its
    write."""
    with _built(tmp_path) as session:
        assert _anonymize(session, KEEP, order) == 1
        patient = session.store.patients[0]
        assert (patient.patient_name, patient.patient_id) == (NAME, PID)
        for inst in _instances(session):
            assert inst.attributes["0010,0010"] == NAME
            assert inst.attributes["0010,0020"] == PID
            assert inst.phi_status is PhiStatus.CLEARED


# ---------------------------------------------------------------------------
# F, G: nothing reached the copy, so the instance's own finding applies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ORDERS)
def test_when_the_owners_own_finding_declines_the_instance_copy_follows_the_owner(
        tmp_path, order):
    """F. The study's SHIFT_DATE declines on 'NOT-A-DATE', so no write
    reached the instance copies. Folding them anyway would leave the real
    original date in `export_dataframe(expand_metadata=True)` and
    `get_flattened_instances()`. Until #624 the instance then shifted its
    own date, a value no file carried: the export stamps the Study's.
    Now each copy holds what the export writes, 'NOT-A-DATE', and each
    instance's finding declines with its own row, beside the study's."""
    with _built(tmp_path, study_date="NOT-A-DATE") as session:
        _anonymize(session, JITTER, order)
        study = session.store.patients[0].studies[0]
        assert study.study_date == "NOT-A-DATE"
        assert study.phi_status is PhiStatus.IDENTIFIED
        for inst in _instances(session):
            assert inst.attributes["0008,0020"] == "NOT-A-DATE"
            assert not inst.date_shift_vouches_for("0008,0020", "NOT-A-DATE")
            assert inst.phi_status is PhiStatus.IDENTIFIED
    declines = _audit_rows(tmp_path / "m.db", "REMEDIATION_DECLINED")
    assert sorted("study_date" in d for d in declines) == [False, False, True], declines
    assert sum("written by the export from the Study" in d for d in declines) == 2, declines


@pytest.mark.parametrize("order", ORDERS)
def test_instance_findings_handed_in_alone_follow_the_owner(tmp_path, order):
    """G. No owner finding, no owner write, nothing to fold into. Until
    #624 the instance copies took `ANONYMIZED` while the file carried the
    Patient's name and ID. Now nothing applies: each copy keeps what the
    export writes, the Patient's values, and each finding is left
    unhandled with no row, since no owner declined (#624, Q-C5): the
    instances read IDENTIFIED until the Patient is acted on."""
    with _built(tmp_path) as session:
        assert _anonymize(session, REPLACE, order,
                          select=lambda f: f.entity_type == "Instance") == 0
        patient = session.store.patients[0]
        assert (patient.patient_name, patient.patient_id) == (NAME, PID)
        for inst in _instances(session):
            assert inst.attributes["0010,0010"] == NAME
            assert inst.attributes["0010,0020"] == PID
            assert inst.phi_status is PhiStatus.IDENTIFIED
    assert _audit_rows(tmp_path / "m.db", "REMEDIATION_DECLINED") == []


# ---------------------------------------------------------------------------
# A fold does not mask a decline on the same instance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ORDERS)
def test_a_fold_beside_a_decline_on_the_same_instance_leaves_it_identified(tmp_path, order):
    """The fold stamps the instance REMEDIATED, as its own success would
    have; #491's pass-end demotion must still take an instance that also
    declined back to IDENTIFIED, and the manifest must say false for it.
    The second instance, fold only, reads true: that half kills a fold that
    stamps nothing."""
    with _built(tmp_path) as session:
        bad, good = _instances(session)
        bad.set_attr("0008,0021", "NOT-A-DATE")
        good.set_attr("0008,0021", "20240101")
        policy = dict(REPLACE, **{"0008,0021": {"action": "JITTER"}})
        _anonymize(session, policy, order)
        assert bad.attributes["0010,0010"] == "ANONYMIZED"
        assert bad.phi_status is PhiStatus.IDENTIFIED
        assert good.phi_status is PhiStatus.REMEDIATED
        out = tmp_path / "manifest.json"
        session.generate_manifest(str(out), format="json")
        items = {i["sop_instance_uid"]: i["anonymized"]
                 for i in json.loads(out.read_text(encoding="utf-8"))["items"]}
        assert items == {bad.sop_instance_uid: False, good.sop_instance_uid: True}


# ---------------------------------------------------------------------------
# The scan: the owner's replacement on a top-level copy is not a finding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("policy", [REPLACE, EMPTY], ids=["replace", "empty"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_re_audit_does_not_flag_the_owners_replacement_on_the_instance(tmp_path, order, policy):
    """Measured on 09fca54: once an instance held the Patient's
    `ANON_<hash>`, a re-audit raised REPLACE on it, the instance went
    IDENTIFIED (the manifest reads false), and a second `anonymize()`
    wrote a second value over it."""
    with _built(tmp_path) as session:
        _anonymize(session, policy, order)
        again = [f for f in session.audit() if f.entity_type == "Instance"]
        assert again == []
        for inst in _instances(session):
            assert inst.phi_status is PhiStatus.CLEARED
        session.anonymize()
        patient = session.store.patients[0]
        for inst in _instances(session):
            assert inst.attributes["0010,0010"] == patient.patient_name
            assert inst.attributes["0010,0020"] == patient.patient_id


@pytest.mark.parametrize("order", ORDERS)
def test_a_re_audit_does_not_flag_the_studys_shifted_date_on_the_instance(tmp_path, order):
    """The date arm of the skip: under a REPLACE rule on StudyDate the
    folded copy holds the study's shifted date, which is not PHI."""
    with _built(tmp_path) as session:
        _anonymize(session, REPLACE_DATE, order)
        study = session.store.patients[0].studies[0]
        for inst in _instances(session):
            assert inst.attributes["0008,0020"] == format_study_date(study.study_date)
        assert [f for f in session.audit() if f.entity_type == "Instance"] == []


def _patient_with_one_instance(name, pid, *, nested_id=None):
    patient = Patient(pid, name)
    study = Study("1.2.826.0.1.496.9", "20240101")
    series = Series("1.2.826.0.1.496.9.1", "OT", 1)
    instance = Instance("1.2.826.0.1.496.9.1.0", SC_SOP_CLASS, 1)
    instance.set_attr("0010,0010", name)
    instance.set_attr("0010,0020", pid)
    if nested_id is not None:
        item = DicomItem()
        item.set_attr("0010,0020", nested_id)
        instance.sequences["0040,a730"] = DicomSequence(tag="0040,a730", items=[item])
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _instance_tags(findings):
    return sorted((f.tag, f.entity_path) for f in findings if f.entity_type == "Instance")


def test_a_copy_equal_to_an_original_owner_value_is_still_a_finding():
    """The skip is for a replacement, not for agreement: before anything is
    anonymized the instance copies equal the Patient's (original) values,
    and both are still findings. The exception is an original that itself
    passes the replacement test; see
    `test_an_original_that_looks_like_a_replacement_is_not_a_finding`."""
    findings = PhiInspector(config_tags=REPLACE, project_secret=FIXED_A).scan_patient(
        _patient_with_one_instance(NAME, PID))
    assert _instance_tags(findings) == [("0010,0010", ()), ("0010,0020", ())]


def test_a_copy_equal_to_an_unshifted_study_date_is_still_a_finding():
    """The date arm's own negative: agreement with a study that has not
    been shifted is agreement with the original date, which is PHI. Kills
    a skip that tests only the value and not `date_shifted`."""
    patient = _patient_with_one_instance(NAME, PID)
    patient.studies[0].series[0].instances[0].set_attr("0008,0020", "20240101")
    assert not patient.studies[0].date_shifted
    findings = PhiInspector(config_tags=REPLACE_DATE, project_secret=FIXED_A).scan_patient(patient)
    assert _instance_tags(findings) == [("0008,0020", ())]


def test_a_nested_copy_of_the_owners_replacement_is_still_a_finding():
    """Top-level only: the exporter's stamp and the owner's write reach the
    dataset root, so a nested copy is the instance scan's to judge, even
    when it happens to equal the owner's replacement."""
    anon_id = "ANON_0123456789ab"
    findings = PhiInspector(config_tags=REPLACE, project_secret=FIXED_A).scan_patient(
        _patient_with_one_instance("ANONYMIZED", anon_id, nested_id=anon_id))
    assert _instance_tags(findings) == [("0010,0020", (("0040,a730", 0),))]


def test_a_copy_of_an_owners_replacement_is_not_skipped_without_an_owner():
    """No patient handed to the instance scan, no skip: an instance copy
    that merely looks like a replacement is judged by the policy."""
    instance = _patient_with_one_instance("ANONYMIZED", "ANON_0123456789ab").studies[0] \
        .series[0].instances[0]
    findings = PhiInspector(config_tags=REPLACE)._scan_instance(  # pylint: disable=protected-access
        instance, "ANON_0123456789ab")
    assert _instance_tags(findings) == [("0010,0020", ())]


def test_an_owned_tag_remove_is_not_skipped_by_the_scan():
    """REMOVE is exempt from the scan's skip: a copy of the owner's
    replacement under a REMOVE rule is still raised, and folds into the
    owner's removal. The name only: a REMOVE on Patient ID is refused
    (#537)."""
    anon_id = "ANON_0123456789ab"
    findings = PhiInspector(config_tags={"0010,0010": {"action": "REMOVE"}},
                            project_secret=FIXED_A).scan_patient(
        _patient_with_one_instance("ANONYMIZED", anon_id))
    assert _instance_tags(findings) == [("0010,0010", ())]


def test_remediation_folds_only_a_copy_the_owners_write_reached():
    """Direct on the service, no session: the instance copy is folded only
    after the Patient's write has reached it, whatever order the list is in."""
    patient = _patient_with_one_instance(NAME, PID)
    instance = patient.studies[0].series[0].instances[0]
    findings = PhiInspector(config_tags=REPLACE, project_secret=FIXED_A).scan_patient(patient)
    for inst_finding in findings:
        if inst_finding.entity_type == "Instance":
            inst_finding.entity = instance
    service = RemediationService(project_secret=FIXED_A)
    assert service.apply_remediation(list(reversed(findings))) == 3
    assert instance.attributes["0010,0020"] == patient.patient_id


# ---------------------------------------------------------------------------
# Review round (#508)
# ---------------------------------------------------------------------------

def _two_studies(tmp_path, dates):
    """One patient, one study per `(key, study_date)`, one instance each
    whose own StudyDate is 20240101."""
    session = DicomSession(str(tmp_path / "m.db"))
    patient = Patient(PID, NAME)
    for key, study_date in dates:
        study = Study(f"1.2.826.0.1.496.{key}", "20240101")
        study.study_date = study_date
        series = Series(f"1.2.826.0.1.496.{key}.1", "OT", 1)
        instance = Instance(f"1.2.826.0.1.496.{key}.1.0", SC_SOP_CLASS, 1)
        instance.set_attr("0008,0020", "20240101")
        series.instances.append(instance)
        study.series.append(series)
        patient.studies.append(study)
    session.store.patients.append(patient)
    return session


@pytest.mark.parametrize("order", ORDERS)
def test_a_fold_is_keyed_on_the_copy_not_on_the_tag(tmp_path, order):
    """F across two studies of one patient. Study 7's date declines and
    study 8's shifts; study 8's write reached only its own instance, so
    study 7's instance does not fold: since #624 its copy follows its own
    Study, 'NOT-A-DATE', and its finding declines. A fold keyed on the tag
    alone folds it into a write it never received: it keeps the original
    20240101, is stamped REMEDIATED, and files no row (review of #508,
    mutant M1)."""
    with _two_studies(tmp_path, [("7", "NOT-A-DATE"), ("8", "20240101")]) as session:
        assert _anonymize(session, JITTER, order) == 3
        declined, shifted = session.store.patients[0].studies
        (own,) = declined.series[0].instances
        (folded,) = shifted.series[0].instances
        assert declined.study_date == "NOT-A-DATE"
        assert own.attributes["0008,0020"] == "NOT-A-DATE"
        assert own.phi_status is PhiStatus.IDENTIFIED
        assert folded.attributes["0008,0020"] == format_study_date(shifted.study_date)
    declines = _audit_rows(tmp_path / "m.db", "REMEDIATION_DECLINED")
    assert sorted("study_date" in d for d in declines) == [False, True], declines
    assert sum("written by the export from the Study" in d for d in declines) == 1, declines


def test_the_entity_first_sort_keeps_private_sequence_removals_deepest_first(tmp_path):
    """#167's deepest-first order survives the entity-first sort. For a
    private sequence inside a private sequence the scan proposes the inner
    removal first, so its REMEDIATION_REMOVE row is written while the item
    holding it is still in the graph. Reverse the instance half of the
    sort and the outer sequence goes first; the inner row then describes a
    dict nothing reaches any more (review of #508, mutant M4). Distinct
    tags, because the row names only the tag. Scan order only: a caller
    who reverses the list reverses this too, as before #496."""
    outer_tag, inner_tag = "0009,1003", "0011,1005"
    with _built(tmp_path) as session:
        instance = _instances(session)[0]
        outer_item = DicomItem()
        outer_item.sequences[inner_tag] = DicomSequence(tag=inner_tag, items=[DicomItem()])
        instance.sequences[outer_tag] = DicomSequence(tag=outer_tag, items=[outer_item])
        session.configuration.remove_private_tags = True
        _anonymize(session, REPLACE, "scan-order")
        assert outer_tag not in instance.sequences
    with sqlite3.connect(str(tmp_path / "m.db")) as conn:
        rows = [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='REMEDIATION_REMOVE' "
            "AND details LIKE 'Removed Sequence %' ORDER BY rowid")]
    assert [row.split()[2] for row in rows] == [inner_tag, outer_tag], rows


def test_an_original_that_looks_like_a_replacement_is_not_a_finding(tmp_path):
    """The skip's cost, named (review of #508). A source file whose own
    PatientName is `ANONYMIZED` and PatientID `ANON_REAL77` raised 2
    instance findings under REPLACE before #496, and `anonymize()`
    returned 3. Now it raises none and returns 1, the study date: the
    copies equal the Patient's values, and those pass `scan_patient`'s
    replacement test, which is why the Patient itself was never raised on
    them either. The copies keep the values.

    REPLACE only since #537. The EMPTY case this also ran emptied Patient
    ID, which is now refused, and an EMPTY rule on the name judges the
    name by the rule it is under: `ANONYMIZED` is not empty, so the
    patient and its copies are emptied, which is the rule's request."""
    with _built(tmp_path, name="ANONYMIZED", pid="ANON_REAL77") as session:
        session.configuration.phi_tags = REPLACE
        findings = list(session.audit())
        assert [f for f in findings if f.entity_type == "Instance"] == []
        assert session.anonymize(findings) == 1
        for copy in _copies(session):
            assert (copy["0010,0010"], copy["0010,0020"]) == ("ANONYMIZED", "ANON_REAL77")


@pytest.mark.parametrize("order", ORDERS)
def test_a_malformed_instance_date_under_a_parseable_study_folds_and_grades_pass(tmp_path, order):
    """The grade change, named (review of #508). Before #496 the
    instance's own SHIFT_DATE declined on '2024-13-45X': a DECLINED row,
    the instance IDENTIFIED, the manifest false, REVIEW_REQUIRED. Now the
    study's write reaches that copy first and puts the study's shifted
    date on it, so the finding folds: no decline, REMEDIATED, true, PASS.
    That is the truthful outcome -- the malformed value is gone from the
    copy, and the exporter stamps the same shifted date."""
    with _built(tmp_path, instance_dates=("20240101", "2024-13-45X")) as session:
        assert _anonymize(session, JITTER, order) == 3
        study = session.store.patients[0].studies[0]
        for inst in _instances(session):
            assert inst.attributes["0008,0020"] == format_study_date(study.study_date)
            assert inst.phi_status is PhiStatus.REMEDIATED
        session.save(sync=True)
        report = tmp_path / "r.md"
        session.generate_report(str(report))
        manifest = tmp_path / "manifest.json"
        session.generate_manifest(str(manifest), format="json")
        items = json.loads(manifest.read_text(encoding="utf-8"))["items"]
        assert [item["anonymized"] for item in items] == [True, True]
    assert _audit_rows(tmp_path / "m.db", "REMEDIATION_DECLINED") == []
    assert "**Grade Basis:** PASS" in report.read_text(encoding="utf-8")


def test_one_copy_and_one_fold_are_counted_in_the_singular(tmp_path):
    """"written to 1 instance copy; 1 instance-level finding", not
    "1 instance copies; 1 instance-level findings" (review of #508)."""
    with _built(tmp_path, instance_dates=("20240101",)) as session:
        assert _anonymize(session, REPLACE, "scan-order") == 3
    rows = _audit_rows(tmp_path / "m.db", "REMEDIATION_REPLACE")
    assert len(rows) == 2, rows
    for row in rows:
        assert row.endswith("; written to 1 instance copy; "
                            "1 instance-level finding on this tag folded into it"), row

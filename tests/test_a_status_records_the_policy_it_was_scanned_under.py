"""A PHI status records the policy it was scanned under (#555).

Measured on 63a64158: `CT_small`, remediated under `privacy_profile: none`
with one rule and reopened bare under the 620-rule floor, still read
`REMEDIATED`, and a plain `export()` wrote Institution Name -- which the
floor empties -- under a `PASS`. Nothing in the store said which policy
the statuses were recorded under, so nothing could notice the policy in
force was another one.

Now each status carries a `ScanPolicy` (a fingerprint and a readable
base), recorded by the scan and inherited by every status transition that
follows it (remediation, redaction's carry, the merge keeps the kept
member's). The store holds it in `phi_policy` and `phi_policy_base`.
Nothing is reinterpreted on load (owner's ruling Q1: report the fact).
`export()`, in both formats, writes one `WARNING` row when the instances
it writes carry statuses recorded under a policy that is neither the one
in force nor one this session scanned under (Q2), and the files are
unchanged.

Expected fingerprints are read from `configuration._scan_policy()` or
`_scan_policy_for()` at the time of the scan, never pasted: L10 and L11
change the floor before 1.0, and `test_the_policy_fingerprint.py` holds
the one literal.

**Why this file imports what it does.** The recording is in
`isocenter.session` and `isocenter.entities`, the fingerprint in
`isocenter.configuration`, the store in `isocenter.persistence`; see
`test_mutation_probe_targets.py`.
"""
import json
import pathlib
import shutil
import sqlite3
from datetime import date

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter import config_manager
from isocenter.configuration import _scan_policy_for
from isocenter.entities import (Equipment, Instance, Patient, PhiStatus,
                                ScanPolicy, Series, Study)
from isocenter.session import DicomSession

NOTICE = "recorded under a policy other than the one in force"
FLOOR = "floor over basic@2026c"
INSTITUTION = "0008,0080"


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def mode(request, monkeypatch):
    if request.param == "processes":
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


def _write_config(tmp_path, name, **body):
    path = tmp_path / name   # JSON is YAML; the loader wants the suffix
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def _narrow(tmp_path, **extra):
    """§1.1's policy: no profile, one rule, which leaves Institution Name."""
    return _write_config(tmp_path, "narrow.yaml", privacy_profile="none",
                         phi_tags={"0008,1010": {"action": "REMOVE"}}, **extra)


def _basic(tmp_path):
    return _write_config(tmp_path, "basic.yaml", privacy_profile="basic")


def _input(tmp_path, *names):
    folder = tmp_path / "in"
    folder.mkdir(exist_ok=True)
    for name in names:
        shutil.copy(get_testdata_file(name), folder / name)
    return str(folder)


COHORT = ("CT_small.dcm", "MR_small.dcm", "rtplan.dcm")


def _entities(session):
    for patient in session.store.patients:
        yield "patient", patient
        for study in patient.studies:
            yield "study", study
            for series in study.series:
                for instance in series.instances:
                    yield "instance", instance


def _instances(session):
    return [e for level, e in _entities(session) if level == "instance"]


def _notices(session):
    return [details for _, action, details
            in session.store_backend.get_audit_errors()
            if NOTICE in details]


def _warnings(session):
    return [details for _, action, details
            in session.store_backend.get_audit_errors()]


def _grade(session, tmp_path, name="report.md"):
    path = tmp_path / name
    session.generate_report(str(path))
    [line] = [ln for ln in path.read_text(encoding="utf-8").splitlines()
              if "Validation Status" in ln]
    return line


def _rows(db, table):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            f"SELECT phi_status, phi_policy, phi_policy_base FROM {table}"
        ).fetchall()


def _exported(folder):
    [path] = list(pathlib.Path(folder).rglob("*.dcm"))
    return pydicom.dcmread(str(path))


def _remediated_under_narrow(tmp_path, db, names=("CT_small.dcm",)):
    """Session 1 of §1.1: load the narrow config, anonymize, save."""
    narrow = _narrow(tmp_path)
    with DicomSession(db) as session:
        session.ingest(_input(tmp_path, *names))
        session.load_config(narrow)
        session.anonymize(session.audit())
        policy = session.configuration._scan_policy()
        session.save(sync=True)
    return narrow, policy


# --- recording -------------------------------------------------------------

@pytest.mark.parametrize("mode", ["threads", "processes"], indirect=True)
def test_audit_records_its_policy_on_every_entity(tmp_path, mode):
    """Kills: `_record_scan_results` not passing the policy."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.load_config(_basic(tmp_path))
        session.audit()
        expected = session.configuration._scan_policy()
        assert expected.fingerprint.startswith("v1:")
        assert expected.base == "basic@2026c"
        seen = set()
        for level, entity in _entities(session):
            seen.add(level)
            assert entity.phi_status is not PhiStatus.UNSCANNED, level
            assert entity.phi_status_policy == expected, level
        assert seen == {"patient", "study", "instance"}


def test_audit_with_a_config_path_records_that_files_policy(tmp_path):
    """Kills: the `config_path` arm using `configuration._scan_policy()`;
    the loader's base discarded."""
    narrow = _narrow(tmp_path)
    tags = config_manager.ConfigLoader.load_unified_config(narrow)[0]
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.audit(config_path=narrow)
        [inst] = _instances(session)
        expected = _scan_policy_for(tags, True, "none")
        assert inst.phi_status_policy == expected
        assert inst.phi_status_policy.base == "none"
        assert (inst.phi_status_policy.fingerprint
                != session.configuration._scan_policy().fingerprint)


def test_the_policy_is_the_flag_the_scan_used(tmp_path):
    """`audit(config_path=)` scans with the session's
    `remove_private_tags`, not the file's. Kills: reading the file's flag."""
    path = _narrow(tmp_path, remove_private_tags=False)
    tags = config_manager.ConfigLoader.load_unified_config(path)[0]
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        assert session.configuration.remove_private_tags is True
        session.audit(config_path=path)
        [inst] = _instances(session)
        assert inst.phi_status_policy.fingerprint == _scan_policy_for(
            tags, True, "none").fingerprint
        assert inst.phi_status_policy.fingerprint != _scan_policy_for(
            tags, False, "none").fingerprint


REQUEST_SEQ = "0040,0275"   # Request Attributes Sequence
STEP = "0040,0007"          # Scheduled Procedure Step Description


def _write_nested_only_ct(path):
    """CT_small whose only finding under a STEP rule is the nested one
    (the shape of `test_nested_remediation_reaches_the_instance.py`)."""
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    item = Dataset()
    item.ScheduledProcedureStepDescription = "Nested^PHI"
    ds.RequestAttributesSequence = Sequence([item])
    ds.remove_private_tags()
    ds.PatientID = "ANON_probe"
    ds.PatientName = "ANONYMIZED"
    if "StudyDate" in ds:
        del ds.StudyDate
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path))


def test_remediation_keeps_its_scans_policy(tmp_path):
    """Remediation stamps REMEDIATED after its own writes, when the status
    already reads UNSCANNED: the policy comes from the entity's record,
    not from the stale reading. A nested item is never scanned, so its
    REMEDIATED carries no policy. Handed the findings as a list, not the
    report, so the report's own policy (`anonymize(report)` after a
    reopen) cannot stand in for the entity's. Kills: the default recording
    `None`; an item given a policy no scan recorded."""
    _write_nested_only_ct(tmp_path / "in" / "a.dcm")
    config = _write_config(tmp_path, "tags.yaml", privacy_profile="none",
                           phi_tags={STEP: {"action": "REPLACE"}})
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.load_config(config)
        session.anonymize(list(session.audit().findings))
        expected = session.configuration._scan_policy()
        [inst] = _instances(session)
        item = inst.sequences[REQUEST_SEQ].items[0]
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status_policy == expected
        assert item.phi_status is PhiStatus.REMEDIATED
        assert item.phi_status_policy is None


LEVELS = ["patient", "study", "instance"]
TABLES = {"patient": "patients", "study": "studies", "instance": "instances"}


def _one(session, level):
    [entity] = [e for lv, e in _entities(session) if lv == level]
    return entity


@pytest.mark.parametrize("level", LEVELS)
def test_a_status_and_its_policy_survive_a_reopen(tmp_path, level):
    """Kills: an upsert that omits the columns, at each level."""
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.load_config(_basic(tmp_path))
        session.audit()
        before = _one(session, level)
        status, policy = before.phi_status, before.phi_status_policy
        session.save(sync=True)
    assert _rows(db, TABLES[level]) == [
        (status.value, policy.fingerprint, "basic@2026c")]
    with DicomSession(db) as session:
        after = _one(session, level)
        assert after.phi_status is status
        assert after.phi_status_policy == policy
        assert not after.has_unsaved_changes


@pytest.mark.parametrize("level", LEVELS)
def test_a_status_edited_away_stores_no_policy(tmp_path, level):
    """Kills: `COALESCE` in `DO UPDATE`, which keeps the old policy."""
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.audit()
        session.save(sync=True)
        assert _rows(db, TABLES[level])[0][1].startswith("v1:")
        entity = _one(session, level)
        entity.mark_modified()
        assert entity.phi_status_policy is None
        session.save(sync=True)
    assert _rows(db, TABLES[level]) == [("unscanned", None, None)]


def test_the_merge_keeps_the_policy_of_the_status_it_keeps(tmp_path):
    """Kills: the merge recording the kept status under the survivor's own
    policy."""
    p = ScanPolicy("v1:" + "a" * 64, "P")
    q = ScanPolicy("v1:" + "b" * 64, "Q")
    with DicomSession(str(tmp_path / "s.db")) as session:
        survivor = Patient("SAME", "A^B")
        other = Patient("SAME", "A^B")
        survivor.record_phi_status(PhiStatus.CLEARED, policy=p)
        other.record_phi_status(PhiStatus.IDENTIFIED, policy=q)
        session.store.patients[:] = [survivor, other]
        session.store._merge_patients_sharing_an_id()
        assert session.store.patients == [survivor]
        assert survivor.phi_status is PhiStatus.IDENTIFIED
        assert survivor.phi_status_policy == q


def test_a_status_recorded_again_under_another_policy_is_a_change():
    """The #173 short-circuit, now on both halves, and by value."""
    entity = Patient("P", "N")
    entity.record_phi_status(PhiStatus.CLEARED,
                             policy=ScanPolicy("v1:" + "a" * 64, "P"))
    entity.mark_subtree_persisted()
    entity.record_phi_status(PhiStatus.CLEARED,
                             policy=ScanPolicy("v1:" + "a" * 64, "P"))
    assert not entity.has_unsaved_changes, "a fresh equal policy is no change"
    entity.record_phi_status(PhiStatus.CLEARED,
                             policy=ScanPolicy("v1:" + "b" * 64, "P"))
    assert entity.has_unsaved_changes
    assert entity.phi_status_policy.fingerprint == "v1:" + "b" * 64


def test_a_second_identical_audit_leaves_no_instance_unsaved(tmp_path):
    """§1.4's cohort. Kills: the policy half of the short-circuit comparing
    by identity (each audit builds a fresh `ScanPolicy`)."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_input(tmp_path, *COHORT))
        session.audit()
        session.save(sync=True)
        session.audit()
        assert [i for i in _instances(session) if i.has_unsaved_changes] == []


def test_an_audit_under_another_policy_leaves_every_instance_unsaved(tmp_path):
    """Kills: a short-circuit comparing the status only (measured at 0 on
    63a64158: the statuses are the same under both policies)."""
    db = str(tmp_path / "s.db")
    narrow = _narrow(tmp_path)
    with DicomSession(db) as session:
        session.ingest(_input(tmp_path, *COHORT))
        session.audit()
        before = [i.phi_status for i in _instances(session)]
        session.save(sync=True)
        session.audit(config_path=narrow)
        assert [i.phi_status for i in _instances(session)] == before, (
            "setup: the two policies are meant to agree on every instance")
        assert len([i for i in _instances(session)
                    if i.has_unsaved_changes]) == 3
        policy = _instances(session)[0].phi_status_policy
        session.save(sync=True)
    assert {r[1:] for r in _rows(db, "instances")} == {
        (policy.fingerprint, "none")}


SERIAL = "SN-555"


def test_redaction_keeps_the_policy(tmp_path):
    """Kills: the redaction carry recording `policy=None`."""
    config = _write_config(
        tmp_path, "tags.yaml", privacy_profile="none",
        phi_tags={"0008,0090": {"name": "Referring", "action": "REPLACE"}})
    with DicomSession(str(tmp_path / "s.db")) as session:
        patient = Patient("P555", "Original^Name")
        study = Study("1.2.826.0.1.555", date(2023, 1, 1))
        series = Series("1.2.826.0.1.555.1", "OT", 1)
        series.equipment = Equipment("Acme", "Model", SERIAL)
        instance = Instance("1.2.826.0.1.555.1.0",
                            "1.2.840.10008.5.1.4.1.1.7", 1)
        instance.file_path = None
        instance.set_pixel_data(np.full((16, 16), 200, dtype=np.uint8))
        instance.set_attr("0008,0090", "Dr^Leak")
        series.instances.append(instance)
        study.series.append(series)
        patient.studies.append(study)
        session.store.patients.append(patient)
        session.configuration.rules = [
            {"serial_number": SERIAL, "redaction_zones": [[0, 8, 0, 8]]}]
        session.save(sync=True)

        session.anonymize(session.audit(config).findings)
        policy = instance.phi_status_policy
        assert instance.phi_status is PhiStatus.REMEDIATED
        assert policy is not None and policy.base == "none"
        session.redact()
        assert instance.attributes.get("0028,0301") == "NO", "redaction ran"
        assert instance.phi_status is PhiStatus.REMEDIATED
        assert instance.phi_status_policy == policy


# --- the export notice -----------------------------------------------------

def test_export_says_when_it_writes_statuses_recorded_under_another_policy(
        tmp_path):
    """§1.1 exactly. Kills: no notice; a notice that edits the output."""
    db = str(tmp_path / "s.db")
    _, narrow_policy = _remediated_under_narrow(tmp_path, db)
    with DicomSession(db) as session:
        floor = session.configuration._scan_policy()
        session.export(str(tmp_path / "out"))
        [notice] = _notices(session)
        assert notice.startswith(f"DICOM export to {tmp_path / 'out'} writes "
                                 "1 instance(s) whose PHI status was "), notice
        assert f"({FLOOR}, {floor.fingerprint[:15]})" in notice
        assert f"none ({narrow_policy.fingerprint[:15]})" in notice
        assert "(#555)" in notice
        assert _warnings(session) == [notice], "the notice is the only row"
        # The export is unchanged: it writes the graph as it holds it.
        assert (_exported(tmp_path / "out").InstitutionName
                == "JFK IMAGING CENTER")
        assert "REVIEW_REQUIRED" in _grade(session, tmp_path)


def test_no_notice_when_the_policy_in_force_recorded_the_statuses(tmp_path):
    """Kills: comparing `base`, or whole `ScanPolicy` objects, instead of
    fingerprints (the scaffold case: one policy under two bases)."""
    db = str(tmp_path / "s.db")
    narrow, _ = _remediated_under_narrow(tmp_path, db)
    with DicomSession(db) as session:
        session.load_config(narrow)
        session.export(str(tmp_path / "out"))
        assert _notices(session) == []
        assert "PASS" in _grade(session, tmp_path)

    scaffold_db = str(tmp_path / "scaffold.db")
    scaffold = str(tmp_path / "scaffold.yaml")
    with DicomSession(scaffold_db) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.create_config(scaffold)
        session.load_config(scaffold)
        session.anonymize(session.audit())
        recorded = _instances(session)[0].phi_status_policy
        session.save(sync=True)
    with DicomSession(scaffold_db) as session:
        in_force = session.configuration._scan_policy()
        assert recorded.base != in_force.base, "setup: two bases"
        assert recorded.fingerprint == in_force.fingerprint, (
            "setup: a scaffold is the floor (L5)")
        session.export(str(tmp_path / "out2"))
        assert _notices(session) == []


def test_a_policy_this_session_scanned_under_raises_no_notice(tmp_path):
    """Owner's ruling Q2 (ii). Kills: the notice consulting the
    configuration only."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.anonymize(session.audit(config_path=_narrow(tmp_path)))
        session.export(str(tmp_path / "out"))
        assert _notices(session) == []


def test_a_rescan_before_export_raises_no_notice(tmp_path):
    """`check_burned_in=True` re-audits under the policy in force first.
    Kills: the notice computed before that audit re-records the statuses."""
    db = str(tmp_path / "s.db")
    _remediated_under_narrow(tmp_path, db)
    with DicomSession(db) as session:
        session.export(str(tmp_path / "out"), check_burned_in=True)
        assert _notices(session) == []
        assert any("withheld instance" in w for w in _warnings(session)), (
            "setup: the floor finds what the narrow policy left")


def test_the_notice_is_one_row_per_export(tmp_path):
    """Kills: one row per instance, or per patient."""
    db = str(tmp_path / "s.db")
    _remediated_under_narrow(tmp_path, db, COHORT)
    with DicomSession(db) as session:
        assert len(session.store.patients) == 3
        session.export(str(tmp_path / "out"))
        [notice] = _notices(session)
        assert " writes 3 instance(s) whose" in notice
        session.export(str(tmp_path / "out2"))
        assert len(_notices(session)) == 2


def test_a_wfdb_export_says_it_too(tmp_path):
    """Kills: the check wired into `_export_dicom` only."""
    from scripts.generate_waveform_test_data import write_fixture
    write_fixture(str(tmp_path / "ecg" / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-555", patient_name="Doe^Jane")
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "ecg"))
        session.load_config(_narrow(tmp_path))
        session.anonymize(session.audit())
        session.save(sync=True)
    with DicomSession(db) as session:
        session.export(str(tmp_path / "wfdb"), format="wfdb")
        [notice] = _notices(session)
        assert notice.startswith(f"WFDB export to {tmp_path / 'wfdb'} writes "
                                 "1 instance(s) whose"), notice


def test_nothing_is_said_for_an_empty_plan(tmp_path):
    """Kills: a row for zero instances."""
    db = str(tmp_path / "s.db")
    _remediated_under_narrow(tmp_path, db)
    with DicomSession(db) as session:
        session.export(str(tmp_path / "out"), patient_ids=[])
        session.export(str(tmp_path / "wfdb"), format="wfdb", patient_ids=[])
        assert _notices(session) == []


def test_the_report_names_a_policy_this_session_scanned_under(tmp_path):
    """From the L5 review (N5): after `audit(config_path=)` the report's
    profile rows describe the session configuration, not the policy the
    scan used. The methodology line now says which other policy this
    session's statuses were recorded under. Kills: the line left as it
    was."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.audit(config_path=_narrow(tmp_path))
        [inst] = _instances(session)
        fp = inst.phi_status_policy.fingerprint
        path = tmp_path / "r.md"
        session.generate_report(str(path))
        text = path.read_text(encoding="utf-8")
        assert f"none ({fp[:15]})" in text
        assert "this session also scanned under" in text

    with DicomSession(str(tmp_path / "t.db")) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.audit()
        path = tmp_path / "r2.md"
        session.generate_report(str(path))
        assert "this session also scanned under" not in path.read_text(
            encoding="utf-8")


# --- a report kept across a reopen -----------------------------------------

def _audited_and_closed(tmp_path, db, config=None):
    """Session 1 ingests and saves, audits, and closes without saving the
    audit: the store holds every status UNSCANNED, with no policy (#644's
    flow)."""
    with DicomSession(db) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        session.save(sync=True)
        if config:
            session.load_config(config)
        report = session.audit()
        policy = session.configuration._scan_policy()
    assert report._scan_policy == policy
    return report, policy


def test_a_report_kept_across_a_reopen_brings_its_policy(tmp_path):
    """The entities a kept report remediates carry no policy of their own;
    the report knows the one its findings were raised under. Kills: the
    adoption removed (the status carries None, and a same-policy export
    draws the notice over a graph a fresh pass would write)."""
    db = str(tmp_path / "s.db")
    narrow = _narrow(tmp_path)
    report, policy = _audited_and_closed(tmp_path, db, narrow)
    with DicomSession(db) as session:
        [inst] = _instances(session)
        assert inst.phi_status is PhiStatus.UNSCANNED
        session.load_config(narrow)
        session.anonymize(report)
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status_policy == policy
        session.export(str(tmp_path / "out"))
        assert _notices(session) == []

    # The same kept report under another policy in force: the notice
    # names the report's policy.
    db2 = str(tmp_path / "s2.db")
    report, policy = _audited_and_closed(tmp_path, db2, narrow)
    with DicomSession(db2) as session:
        session.anonymize(report)
        session.export(str(tmp_path / "out2"))
        [notice] = _notices(session)
        assert f"none ({policy.fingerprint[:15]})" in notice


def test_a_bare_findings_list_brings_no_policy(tmp_path):
    """A findings list is not a report, and names no scan: the status
    carries no policy, and the export says so."""
    db = str(tmp_path / "s.db")
    report, _ = _audited_and_closed(tmp_path, db)
    with DicomSession(db) as session:
        session.anonymize(list(report.findings))
        [inst] = _instances(session)
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status_policy is None
        session.export(str(tmp_path / "out"))
        [notice] = _notices(session)
        assert "1 with no recorded policy" in notice


def test_a_kept_report_narrowed_in_place_brings_no_policy(tmp_path):
    """Review of #750, finding 1, measured at 1f857655: a kept report whose
    findings list was narrowed in place still carried its scan's policy, so
    a pass that left Institution Name in place was labelled REMEDIATED under
    the floor, the export carried JFK IMAGING CENTER with no notice, the
    grade read PASS, and the saved row claimed a conclusion no scan
    reached (the scan under that policy concluded IDENTIFIED). The report
    no longer holds every finding its scan raised, so it does not speak for
    the pass: nothing is adopted, the status carries no policy, and the
    export says so. Kills: the completeness check skipped."""
    db = str(tmp_path / "s.db")
    report, _ = _audited_and_closed(tmp_path, db)
    assert any(f.tag == INSTITUTION for f in report.findings)
    report.findings[:] = [f for f in report.findings if f.tag != INSTITUTION]
    with DicomSession(db) as session:
        [inst] = _instances(session)
        session.anonymize(report)
        assert inst.attributes.get(INSTITUTION)   # the pass left it
        assert inst.phi_status_policy is None
        session.export(str(tmp_path / "out"))
        [notice] = _notices(session)
        # Finding 2: true of a report a scan did run for.
        assert ("1 with no recorded policy (written before 1.0, or "
                "remediated from findings that are not a whole audit() "
                "report)") in notice
        assert "REVIEW_REQUIRED" in _grade(session, tmp_path)


def test_a_kept_report_with_a_finding_added_still_brings_its_policy(tmp_path):
    """The check is that the report still holds every finding its scan
    raised, not that it holds nothing else: an appended finding does not
    withdraw what the scan concluded. Kills: completeness read as
    equality."""
    from isocenter.privacy import PhiFinding
    db = str(tmp_path / "s.db")
    report, policy = _audited_and_closed(tmp_path, db)
    [first] = report.findings[:1]
    report.findings.append(PhiFinding(
        entity_uid=first.entity_uid, entity_type=first.entity_type,
        field_name="extra", value=None, reason="added by hand", tag=None))
    with DicomSession(db) as session:
        [inst] = _instances(session)
        session.anonymize(report)
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status_policy == policy


def test_a_kept_report_whose_nested_item_is_gone_brings_no_policy(tmp_path):
    """The report still holds every finding, but one no longer resolves to
    a live target: its sequence item was removed before `anonymize()`. The
    pass settles the rest and records REMEDIATED; the report does not speak
    for an entity it could not reach, so nothing is adopted and the export
    says so (coordinator's ruling on #750). Kills: the resolution half of
    the check skipped."""
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    item = Dataset()
    item.ScheduledProcedureStepDescription = "Nested^PHI"
    ds.RequestAttributesSequence = Sequence([item])
    folder = tmp_path / "in"
    folder.mkdir()
    ds.save_as(str(folder / "a.dcm"))
    config = _write_config(
        tmp_path, "nested.yaml", privacy_profile="none",
        phi_tags={INSTITUTION: {"action": "REPLACE"},
                  "0040,0007": {"action": "REPLACE"}})
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(folder))
        session.save(sync=True)
        session.load_config(config)
        report = session.audit()
    assert {f.tag for f in report.findings} >= {INSTITUTION, "0040,0007"}
    with DicomSession(db) as session:
        session.load_config(config)
        [inst] = _instances(session)
        inst.sequences["0040,0275"].items.clear()
        session.anonymize(report)
        assert inst.attributes[INSTITUTION] != "JFK IMAGING CENTER"
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status_policy is None
        session.export(str(tmp_path / "out"))
        [notice] = _notices(session)
        assert "1 with no recorded policy" in notice


def test_a_kept_report_with_a_finding_rehydration_lost_brings_no_policy(
        tmp_path):
    """A finding whose rehydration found nothing (`_live_target` -> None,
    so it carries a proposal and no entity) reached no live target. The
    report holds it, and does not speak for the pass. Kills: a
    proposal-bearing finding with no entity counted as resolved."""
    db = str(tmp_path / "s.db")
    report, _ = _audited_and_closed(tmp_path, db)
    [lost] = [f for f in report.findings if f.tag == INSTITUTION]
    lost.entity = None
    with DicomSession(db) as session:
        [inst] = _instances(session)
        session.anonymize(report)
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status_policy is None


def test_a_report_applied_to_another_store_brings_no_policy(tmp_path):
    """A whole report from store A applied to store B, which holds the same
    patient and study but not A's instance: the patient and study findings
    resolve and are remediated, the instance findings resolve to nothing.
    The report was not raised on this graph, and nothing is adopted."""
    report, _ = _audited_and_closed(tmp_path, str(tmp_path / "a.db"))
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.SOPInstanceUID = ds.SOPInstanceUID + ".9"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    folder = tmp_path / "other"
    folder.mkdir()
    ds.save_as(str(folder / "b.dcm"))
    with DicomSession(str(tmp_path / "b.db")) as session:
        session.ingest(str(folder))
        session.anonymize(report)
        remediated = [e for _, e in _entities(session)
                      if e.phi_status is PhiStatus.REMEDIATED]
        assert remediated   # the pass reached the patient or the study
        for entity in remediated:
            assert entity.phi_status_policy is None, entity


def test_only_a_status_the_pass_recorded_adopts_the_reports_policy(tmp_path):
    """Kills: the adoption relabelling a policy-less status the pass did not
    record -- a store's pre-1.0 REMEDIATED beside the one the report
    touched."""
    from isocenter.privacy import PhiFinding, PhiRemediation, PhiReport
    policy = ScanPolicy("v1:" + "c" * 64, "kept")
    with DicomSession(str(tmp_path / "s.db")) as session:
        patient = Patient("P555", "Original^Name")
        study = Study("1.2.826.0.1.555", date(2023, 1, 1))
        series = Series("1.2.826.0.1.555.1", "OT", 1)
        untouched = Instance("1.2.826.0.1.555.1.1",
                             "1.2.840.10008.5.1.4.1.1.7", 1)
        touched = Instance("1.2.826.0.1.555.1.2",
                           "1.2.840.10008.5.1.4.1.1.7", 2)
        for inst in (untouched, touched):
            inst.file_path = None
            inst.set_attr("0008,0090", "Dr^Leak")
            inst.record_phi_status(PhiStatus.REMEDIATED, policy=None)
            series.instances.append(inst)
        study.series.append(series)
        patient.studies.append(study)
        session.store.patients.append(patient)

        report = PhiReport([PhiFinding(
            entity_uid=touched.sop_instance_uid, entity_type="Instance",
            field_name="0008,0090", value="Dr^Leak", reason="test",
            tag="0008,0090", entity=touched,
            remediation_proposal=PhiRemediation(
                action_type="REPLACE_TAG", target_attr="0008,0090",
                new_value="ANON", original_value="Dr^Leak"))])
        report._scan_policy = policy
        report._scan_findings = tuple(report.findings)
        assert session.anonymize(report) == 1
        assert touched.attributes["0008,0090"] == "ANON"
        assert touched.phi_status_policy == policy
        assert untouched.phi_status is PhiStatus.REMEDIATED
        assert untouched.phi_status_policy is None

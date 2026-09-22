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
`isocenter.configuration`, the store in `isocenter.persistence`, and
the scan tally a kept report carries in `isocenter.remediation`; see
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
#: Study ID is `Z` in PS3.15 Table E.1-1, so the floor and `basic` both
#: empty it. The two split-report tests below assert the value the
#: remediated tag holds; Institution Name is `X/Z/D`, whose value moved
#: from empty to the dummy in #557, which is not what they pin (L11's
#: rebase onto #750).
STUDY_ID = "0020,0010"


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
        # Review of #750, finding 2: true of a bare list, and of a report
        # whose scan did run but that no longer carries it.
        assert ("1 with no recorded policy (written before 1.0, or "
                "remediated from findings that are not a whole audit() "
                "report)") in notice


def test_a_kept_report_speaks_for_every_session_it_is_handed_to(tmp_path):
    """One kept report, handed to two reopened sessions in turn, neither of
    which saves: each pass settles against the report's tally as its audit
    left it. Kills: a reopened pass settling the report's own tally in
    place, which drained it, so the second session adopted nothing."""
    db = str(tmp_path / "s.db")
    report, policy = _audited_and_closed(tmp_path, db)
    for _ in range(2):
        with DicomSession(db) as session:
            [inst] = _instances(session)
            session.anonymize(report)
            assert inst.phi_status is PhiStatus.REMEDIATED
            assert inst.phi_status_policy == policy


# --- the same pass in the scanning session and across a reopen --------------
#
# The ruling on #750 (re-review at 17f002c1): a pass over a kept report
# records exactly what the same pass would have recorded in the session
# that scanned it. `audit()` puts its scan tally on the report; a reopened
# pass settles against it, demoting what it leaves unsettled, and the
# report's policy goes only to entities the scan raised under. Each case
# runs twice -- in the scanning session, and across a reopen -- and the
# statuses, policies, notices and grade must be identical.

NESTED_SEQ = "0040,0275"      # Request Attributes Sequence
NESTED_STEP = "0040,0007"     # Scheduled Procedure Step Description


def _write_ct(folder, name="a.dcm", nested=False, sop_suffix=""):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    if nested:
        from pydicom.dataset import Dataset
        from pydicom.sequence import Sequence
        item = Dataset()
        item.ScheduledProcedureStepDescription = "Nested^PHI"
        ds.RequestAttributesSequence = Sequence([item])
    if sop_suffix:
        ds.SOPInstanceUID = ds.SOPInstanceUID + sop_suffix
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    folder.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(folder / name))
    return str(folder)


def _snapshot(session):
    return [(level, entity.phi_status.value,
             entity.phi_status_policy and entity.phi_status_policy.fingerprint)
            for level, entity in _entities(session)]


def _outcome(session, root):
    snapshot = _snapshot(session)
    out = root / "out"
    session.export(str(out))
    notices = [n.replace(str(out), "<out>") for n in _notices(session)]
    return {"statuses": snapshot, "notices": notices,
            "grade": _grade(session, root)}


def _both_ways(tmp_path, *, nested=False, config=None, mutate=None,
               before=None, passes=None):
    """`(in_session, reopened)` outcomes of one flow.

    In session: ingest, save, `audit()`, then `before(session, root)` and
    `mutate(report)`, then `anonymize(report)`. Reopened: the same, but the
    session closes after `audit()` without saving it, and a new session
    over the same store runs `before`, `mutate` and `anonymize` (#644's
    flow). `mutate` may return a replacement report. `passes`, in place of
    `mutate`, is one mutation per `anonymize(report)` call, in order.
    """
    steps = passes or [mutate]
    results = []
    for mode in ("in_session", "reopened"):
        root = tmp_path / mode
        db = str(root / "s.db")
        folder = _write_ct(root / "in", nested=nested)
        with DicomSession(db) as session:
            session.ingest(folder)
            session.save(sync=True)
            if config:
                session.load_config(config(tmp_path))
            report = session.audit()
            if mode == "in_session":
                if before:
                    before(session, root)
                for step in steps:
                    report = (step(report) if step else None) or report
                    session.anonymize(report)
                results.append(_outcome(session, root))
                continue
        with DicomSession(db) as session:
            if config:
                session.load_config(config(tmp_path))
            if before:
                before(session, root)
            for step in steps:
                report = (step(report) if step else None) or report
                session.anonymize(report)
            results.append(_outcome(session, root))
    return results


def _withdraw_institution(report):
    [finding] = [f for f in report.findings if f.tag == INSTITUTION]
    finding.remediation_proposal = None


def _narrow_institution(report):
    report.findings[:] = [f for f in report.findings if f.tag != INSTITUTION]


def _nested_config(tmp_path):
    return _write_config(
        tmp_path, "nested.yaml", privacy_profile="none",
        phi_tags={INSTITUTION: {"action": "REPLACE"},
                  NESTED_STEP: {"action": "REPLACE"}})


def _remove_the_nested_item(session, root):
    [inst] = _instances(session)
    inst.sequences[NESTED_SEQ].items.clear()


def _ingest_a_second_instance(session, root):
    session.ingest(_write_ct(root / "later", sop_suffix=".9"))


def _pickled(report):
    import pickle
    return pickle.loads(pickle.dumps(report))


def test_a_withdrawn_proposal_is_settled_as_in_the_scanning_session(tmp_path):
    """Re-review (a): a finding kept in the report with its proposal set to
    None. At 17f002c1 the reopened pass read REMEDIATED under the floor
    over JFK IMAGING CENTER, no notice, PASS; in the scanning session the
    tally demotes the instance to IDENTIFIED. Kills: the carried tally
    ignored."""
    in_session, reopened = _both_ways(tmp_path, mutate=_withdraw_institution)
    assert ("instance", "identified") in {s[:2] for s in in_session["statuses"]}
    assert reopened == in_session


def test_a_carry_to_an_instance_the_scan_never_saw_adopts_nothing(tmp_path):
    """Re-review (b), as a flow: a second instance of the same patient,
    ingested after the audit, is reached by the patient-level carry. The
    scan never saw it: in the scanning session its status carries no
    policy, and across a reopen it must not adopt the report's."""
    in_session, reopened = _both_ways(tmp_path, before=_ingest_a_second_instance)
    instances = [s for s in in_session["statuses"] if s[0] == "instance"]
    assert len(instances) == 2 and None in {s[2] for s in instances}
    assert reopened == in_session


def test_the_reviewers_carry_sequence_gives_the_unseen_instance_no_policy(
        tmp_path):
    """Re-review (b), S1-S4 as written: I2 remediated from a bare list
    (REMEDIATED, no policy) and saved; then `anonymize(r1)` from a session
    whose audit never saw I2. At 17f002c1 I2 adopted r1's policy while it
    still held JFK IMAGING CENTER, and the export graded PASS."""
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:                                # S1
        session.ingest(_write_ct(tmp_path / "i1"))
        session.save(sync=True)
        r1 = session.audit()
    with DicomSession(db) as session:                                # S2
        session.ingest(_write_ct(tmp_path / "i2", sop_suffix=".9"))
        session.save(sync=True)
        r2 = session.audit()
        i2_uid = [i for i in _instances(session)
                  if i.sop_instance_uid.endswith(".9")][0].sop_instance_uid
    with DicomSession(db) as session:                                # S3
        session.anonymize([f for f in r2.findings
                           if f.entity_uid == i2_uid and f.tag != INSTITUTION])
        session.save(sync=True)
    with DicomSession(db) as session:                                # S4
        session.anonymize(r1)
        [i2] = [i for i in _instances(session) if i.sop_instance_uid == i2_uid]
        assert i2.attributes.get(INSTITUTION) == "JFK IMAGING CENTER"
        assert i2.phi_status_policy is None
        session.export(str(tmp_path / "out"))
        assert _notices(session)
        assert "REVIEW_REQUIRED" in _grade(session, tmp_path)


def test_two_complementary_passes_complete_as_in_the_scanning_session(
        tmp_path):
    """Third review of #750, at 934b794a: the report narrowed in place to
    everything but Institution Name, anonymized, then narrowed to Institution
    Name alone and anonymized again -- the documented partial workflow. In
    the scanning session the second pass completes the first (#553's
    `_partial`): REMEDIATED, PASS. Across a reopen each call took a fresh
    copy of the report's tally, the second pass had lost the first's
    progress, and the instance read IDENTIFIED, REVIEW_REQUIRED. Kills: a
    fresh copy of the tally per call."""
    kept = {}

    def all_but_institution(report):
        kept.setdefault(id(report), list(report.findings))
        report.findings[:] = [f for f in kept[id(report)]
                              if f.tag != INSTITUTION]

    def institution_only(report):
        report.findings[:] = [f for f in kept[id(report)]
                              if f.tag == INSTITUTION]

    in_session, reopened = _both_ways(
        tmp_path, passes=[all_but_institution, institution_only])
    assert {s[1] for s in in_session["statuses"]} == {"remediated"}
    assert "PASS" in in_session["grade"]
    assert reopened == in_session


def test_a_copy_split_completes_as_in_the_scanning_session(tmp_path):
    """Fourth review of #750, at 4f28e5c1: a report split with `copy.copy`
    into complementary halves -- all but Institution Name, then Institution
    Name alone -- each anonymized in turn. Both halves carry the audit's
    tally; in the scanning session both settle against its one tally and
    the instance ends REMEDIATED, PASS. Keyed per report, the reopened
    session gave each half its own working copy and read IDENTIFIED,
    REVIEW_REQUIRED. Kills: a working copy per call or per report."""
    import copy
    audit = {}   # the report each mode's audit returned; the first step sets it

    def half(keep, first=False):
        def step(report):
            if first:
                audit["report"] = report
            part = copy.copy(audit["report"])
            part.findings = [f for f in audit["report"].findings if keep(f)]
            return part
        return step

    in_session, reopened = _both_ways(tmp_path, passes=[
        half(lambda f: f.tag != INSTITUTION, first=True),
        half(lambda f: f.tag == INSTITUTION)])
    assert {s[1] for s in in_session["statuses"]} == {"remediated"}
    assert "PASS" in in_session["grade"]
    assert reopened == in_session


def _deepcopy_split(audit):
    import copy
    return copy.deepcopy(audit["report"])


def _pickled_half(audit):
    import copy
    return _pickled(copy.copy(audit["report"]))


def _loaded_from_disk(audit):
    import pickle
    audit.setdefault("blob", pickle.dumps(audit["report"]))
    return pickle.loads(audit["blob"])


@pytest.mark.parametrize("make_half", [
    _deepcopy_split, _pickled_half, _loaded_from_disk],
    ids=["deepcopy", "pickled_halves", "kept_on_disk_loaded_per_step"])
def test_a_split_whose_halves_carry_equal_tallies_completes_as_in_the_scanning_session(
        tmp_path, make_half):
    """Fifth review of #750, at 6658dbc4: the halves of a split carry the
    same audit's tally but not the same *object* -- `copy.deepcopy`, each
    `copy.copy` half pickled, or the report pickled once after `audit()`
    and loaded from those bytes for each step (a report kept on disk). In
    the scanning session both halves settle against its one tally:
    REMEDIATED, PASS. Keyed by tally identity, the reopened session gave
    each half its own working copy and read IDENTIFIED, REVIEW_REQUIRED.
    Kills: a working copy keyed by anything narrower than the audit, and
    an audit token that `copy()`, pickle or deepcopy does not carry."""
    audit = {}   # the report each mode's audit returned; the first step sets it

    def half(keep, first=False):
        def step(report):
            if first:
                audit.clear()
                audit["report"] = report
            part = make_half(audit)
            part.findings = [f for f in part.findings if keep(f)]
            return part
        return step

    in_session, reopened = _both_ways(tmp_path, passes=[
        half(lambda f: f.tag != INSTITUTION, first=True),
        half(lambda f: f.tag == INSTITUTION)])
    assert {s[1] for s in in_session["statuses"]} == {"remediated"}
    assert "PASS" in in_session["grade"]
    assert reopened == in_session


def test_a_tally_keeps_its_audit_through_copy_pickle_and_deepcopy():
    """The contract `Session._working_tally` keys by (fifth review of
    #750): a tally names its audit, and every way a report's tally is
    reproduced keeps that name, while another audit's tally has its own.
    `copy()`'s carry is not observable through a session today -- the
    session keys by the report's tally, and `audit()` copies once -- so it
    is pinned here, where a later caller of `copy()` would rely on it.
    Kills: `copy()` not carrying the token, or minting a fresh one."""
    import copy
    import pickle
    from isocenter.remediation import _ScanTally
    tally = _ScanTally(())
    for same in (tally.copy(), copy.deepcopy(tally),
                 pickle.loads(pickle.dumps(tally))):
        assert same._audit == tally._audit
    assert _ScanTally(())._audit != tally._audit


def test_two_audits_split_across_a_reopen_fail_closed(tmp_path):
    """The one known difference from the scanning session (fourth review of
    #750): two audits, the first's report narrowed to all but Study ID,
    the second's to Study ID alone. In the scanning session
    both passes settle against the *latest* audit's tally, which the second
    completes: REMEDIATED, PASS. After a reopen each report brings its own
    audit's tally, and neither is told which came last, so neither
    completes: the instance stays IDENTIFIED under the scan's policy and
    the grade is REVIEW_REQUIRED. The output is the same. Pinned so a
    later change cannot quietly turn it into a PASS that no single tally
    supports."""
    db = str(tmp_path / "s.db")
    folder = _write_ct(tmp_path / "in")
    with DicomSession(db) as session:
        session.ingest(folder)
        session.save(sync=True)
        r1 = session.audit()
        r2 = session.audit()
        policy = session.configuration._scan_policy()
    r1.findings[:] = [f for f in r1.findings if f.tag != STUDY_ID]
    r2.findings[:] = [f for f in r2.findings if f.tag == STUDY_ID]
    with DicomSession(db) as session:
        session.anonymize(r1)
        session.anonymize(r2)
        [inst] = _instances(session)
        assert inst.attributes.get(STUDY_ID) == ""
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert inst.phi_status_policy == policy
        session.export(str(tmp_path / "out"))
        assert "REVIEW_REQUIRED" in _grade(session, tmp_path)


def test_one_entitys_findings_split_across_two_sessions_fail_closed(tmp_path):
    """The second known difference from the scanning session (sixth review
    of #750): one report's findings for one instance split in two -- all but
    Study ID, then Study ID alone. In the scanning session
    the second pass completes the first (#553's `_partial`): REMEDIATED,
    PASS. Here the first half is applied in one reopened session, which
    saves, and the second in another: that session's working copy starts
    from the audit, the first session's partial progress on the instance
    died with it, and the second pass handles one of its keys, so the
    instance stays IDENTIFIED and the grade is REVIEW_REQUIRED. Study ID
    is still emptied. Closing it would need the working tally stored,
    which the design excludes. Pinned so a later change cannot quietly turn
    it into a PASS that no tally supports."""
    db = str(tmp_path / "s.db")
    folder = _write_ct(tmp_path / "in")
    with DicomSession(db) as session:
        session.ingest(folder)
        session.save(sync=True)
        report = session.audit()
        policy = session.configuration._scan_policy()
    every = list(report.findings)
    with DicomSession(db) as session:
        report.findings[:] = [f for f in every if f.tag != STUDY_ID]
        session.anonymize(report)
        session.save(sync=True)
    with DicomSession(db) as session:
        report.findings[:] = [f for f in every if f.tag == STUDY_ID]
        session.anonymize(report)
        [inst] = _instances(session)
        assert inst.attributes.get(STUDY_ID) == ""
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert inst.phi_status_policy == policy
        session.export(str(tmp_path / "out"))
        assert "REVIEW_REQUIRED" in _grade(session, tmp_path)


def test_a_narrowed_report_is_settled_as_in_the_scanning_session(tmp_path):
    """Review of #750, finding 1: `report.findings[:]` without Institution
    Name. The tally demotes the instance in both sessions."""
    in_session, reopened = _both_ways(tmp_path, mutate=_narrow_institution)
    assert ("instance", "identified") in {s[:2] for s in in_session["statuses"]}
    assert reopened == in_session


def test_a_removed_nested_item_is_settled_as_in_the_scanning_session(tmp_path):
    """The nested finding's item is removed before `anonymize()`: its key
    is settled as gone (#644) in both sessions, and the outcomes match."""
    in_session, reopened = _both_ways(
        tmp_path, nested=True, config=_nested_config,
        before=_remove_the_nested_item)
    assert reopened == in_session


def test_a_pickled_report_is_settled_as_in_the_scanning_session(tmp_path):
    """A whole report through a pickle round trip: its tally and policy
    travel with it, and the reopened pass matches the scanning session's --
    REMEDIATED under the scan's policy, no notice."""
    in_session, reopened = _both_ways(tmp_path, mutate=_pickled)
    assert {s[1] for s in in_session["statuses"]} == {"remediated"}
    assert in_session["notices"] == []
    assert reopened == in_session


def test_a_report_applied_to_another_store_gives_its_instance_no_policy(
        tmp_path):
    """A report from store A applied to store B, which holds A's patient
    and study but another instance. The scan never saw B's instance: it
    is given no policy, and the report grades REVIEW_REQUIRED, whether A's
    session is still open or closed (the report's tally is the same object
    either way). Measured: B's instance is not reached and stays
    UNSCANNED; A's patient-level keys do not settle on B's patient, which
    is demoted to IDENTIFIED under the scan's policy."""
    outcomes = []
    for mode in ("a_open", "a_closed"):
        root = tmp_path / mode
        root.mkdir()
        a = DicomSession(str(root / "a.db"))
        try:
            a.ingest(_write_ct(root / "a"))
            a.save(sync=True)
            report = a.audit()
            if mode == "a_closed":
                a.close()
            with DicomSession(str(root / "b.db")) as b:
                b.ingest(_write_ct(root / "b", sop_suffix=".9"))
                b.anonymize(report)
                [inst] = _instances(b)
                assert inst.phi_status_policy is None
                outcomes.append(_outcome(b, root))
        finally:
            if mode == "a_open":
                a.close()
    assert "REVIEW_REQUIRED" in outcomes[0]["grade"]
    assert outcomes[0] == outcomes[1]


# --- the tally travels between processes -----------------------------------

def test_the_key_hash_is_pinned_and_types_stay_apart():
    """The tally's digest is the same in every process and interpreter:
    blake2b over a canonical, type-tagged spelling. Pinned by value, so
    `hash()` (salted per process) cannot come back unnoticed. Kills: the
    digest restored to `hash(key)`."""
    from isocenter.remediation import _canonical_key, _key_hash
    key = ("1.2.3", (("0040,0275", 0),), "0040,0007")
    assert _canonical_key(key) == '["1.2.3",[["0040,0275",0]],"0040,0007"]'
    assert _key_hash(key) == KEY_HASH
    assert len({_canonical_key(v) for v in (1, True, "1", None)}) == 4
    # A key JSON cannot spell (only a hand-built finding's) still gets one
    # spelling per value, apart from every JSON spelling.
    assert _canonical_key(("1.2.3", 1.5j)) == '~t(s"1.2.3",ocomplex:1.5j)'


#: blake2b(digest_size=8) of the key above's canonical spelling, as a
#: little-endian int. Computed once and pasted: it must never change.
KEY_HASH = 16290191241151387406


def test_a_pickled_tally_settles_alike_in_a_child_process(tmp_path):
    """A real tally, pickled into a spawned child under another
    `PYTHONHASHSEED`, settles each uid as it does in the parent. Measured
    before the digest was stable: every uid settled False in the child
    (ruling on #750, condition 2)."""
    import os
    import pickle
    import subprocess
    import sys
    from isocenter.remediation import _remediation_key
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_input(tmp_path, "CT_small.dcm"))
        report = session.audit()
        tally = report._scan_tally
        keys = {}
        for finding in report.findings:
            if finding.remediation_proposal is not None:
                keys.setdefault(finding.entity_uid, set()).add(
                    _remediation_key(finding))
    path = tmp_path / "tally.pkl"
    path.write_bytes(pickle.dumps((tally, keys)))
    parent = {uid: pickle.loads(path.read_bytes())[0].settle(uid, k)
              for uid, k in keys.items()}
    assert parent and set(parent.values()) == {True}
    child_code = (
        "import json, pickle, sys\n"
        "tally, keys = pickle.load(open(sys.argv[1], 'rb'))\n"
        "print(json.dumps({u: tally.settle(u, k) for u, k in keys.items()}))\n")
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run([sys.executable, "-c", child_code, str(path)],
                             env=env, capture_output=True, text=True,
                             check=True)
        assert json.loads(run.stdout) == parent, seed


def test_only_a_status_the_pass_recorded_adopts_the_reports_policy(tmp_path):
    """Kills: the adoption relabelling a policy-less status the pass did not
    record -- a store's pre-1.0 REMEDIATED beside the one the report
    touched, both named by the report's tally."""
    from isocenter.privacy import PhiFinding, PhiRemediation, PhiReport
    from isocenter.remediation import _ScanTally
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

        def finding(inst):
            return PhiFinding(
                entity_uid=inst.sop_instance_uid, entity_type="Instance",
                field_name="0008,0090", value="Dr^Leak", reason="test",
                tag="0008,0090", entity=inst,
                remediation_proposal=PhiRemediation(
                    action_type="REPLACE_TAG", target_attr="0008,0090",
                    new_value="ANON", original_value="Dr^Leak"))
        report = PhiReport([finding(touched)])
        report._scan_policy = policy
        report._scan_tally = _ScanTally([finding(touched), finding(untouched)])
        assert session.anonymize(report) == 1
        assert touched.attributes["0008,0090"] == "ANON"
        assert touched.phi_status_policy == policy
        assert untouched.phi_status is PhiStatus.REMEDIATED
        assert untouched.phi_status_policy is None

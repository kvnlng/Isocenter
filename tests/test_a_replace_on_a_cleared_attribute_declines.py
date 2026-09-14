"""`REPLACE` with a value declines on a Python attribute the caller cleared
to None (#625).

The arm that writes `Patient.patient_name`, `Patient.patient_id`,
`Study.study_date`, `Study.study_time` and `Series.modality` tested
`hasattr(entity, target_attr)`, which is True of a slots field holding
None. So `0008,0020: {action: REPLACE, value: "19000101"}` over a Study
whose date was set to None between `audit()` and `anonymize()` wrote
`date(1900, 1, 1)` into it, filed `REMEDIATION_REPLACE ... written to 1
instance copy`, exported the date and graded PASS -- #569's re-creation
on the attribute arm, where `_replace_on_item` has declined a vanished
item target since #547. Measured on a67eb30, 3.12 threads and 3.14t
processes, for every attribute the arm reaches.

**The rule these tests hold.** A slot holding None refuses a non-empty
value with one `REMEDIATION_DECLINED` row that names the attribute and
the type and never a value, leaves the attribute None, does not stamp
the entity, and grades the run REVIEW_REQUIRED. `EMPTY` on the same slot
still writes `""` with its success row: the exporter writes None and
`""` as the same zero-length element, so nothing is fabricated. A name
the entity lacks refuses as it always did (`has no attribute or
setter`), which `test_declined_remediation_is_recorded.py`'s `_Bare`
REPLACE test pins; it is cited rather than duplicated here.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, the arm through `isocenter.remediation`, the
hand-built findings through `isocenter.privacy` and the graph through
`isocenter.entities`, so it charges those four modules' probe rows; see
`test_mutation_probe_targets.py`.
"""
import datetime
import os
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter.entities import Patient, PhiStatus, Series, Study
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret

STUDY_DATE = "0008,0020"
ROOT = "1.2.826.0.1.3680043.10.625"
STUDY_UID = f"{ROOT}.1"
SOP_UID = f"{ROOT}.1.1.1"
VALUE = "19000101"

MODES = ["threads", "processes"]


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


class _Rows:
    """A store stand-in that keeps the audit rows and nothing else."""

    def __init__(self):
        self.rows = []

    def log_audit_batch(self, rows):
        self.rows.extend(rows)

    def log_audit(self, *row):
        self.rows.append(row)


def _finding(entity, uid, entity_type, attr, new_value, original="orig"):
    return PhiFinding(
        entity_uid=uid, entity_type=entity_type, field_name=attr,
        value=original, reason="test", tag=attr, entity=entity,
        remediation_proposal=PhiRemediation(
            action_type="REPLACE_TAG", target_attr=attr, new_value=new_value,
            original_value=original, metadata={"patient_id": "P1"}))


def _apply(entity, uid, entity_type, attr, new_value):
    """One hand-built REPLACE through `apply_remediation`, with its rows."""
    rows = _Rows()
    service = RemediationService(store_backend=rows, project_secret=FIXED_A)
    applied = service.apply_remediation(
        [_finding(entity, uid, entity_type, attr, new_value)])
    return applied, rows.rows


def _declines(rows):
    return [details for action, _uid, details, _scope, _tag in rows
            if action == "REMEDIATION_DECLINED"]


def _session(tmp_path, action="REPLACE"):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PAT-625"
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.SOPInstanceUID = SOP_UID
    ds.file_meta.MediaStorageSOPInstanceUID = SOP_UID
    src = tmp_path / "src"
    src.mkdir()
    ds.save_as(str(src / "a.dcm"))
    session = DicomSession(str(tmp_path / "m.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    rule = {"name": "study date", "action": action}
    if action == "REPLACE":
        rule["value"] = VALUE
    session.configuration.phi_tags = {STUDY_DATE: rule}
    session.configuration.remove_private_tags = False
    session.ingest(str(src))
    return session


def _graph(session):
    study = session.store.patients[0].studies[0]
    return study, study.series[0].instances[0]


def _rows(session, action_type):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if "**Grade Basis:**" in line]


def _exported(session, tmp_path):
    out = tmp_path / "out"
    session.export(str(out), use_compression=False)
    files = [os.path.join(root, name) for root, _, names in os.walk(out)
             for name in names if name.endswith(".dcm")]
    assert len(files) == 1, files
    return pydicom.dcmread(files[0], stop_before_pixels=True)


# ---------------------------------------------------------------------------
# T1: the issue as filed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_study_date_cleared_after_the_audit_is_not_recreated_by_replace(
        tmp_path, mode):
    """The issue as filed. Red before: `study_date` came back as
    `date(1900, 1, 1)`, with a `REMEDIATION_REPLACE` row that claimed a
    fold, REMEDIATED, PASS, and `19000101` in the exported file.

    The instance's own copy of the tag is not folded into a write that
    did not happen, so it takes the rule's value by itself and the
    instance reads REMEDIATED: the graph and the file then disagree the
    way they do when a Study's shift declines (#624, accepted for 1.0).
    The exported Study Date is stamped from the Study, so it is empty.
    """
    session = _session(tmp_path)
    with session:
        report = session.audit()
        study, instance = _graph(session)
        # Non-vacuity: the scan raised the Study-level REPLACE.
        assert [f.remediation_proposal.action_type for f in report.findings
                if f.entity is study and f.tag == STUDY_DATE] == ["REPLACE_TAG"]
        assert study.study_date is not None
        study.study_date = None
        session.anonymize(report)

        assert study.study_date is None, mode
        declines = [d for u, d in _rows(session, "REMEDIATION_DECLINED")
                    if u == STUDY_UID]
        assert len(declines) == 1, declines
        assert "study_date is no longer set on the Study" in declines[0], declines
        assert "1900" not in declines[0], declines
        assert [d for u, d in _rows(session, "REMEDIATION_REPLACE")
                if u == STUDY_UID] == []
        assert study.phi_status is PhiStatus.IDENTIFIED
        assert instance.attributes[STUDY_DATE] == VALUE
        assert instance.phi_status is PhiStatus.REMEDIATED
        grade = _grade(session, tmp_path)
        assert len(grade) == 1 and "REVIEW_REQUIRED" in grade[0], grade
        assert _exported(session, tmp_path).StudyDate == ""


# ---------------------------------------------------------------------------
# T2-T5: the arm is generic, so the rule is
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("build", [
    pytest.param(lambda: (Patient("P1", None), "P1", "Patient", "patient_name", "ANONYMIZED"),
                 id="patient_name"),
    pytest.param(lambda: (Patient(None, "Doe^J"), "P1", "Patient", "patient_id", "ANON_X"),
                 id="patient_id"),
    pytest.param(lambda: (Series("1.2.3.4", None, 1), "1.2.3.4", "Series", "modality", "OT"),
                 id="modality"),
    pytest.param(lambda: (Study("1.2.3", None), "1.2.3", "Study", "study_date", VALUE),
                 id="study_date"),
    pytest.param(lambda: (Study("1.2.3", "20040119"), "1.2.3", "Study", "study_time", "120000"),
                 id="study_time"),
])
def test_a_none_attribute_refuses_a_value(build):
    """Every field the arm reaches, at None, under a value: one decline
    naming the attribute and the type, the field still None, nothing
    applied, the status untouched. Red before on all five: written,
    `REMEDIATION_REPLACE`, REMEDIATED."""
    entity, uid, entity_type, attr, value = build()
    before = entity.phi_status

    applied, rows = _apply(entity, uid, entity_type, attr, value)

    assert applied == 0
    assert getattr(entity, attr) is None
    assert [a for a, *_ in rows] == ["REMEDIATION_DECLINED"], rows
    declined = _declines(rows)
    assert f"{attr} is no longer set on the {entity_type}" in declined[0], declined
    assert value not in declined[0], declined
    assert entity.phi_status is before


@pytest.mark.parametrize("build", [
    pytest.param(lambda: (Study("1.2.3", None), "1.2.3", "Study", "study_date"),
                 id="study"),
    pytest.param(lambda: (Patient("P1", None), "P1", "Patient", "patient_name"),
                 id="patient"),
])
def test_empty_on_a_none_attribute_writes_an_empty_value(build):
    """`EMPTY` on a None slot is not refused (owner ruling Q2): it writes
    `""`, which the exporter writes as the same zero-length element None
    would be, with its success row and a REMEDIATED stamp, as before.

    Kills: the None check refusing every value, `""` included.
    """
    entity, uid, entity_type, attr = build()

    applied, rows = _apply(entity, uid, entity_type, attr, "")

    assert applied == 1
    assert getattr(entity, attr) == ""
    assert [a for a, *_ in rows] == ["REMEDIATION_REPLACE"], rows
    assert entity.phi_status is PhiStatus.REMEDIATED


def test_a_present_attribute_is_still_written():
    """The check reads None and nothing else: a set date takes the value,
    and a present-but-empty name does too, as `_replace_on_item` writes a
    present empty element."""
    study = Study("1.2.3", "20040119")
    applied, rows = _apply(study, "1.2.3", "Study", "study_date", VALUE)
    assert applied == 1
    assert study.study_date == datetime.date(1900, 1, 1)
    assert [a for a, *_ in rows] == ["REMEDIATION_REPLACE"], rows

    patient = Patient("P1", "")
    applied, rows = _apply(patient, "P1", "Patient", "patient_name", "ANONYMIZED")
    assert applied == 1
    assert patient.patient_name == "ANONYMIZED"
    assert [a for a, *_ in rows] == ["REMEDIATION_REPLACE"], rows


def test_a_patient_decline_names_no_patient_in_the_log(caplog):
    """The WARNING says `a patient`, as every decline in the arm does,
    and carries neither the Patient ID nor the value the rule would have
    written. The row keeps the ID: the store is guarded, the log is not.
    """
    patient = Patient("PAT-625", None)
    with caplog.at_level("WARNING", logger="isocenter"):
        _apply(patient, "PAT-625", "Patient", "patient_name", "ANONYMIZED")

    logged = [r.getMessage() for r in caplog.records
              if "Remediation declined" in r.getMessage()]
    assert len(logged) == 1, logged
    assert "a patient" in logged[0], logged
    assert "PAT-625" not in logged[0], logged
    assert "ANONYMIZED" not in logged[0], logged
    assert "patient_name is no longer set on the Patient" in logged[0], logged


# ---------------------------------------------------------------------------
# T6: the reuse path is untouched
# ---------------------------------------------------------------------------

def test_one_report_applied_twice_under_a_replace_rule_declines_nothing(
        tmp_path):
    """`anonymize(report)` twice under a REPLACE-value rule on a live Study
    writes no decline: the second call meets a Study that holds the
    value, not None, and writes it again."""
    session = _session(tmp_path)
    with session:
        report = session.audit()
        study, _ = _graph(session)
        assert [f for f in report.findings
                if f.entity is study and f.tag == STUDY_DATE]
        session.anonymize(report)
        session.anonymize(report)

        assert study.study_date == datetime.date(1900, 1, 1)
        assert _rows(session, "REMEDIATION_DECLINED") == []

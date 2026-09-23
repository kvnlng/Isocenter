"""An edit of a Patient, Study or Series field is a change (#767, #768).

Assigning `patient.patient_name`, `study.study_date` or any field the
export writes from an owner, or the scan reads on one, left `_revision`
where it was. So a status recorded before the edit kept reading
REMEDIATED or CLEARED over a value no scan had read -- the export stamped
the real name and the run graded PASS -- and the save walk, which writes
an owner row only when it holds unsaved changes, never stored the edit:
a reopen brought the old value back.

An assignment of a tracked field that changes its value now advances the
revision (`mark_modified()`), after the value is in place. The status
reads UNSCANNED, and grade condition 8 names every owner edited since its
status was recorded, until a scan reads it again (owner ruling on #767,
2026-09-23: (C), both levers). #624's sync of an instance copy to a value
no record vouches for records the instance IDENTIFIED rather than handing
back the status it had.
"""
import copy
import datetime
import pickle
import re

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import (Equipment, Patient, PhiStatus, Series,
                                Study, TrackedEntity)

from support.ct_small_files import write_ct

EDITED = "edited after the last PHI scan"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _patient():
    return Patient("PID-767", "Alpha^One")


def _study():
    return Study("1.2.3", datetime.date(2003, 3, 6), study_time="120000")


def _series():
    return Series("1.2.3.4", "CT", 1)


#: Every tracked field: (builder, field, a value different from the one
#: the builder holds). Series.equipment is tracked for persistence
#: (coordinator ruling Q1): the save writes it and redaction matches on it.
EDITS = [
    (_patient, "patient_id", "PID-OTHER"),
    (_patient, "patient_name", "Beta^Two"),
    (_study, "study_instance_uid", "1.2.3.9"),
    (_study, "study_date", datetime.date(2020, 2, 2)),
    (_study, "study_time", "130000"),
    (_series, "series_instance_uid", "1.2.3.4.9"),
    (_series, "modality", "MR"),
    (_series, "series_number", 2),
    (_series, "equipment", Equipment.from_parts("Acme", "Golden", "SN-2")),
]


def _scanned(entity):
    entity.record_phi_status(PhiStatus.REMEDIATED)
    entity.mark_persisted()
    return entity


@pytest.mark.parametrize("build, name, value", EDITS,
                         ids=[f"{b.__name__[1:]}.{n}" for b, n, _ in EDITS])
def test_an_edit_of_a_tracked_field_is_a_change(build, name, value):
    """The status recorded before the edit stops describing the entity,
    and the store is told there is something to write. Kills the bump
    dropped in each class."""
    entity = _scanned(build())
    before = entity._revision
    setattr(entity, name, value)
    assert entity._revision > before
    assert entity.phi_status is PhiStatus.UNSCANNED
    assert entity.has_unsaved_changes


@pytest.mark.parametrize("build, name, value", EDITS,
                         ids=[f"{b.__name__[1:]}.{n}" for b, n, _ in EDITS])
def test_the_same_value_again_is_not_a_change(build, name, value):
    """Assigning the value the entity already holds changes nothing, as
    recording the status it already carries changes nothing: a graph
    re-assigned in place is not dirtied. Kills the equality check dropped."""
    entity = build()
    setattr(entity, name, value)
    entity = _scanned(entity)
    before = entity._revision
    setattr(entity, name, copy.copy(value))
    assert entity._revision == before
    assert entity.phi_status is PhiStatus.REMEDIATED
    assert not entity.has_unsaved_changes


def test_a_study_date_is_compared_as_it_is_held():
    """`Study.study_date` is normalised on assignment (#189): the DA string
    of the date it holds is the same value, not an edit."""
    study = _scanned(_study())
    before = study._revision
    study.study_date = "20030306"
    assert study._revision == before
    assert study.phi_status is PhiStatus.REMEDIATED


@pytest.mark.parametrize("build", [_patient, _study, _series])
def test_construction_is_not_an_edit(build):
    """The constructor assigns each field once, into an empty slot. Kills
    an unset slot read as a change."""
    assert build()._revision == 1


@pytest.mark.parametrize("build", [_patient, _study, _series])
def test_a_copy_keeps_the_status(build):
    """Pickle (the worker hand-off) and deepcopy restore the fields
    without reaching the hook: the copy reads the status it was given."""
    entity = _scanned(build())
    for clone in (pickle.loads(pickle.dumps(entity)), copy.deepcopy(entity)):
        assert clone.phi_status is PhiStatus.REMEDIATED
        assert clone._revision == entity._revision


def test_the_value_is_in_place_before_the_revision_moves(monkeypatch):
    """A background save captures the revision before it reads the fields.
    Moved first, a save between the two statements would capture the new
    revision, read the old value and mark it persisted -- the edit lost.
    So when `mark_modified` runs, the new value is already there. Kills
    the bump moved ahead of the write."""
    seen = []
    original = TrackedEntity.mark_modified

    def spy(self):
        seen.append(self.patient_name)
        original(self)

    monkeypatch.setattr(TrackedEntity, "mark_modified", spy)
    patient = _patient()
    patient.patient_name = "Beta^Two"
    assert seen == ["Beta^Two"]


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def _saved(tmp_path, suffix="5767"):
    write_ct(tmp_path / "in" / "a.dcm", "PID-767", suffix, name="Alpha^One")
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    return session


def _owners(session):
    [patient] = session.store.patients
    [study] = patient.studies
    [series] = study.series
    return patient, study, series


def test_a_reopened_store_is_clean_and_keeps_its_statuses(tmp_path):
    """Hydration assigns `Series.equipment` after construction; that runs
    before the statuses are restored and the subtree is marked persisted,
    so a reopened store reads clean with every status it saved."""
    with _saved(tmp_path) as session:
        session.anonymize(session.audit())
        session.save(sync=True)
        expected = [e.phi_status for e in _owners(session)]
    with Session(str(tmp_path / "s.db")) as session:
        owners = _owners(session)
        assert [e.phi_status for e in owners] == expected
        assert not any(e.has_unsaved_changes for e in owners)


def test_an_owner_edit_in_a_reopened_store_reaches_the_store(tmp_path):
    """The save writes an owner row only when the owner holds unsaved
    changes. In the session that ingested it an owner is never marked
    persisted (the save walk marks instances only, #307), so its every
    save rewrote it and hid this; a reopened store's owners are hydrated
    clean, and an untracked edit there was never written -- the next
    reopen brought the old value back."""
    serial = Equipment.from_parts("Acme", "Golden", "SN-767")
    with _saved(tmp_path) as session:
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        patient, study, series = _owners(session)
        patient.patient_name = "Beta^Two"
        study.study_date = datetime.date(2020, 2, 2)
        series.series_number = 7
        series.equipment = serial
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        patient, study, series = _owners(session)
        assert patient.patient_name == "Beta^Two"
        assert study.study_date == datetime.date(2020, 2, 2)
        assert series.series_number == 7
        assert series.equipment.device_serial_number == "SN-767"


# ---------------------------------------------------------------------------
# The grade
# ---------------------------------------------------------------------------

def _graded(session, tmp_path, name="r.md"):
    session.export(str(tmp_path / f"out-{name}"), use_compression=False)
    session.generate_report(str(tmp_path / name))
    text = (tmp_path / name).read_text(encoding="utf-8")
    section_5 = text.split("## 5. Validation & Verification", 1)[1]
    basis = section_5.split("*   **Metadata Remediation:**", 1)[0]
    reasons = ([] if "**Grade Basis:** PASS" in basis
               else re.findall(r"^    \*   (.*)$", basis, flags=re.M))
    return reasons, ("**PASS**" in text), tmp_path / f"out-{name}"


def _edited(reasons):
    return [r for r in reasons if EDITED in r]


def test_a_name_set_back_after_a_pass_then_an_instance_only_pass_is_not_pass(tmp_path):
    """#767's probe. The owners write in pass 1; `patient.patient_name` is
    then set back to the source by plain assignment, and pass 2 hands in
    the instance findings only. The file carries the source name. It
    graded PASS: the Patient still read REMEDIATED, and the instance's
    copy was synced to the source name while it kept the REMEDIATED it
    had. Now the Patient reads UNSCANNED and condition 8 names it, and the
    instance, whose copy holds a source value no record vouches for, reads
    IDENTIFIED (condition 7). Kills (A) and (B) each."""
    with _saved(tmp_path) as session:
        report = session.audit()
        session.anonymize(report)
        patient, _study, _series = _owners(session)
        [inst] = [i for i in _series.instances]
        assert patient.phi_status is PhiStatus.REMEDIATED
        patient.patient_name = "Alpha^One"
        assert patient.phi_status is PhiStatus.UNSCANNED
        session.anonymize([f for f in report.findings if f.entity_type == "Instance"])
        assert inst.attributes["0010,0010"] == "Alpha^One"
        assert inst.phi_status is PhiStatus.IDENTIFIED
        reasons, passed, out = _graded(session, tmp_path)
    [written] = list(out.rglob("*.dcm"))
    assert str(pydicom.dcmread(str(written)).PatientName) == "Alpha^One"
    assert not passed
    assert _edited(reasons) == [
        f"1 entity {EDITED}: a field the export writes from it, or the scan "
        "reads on it, was assigned a new value after its PHI status was "
        "recorded, and no scan has read that value; `audit()` reads it "
        "(patients 1, studies 0, series 0)"], reasons
    assert any("read IDENTIFIED" in r and "instances 1" in r for r in reasons), reasons


def test_an_edit_after_a_pass_with_no_pass_since_is_not_pass(tmp_path):
    """The shape (A) cannot reach: the name set back after a full pass,
    then an export with no pass since. Only condition 8 sees it; a
    re-audit reads the name, and the line goes (condition 7 takes over,
    since the scan now finds it)."""
    with _saved(tmp_path) as session:
        session.anonymize(session.audit())
        patient, _study, _series = _owners(session)
        patient.patient_name = "Alpha^One"
        reasons, passed, _out = _graded(session, tmp_path)
        assert not passed and len(_edited(reasons)) == 1, reasons
        session.audit()
        assert patient.phi_status is PhiStatus.IDENTIFIED
        reasons, passed, _out = _graded(session, tmp_path, "r2.md")
        assert _edited(reasons) == [], reasons
        assert not passed


def test_a_study_date_cleared_after_the_audit_and_not_handed_in(tmp_path):
    """#768. The Study Date is cleared after `audit()` and the Study's
    finding is not handed to the pass. The file's date is '' -- nothing
    leaks -- and the run graded PASS with the Study reading IDENTIFIED.
    Now the Study reads UNSCANNED and condition 8 grades REVIEW_REQUIRED
    until a scan reads it (owner ruling on #767: #768 pinned under (B));
    a re-audit and a pass grade PASS."""
    with _saved(tmp_path, "5768") as session:
        report = session.audit()
        _patient, study, _series = _owners(session)
        study.study_date = None
        assert study.phi_status is PhiStatus.UNSCANNED
        session.anonymize([f for f in report.findings if f.entity_type != "Study"])
        reasons, passed, out = _graded(session, tmp_path)
        assert not passed
        assert _edited(reasons) and "studies 1" in _edited(reasons)[0], reasons
        session.anonymize(session.audit())
        reasons, passed, _out = _graded(session, tmp_path, "r2.md")
        assert _edited(reasons) == [], reasons
        assert passed, reasons
    [written] = list(out.rglob("*.dcm"))
    assert str(pydicom.dcmread(str(written)).get("StudyDate", "")) == ""


def test_a_never_scanned_owner_does_not_grade_under_condition_8(tmp_path):
    """Condition 8 is about a status an edit made stale, not about the
    absence of one (Q3): an owner edited with no scan behind it is
    UNSCANNED, counted in section 5 as before, and does not grade here."""
    with _saved(tmp_path) as session:
        patient, _study, _series = _owners(session)
        patient.patient_name = "Beta^Two"
        reasons, _passed, _out = _graded(session, tmp_path)
    assert _edited(reasons) == [], reasons


def test_an_owner_a_pass_wrote_is_not_edited(tmp_path):
    """Remediation assigns the owner's field and records REMEDIATED after
    it: the status describes the value, and nothing grades. The ordinary
    path stays PASS."""
    with _saved(tmp_path) as session:
        session.anonymize(session.audit())
        reasons, passed, _out = _graded(session, tmp_path)
    assert passed and reasons == [], reasons


def test_a_restored_identity_grades_until_a_scan_reads_it(tmp_path):
    """`recover_patient_identity(restore=True)` writes the source name and
    ID back onto the Patient, which the export then stamps into every
    file. On main the Patient read UNSCANNED and the run graded PASS with
    the real name exported; the restore is an edit after the pass, so
    condition 8 names the Patient until a scan reads it."""
    with _saved(tmp_path, "5769") as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        report = session.audit()
        session.lock_identities("PID-767")
        session.anonymize(report)
        patient, _study, _series = _owners(session)
        session.recover_patient_identity(patient.patient_id, restore=True)
        assert (patient.patient_name, patient.patient_id) == ("Alpha^One", "PID-767")
        assert patient.phi_status is PhiStatus.UNSCANNED
        reasons, passed, out = _graded(session, tmp_path)
    assert not passed
    assert _edited(reasons) and "patients 1" in _edited(reasons)[0], reasons
    [written] = list(out.rglob("*.dcm"))
    assert str(pydicom.dcmread(str(written)).PatientName) == "Alpha^One"


def test_a_series_with_a_status_is_counted_when_edited(tmp_path):
    """Condition 8 counts series: nothing on this release records a status
    on one, but a scan that reads a Series UID may (#544), and an edit then
    makes it stale like any owner's. The status is recorded by hand here."""
    with _saved(tmp_path) as session:
        session.anonymize(session.audit())
        _patient, _study, series = _owners(session)
        series.record_phi_status(PhiStatus.CLEARED)
        series.series_number = 9
        reasons, passed, _out = _graded(session, tmp_path)
    assert not passed
    assert _edited(reasons) and _edited(reasons)[0].endswith(
        "(patients 0, studies 0, series 1)"), reasons

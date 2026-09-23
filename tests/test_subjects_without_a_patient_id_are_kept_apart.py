"""Files with no Patient ID are not merged into one patient (#584).

Ingest spelled an absent Patient ID `UnknownPatient` and kept an empty one
as `''`, and keyed its patient map on that string, so every ID-less file in
a store became one patient. Measured at 63a64158 (spec
`.agent/v1/L8-spec.md` §1.2), CT_small under the floor policy:

- two files `Alpha^One` and `Beta^Two` with the ID **empty**: one patient
  named `Alpha^One` holding both studies; every pass declined the date
  shift ("could not resolve a PatientID"), REVIEW_REQUIRED;
- the same with the ID **absent**: one patient `UnknownPatient`, both
  subjects under **one pseudonym and one date offset**, graded PASS;
- `KEEP` on `0010,0020`, ID absent: the file exported `UnknownPatient`, an
  identifier the source never held;
- a real Patient ID `UnknownPatient` merged with an ID-less file.

Now a file with no usable Patient ID belongs to the patient holding its
study, or to a new patient of that study alone, keyed
`NO_PATIENT_ID_PREFIX + <StudyInstanceUID>`. That key is never exported:
the file's `0010,0020` is empty under `KEEP` and `REPLACE` alike (owner
ruling Q4, 2026-09-21).
"""
import datetime
import logging

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import (NO_PATIENT_ID_PREFIX, SOURCE_SOP_UID_ATTR, PhiStatus,
                                exported_patient_id, is_synthetic_patient_id)
from isocenter.exporters.wfdb import record_name_for
from isocenter.privacy import _replacement_uid_for

from support.ct_small_files import study_uid, write_ct
from support.project_secret import FIXED_A, load_fixed_secret
from support.store_secret import secret_of

MODES = ["threads", "processes"]

#: The token a restore takes the patient's identity from (#583, and review
#: round 4 of #584): the first found, unless the patient has a Patient ID
#: and that token's is blank, when it is the first holding one.
SPEAKER = "the token the patient's identity was restored from"


def _tokenless(count, total):
    return (f"{count} of {total} instances of this patient carry no identity "
            "token, so they took only the patient-level identifiers (group "
            f"0010) of {SPEAKER}, and their other locked identifiers keep what "
            "anonymize() left (#583).")


def _disagree(count, total):
    return (f"{count} of {total} identity tokens of this patient hold a "
            f"Patient's Name or Patient ID different from {SPEAKER} (the first "
            "found, or the first holding a Patient ID); the patient takes that "
            "token's, which export() stamps on every study (#583).")


def _restore_warnings(session, caplog, patient_id):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        session.recover_patient_identity(patient_id, restore=True)
    return [r.getMessage() for r in caplog.records if "(#583)" in r.getMessage()]


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


def _id_less(path, suffix, name, how):
    """CT_small with the Patient ID absent or empty."""
    write_ct(path, "TMP", suffix, name=name)
    ds = pydicom.dcmread(str(path))
    if how == "absent":
        del ds.PatientID
    else:
        # Blank with a tab: pydicom strips trailing spaces on read, so
        # "   " arrives as "" and would test the empty case twice; " \t "
        # arrives as " \t", which only the `.strip()` makes unusable.
        ds.PatientID = " \t " if how == "blank" else ""
    ds.save_as(str(path))
    return str(path)


def _by_source_sop(patient):
    """The patient's instances by the SOP Instance UID each was ingested
    under: a pass replaces the UID of every instance its report scanned
    (#544), and a file ingested after the audit keeps its own."""
    return {i.attributes.get(SOURCE_SOP_UID_ATTR, i.sop_instance_uid): i
            for st in patient.studies for se in st.series for i in se.instances}


def _exported(out):
    return {str(ds.StudyInstanceUID): ds for ds in
            (pydicom.dcmread(str(p)) for p in sorted(out.rglob("*.dcm")))}


@pytest.mark.parametrize("mode", MODES, indirect=True)
@pytest.mark.parametrize("how", ["empty", "absent", "blank"])
def test_two_id_less_subjects_stay_two_patients(tmp_path, mode, how):
    """T-B1. Kills M-B1 (the absent spelling `UnknownPatient` restored),
    M-B2 (the key drops the study UID), M-B3 (the stamp reads
    `patient.patient_id`), M-B12 (the patient-level synthetic check
    dropped: the key is pseudonymized and exported), and, through the
    blank case, MI3 (the `.strip()` dropped) and M-B13 (the instance-level
    synthetic arm dropped: the `" \\t"` copy is raised and pseudonymized)."""
    _id_less(tmp_path / "in" / "a.dcm", "5841", "Alpha^One", how)
    _id_less(tmp_path / "in" / "b.dcm", "5842", "Beta^Two", how)
    source_date = str(pydicom.dcmread(str(tmp_path / "in" / "a.dcm")).StudyDate)
    with Session(str(tmp_path / "s.db")) as session:
        # A fixed secret, so "two different offsets" is a fact about this
        # test and not a 1-in-365 chance of two random ones colliding.
        load_fixed_secret(session, tmp_path)
        session.ingest(str(tmp_path / "in"))
        patients = sorted(session.store.patients, key=lambda p: p.patient_id)
        assert [p.patient_id for p in patients] == [
            NO_PATIENT_ID_PREFIX + study_uid("5841"),
            NO_PATIENT_ID_PREFIX + study_uid("5842")]
        assert all(is_synthetic_patient_id(p.patient_id) for p in patients)
        assert [p.patient_name for p in patients] == ["Alpha^One", "Beta^Two"]
        assert [len(p.studies) for p in patients] == [1, 1]

        report = session.audit()
        # Top level only: CT_small's Other Patient IDs Sequence carries
        # nested `0010,0020` values, which are the instance scan's to judge.
        top_level_ids = [(f.entity_type, f.value) for f in report.findings
                         if f.tag == "0010,0020" and not f.entity_path]
        assert top_level_ids == []
        session.anonymize(report)
        copies = {i.attributes.get("0010,0020") for p in patients
                  for st in p.studies for se in st.series for i in se.instances}
        session.export(str(tmp_path / "out"), use_compression=False)
        frame = session.export_dataframe(expand_metadata=True)

    files = _exported(tmp_path / "out")
    # Each study's UID is replaced (#544); the patient key keeps the source.
    a, b = (files[_replacement_uid_for(study_uid(n), FIXED_A)] for n in ("5841", "5842"))
    assert str(a.PatientID) == "" and str(b.PatientID) == ""
    source = datetime.datetime.strptime(source_date, "%Y%m%d")
    offsets = {(datetime.datetime.strptime(ds.StudyDate, "%Y%m%d") - source).days
               for ds in (a, b)}
    # Shifted, and by two different offsets: one per subject.
    assert len(offsets) == 2 and 0 not in offsets, offsets
    # The instance copies hold what the file held, not a pseudonym.
    assert {str(c).strip() for c in copies if c is not None} <= {""}, copies
    # No column at all when the files carried no copy (the absent case).
    shown = frame["0010,0020"] if "0010,0020" in frame else []
    assert {str(v).strip() for v in shown if v == v and v is not None} <= {""}, shown


def test_a_later_file_of_the_same_study_joins_its_patient(tmp_path):
    """T-B2: the key is derived from the file, so a reopened store and a
    second ingest of the same study resolve to the same patient. Kills M-B4
    (a nondeterministic key) and M-B5 (the patient created before the study
    lookup)."""
    _id_less(tmp_path / "in1" / "a.dcm", "5843", "Alpha^One", "absent")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
    path = _id_less(tmp_path / "in2" / "b.dcm", "5843", "Alpha^One", "empty")
    ds = pydicom.dcmread(path)
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = study_uid("5843") + ".1.2"
    ds.save_as(path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in2"))
        [patient] = session.store.patients
        assert patient.patient_id == NO_PATIENT_ID_PREFIX + study_uid("5843")
        [study] = patient.studies
        assert sum(len(se.instances) for se in study.series) == 2


def test_a_real_id_that_reads_unknownpatient_is_not_an_id_less_file(tmp_path, caplog):
    """T-B3. Kills M-B6 (an absent ID normalized to the literal), and MI17
    (the INFO count of ID-less files dropped: one file here, not two)."""
    write_ct(tmp_path / "in" / "real.dcm", "UnknownPatient", "5844", name="Real^Person")
    _id_less(tmp_path / "in" / "none.dcm", "5845", "Other^Person", "absent")
    with caplog.at_level("INFO", logger="isocenter"):
        with Session(str(tmp_path / "s.db")) as session:
            session.ingest(str(tmp_path / "in"))
            ids = sorted(p.patient_id for p in session.store.patients)
    assert ids == ["UnknownPatient", NO_PATIENT_ID_PREFIX + study_uid("5845")]
    counts = [r.getMessage() for r in caplog.records
              if "carried no Patient ID" in r.getMessage()]
    assert counts == ["1 file(s) carried no Patient ID; each of their "
                      "studies was made its own patient."], counts


def test_keep_exports_an_empty_id_and_no_key_reaches_a_name(tmp_path):
    """T-B4: KEEP on 0010,0020 with the ID absent. Kills M-B3 (the stamp)
    and M-B7 (the WFDB record name reads `patient_id`)."""
    _id_less(tmp_path / "in" / "a.dcm", "5846", "Alpha^One", "absent")
    config = tmp_path / "keep.yaml"
    config.write_text("phi_tags:\n  '0010,0020': {action: KEEP}\n", encoding="utf-8")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(config))
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False)
        [patient] = session.store.patients
        study = patient.studies[0]
        series = study.series[0]
        name = record_name_for(patient, study, series, series.instances[0])
        assert exported_patient_id(patient) == ""

    [path] = list((tmp_path / "out").rglob("*.dcm"))
    assert str(pydicom.dcmread(str(path)).PatientID) == ""
    subject = path.relative_to(tmp_path / "out").parts[0]
    assert subject == "Subject_UnknownPatient"
    digits = study_uid("5846").replace(".", "")
    assert "no-patient-id" not in name and digits not in name.replace("_", ""), name
    assert "5846" not in name, name


def _with_id(path, pid, suffix, sop_tail, name):
    write_ct(path, pid, suffix, name=name)
    ds = pydicom.dcmread(str(path))
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = study_uid(suffix) + sop_tail
    ds.save_as(str(path))


def test_a_real_id_arriving_later_rekeys_the_id_less_patient(tmp_path):
    """T-B9, the reverse order (§3.3): file 1 of study S has no ID, file 2
    of S carries `PA`. One patient `PA` holding S with both instances, and
    no empty patient. Kills M-B14 (the re-key dropped: `PA` created empty,
    or the file linked under the synthetic patient)."""
    _id_less(tmp_path / "in1" / "a.dcm", "5847", "Alpha^One", "absent")
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5847", ".1.2", "Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        session.ingest(str(tmp_path / "in2"))
        shape = {p.patient_id: [sum(len(se.instances) for se in st.series)
                                for st in p.studies]
                 for p in session.store.patients}
        session.save(sync=True)
    assert shape == {"PA": [2]}, shape
    with Session(str(tmp_path / "s.db")) as reopened:
        assert [p.patient_id for p in reopened.store.patients] == ["PA"]


def test_a_third_file_after_a_merge_in_one_ingest_links_under_the_survivor(tmp_path):
    """One ingest, in file order: an ID-less file of S, then two `PA` files
    of S, with `PA` already holding another study. The second file merges
    the ID-less patient's study onto `PA`; the third must find S held by
    `PA`, not by the patient the merge removed. Kills MI13 (the owner map
    left pointing at the removed patient: the third file fails linkage)."""
    _with_id(tmp_path / "in0" / "p.dcm", "PA", "5855", ".1.1", "Alpha^One")
    _id_less(tmp_path / "in1" / "a.dcm", "5856", "Alpha^One", "absent")
    _with_id(tmp_path / "in1" / "b.dcm", "PA", "5856", ".1.2", "Alpha^One")
    _with_id(tmp_path / "in1" / "c.dcm", "PA", "5856", ".1.3", "Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in0"))
        session.ingest(str(tmp_path / "in1"))
        shape = {p.patient_id: sorted(sum(len(se.instances) for se in st.series)
                                      for st in p.studies)
                 for p in session.store.patients}
        errors = _audit_rows(session, "ERROR")
    assert shape == {"PA": [1, 3]}, shape
    assert errors == [], errors


@pytest.mark.parametrize("existing", [False, True], ids=["rekey", "merge"])
def test_a_re_key_of_a_stored_id_less_patient_reaches_the_store(tmp_path, existing):
    """T-B9 across a reopen: the ID-less patient's row was already written
    under its key when the real-ID file arrives in a later session. The
    store ends holding `PA` alone -- no row left under the key, none
    empty. The re-key's `mark_modified()` is not what this pins: the save
    writes a patient with no row under its ID whatever its revision says
    (#552), which a re-keyed patient always is, so dropping the call
    survives (MI10, equivalent)."""
    if existing:
        _with_id(tmp_path / "in0" / "p.dcm", "PA", "5853", ".1.1", "Alpha^One")
    _id_less(tmp_path / "in1" / "a.dcm", "5854", "Alpha^One", "absent")
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5854", ".1.2", "Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        if existing:
            session.ingest(str(tmp_path / "in0"))
        session.ingest(str(tmp_path / "in1"))
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in2"))
        session.save(sync=True)
    import sqlite3
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        rows = [r[0] for r in conn.execute("SELECT patient_id FROM patients")]
    assert rows == ["PA"], rows
    with Session(str(tmp_path / "s.db")) as reopened:
        [patient] = reopened.store.patients
        assert patient.patient_id == "PA"
        assert sorted(sum(len(se.instances) for se in st.series)
                      for st in patient.studies) == ([1, 2] if existing else [2])


@pytest.mark.parametrize("batches", [2, 1], ids=["two-ingests", "one-ingest"])
def test_an_id_less_file_joins_the_patient_holding_its_study(tmp_path, batches):
    """The forward order: `PA`'s file of study S first, then a file of S
    with its Patient ID stripped. One patient `PA`, both instances, and no
    empty synthetic patient beside it. Kills M-B5 (the patient looked up by
    key before the study, creating an empty one). In one ingest, where the
    owner map is not rebuilt from the graph between the two files, also
    MI14 (a new study not recorded as its patient's), in either order."""
    second = "in2" if batches == 2 else "in1"
    _with_id(tmp_path / "in1" / "a.dcm", "PA", "5851", ".1.1", "Alpha^One")
    _id_less(tmp_path / second / "b.dcm", "5851", "Alpha^One", "empty")
    path = tmp_path / second / "b.dcm"
    ds = pydicom.dcmread(str(path))
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = study_uid("5851") + ".1.2"
    ds.save_as(str(path))
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        if batches == 2:
            session.ingest(str(tmp_path / "in2"))
        shape = {p.patient_id: [sum(len(se.instances) for se in st.series)
                                for st in p.studies]
                 for p in session.store.patients}
    assert shape == {"PA": [2]}, shape


def test_a_real_id_joining_an_existing_patient_takes_the_study_along(tmp_path):
    """T-B9, when `PA` already exists with another study: the synthetic
    patient's study moves onto it."""
    _with_id(tmp_path / "in0" / "p.dcm", "PA", "5848", ".1.1", "Alpha^One")
    _id_less(tmp_path / "in1" / "a.dcm", "5849", "Alpha^One", "absent")
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5849", ".1.2", "Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        for folder in ("in0", "in1", "in2"):
            session.ingest(str(tmp_path / folder))
        shape = {p.patient_id: sorted(st.study_instance_uid for st in p.studies)
                 for p in session.store.patients}
        session.save(sync=True)
    assert shape == {"PA": sorted([study_uid("5848"), study_uid("5849")])}, shape
    with Session(str(tmp_path / "s.db")) as reopened:
        [patient] = reopened.store.patients
        assert patient.patient_id == "PA" and len(patient.studies) == 2


def test_after_a_shift_the_real_id_file_links_under_the_id_less_patient(tmp_path):
    """T-B9's second case: the synthetic patient's dates are already
    shifted, so re-keying would give them a second offset. The file links
    under the synthetic patient with one WARNING row; no empty `PA`."""
    _id_less(tmp_path / "in1" / "a.dcm", "5850", "Alpha^One", "absent")
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5850", ".1.2", "Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        session.anonymize(session.audit())
        session.ingest(str(tmp_path / "in2"))
        [patient] = session.store.patients
        assert is_synthetic_patient_id(patient.patient_id)
        [study] = patient.studies
        assert sum(len(se.instances) for se in study.series) == 2
        warnings = [(uid, d) for uid, d in _audit_rows(session, "WARNING")]
    sop = study_uid("5850") + ".1.2"
    linked = [d for uid, d in warnings if uid == sop]
    assert len(linked) == 1, warnings
    assert "PA" not in linked[0] and "Alpha" not in linked[0], linked


@pytest.mark.parametrize("level", ["Study", "Study-edited"])
def test_a_partial_pass_already_gave_the_id_less_patient_its_offset(tmp_path, level):
    """A pass handed only the Study's findings shifts the Study Date by the
    ID-less patient's offset while the patient still reads IDENTIFIED; a
    study edited after it reads UNSCANNED with its date still shifted.
    Re-keying then would give that date a second offset. Kills MI7 and MI8
    (`date_shifted` not read). The instance-level shapes are
    `test_a_shift_the_status_does_not_show_refuses_the_re_key`."""
    _id_less(tmp_path / "in1" / "a.dcm", "5852", "Alpha^One", "absent")
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5852", ".1.2", "Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        report = session.audit()
        applied = session.anonymize(
            [f for f in report.findings if f.entity_type == "Study"])
        [patient] = session.store.patients
        assert applied and patient.phi_status.name == "IDENTIFIED"
        if level == "Study-edited":
            [study] = patient.studies
            study.mark_modified()
            assert study.date_shifted and study.phi_status.name == "UNSCANNED"
        session.ingest(str(tmp_path / "in2"))
        [patient] = session.store.patients
        assert is_synthetic_patient_id(patient.patient_id)
        [study] = patient.studies
        assert sum(len(se.instances) for se in study.series) == 2
        warnings = _audit_rows(session, "WARNING")
    sop = study_uid("5852") + ".1.2"
    assert len([d for uid, d in warnings if uid == sop]) == 1, warnings


def _study_with_and_without_an_id(tmp_path, suffix, study_date="keep"):
    """a: no Patient ID; b: `PA`, the same study, SOP `.1.2`."""
    a = write_ct(tmp_path / "in1" / "a.dcm", "TMP", suffix,
                 study_date=study_date, name="Alpha^One")
    ds = pydicom.dcmread(str(a))
    del ds.PatientID
    ds.save_as(str(a))
    _with_id(tmp_path / "in2" / "b.dcm", "PA", suffix, ".1.2", "Alpha^One")
    if study_date is None:
        ds = pydicom.dcmread(str(tmp_path / "in2" / "b.dcm"))
        del ds.StudyDate
        ds.save_as(str(tmp_path / "in2" / "b.dcm"))


@pytest.mark.parametrize("shape", ["no-date", "date-kept", "check-burned-in"])
def test_an_audited_id_less_patient_is_not_re_keyed(tmp_path, shape):
    """Review of #763 at 49037891, F5-1, and the owner's ruling of
    2026-09-22: a recorded scan status counts as a value derived under the
    ID-less identity, like a shift or a token. An ID-less file a, then
    `audit()`, then a `PA` file b of its study, then `anonymize(report)`
    and `export()`. At 49037891 b re-keyed the patient to `PA`; the
    report's findings were raised under the key, where the scan raises
    no Patient ID finding (#584), so nothing covered `PA`, and both files
    exported it in the clear -- and with no Study Date to decline, the run
    graded PASS. Now b links under the ID-less patient with its WARNING
    row, both files export an empty Patient ID, and the run grades
    REVIEW_REQUIRED. The three shapes are the reviewer's probe: no Study
    Date and the default export, the Study Date kept, and
    `check_burned_in=True`. Kills MG6 (the status read dropped)."""
    _study_with_and_without_an_id(
        tmp_path, "5905", study_date=None if shape == "no-date" else "keep")
    sop_b = study_uid("5905") + ".1.2"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        report = session.audit()
        [patient] = session.store.patients
        key = patient.patient_id
        session.ingest(str(tmp_path / "in2"))
        assert [p.patient_id for p in session.store.patients] == [key]
        assert is_synthetic_patient_id(key)
        linked = [d for uid, d in _audit_rows(session, "WARNING") if uid == sop_b]
        assert len(linked) == 1, linked
        session.anonymize(report)
        session.export(str(tmp_path / "out"), use_compression=False,
                       check_burned_in=shape == "check-burned-in")
        session.generate_report(str(tmp_path / "r.md"))
    exported = {str(ds.SOPInstanceUID): str(ds.PatientID)
                for ds in (pydicom.dcmread(str(f))
                           for f in (tmp_path / "out").rglob("*.dcm"))}
    assert "PA" not in exported.values(), exported
    if shape != "check-burned-in":
        # a's UID is replaced by the report's pass (#544); b arrived after
        # the audit, so the report holds no finding on it.
        sop_a = _replacement_uid_for(study_uid("5905") + ".1.1", secret_of(tmp_path / "s.db"))
        assert exported == {sop_a: "", sop_b: ""}, exported
    content = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "**REVIEW_REQUIRED**" in content
    assert "**PASS**" not in content


@pytest.mark.parametrize("state", ["audited", "patient-pass", "stale", "reopened",
                                   "reopened-patient-only", "reopened-study-only",
                                   "reopened-instance-only"])
def test_a_recorded_scan_status_refuses_the_re_key(tmp_path, state):
    """The gate's status read, in each state it must hold (F5-1's ruling):

    - `audited`: `audit()` recorded IDENTIFIED;
    - `patient-pass`: a pass handed only the name finding, which shifted
      nothing and left the patient REMEDIATED -- until F5-1 this state
      re-keyed (the gate read no status: "a pass that shifted nothing
      still lets the real ID re-key");
    - `stale`: every bearer edited since the scan, so each `phi_status`
      reads UNSCANNED; the scan still ran under the key, and the raw
      record is what the gate reads;
    - `reopened`: the audit saved and the store reopened; the status is
      stored and hydrated;
    - `reopened-patient-only`: the studies and instances edited before
      the save, so each is stored UNSCANNED and only the patient's status
      survives the reopen;
    - `reopened-study-only` / `reopened-instance-only`: likewise, only
      the study's, or only the instance's, status survives (review round
      6: a patient-level edit after the scan, then a save).

    Each refuses the re-key: b links under the ID-less patient with one
    WARNING row. Kills MG6 (the read dropped) and, `stale`, MG7 (the
    revision-checked `phi_status` read instead of the record) and,
    `reopened-patient-only`, MG8 (the patient's status not read), and
    MG9 / MG10 (a study's, an instance's status not read)."""
    _study_with_and_without_an_id(tmp_path, "5871")
    sop_b = study_uid("5871") + ".1.2"
    session = Session(str(tmp_path / "s.db"))
    try:
        session.ingest(str(tmp_path / "in1"))
        report = session.audit()
        [patient] = session.store.patients
        if state == "patient-pass":
            session.anonymize([f for f in report.findings
                               if f.entity_type == "Patient" and f.tag == "0010,0010"])
            assert patient.phi_status is PhiStatus.REMEDIATED
            assert not patient.studies[0].date_shifted
        elif state == "stale":
            bearers = [patient, *patient.studies,
                       *(i for st in patient.studies for se in st.series
                         for i in se.instances)]
            for entity in bearers:
                entity.mark_modified()
            assert {e.phi_status for e in bearers} == {PhiStatus.UNSCANNED}
        elif state.startswith("reopened"):
            def levels(p):
                return {"patient": [p], "study": list(p.studies),
                        "instance": [i for st in p.studies for se in st.series
                                     for i in se.instances]}
            kept = state[len("reopened-"):-len("-only")] if state != "reopened" else None
            if kept:
                for level, entities in levels(patient).items():
                    if level != kept:
                        for entity in entities:
                            entity.mark_modified()
            session.save(sync=True)
            session.close()
            session = Session(str(tmp_path / "s.db"))
            if kept:
                [patient] = session.store.patients
                for level, entities in levels(patient).items():
                    statuses = {e._phi_status for e in entities}
                    if level == kept:
                        assert statuses == {PhiStatus.IDENTIFIED}, (level, statuses)
                    else:
                        assert statuses <= {None, PhiStatus.UNSCANNED}, (level, statuses)
        session.ingest(str(tmp_path / "in2"))
        [patient] = session.store.patients
        assert is_synthetic_patient_id(patient.patient_id)
        linked = [d for uid, d in _audit_rows(session, "WARNING") if uid == sop_b]
        assert len(linked) == 1, linked
    finally:
        session.close()



def test_a_report_from_an_unsaved_audit_across_a_reopen_grades_review_required(
        tmp_path):
    """The path the gate cannot close (owner ruling, 2026-09-22: #752's
    class). An ID-less file a, `audit()` never saved, `close()`: the store
    holds no status for it. Reopened, a `PA` file b of its study re-keys
    the patient -- nothing in the store says a value was derived under
    the key -- and `anonymize()` of the old report declines its patient-
    and study-level findings, which were filed under the key. Both files
    export the patient-level identity as the source held it: Patient ID
    `PA` (b's own, and a's through its patient), Patient's Name
    `Alpha^One`, and the source Study Date, unshifted (review round 6,
    F6-1: the date is kept in the fixture, so that half is asserted).
    `main` (8f631b99) links b under a's patient and grades PASS on these
    inputs; here the run grades REVIEW_REQUIRED on the declines, which
    tells the truth: a report applied to a graph that changed after the
    scan."""
    _study_with_and_without_an_id(tmp_path, "5906")
    source_date = str(pydicom.dcmread(str(tmp_path / "in1" / "a.dcm")).StudyDate)
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in1"))
    report = session.audit()
    session.close()
    with Session(str(tmp_path / "s.db")) as session:
        [patient] = session.store.patients
        assert patient._phi_status in (None, PhiStatus.UNSCANNED)
        session.ingest(str(tmp_path / "in2"))
        assert [p.patient_id for p in session.store.patients] == ["PA"]
        session.anonymize(report)
        declined = sorted((uid, d) for uid, d in
                          _audit_rows(session, "REMEDIATION_DECLINED"))
        session.export(str(tmp_path / "out"), use_compression=False)
        session.generate_report(str(tmp_path / "r.md"))
    exported = {str(ds.SOPInstanceUID):
                (str(ds.PatientID), str(ds.PatientName), str(ds.StudyDate))
                for ds in (pydicom.dcmread(str(f))
                           for f in (tmp_path / "out").rglob("*.dcm"))}
    study = study_uid("5906")
    # The old report's findings were filed under the key: the patient's
    # (its name) no longer resolves, and the Study Date on the study
    # declines because the offset's seed is not the patient's ID any more.
    # a's instance copies of the name and the date follow their owners,
    # which did not write (#624): each declines, holding the source value
    # the export writes.
    owner_row = "is written by the export from the"
    assert [(uid, "offset is seeded on" in d, "could not be resolved" in d,
             owner_row in d) for uid, d in declined] == [
        (study, True, False, False),
        (study + ".1.1", False, False, True),
        (study + ".1.1", False, False, True),
        (NO_PATIENT_ID_PREFIX + study, False, True, False)], declined
    assert sorted(d.split(": ")[1].split(" ")[0] for uid, d in declined
                  if uid == study + ".1.1") == ["0008,0020", "0010,0010"], declined
    # a's UID is replaced by the old report's pass (#544); b arrived
    # after the audit.
    sop_a = _replacement_uid_for(study + ".1.1", secret_of(tmp_path / "s.db"))
    assert exported == {sop_a: ("PA", "Alpha^One", source_date),
                        study + ".1.2": ("PA", "Alpha^One", source_date)}, exported
    content = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "**REVIEW_REQUIRED**" in content
    assert "**PASS**" not in content
    assert "4 declined remediation(s)" in content



def test_an_id_less_patient_never_scanned_is_still_re_keyed(tmp_path):
    """The other side of the status read: with no scan recorded, no shift
    and no lock, nothing has been derived under the key, so a real ID
    re-keys the patient and no WARNING row is written."""
    _study_with_and_without_an_id(tmp_path, "5872")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        session.ingest(str(tmp_path / "in2"))
        assert [p.patient_id for p in session.store.patients] == ["PA"]
        linked = [d for uid, d in _audit_rows(session, "WARNING")
                  if uid == study_uid("5872") + ".1.2"]
    assert linked == [], linked


@pytest.mark.parametrize("mode", ["lock-only", "lock-by-id", "lock-then-patient-pass",
                                  "lock-then-reopen"])
def test_a_locked_identity_refuses_the_re_key(tmp_path, mode):
    """Review round 2, R2-1. `lock_identities()` stashes an ID-less
    patient's Patient ID as `''` (the ID the export writes). If a real-ID
    file then re-keyed the patient to `PA`, a restore wrote the token's
    `''` over `PA`, the patient read as a pre-1.0 `''` group, every later
    date shift declined ("could not resolve a PatientID"), and the next
    open wrote a false pre-1.0 WARNING. A token of this store anywhere in
    the subtree is evidence the gate honours, as a shift is (coordinator
    ruling): the file links under the ID-less patient with its WARNING row,
    and the restore keeps the key. Red at ae5212ff in both modes (the pass handed only the patient's
    findings shifts nothing). Kills MT1 (the token not read).

    The restore then gives each file back what it held (coordinator ruling,
    review round 2): b, ingested after the lock and carrying no token,
    keeps its own `PA` rather than the token's blank ID (MR2: the guard
    dropped). c, also token-less, whose copy is absent, still takes the
    token's group 0010 as before: its name, and an empty ID (MR3: the
    guard applied whatever the copy holds).

    Since F5-1 a recorded scan status refuses the re-key too, so after
    `audit()` the token is not the only evidence. `lock-by-id` locks by
    Patient ID with no audit, which records no status (measured): the
    token is all the gate has, and `lock-then-reopen` does the same
    across a reopen, so MT1 stays killable."""
    _id_less(tmp_path / "in1" / "a.dcm", "5895", "Alpha^One", "absent")
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5895", ".1.2", "Alpha^One")
    c = tmp_path / "in2" / "c.dcm"
    _id_less(c, "5895", "Other^Name", "absent")
    ds = pydicom.dcmread(str(c))
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = study_uid("5895") + ".1.3"
    ds.save_as(str(c))
    sop_b = study_uid("5895") + ".1.2"
    sop_c = study_uid("5895") + ".1.3"
    session = Session(str(tmp_path / "s.db"))
    try:
        session.ingest(str(tmp_path / "in1"))
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        if mode in ("lock-by-id", "lock-then-reopen"):
            [patient] = session.store.patients
            session.lock_identities([patient.patient_id])
            assert patient._phi_status is None
        else:
            report = session.audit()
            session.lock_identities(report)
        if mode == "lock-then-patient-pass":
            session.anonymize([f for f in report.findings
                               if f.entity_type == "Patient"])
        [patient] = session.store.patients
        assert not patient.studies[0].date_shifted
        if mode == "lock-then-reopen":
            # The token gate across a reopen: the stamp is stored and
            # hydrated (#607), and the gate reads the hydrated stamp.
            session.save(sync=True)
            session.close()
            session = Session(str(tmp_path / "s.db"))
            session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.ingest(str(tmp_path / "in2"))
        [patient] = session.store.patients
        assert is_synthetic_patient_id(patient.patient_id)
        linked = [d for uid, d in _audit_rows(session, "WARNING") if uid == sop_b]
        assert len(linked) == 1, linked
        session.recover_patient_identity(patient.patient_id, restore=True)
        [patient] = session.store.patients
        assert is_synthetic_patient_id(patient.patient_id)
        held = {i.sop_instance_uid: i for st in patient.studies
                for se in st.series for i in se.instances}
        assert held[sop_b].attributes.get("0010,0020") == "PA"
        assert "0010,0020" in held[sop_c].attributes
        assert held[sop_c].attributes["0010,0020"] == ""
        assert held[sop_c].attributes.get("0010,0010") == "Alpha^One"
        session.anonymize(session.audit())
        # Kept through the next pass: the §3.4 arm leaves a synthetic
        # patient's copy alone, and the export writes it empty.
        assert held[sop_b].attributes.get("0010,0020") == "PA"
        declined = [d for _uid, d in _audit_rows(session, "REMEDIATION_DECLINED")
                    if "PatientID" in d]
        assert declined == [], declined
        session.save(sync=True)
    finally:
        session.close()
    with Session(str(tmp_path / "s.db")) as reopened:
        pre_1_0 = [d for _uid, d in _audit_rows(reopened, "WARNING")
                   if "before 1.0" in d]
    assert pre_1_0 == [], pre_1_0


@pytest.mark.parametrize("order", ["rekey", "forward", "rekey-reopened"])
@pytest.mark.parametrize("how", ["empty", "absent"])
def test_a_restore_gives_a_joined_patient_its_real_id(tmp_path, order, how, caplog):
    """Review round 3, R3-1. An ID-less file a and a `PA` file b of one
    study make patient `PA` (a re-key, or a forward join) before any lock.
    The lock stashes each instance's own copy, faithfully: a's is `''`
    when its ID was empty. The restore took the patient's ID from the
    first token in graph order, so with a first it wrote `''` over `PA`,
    and a token-less `PA` file c took `''` too; the next open wrote a false
    pre-1.0 row. Now the patient, and a token-less instance, take the
    first token whose Patient ID is not blank (coordinator ruling): the
    patient and c get `PA`, a gets back its own. Red at 9b831175 for
    `rekey-empty`. Kills MS2 (the first token speaks).

    Review round 4 (R4-1): the #583 WARNINGs named "the first token
    found", which here is a's blank token, not the one the patient's
    identity came from; and a's blank ID counted as a token disagreeing
    on the Patient ID, though the speaker rule passed it over on purpose.
    Now they name the speaking token, and a blank-ID token does not
    disagree: only c's token-less line is logged. Red at bb00b8b1 for
    `*-empty` (the disagreement line) and every case (the wording)."""
    first, second = ("in2", "in1") if order == "forward" else ("in1", "in2")
    _id_less(tmp_path / first / "a.dcm", "5897", "Alpha^One", how)
    _with_id(tmp_path / second / "b.dcm", "PA", "5897", ".1.2", "Alpha^One")
    _with_id(tmp_path / "in3" / "c.dcm", "PA", "5897", ".1.3", "Alpha^One")
    sop_a, sop_b, sop_c = (study_uid("5897") + tail for tail in (".1.1", ".1.2", ".1.3"))
    session = Session(str(tmp_path / "s.db"))
    try:
        session.ingest(str(tmp_path / "in1"))
        session.ingest(str(tmp_path / "in2"))
        [patient] = session.store.patients
        assert patient.patient_id == "PA"
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        report = session.audit()
        session.lock_identities(report)
        session.ingest(str(tmp_path / "in3"))
        session.anonymize(report)
        if order == "rekey-reopened":
            # The tokens and the patient come back from the store.
            session.save(sync=True)
            session.close()
            session = Session(str(tmp_path / "s.db"))
            session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        [patient] = session.store.patients
        warnings = _restore_warnings(session, caplog, patient.patient_id)
        assert warnings == [_tokenless(1, 3)], warnings
        [patient] = session.store.patients
        assert patient.patient_id == "PA"
        held = _by_source_sop(patient)
        # a's own copy comes back as the file held it: `''` when empty;
        # when absent the lock stashed the patient's `PA` for it.
        assert held[sop_a].attributes.get("0010,0020") == ("" if how == "empty" else "PA")
        assert held[sop_b].attributes.get("0010,0020") == "PA"
        assert held[sop_c].attributes.get("0010,0020") == "PA"
        session.anonymize(session.audit())
        declined = [d for _uid, d in _audit_rows(session, "REMEDIATION_DECLINED")
                    if "PatientID" in d]
        assert declined == [], declined
        session.save(sync=True)
    finally:
        session.close()
    with Session(str(tmp_path / "s.db")) as reopened:
        assert [p.patient_id for p in reopened.store.patients] == [
            patient.patient_id]
        pre_1_0 = [d for _uid, d in _audit_rows(reopened, "WARNING")
                   if "before 1.0" in d]
    assert pre_1_0 == [], pre_1_0


def test_a_restore_keeps_an_id_less_patients_key_whatever_a_later_token_holds(
        tmp_path, caplog):
    """The other side of R3-1's rule. An ID-less patient locked, then a
    `PA` file of its study linked under it (the token refuses the re-key),
    then locked again: its tokens are a's `''` and b's `PA`. The patient
    keeps its key -- the first token speaks for an ID-less patient, never
    the first non-blank one, which would rename it to `PA` after values
    were derived under the key (review round 2's rule) -- and a token-less
    ID-less file c still takes the blank ID. Kills MS3 (the non-blank
    choice applied to an ID-less patient too).

    Review round 4 (R4-1): b's `PA` does disagree with the speaker's blank
    -- the patient keeps its key and exports it empty -- so one token is
    counted, in the wording that names the speaking token. c's copy was
    absent at ingest, so its `''` after the restore is the token's, not
    an untouched copy."""
    _id_less(tmp_path / "in1" / "a.dcm", "5898", "Alpha^One", "absent")
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5898", ".1.2", "Alpha^One")
    c = tmp_path / "in3" / "c.dcm"
    _id_less(c, "5898", "Alpha^One", "absent")
    ds = pydicom.dcmread(str(c))
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = study_uid("5898") + ".1.3"
    ds.save_as(str(c))
    sop_b, sop_c = study_uid("5898") + ".1.2", study_uid("5898") + ".1.3"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.lock_identities(session.audit())
        session.ingest(str(tmp_path / "in2"))
        [patient] = session.store.patients
        key = patient.patient_id
        assert is_synthetic_patient_id(key)
        session.lock_identities(session.audit())
        session.ingest(str(tmp_path / "in3"))
        session.anonymize(session.audit())
        held = _by_source_sop(patient)
        assert "0010,0020" not in held[sop_c].attributes
        warnings = _restore_warnings(session, caplog, key)
        assert warnings == [_tokenless(1, 3), _disagree(1, 2)], warnings
        [patient] = session.store.patients
        assert patient.patient_id == key
        assert held[sop_b].attributes.get("0010,0020") == "PA"
        assert held[sop_c].attributes.get("0010,0020") == ""



#: The floor, plus SHIFT on Series, Acquisition and Content Date: dates an
#: instance owns, so a pass can shift them while the instance's status says
#: nothing about it (review of this PR, finding 1).
_INSTANCE_DATES = ("0008,0021", "0008,0022", "0008,0023")
_SHIFT_INSTANCE_DATES = "phi_tags:\n" + "".join(
    f"  '{tag}': {{action: SHIFT}}\n" for tag in _INSTANCE_DATES)
_DATES = ("StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate")


def _nest_the_content_date(path):
    """File a with its one instance-owned date moved into a sequence item:
    `0008,1140[0] > 0008,0023`, the top-level three removed."""
    ds = pydicom.dcmread(str(path))
    item = pydicom.Dataset()
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.ReferencedSOPInstanceUID = ds.SOPInstanceUID + ".9"
    item.ContentDate = ds.ContentDate
    for keyword in ("SeriesDate", "AcquisitionDate", "ContentDate"):
        if keyword in ds:
            delattr(ds, keyword)
    ds.ReferencedImageSequence = pydicom.Sequence([item])
    ds.save_as(str(path))


@pytest.mark.parametrize("shape", ["dates-only", "edited", "reopened", "nested",
                                   "nested-reopened", "study-reopened"])
def test_a_shift_the_status_does_not_show_refuses_the_re_key(tmp_path, shape):
    """Review finding 1. A pass handed only some of an instance's findings
    shifts its dates and leaves it IDENTIFIED (#553); an edit after the
    pass leaves it UNSCANNED; a reopen hydrates the shift record and no
    status says more. The evidence is the shift record, at the root or in
    a sequence item (#513). A real-ID file of the study then links under
    the ID-less patient with its WARNING row, and after a full pass the
    subject's files export one offset per date, never two. At 3fc566e1
    every shape re-keyed silently and split the subject (-18 / -275
    days). Kills MG1 (the record not read) and, nested, MG2 (the nested
    items not walked).

    Since F5-1 a recorded scan status refuses the re-key as well, and
    every pass that shifts records one, so in one session the shift is
    never the only evidence. Across a reopen it can be: a status the
    entity has since left is saved as UNSCANNED (the store holds the
    status as read), while the shift record is stored with the value. So
    each `*-reopened` shape leaves every status stale before the save --
    the patient, its studies and instances edited after the pass -- and
    the shift record, at the root (`reopened`), in a sequence item
    (`nested-reopened`) or on the study (`study-reopened`, the Study
    Date), is all the reopened gate has. Kills MG1, MG2 and MG3 (the
    study's `date_shifted` not read) with the status read in place."""
    a = tmp_path / "in1" / "a.dcm"
    _id_less(a, "5870", "Alpha^One", "absent")
    if shape.startswith("nested"):
        _nest_the_content_date(a)
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5870", ".1.2", "Alpha^One")
    source = {k: str(pydicom.dcmread(str(tmp_path / "in2" / "b.dcm")).get(k))
              for k in _DATES}
    config = tmp_path / "c.yaml"
    config.write_text(_SHIFT_INSTANCE_DATES, encoding="utf-8")
    sop_b = study_uid("5870") + ".1.2"
    session = Session(str(tmp_path / "s.db"))
    try:
        load_fixed_secret(session, tmp_path)
        session.load_config(str(config))
        session.ingest(str(tmp_path / "in1"))
        report = session.audit()
        if shape == "study-reopened":
            handed = [f for f in report.findings if f.entity_type == "Study"
                      and f.tag == "0008,0020"]
        else:
            handed = [f for f in report.findings if f.entity_type == "Instance"
                      and f.tag in _INSTANCE_DATES
                      and bool(f.entity_path) == shape.startswith("nested")]
        assert handed and len(handed) < len(
            [f for f in report.findings if f.entity_type == "Instance"])
        session.anonymize(handed)
        [patient] = session.store.patients
        [inst] = [i for st in patient.studies for se in st.series
                  for i in se.instances]
        assert patient.studies[0].date_shifted == (shape == "study-reopened")
        if shape == "edited":
            inst.set_attr("0008,103e", "edited after the pass")
            assert inst.phi_status.name == "UNSCANNED"
        elif shape != "study-reopened":
            assert inst.phi_status.name == "IDENTIFIED"
        if shape.endswith("reopened"):
            bearers = [patient, *patient.studies, inst]
            for entity in bearers:
                entity.mark_modified()
            session.save(sync=True)
            session.close()
            session = Session(str(tmp_path / "s.db"))
            session.load_config(str(config))
            [patient] = session.store.patients
            reopened = [patient, *patient.studies,
                        *(i for st in patient.studies for se in st.series
                          for i in se.instances)]
            # No status survives: the shift record is the only evidence.
            assert {e._phi_status for e in reopened} <= {None, PhiStatus.UNSCANNED}
        session.ingest(str(tmp_path / "in2"))
        [patient] = session.store.patients
        assert is_synthetic_patient_id(patient.patient_id)
        linked = [d for uid, d in _audit_rows(session, "WARNING") if uid == sop_b]
        assert len(linked) == 1, linked
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False)
    finally:
        session.close()

    def day(value):
        return datetime.datetime.strptime(str(value), "%Y%m%d")

    offsets = set()
    for path in (tmp_path / "out").rglob("*.dcm"):
        ds = pydicom.dcmread(str(path))
        dated = [(k, ds.get(k)) for k in _DATES if ds.get(k)]
        dated += [("ContentDate", item.ContentDate)
                  for item in ds.get("ReferencedImageSequence", [])
                  if item.get("ContentDate")]
        offsets |= {(day(v) - day(source[k])).days for k, v in dated}
    assert len(offsets) == 1 and 0 not in offsets, offsets


def _audit_rows(session, action_type):
    import sqlite3
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute("SELECT entity_uid, details FROM audit_log "
                            "WHERE action_type=?", (action_type,)).fetchall()

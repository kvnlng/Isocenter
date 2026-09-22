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

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import (NO_PATIENT_ID_PREFIX, exported_patient_id,
                                is_synthetic_patient_id)
from isocenter.exporters.wfdb import record_name_for

from support.ct_small_files import study_uid, write_ct
from support.project_secret import load_fixed_secret

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
    a, b = files[study_uid("5841")], files[study_uid("5842")]
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


@pytest.mark.parametrize("level", ["Study", "Study-edited", "Instance"])
def test_a_partial_pass_already_gave_the_id_less_patient_its_offset(tmp_path, level):
    """The re-key refusal reads more than the patient's own status. A pass
    handed only the Study's findings shifts the Study Date, and one handed
    only the instance's shifts its Series, Acquisition and Content dates --
    each by the ID-less patient's offset -- while the patient itself still
    reads IDENTIFIED. Re-keying then would give those dates a second
    offset. Kills MI7 (only the patient's status read); for the instance
    pass, MI9 (the instance walk dropped); and for a study edited after
    the pass, which reads UNSCANNED with its date still shifted, MI8
    (`date_shifted` not read)."""
    _id_less(tmp_path / "in1" / "a.dcm", "5852", "Alpha^One", "absent")
    _with_id(tmp_path / "in2" / "b.dcm", "PA", "5852", ".1.2", "Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        report = session.audit()
        applied = session.anonymize(
            [f for f in report.findings if f.entity_type == level.split("-")[0]])
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


def _audit_rows(session, action_type):
    import sqlite3
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute("SELECT entity_uid, details FROM audit_log "
                            "WHERE action_type=?", (action_type,)).fetchall()

"""A Study Date put back by a restore reaches the exported file (#566).

`recover_patient_identity(restore=True)` wrote the locked `0008,0020`
onto every instance, but `export()` stamps Study Date from the `Study`
(`export_stamp_attributes`), and `Study.study_date` kept the shifted value.
Measured on 57400d1 (3.12 and 3.14t, `probes-E/p566.py`): instance
`20040119`, study shifted, the exported file shifted, and the split
survived a save and reopen.

Until #583 the token was captured from the patient's *first* instance, so
for a patient with several studies it held one study's date: writing it
onto every `Study` would have exported study 1's original date as study
2's, so only a single-study patient's `Study` took it, and a multi-study
restore left every study alone and said so once, by count. Since #583 the
lock writes one token per distinct set of values, captured per instance,
so each study's instances carry a token holding that study's own date,
and each `Study` takes its own, with no WARNING.
"""
import logging
from datetime import date

import pydicom
import pytest

from isocenter import Session
from isocenter.io_handlers import format_study_date

from support.ct_small_files import study_uid, write_ct

TAGS = ["0010,0010", "0010,0020", "0008,0020"]
PID = "PAT-566"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _locked_and_anonymized(tmp_path, dates):
    """One patient, one study per date, locked with Study Date and
    anonymized, saved. Returns (db, key, pseudonym, {uid: shifted DA})."""
    for n, day in enumerate(dates, start=1):
        write_ct(tmp_path / "in" / f"{n}.dcm", PID, f"566{n}", study_date=day)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(key)
        report = session.audit()
        session.lock_identities(PID, tags_to_lock=TAGS)
        session.anonymize(report)
        session.save(sync=True)
        [patient] = session.store.patients
        shifted = {st.study_instance_uid: format_study_date(st.study_date)
                   for st in patient.studies}
        return db, key, patient.patient_id, shifted


def _exported_study_dates(folder):
    found = []
    for path in sorted(folder.rglob("*.dcm")):
        ds = pydicom.dcmread(str(path))
        found.append((str(ds.StudyInstanceUID), str(ds.StudyDate)))
    return found


def test_a_single_study_restore_reaches_the_file_and_the_store(tmp_path):
    """Kills no sync (the export and `study_date`), and a sync without
    `mark_modified()` (the reopen). The re-audit and re-anonymize show the
    record was left alone: a Study-level SHIFT_DATE is raised again and the
    re-shift lands on the date the first anonymize produced."""
    db, key, pseudonym, shifted = _locked_and_anonymized(tmp_path, ["20040119"])
    [(uid, first_shift)] = shifted.items()
    assert first_shift != "20040119"

    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        session.recover_patient_identity(pseudonym, restore=True)
        [study] = session.store.patients[0].studies
        assert study.study_date == date(2004, 1, 19)
        session.export(str(tmp_path / "out"), use_compression=False)
        session.save(sync=True)
    assert _exported_study_dates(tmp_path / "out") == [(uid, "20040119")]

    with Session(db) as session:
        [study] = session.store.patients[0].studies
        assert study.study_date == date(2004, 1, 19)
        report = session.audit()
        assert [f for f in report.findings
                if f.entity_type == "Study" and f.tag == "0008,0020"
                and f.remediation_proposal.action_type == "SHIFT_DATE"]
        session.anonymize(report)
        assert format_study_date(study.study_date) == first_shift


def test_a_multi_study_restore_gives_each_study_its_own_date(tmp_path, caplog):
    """Each study's instances carry their own token since #583, so each
    `Study` takes its own date back, the store and the exported files say
    so, and no Study Date WARNING is logged. Until #583 the one token held
    study 1's date, every study kept its de-identified date, and one
    WARNING gave the count. Kills the sync taken from the first token for
    every study (M8: study 2 would export `20040119`) and #566's
    single-study guard kept (every study keeps its shifted date)."""
    db, key, pseudonym, shifted = _locked_and_anonymized(
        tmp_path, ["20040119", "20050505"])
    assert set(shifted.values()).isdisjoint({"20040119", "20050505"})
    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        after = {st.study_instance_uid: format_study_date(st.study_date)
                 for st in session.store.patients[0].studies}
        session.save(sync=True)
        session.export(str(tmp_path / "out"), use_compression=False)
    assert after == {study_uid("5661"): "20040119", study_uid("5662"): "20050505"}
    assert not [r for r in caplog.records
                if r.name == "isocenter" and r.levelno == logging.WARNING
                and "Study Date" in r.getMessage()], caplog.text
    assert _exported_study_dates(tmp_path / "out") == [
        (study_uid("5661"), "20040119"), (study_uid("5662"), "20050505")]
    with Session(db) as session:
        assert {st.study_instance_uid: format_study_date(st.study_date)
                for st in session.store.patients[0].studies} == after


def test_the_study_count_is_taken_before_the_merge(tmp_path, caplog):
    """A raw study for the original ID, ingested before the restore, merges
    into the restored patient. The token is the stored study's, so that
    study takes it and the raw study keeps its own date. Kills the sync
    placed after the merge, which sees two studies and syncs neither."""
    db, key, pseudonym, _ = _locked_and_anonymized(tmp_path, ["20040119"])
    write_ct(tmp_path / "raw" / "r.dcm", PID, "5669", study_date="20050505")
    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "raw"))
        assert len(session.store.patients) == 2
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        [patient] = session.store.patients
        dates = {st.study_instance_uid: st.study_date for st in patient.studies}
    assert dates == {study_uid("5661"): date(2004, 1, 19),
                     study_uid("5669"): date(2005, 5, 5)}
    assert not [r for r in caplog.records if "Study Date" in r.getMessage()], caplog.text


def test_a_restore_without_a_locked_study_date_leaves_the_study(tmp_path):
    """A token that holds no `0008,0020` touches no `Study`."""
    write_ct(tmp_path / "in" / "a.dcm", PID, "5667", study_date="20040119")
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(key)
        report = session.audit()
        session.lock_identities(PID, tags_to_lock=["0010,0010", "0010,0020"])
        session.anonymize(report)
        [study] = session.store.patients[0].studies
        shifted, revision = study.study_date, study._revision
        session.recover_patient_identity(session.store.patients[0].patient_id, restore=True)
        assert study.study_date == shifted
        assert study._revision == revision


def test_a_restore_onto_a_date_that_never_moved_records_no_change(tmp_path):
    """Locked and restored with no anonymize between: the study already holds
    the date, so its revision -- and the status recorded at it -- stays.
    Kills the difference test dropped (a restore marking every study it
    visits), which the patient arm above it already avoids."""
    write_ct(tmp_path / "in" / "a.dcm", PID, "5668", study_date="20040119")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.audit()
        session.lock_identities(PID, tags_to_lock=TAGS)
        [study] = session.store.patients[0].studies
        revision = study._revision
        session.recover_patient_identity(PID, restore=True)
        assert study.study_date == date(2004, 1, 19)
        assert study._revision == revision

"""A file whose Study Instance UID another patient's study already holds
is declined, with a WARNING row (#745, owner ruling Q5 A, 2026-10-01).

Measured at 7579d4df with two CT_small files under one real Study UID, one
with Patient ID `PA` (`Alpha^Ann`) and one with `PB` (`Beta^Bob`, Study
Date 2020-01-01), in one ingest and in two: PB's file was linked into PA's
study. It was exported under PA's pseudonym, PA's folder and PA's shifted
Study Date; PB's own date was gone; an empty `PB` patient stayed in the
graph; no row was written, and the run graded PASS.

Now the later file is declined above the sidecar write, as a duplicate
SOP Instance UID is (#431). "Later" means later in path order among the
new files, and a study the store already holds beats every new file. A
file of a study this store already anonymized, carrying the patient's
original ID, is the same patient and still links. So does a file with no
Patient ID (#584).
"""
import os
import re
import sqlite3

import pydicom
import pytest

from isocenter import Session

from support.ct_small_files import study_uid, write_ct

SHARED = 745
DETAIL = "is held by a patient with a different Patient ID"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(path, patient_id, name, sop_suffix, study_date="keep"):
    """CT_small under the shared Study UID, with its own series and SOP."""
    write_ct(path, patient_id or "X", SHARED, name=name, study_date=study_date)
    ds = pydicom.dcmread(str(path))
    if patient_id is None:
        del ds.PatientID
    ds.SeriesInstanceUID = f"{study_uid(SHARED)}.{sop_suffix}"
    ds.SOPInstanceUID = f"{study_uid(SHARED)}.{sop_suffix}.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.save_as(str(path))
    return ds.SOPInstanceUID


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute("SELECT action_type, entity_uid, details "
                            "FROM audit_log ORDER BY id").fetchall()


def _shared_rows(session):
    return [(a, uid, d) for a, uid, d in _rows(session) if DETAIL in (d or "")]


def _blob_rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute("SELECT COUNT(*) FROM instance_blobs").fetchone()[0]


def _report(session, tmp_path):
    """The grade basis lines, and the report's whole text."""
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines()
            if "**Grade Basis:**" in line], text


def _exported(folder):
    return [os.path.join(root, f) for root, _, names in os.walk(folder)
            for f in names if f.endswith(".dcm")]


def test_a_later_patients_file_of_a_held_study_is_declined(tmp_path):
    _write(tmp_path / "a" / "a.dcm", "PA", "Alpha^Ann", 1)
    pb = _write(tmp_path / "b" / "b.dcm", "PB", "Beta^Bob", 2,
                study_date="20200101")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "a"))
        summary = session.ingest(str(tmp_path / "b"))
        assert summary.declined == 1
        assert summary.ingested == 0
        assert [p.patient_id for p in session.store.patients] == ["PA"]
        rows = _shared_rows(session)
        assert [(a, uid) for a, uid, _ in rows] == [("WARNING", pb)]
        assert "PB" not in rows[0][2] and "Beta" not in rows[0][2]
        assert "PA" not in rows[0][2] and "Alpha" not in rows[0][2]
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        assert len(_exported(tmp_path / "out")) == 1
        grade, text = _report(session, tmp_path)
    assert len(grade) == 1
    assert re.search(r"\*\*Grade Basis:\*\* REVIEW_REQUIRED, for 1 reason", grade[0]), grade
    assert "1 row(s) in section 4 (Exceptions & Errors)" in text


def test_one_ingest_declines_the_later_file_in_path_order(tmp_path):
    _write(tmp_path / "in" / "a.dcm", "PA", "Alpha^Ann", 1)
    pb = _write(tmp_path / "in" / "b.dcm", "PB", "Beta^Bob", 2)
    with Session(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert (summary.ingested, summary.declined) == (1, 1)
        assert [uid for _, uid, _ in _shared_rows(session)] == [pb]
        assert [p.patient_id for p in session.store.patients] == ["PA"]


def test_the_declined_files_frame_is_not_written(tmp_path):
    _write(tmp_path / "a" / "a.dcm", "PA", "Alpha^Ann", 1)
    _write(tmp_path / "b" / "b.dcm", "PB", "Beta^Bob", 2)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "a"))
        before = _blob_rows(session)
        size = os.path.getsize(session.store_backend.sidecar_path)
        session.ingest(str(tmp_path / "b"))
        assert _blob_rows(session) == before
        assert os.path.getsize(session.store_backend.sidecar_path) == size


def test_the_same_patient_after_anonymize_still_links(tmp_path):
    """A new file of PA's study under PA's original ID, after the pass
    replaced it, is PA's: the patient merge (#548) folds it back."""
    _write(tmp_path / "a" / "a.dcm", "PA", "Alpha^Ann", 1)
    _write(tmp_path / "b" / "b.dcm", "PA", "Alpha^Ann", 2)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        session.save(sync=True)
        summary = session.ingest(str(tmp_path / "b"))
        assert (summary.ingested, summary.declined) == (1, 0)
        assert _shared_rows(session) == []
        assert sum(len(se.instances) for p in session.store.patients
                   for st in p.studies for se in st.series) == 2


def test_a_file_with_no_patient_id_links_under_the_holder(tmp_path):
    _write(tmp_path / "a" / "a.dcm", "PA", "Alpha^Ann", 1)
    _write(tmp_path / "b" / "b.dcm", None, "Alpha^Ann", 2)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "a"))
        summary = session.ingest(str(tmp_path / "b"))
        assert (summary.ingested, summary.declined) == (1, 0)
        assert _shared_rows(session) == []
        assert [(p.patient_id, sum(len(se.instances) for st in p.studies
                                   for se in st.series))
                for p in session.store.patients] == [("PA", 2)]


def test_a_second_file_of_the_same_patient_links(tmp_path):
    _write(tmp_path / "in" / "a.dcm", "PA", "Alpha^Ann", 1)
    _write(tmp_path / "in" / "b.dcm", "PA", "Alpha^Ann", 2)
    with Session(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert (summary.ingested, summary.declined) == (2, 0)
        assert _shared_rows(session) == []

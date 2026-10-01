"""A new file of a study `anonymize()` already de-identified, carrying the
patient's original ID, is settled by the next audit and pass (#894).

Measured at 7579d4df: after a pass over PA's study, a second file of that
study under Patient ID `PA` is linked into the `ANON_` patient's study.
`audit()` then raises the new instance's top-level copies of Patient's
Name, Patient ID, Study Instance UID and Study Date. Their owners already
hold this project's replacements, and the patient findings name the empty
`PA` patient that ingest created, not the `ANON_` one. `anonymize(report)`
wrote each copy to its owner's value, recorded it as a remediation's
output (another instance under the owner vouches for it), and then left
the finding unhandled because the owner "was not handed in". The new
instance read IDENTIFIED, the run graded REVIEW_REQUIRED under condition
7, and a third `audit()` raised nothing.

A copy now holding its owner's vouched value is the end state
`_owner_stamps_copy` already reads as satisfied when it finds it there,
so it is satisfied when the pass has just put it there.
"""
import sqlite3

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import PhiStatus

from support.ct_small_files import study_uid, write_ct

SHARED = 894


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(path, suffix):
    write_ct(path, "PA", SHARED, name="Alpha^Ann")
    ds = pydicom.dcmread(str(path))
    ds.SeriesInstanceUID = f"{study_uid(SHARED)}.{suffix}"
    ds.SOPInstanceUID = f"{study_uid(SHARED)}.{suffix}.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.save_as(str(path))


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _declines(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute("SELECT entity_uid, details FROM audit_log "
                            "WHERE action_type='REMEDIATION_DECLINED'").fetchall()


def _grade_basis(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if "**Grade Basis:**" in line]


def test_the_reingested_instance_ends_remediated(tmp_path):
    _write(tmp_path / "a" / "a.dcm", 1)
    _write(tmp_path / "b" / "b.dcm", 2)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        session.save(sync=True)
        first = {id(i) for i in _instances(session)}
        session.ingest(str(tmp_path / "b"))
        new = [i for i in _instances(session) if id(i) not in first]
        assert len(new) == 1

        session.anonymize(session.audit())

        assert [p.patient_id[:5] for p in session.store.patients] == ["ANON_"]
        assert new[0].phi_status is PhiStatus.REMEDIATED
        assert _declines(session) == []
        assert session.audit().findings == []
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        basis = _grade_basis(session, tmp_path)
    assert len(basis) == 1 and basis[0].startswith("*   **Grade Basis:** PASS -- "), basis

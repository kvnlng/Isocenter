"""A subject with an empty Patient ID has its dates shifted (#585); a pre-1.0
ID-less group is named at every open (#584, owner ruling Q5).

#585, measured at 63a64158 (`.agent/v1/L8-spec.md` §1.3) on 3.12 and 3.14t:
every pass declined the Study and instance date twice, "could not resolve
a PatientID to seed the jitter", because `_resolve_patient_id` tested the
empty ID for truth. The dates stayed IDENTIFIED, every re-audit raised them
again, the plain export wrote them unshifted and the safe export withheld
the instance. With #584 the subject has its own key, so the date is shifted
by its own offset and nothing in `remediation.py` changes.

A store written before 1.0 keeps its grouping: splitting it on open would
give already-shifted dates a second offset. A `''` group keeps declining,
loudly; an `UnknownPatient` group kept one shared offset silently, so every
open now writes one count-only `WARNING` row naming how many such patients
the store holds.
"""
import sqlite3

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import PhiStatus
from isocenter.persistence import SqliteStore
from isocenter.builders import DicomBuilder

from support.ct_small_files import write_ct

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


def _rows(session, action_type):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_an_empty_id_subjects_date_is_shifted(tmp_path, mode):
    """T-B7. Red at 63a64158 with two declines."""
    path = write_ct(tmp_path / "in" / "a.dcm", "TMP", "5851")
    ds = pydicom.dcmread(path)
    ds.PatientID = ""
    ds.save_as(path)
    source_date = str(ds.StudyDate)

    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        declined = _rows(session, "REMEDIATION_DECLINED")
        [patient] = session.store.patients
        study = patient.studies[0]
        instance = study.series[0].instances[0]
        statuses = (study.phi_status, instance.phi_status)
        again = session.audit()
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 check_burned_in=True)

    assert declined == []
    assert statuses == (PhiStatus.REMEDIATED, PhiStatus.REMEDIATED)
    assert not [f for f in again.findings if f.tag == "0008,0020"]
    assert summary.written == 1
    [written] = list((tmp_path / "out").rglob("*.dcm"))
    exported = pydicom.dcmread(str(written))
    assert str(exported.StudyDate) != source_date
    assert str(exported.PatientID) == ""


def _legacy_store(tmp_path, patient_ids):
    """A store as a release before 1.0 wrote it: one patient per given ID,
    `''` and `UnknownPatient` included, written by `SqliteStore` directly
    from a hand-built graph (ingest can no longer produce either ID)."""
    import datetime
    db = tmp_path / "legacy.db"
    patients = []
    for n, pid in enumerate(patient_ids):
        root = f"1.2.826.0.1.3680043.10.9998.{n}"
        builder = (DicomBuilder.start_patient(pid, f"Legacy^{n}")
                   .add_study(root, datetime.date(2004, 1, 19))
                   .add_series(root + ".1", "CT", 1))
        builder.add_instance(root + ".1.1", "1.2.840.10008.5.1.4.1.1.2", 1) \
            .set_attribute("0008,0020", "20040119")
        patients.append(builder.end_series().end_study().build())
    store = SqliteStore(str(db))
    try:
        store.save_all(patients)
    finally:
        store.stop()
    return db


def test_a_legacy_empty_id_patient_still_declines_its_shift(tmp_path):
    """T-B8, §3.5: nothing is split on open, and a `''` patient's date is
    still declined -- no silent shared seed."""
    db = _legacy_store(tmp_path, [""])
    with Session(str(db)) as session:
        [patient] = session.store.patients
        assert patient.patient_id == ""
        session.anonymize(session.audit())
        declined = _rows(session, "REMEDIATION_DECLINED")
    assert declined and all("PatientID" in details for _uid, details in declined), declined


@pytest.mark.parametrize("ids, expected", [
    (["", "UnknownPatient", "P1"], 2),
    (["UnknownPatient"], 1),
    (["P1"], 0),
], ids=["both", "placeholder", "control"])
def test_every_open_names_a_pre_1_0_id_less_group(tmp_path, ids, expected):
    """Q5: one count-only `WARNING` row at every open of a store holding a
    `''` or `UnknownPatient` patient. Kills M-B17 (the check dropped) and
    M-B18 (only `''` counted: the silent group is the one that needed it)."""
    db = _legacy_store(tmp_path, ids)
    for _ in range(2):
        with Session(str(db)) as session:
            pass
    with Session(str(db)) as session:
        rows = [details for _uid, details in _rows(session, "WARNING")
                if "no Patient ID" in details]
    if not expected:
        assert rows == []
        return
    assert len(rows) == 3, rows
    noun = "patient was" if expected == 1 else "patients were"
    assert all(details.startswith(f"{expected} {noun} grouped by a release "
                                  "before 1.0 from files with no Patient ID")
               for details in rows), rows
    # Counts only: no Patient ID, name or UID.
    assert all("Legacy^" not in d and "9998" not in d for d in rows)

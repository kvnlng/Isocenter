"""The export gates that do not depend on the format run once, in the session (#783).

Before #783, `Session._export_dicom` and `WfdbExporter.export` each held
their own copy of the gates around the write: the reading of
`patient_ids` and its unmatched-ID row, the save, the stale-policy
notice, the `EXPORT` row and `ExportError`. #783 moves them into one
private method, `Session._export_pipeline`, which both built-in formats
call; the format keeps only its own plan and write.

The hoist is meant to change no output and no row text. It does fix one
thing, measured at a946a1e6 on 3.12.14 and 3.14.7t: `wfdb` wrote its
unmatched-`patient_ids` `WARNING` row **before** its save, so when the save
refused (#721, #949) the store kept a row saying an export had selected
fewer patients than asked, for an export that never ran. `dicom` writes
that row after the save, and its comment calls the other order a
fabrication. Both formats now write it after the save.

These tests pin the order the hoist could break, and the row texts the
fingerprint's Accounting rows hold, byte for byte.

This file names `isocenter.session`, `isocenter.exporters` and
`isocenter.exporters.dicom`, so their probe rows are charged with it.
"""
import os
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter.io_handlers import ExportError
from isocenter.session import DicomSession

ROOT = "1.2.826.0.1.3680043.10.9999.783"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _ct(path, number):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PAT-783"
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.SOPInstanceUID = f"{ROOT}.1.1.{number}"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.InstanceNumber = number
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    ds.save_as(str(path))


def _ct_session(tmp_path, count=1, name="ct"):
    src = tmp_path / f"{name}-in"
    for number in range(1, count + 1):
        _ct(src / f"{number}.dcm", number)
    session = DicomSession(str(tmp_path / f"{name}.db"))
    session.ingest(str(src))
    return session


def _ecg_session(tmp_path, name="ecg"):
    from scripts.generate_waveform_test_data import write_fixture

    src = tmp_path / f"{name}-in"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-783", patient_name="Doe^Jane")
    session = DicomSession(str(tmp_path / f"{name}.db"))
    session.ingest(str(src))
    return session


def _instances(session):
    return [instance for patient in session.store.patients
            for study in patient.studies
            for series in study.series
            for instance in series.instances]


def _rows(db_path, action=None):
    """`(action, details)` in written order, read after the session is
    closed so the audit writer has drained."""
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT action_type, details FROM audit_log ORDER BY rowid"
        ).fetchall()
    return [r for r in rows if action is None or r[0] == action]


def _record_order(session, monkeypatch):
    """Record each `save()` and each audit row, in the order they happen."""
    events = []
    real_save = session.save
    real_log = session.store_backend.log_audit

    def save(*args, **kwargs):
        events.append(("save", None))
        return real_save(*args, **kwargs)

    def log_audit(action_type, entity_uid, details, *args, **kwargs):
        events.append((action_type, details))
        return real_log(action_type=action_type, entity_uid=entity_uid,
                        details=details, *args, **kwargs)

    monkeypatch.setattr(session, "save", save)
    monkeypatch.setattr(session.store_backend, "log_audit", log_audit)
    return events


# --------------------------------------------------------------------------
# The fix: no unmatched-ID row for an export whose save refused
# --------------------------------------------------------------------------

def test_a_wfdb_export_whose_save_refuses_writes_no_unmatched_id_row(tmp_path):
    """An unsaved instance with a SOP Instance UID that is not a `str`
    makes the opening save raise `ValueError` (#721). Before #783 the
    `wfdb` row about the unknown ID was already in the store: 1 row,
    measured. `dicom` wrote 0."""
    db = tmp_path / "ecg.db"
    with _ecg_session(tmp_path) as session:
        before = len(_rows(db, "WARNING"))
        [instance] = _instances(session)
        instance.sop_instance_uid = 12345
        with pytest.raises(ValueError):
            session.export(str(tmp_path / "out"), format="wfdb",
                           patient_ids=["NOPE"])
        instance.sop_instance_uid = f"{ROOT}.9"
    assert len(_rows(db, "WARNING")) == before


def test_a_dicom_export_whose_save_refuses_writes_no_unmatched_id_row(
        tmp_path):
    db = tmp_path / "ct.db"
    with _ct_session(tmp_path) as session:
        before = len(_rows(db, "WARNING"))
        [instance] = _instances(session)
        instance.sop_instance_uid = 12345
        with pytest.raises(ValueError):
            session.export(str(tmp_path / "out"), patient_ids=["NOPE"],
                           show_progress=False)
        instance.sop_instance_uid = f"{ROOT}.9"
    assert len(_rows(db, "WARNING")) == before


@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_both_formats_write_their_unmatched_row_once_and_after_the_save(
        tmp_path, monkeypatch, fmt):
    make = _ct_session if fmt == "dicom" else _ecg_session
    with make(tmp_path) as session:
        events = _record_order(session, monkeypatch)
        session.export(str(tmp_path / "out"), format=fmt,
                       patient_ids=["NOPE"])
    unmatched = [n for n, (action, details) in enumerate(events)
                 if action == "WARNING" and details
                 and "patient_ids" in details]
    saves = [n for n, (action, _) in enumerate(events) if action == "save"]
    assert len(unmatched) == 1, events
    assert saves and saves[0] < unmatched[0], events


# --------------------------------------------------------------------------
# Every refusal before the scan and the save
# --------------------------------------------------------------------------

def _record_calls(session, monkeypatch):
    calls = []
    for name in ("audit", "save"):
        real = getattr(session, name)

        def recorded(*args, _name=name, _real=real, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(session, name, recorded)
    return calls


def test_a_typo_beside_check_burned_in_records_no_status_and_no_row(
        tmp_path, monkeypatch):
    db = tmp_path / "ct.db"
    with _ct_session(tmp_path) as session:
        before = _rows(db)
        statuses = [i._phi_status for i in _instances(session)]
        calls = _record_calls(session, monkeypatch)
        with pytest.raises(TypeError):
            session.export(str(tmp_path / "out"), check_burned_in=True,
                           bogus=1)
        assert calls == []
        assert [i._phi_status for i in _instances(session)] == statuses
    assert _rows(db) == before


def test_a_bare_str_patient_ids_is_refused_before_the_scan(
        tmp_path, monkeypatch):
    with _ct_session(tmp_path) as session:
        calls = _record_calls(session, monkeypatch)
        with pytest.raises(TypeError):
            session.export(str(tmp_path / "out"), check_burned_in=True,
                           patient_ids="PAT-783")
        assert calls == []


def test_a_bad_subset_is_refused_before_the_scan_and_the_save(
        tmp_path, monkeypatch):
    with _ct_session(tmp_path) as session:
        calls = _record_calls(session, monkeypatch)
        with pytest.raises(TypeError):
            session.export(str(tmp_path / "out"), check_burned_in=True,
                           subset=b"not a subset")
        assert calls == []


def test_the_counters_are_cleared_when_the_format_refuses(tmp_path):
    with _ct_session(tmp_path) as session:
        session.export(str(tmp_path / "out1"), show_progress=False)
        assert session._last_export_written == 1
        with pytest.raises(TypeError):
            session.export(str(tmp_path / "out2"), patient_ids="PAT-783")
        assert session._last_export_written is None
        assert session._last_export_requested is None


# --------------------------------------------------------------------------
# One EXPORT row, in the words it always had
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_each_format_writes_one_export_row(tmp_path, fmt):
    make = _ct_session if fmt == "dicom" else _ecg_session
    with make(tmp_path, name=fmt) as session:
        session.export(str(tmp_path / "out"), format=fmt)
    assert len(_rows(tmp_path / f"{fmt}.db", "EXPORT")) == 1


def _export_detail(db):
    [(_, details)] = _rows(db, "EXPORT")
    return details


def test_the_export_rows_keep_their_text(tmp_path):
    """One export per DICOM branch and a clean WFDB one, each row compared
    whole: the local twin of the fingerprint's Accounting rows."""
    out = str(tmp_path / "out")

    with _ct_session(tmp_path, name="clean") as session:
        session.export(out, show_progress=False)
    assert _export_detail(tmp_path / "clean.db") == (
        f"DICOM export to {out}: wrote 1 of 1 planned instances from 1 "
        f"patients.")

    with _ct_session(tmp_path, count=2, name="partial") as session:
        _instances(session)[1].sop_instance_uid = ".refused"
        session.export(out, show_progress=False)
    assert _export_detail(tmp_path / "partial.db") == (
        f"DICOM export to {out}: wrote 1 of 2 planned instances from 1 "
        f"patients.")

    with _ct_session(tmp_path, name="withheld") as session:
        session.export(out, check_burned_in=True, show_progress=False)
    assert _export_detail(tmp_path / "withheld.db") == (
        f"DICOM export to {out}: wrote 0 of 1 requested instances; all 1 "
        f"were withheld by the pre-export scan (check_burned_in=True).")

    with _ct_session(tmp_path, name="empty") as session:
        session.export(out, patient_ids=[], show_progress=False)
    assert _export_detail(tmp_path / "empty.db") == (
        f"DICOM export to {out}: wrote 0 of 0 planned instances; nothing "
        f"matched the export plan.")

    with _ecg_session(tmp_path, name="ecg") as session:
        session.export(out, format="wfdb")
    assert _export_detail(tmp_path / "ecg.db") == (
        f"WFDB export to {out}: wrote 1 record, 0 instances failed.")


def test_every_wfdb_record_failing_raises_after_its_export_row(
        tmp_path, monkeypatch):
    from isocenter.exporters.wfdb import WfdbExporter

    def failing(*_args, **_kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(WfdbExporter, "_write_instance", failing)
    with _ecg_session(tmp_path) as session:
        with pytest.raises(ExportError) as raised:
            session.export(str(tmp_path / "out"), format="wfdb")
    assert raised.value.attempted == 1
    assert _export_detail(tmp_path / "ecg.db").endswith(
        "wrote 0 records, 1 instance failed.")

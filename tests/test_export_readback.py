"""`export(verify_readback=True)` re-reads what it wrote (#209).

"The write did not raise" is a weaker claim than "a file exists that
decodes to what we meant", and the compliance report presents the
stronger one. Opting in makes the worker `dcmread` each file straight
after writing it and compare the descriptors that tie the pixels to
their meaning -- Rows, Columns, SamplesPerPixel, NumberOfFrames,
BitsAllocated -- against the dataset it just serialized. An unreadable
file or a mismatch is an export failure: it travels back through
`ExportOutcome(ok=False)`, files an `ERROR` audit row and takes the
grade to `REVIEW_REQUIRED`, exactly as a write that raised does (#181).

The check runs against the temporary file, *before* the rename that
publishes it (#199) -- so a file that fails verification never appears
under its real name, and "Instances Written" keeps counting only files
a recipient can trust.

Worker-level arms call `_export_instance_worker` in this process and
booby-trap `pydicom.dcmread`, because a real readback failure means the
serializer lied about what it wrote -- precisely the defect the suite
cannot produce on demand. The session-level arms force threads
(`ISOCENTER_FORCE_THREADS`) so the same trap is visible inside the
workers; `_run_export_batch` pins `maxtasksperchild=25`, which rules
threads out, so those arms route the real batch through
`DicomExporter.export_batch` without it.
"""
import logging
import sqlite3
from datetime import date

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import (DicomExporter, ExportContext,
                                   _export_instance_worker)
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

CT_REQUIRED = (
    ("0008,0020", "20230101"), ("0008,0030", "120000"),
    ("0008,0060", "CT"),
    ("0018,0050", "1.0"), ("0018,0060", "120"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
)


def _instance(n=0):
    inst = Instance(f"1.2.826.0.1.{n}", CT_STORAGE, n + 1)
    inst.file_path = None
    for tag, value in CT_REQUIRED:
        inst.set_attr(tag, value)
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
    return inst


def _ctx(tmp_path, **kwargs):
    return ExportContext(
        instance=_instance(),
        output_path=str(tmp_path / "out" / "1.2.826.0.1.0.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        **kwargs)


def _session(tmp_path):
    session = DicomSession(str(tmp_path / "readback.db"))
    patient = Patient("PAT1", "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)
    for n in range(3):
        series.instances.append(_instance(n))
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save()
    return session


def _files(out):
    return sorted(p.name for p in out.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Worker-level: what the readback accepts, rejects, and never runs.
# ---------------------------------------------------------------------------

def test_readback_is_off_by_default_and_reads_nothing_back(
        tmp_path, monkeypatch):
    """Off means off: no second parse, whatever state pydicom is in."""
    def _boom(*_a, **_k):
        raise AssertionError("readback ran without being asked for")
    monkeypatch.setattr(pydicom, "dcmread", _boom)

    outcome = _export_instance_worker(_ctx(tmp_path))

    assert outcome.ok, outcome.error
    assert (tmp_path / "out" / "1.2.826.0.1.0.dcm").exists()


def test_readback_passes_a_faithful_file(tmp_path):
    outcome = _export_instance_worker(_ctx(tmp_path, verify_readback=True))

    assert outcome.ok, outcome.error
    assert _files(tmp_path / "out") == ["1.2.826.0.1.0.dcm"]


def test_an_unreadable_file_is_an_export_failure(tmp_path, monkeypatch):
    def _unreadable(*_a, **_k):
        raise InvalidDicomError("no DICM marker")
    monkeypatch.setattr(pydicom, "dcmread", _unreadable)

    outcome = _export_instance_worker(_ctx(tmp_path, verify_readback=True))

    assert not outcome.ok
    assert "read back" in str(outcome.error), outcome.error
    # A file that failed verification is not delivered, and the temp it
    # was verified under is cleaned up by the worker.
    assert _files(tmp_path / "out") == [], _files(tmp_path / "out")


def test_a_descriptor_mismatch_is_an_export_failure_naming_the_descriptor(
        tmp_path, monkeypatch):
    real = pydicom.dcmread

    def _lying(*args, **kwargs):
        ds = real(*args, **kwargs)
        ds.Rows = ds.Rows + 1
        return ds
    monkeypatch.setattr(pydicom, "dcmread", _lying)

    outcome = _export_instance_worker(_ctx(tmp_path, verify_readback=True))

    assert not outcome.ok
    assert "Rows" in str(outcome.error), outcome.error
    assert _files(tmp_path / "out") == [], _files(tmp_path / "out")


# ---------------------------------------------------------------------------
# Session-level: the option threads through, and the failure reaches the
# report through the same channel a failed write does (#181).
# ---------------------------------------------------------------------------

def _thread_visible_batch(monkeypatch):
    """Route the export batch where a parent monkeypatch can see it.

    `_run_export_batch` pins `maxtasksperchild=25`, which forces
    processes however the environment is set; dropping it and forcing
    threads keeps the whole pipeline real from `export_batch` down.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    def _batch(tasks, show_progress, store_backend=None):
        return DicomExporter.export_batch(
            tasks, show_progress=show_progress, total=len(tasks),
            store_backend=store_backend)
    monkeypatch.setattr(DicomSession, "_run_export_batch",
                        staticmethod(_batch))


def test_a_readback_failure_files_an_error_row_and_fails_the_grade(
        tmp_path, monkeypatch):
    _thread_visible_batch(monkeypatch)
    real = pydicom.dcmread

    def _lying(*args, **kwargs):
        ds = real(*args, **kwargs)
        ds.Rows = ds.Rows + 1
        return ds
    monkeypatch.setattr(pydicom, "dcmread", _lying)

    session = _session(tmp_path)
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    try:
        session.anonymize()
        session.export(str(out), show_progress=False, verify_readback=True)
        session.generate_report(str(report))
        db_path = session.store_backend.db_path
    finally:
        session.close()

    assert _files(out) == [], _files(out)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action_type='ERROR'"
        ).fetchall()
    assert len(rows) == 3, rows
    assert all("Rows" in details for (details,) in rows), rows

    content = report.read_text(encoding="utf-8")
    assert "| **Validation Status** | **REVIEW_REQUIRED** |" in content
    assert "| Instances Written | 0 of 3 requested |" in content


def test_with_readback_disabled_the_same_trap_changes_nothing(
        tmp_path, monkeypatch):
    """Default off must mean the exact pre-#209 behavior, cost included."""
    _thread_visible_batch(monkeypatch)

    def _boom(*_a, **_k):
        raise AssertionError("readback ran without being asked for")
    monkeypatch.setattr(pydicom, "dcmread", _boom)

    session = _session(tmp_path)
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    try:
        session.anonymize()
        session.export(str(out), show_progress=False)
        session.generate_report(str(report))
    finally:
        session.close()

    assert len(_files(out)) == 3, _files(out)
    content = report.read_text(encoding="utf-8")
    assert "| **Validation Status** | **PASS** |" in content


def test_a_healthy_export_with_readback_enabled_stays_pass(tmp_path):
    """The check must be free of false positives on the ordinary path.

    Real subprocess workers, no traps: this is the arm that proves the
    option survives pickling into `ExportContext` and that a verified
    clean run reads exactly like an unverified one.
    """
    session = _session(tmp_path)
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    try:
        session.anonymize()
        session.export(str(out), show_progress=False, verify_readback=True)
        session.generate_report(str(report))
        db_path = session.store_backend.db_path
    finally:
        session.close()

    assert len(_files(out)) == 3, _files(out)
    with sqlite3.connect(db_path) as conn:
        errors = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type='ERROR'"
        ).fetchone()[0]
    assert errors == 0
    content = report.read_text(encoding="utf-8")
    assert "| **Validation Status** | **PASS** |" in content
    assert "| Instances Written | 3 of 3 requested |" in content

"""Every export run writes an EXPORT audit row, and the report knows the
boundary that row marks (#166, #153).

Two halves of one seam:

- README promises the Audit Trail counts "every action (Anonymize,
  Redact, Export)". Anonymize and Redact wrote rows; Export never did
  (#166). `log_audit`'s own docstring lists 'EXPORT' as its first
  example action type -- the row was always intended.

- `generate_report()` grades the audit log as it stands when called.
  Export-time losses are written during `export()`, so a report
  generated first misses every one of them and grades PASS on a run
  that dropped data (#153: `report_then_export -> PASS`,
  `export_then_report -> REVIEW_REQUIRED`, same session, same loss).
  The EXPORT row is what lets the report say so out loud: no EXPORT
  row in the log means export-time losses cannot have been recorded
  yet, durably, across a session reopened on an existing store.

The end-to-end tests here also close #153's coverage note: nothing
exercised emitter -> grade. `tests/test_export_loss_audit.py` stops at
the audit row; `tests/test_data_loss_reporting.py` hand-injects rows.
These plant a real export-time loss, export, generate, and read the
grade.
"""
import os
import sqlite3

import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.session import DicomSession

# `_fallback_encoding` returns None for this: not bytes, not a number,
# not a string. It is the one shape that reaches `_merge`'s "no VR
# fits" arm -- the only export-side emitter that derives a PRIVATE
# scope from a real tag (#153).
UNENCODABLE = object()

PRIVATE_TAG = "0009,1003"

BOUNDARY_NOTE_MARKER = "generated before any export"


def _write_src(folder):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)
    return ds.SOPInstanceUID


def _session(tmp_path, name="boundary.db"):
    src = tmp_path / "src"
    if not src.exists():
        src.mkdir()
        _write_src(str(src))
    session = DicomSession(persistence_file=str(tmp_path / name))
    session.ingest(str(src))
    return session


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _export_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='EXPORT'").fetchall()


def _report_text(session, tmp_path, name):
    path = str(tmp_path / name)
    session.generate_report(path)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _grade(text):
    for line in text.splitlines():
        if "Validation Status" in line:
            return line
    raise AssertionError(f"no Validation Status line in report:\n{text}")


# ---------------------------------------------------------------- #166


def test_export_writes_an_unconditional_export_audit_row(tmp_path):
    """One row per run, naming the destination and the counts."""
    out = str(tmp_path / "out")
    session = _session(tmp_path)
    try:
        session.export(out, format="dicom", show_progress=False)
        summary = session.store_backend.get_audit_summary()
        db_path = session.store_backend.db_path
    finally:
        session.close()

    assert summary.get("EXPORT") == 1, summary
    rows = _export_rows(db_path)
    assert len(rows) == 1, rows
    entity_uid, details = rows[0]
    assert entity_uid == out
    # The counts are the row's payload: "an export happened" without
    # them cannot say whether it delivered the plan.
    assert "1 of 1" in details, details
    assert out in details, details


def test_each_export_run_writes_its_own_row(tmp_path):
    session = _session(tmp_path)
    try:
        session.export(str(tmp_path / "out1"), format="dicom",
                       show_progress=False)
        session.export(str(tmp_path / "out2"), format="dicom",
                       show_progress=False)
        summary = session.store_backend.get_audit_summary()
    finally:
        session.close()

    assert summary.get("EXPORT") == 2, summary


def test_the_export_row_reaches_the_audit_trail_table(tmp_path):
    """README.md promises the count; the table is rendered straight
    from `get_audit_summary()`, so the row is the promise."""
    session = _session(tmp_path)
    try:
        session.export(str(tmp_path / "out"), format="dicom",
                       show_progress=False)
        text = _report_text(session, tmp_path, "report.md")
    finally:
        session.close()

    assert "| EXPORT | 1 |" in text, text


def test_a_wfdb_export_writes_an_export_row_too(tmp_path):
    """The boundary note keys on the row's absence, so a format whose
    run wrote no row would re-open #153 for its users: a waveform-only
    session would carry "generated before any export" over a report
    generated after one."""
    from scripts.generate_waveform_test_data import write_fixture

    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-1", patient_name="Doe^Jane")

    session = DicomSession(persistence_file=str(tmp_path / "wfdb.db"))
    try:
        session.ingest(str(src))
        session.export(str(tmp_path / "out"), format="wfdb")
        summary = session.store_backend.get_audit_summary()
    finally:
        session.close()

    assert summary.get("EXPORT") == 1, summary


# ---------------------------------------------------------------- #153


def test_a_report_generated_before_any_export_carries_the_boundary_note(
        tmp_path):
    session = _session(tmp_path)
    try:
        session.anonymize()
        text = _report_text(session, tmp_path, "before.md")
    finally:
        session.close()

    assert BOUNDARY_NOTE_MARKER in text, text
    # The note states the boundary; it does not flunk the run. An
    # audit-only session that never exports keeps its grade.
    assert "PASS" in _grade(text), text


def test_a_report_generated_after_export_does_not_carry_the_note(tmp_path):
    session = _session(tmp_path)
    try:
        session.anonymize()
        session.export(str(tmp_path / "out"), format="dicom",
                       show_progress=False)
        text = _report_text(session, tmp_path, "after.md")
    finally:
        session.close()

    assert BOUNDARY_NOTE_MARKER not in text, text


def test_the_boundary_survives_a_reopened_session(tmp_path):
    """Keyed on the audit log, not a per-process flag: a session
    reopened on a store that already exported must not claim the
    boundary."""
    db = str(tmp_path / "reopen.db")
    session = _session(tmp_path, "reopen.db")
    try:
        session.export(str(tmp_path / "out"), format="dicom",
                       show_progress=False)
    finally:
        session.close()

    reopened = DicomSession(persistence_file=db)
    try:
        text = _report_text(reopened, tmp_path, "reopened.md")
    finally:
        reopened.close()

    assert BOUNDARY_NOTE_MARKER not in text, text


# ------------------------------------------------- emitter -> grade


def test_an_export_time_loss_reaches_the_grade_when_report_follows_export(
        tmp_path):
    """The pipeline #153 proved broken in the documented order, run in
    the order both documents now agree on: a real loss planted on the
    graph, exported, and read back as the grade -- no hand-injected
    audit rows anywhere."""
    session = _session(tmp_path)
    try:
        for inst in _instances(session):
            inst.attributes[PRIVATE_TAG] = UNENCODABLE
        session.export(str(tmp_path / "out"), format="dicom",
                       show_progress=False)
        text = _report_text(session, tmp_path, "graded.md")
    finally:
        session.close()

    assert "REVIEW_REQUIRED" in _grade(text), text
    assert PRIVATE_TAG in text, text


def test_the_old_order_now_says_out_loud_what_it_cannot_see(tmp_path):
    """CLAUDE.md's pre-#153 order, replayed: the pre-export report
    still cannot contain the loss -- it does not exist yet -- but it
    now names its own boundary instead of silently grading PASS."""
    session = _session(tmp_path)
    try:
        # Anonymize first so the pre-export grade is PASS on its own
        # merits (an empty audit trail grades REVIEW_REQUIRED for a
        # different reason), which is #153's measured setup.
        session.anonymize()
        for inst in _instances(session):
            inst.attributes[PRIVATE_TAG] = UNENCODABLE
        before = _report_text(session, tmp_path, "before.md")
        session.export(str(tmp_path / "out"), format="dicom",
                       show_progress=False)
        after = _report_text(session, tmp_path, "after.md")
    finally:
        session.close()

    assert BOUNDARY_NOTE_MARKER in before, before
    assert "PASS" in _grade(before), before
    assert BOUNDARY_NOTE_MARKER not in after, after
    assert "REVIEW_REQUIRED" in _grade(after), after

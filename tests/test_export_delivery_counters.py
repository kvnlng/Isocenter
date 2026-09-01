"""The delivery counters must describe *this* export, and count real files.

Two ways the `Instances Written` row and the recoverable-identity
disclosure introduced by #181/#187 could state something false:

* **Staleness (#196).** `_last_export_written`/`_last_export_requested`
  were assigned only at the successful end of `_export_dicom`, so an
  export that returned early (empty plan) or raised left a *previous*
  export's numbers standing -- "3 of 3 requested" under a PASS, beside
  an empty output folder. The fix clears both at the top of the export,
  so an export that does not complete leaves the row absent: an absent
  row says "not answered here", where a stale one answers for the wrong
  export.

* **Duplicate SOP Instance UID (#197).** Filenames are the SOP Instance
  UID, so two instances sharing one are written to the same path and
  the second silently overwrites the first -- while the counters
  described two delivered files and the disclosure counted its
  numerator over the tasks and its denominator over a de-duplicated
  set, rendering "2 of 1 exported instances". Both counts now come from
  the de-duplicated set, and the overwrite itself is filed as an
  `ERROR` audit row -- the same channel #181 uses for every other
  instance that was requested and is not in the folder.
"""
import logging
import os
import sqlite3
from datetime import date

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import DicomExporter
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

#: Everything IODValidator demands of a CT image.
CT_REQUIRED = (
    ("0008,0020", "20230101"), ("0008,0030", "120000"),
    ("0008,0060", "CT"),
    ("0018,0050", "1.0"), ("0018,0060", "120"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
)

#: The one UID both instances of the duplicate fixture carry. A
#: duplicated file in a real archive arrives exactly like this: same
#: bytes, same SOP Instance UID, two entries in the in-memory graph.
DUP_UID = "1.2.826.0.1.999"


def _instance(uid, number):
    inst = Instance(uid, CT_STORAGE, number)
    inst.file_path = None
    for tag, value in CT_REQUIRED:
        inst.set_attr(tag, value)
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
    return inst


def _session(tmp_path, uids, lock=False):
    session = DicomSession(str(tmp_path / "counters.db"))
    if lock:
        session.enable_reversible_anonymization(str(tmp_path / "test.key"))

    pid = "PAT1"
    patient = Patient(pid, "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)
    for n, uid in enumerate(uids):
        inst = _instance(uid, n + 1)
        inst.set_attr("0010,0010", "Original Name")
        inst.set_attr("0010,0020", pid)
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save()

    if lock:
        session.lock_identities(pid)
        session.save()
    return session


def _audit(db_path, action_type):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


def _report(session, path):
    session.generate_report(str(path))
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 196 -- an export that does not complete must not inherit the last one's row
# ---------------------------------------------------------------------------

def test_an_empty_plan_does_not_reuse_the_previous_exports_numbers(tmp_path):
    """Export 2 matches nothing; the report must not say "3 of 3"."""
    session = _session(tmp_path, ["1.2.826.0.1.0", "1.2.826.0.1.1",
                                  "1.2.826.0.1.2"])
    try:
        session.anonymize()
        session.export(str(tmp_path / "out1"), show_progress=False)
        first = _report(session, tmp_path / "report1.md")
        assert "| Instances Written | 3 of 3 requested |" in first, first

        session.export(str(tmp_path / "out2"), show_progress=False,
                       subset=["NO_SUCH_UID"])
        second = _report(session, tmp_path / "report2.md")
    finally:
        session.close()

    assert list((tmp_path / "out2").rglob("*.dcm")) == []
    assert "Instances Written" not in second, second


def test_a_raising_export_does_not_reuse_the_previous_exports_numbers(
        tmp_path, monkeypatch):
    """Export 2's batch dies at the pool; the row must go absent, not stale.

    `export_batch` is called in the parent process, so a monkeypatch
    reaches it -- unlike the workers, which are always subprocesses.
    """
    session = _session(tmp_path, ["1.2.826.0.1.0", "1.2.826.0.1.1",
                                  "1.2.826.0.1.2"])
    try:
        session.anonymize()
        session.export(str(tmp_path / "out1"), show_progress=False)
        first = _report(session, tmp_path / "report1.md")
        assert "| Instances Written | 3 of 3 requested |" in first, first

        def _pool_died(*_args, **_kwargs):
            raise RuntimeError("pool died")

        monkeypatch.setattr(DicomExporter, "export_batch", _pool_died)
        with pytest.raises(RuntimeError, match="pool died"):
            session.export(str(tmp_path / "out2"), show_progress=False)
        second = _report(session, tmp_path / "report2.md")
    finally:
        session.close()

    assert list((tmp_path / "out2").rglob("*.dcm")) == []
    assert "Instances Written" not in second, second


# ---------------------------------------------------------------------------
# 197 -- two instances, one SOP Instance UID, one file
# ---------------------------------------------------------------------------

def test_duplicate_uid_counters_count_files_not_write_operations(tmp_path):
    """One file exists; "2 of 2 requested" described the overwrite as
    two delivered files."""
    session = _session(tmp_path, [DUP_UID, DUP_UID])
    out = tmp_path / "out"
    try:
        session.anonymize()
        session.export(str(out), show_progress=False)
        content = _report(session, tmp_path / "report.md")
    finally:
        session.close()

    files = list(out.rglob("*.dcm"))
    assert len(files) == 1, files
    assert "| Instances Written | 1 of 2 requested |" in content, content


def test_duplicate_uid_disclosure_numerator_and_denominator_agree(
        tmp_path, caplog):
    """"2 of 1 exported instances" cannot be true under any reading.

    The numerator was counted over the tasks and the denominator over a
    de-duplicated set. Both must come from the same collection, so the
    row reads "1 of 1": one delivered file carries a token.
    """
    session = _session(tmp_path, [DUP_UID, DUP_UID], lock=True)
    out = tmp_path / "out"
    try:
        with caplog.at_level(logging.WARNING):
            session.export(str(out), show_progress=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    assert len(list(out.rglob("*.dcm"))) == 1

    rows = _audit(db_path, "REVERSIBLE_EXPORT")
    assert len(rows) == 1, rows
    assert "1 of 1 exported instances" in rows[0][1], rows[0][1]
    msgs = [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]
    assert any("1 of 1 exported instances" in m for m in msgs), msgs


def test_the_overwrite_is_filed_in_the_audit_log_and_moves_the_grade(
        tmp_path):
    """The folder holds one file for two requested instances; the report's
    story has to match the folder.

    `ERROR` is the channel #181 built for exactly this end state -- an
    instance that was requested and is not in the folder -- so the row
    lands in the Exceptions section and the run grades REVIEW_REQUIRED,
    the same as any other undelivered instance.
    """
    session = _session(tmp_path, [DUP_UID, DUP_UID])
    try:
        session.anonymize()
        session.export(str(tmp_path / "out"), show_progress=False)
        content = _report(session, tmp_path / "report.md")
        db_path = session.store_backend.db_path
    finally:
        session.close()

    rows = _audit(db_path, "ERROR")
    assert len(rows) == 1, rows
    uid, details = rows[0]
    assert uid == DUP_UID
    assert DUP_UID in details, details
    assert "2" in details, details
    assert "overw" in details.lower(), details
    assert "|" not in details and "\n" not in details, (
        "the detail is rendered into a markdown table row")

    assert "| **Validation Status** | **REVIEW_REQUIRED** |" in content, content
    assert "## 4. Exceptions & Errors" in content
    assert DUP_UID in content.split("## 4. Exceptions & Errors")[1], content


def test_unique_uids_file_no_collision_row(tmp_path):
    """The control: the row exists for collisions, not for exporting."""
    session = _session(tmp_path, ["1.2.826.0.1.0", "1.2.826.0.1.1"])
    try:
        session.anonymize()
        session.export(str(tmp_path / "out"), show_progress=False)
        content = _report(session, tmp_path / "report.md")
        db_path = session.store_backend.db_path
    finally:
        session.close()

    assert _audit(db_path, "ERROR") == []
    assert "| Instances Written | 2 of 2 requested |" in content, content
    assert "| **Validation Status** | **PASS** |" in content, content

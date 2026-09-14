"""An instance the pre-export scan withholds is audited, counted and graded (#536).

`export(check_burned_in=True)` scans before it writes and withholds every
instance that still carries an identifier, at any level of its hierarchy.
Withholding is right. What was missing is every record of it: the filter
logged one line and dropped the instance from the plan, so the audit log,
the delivery counters and the grade all described the cohort that was
*written* as though it were the cohort that was *asked for*.

Measured on ac33641, before the fix:

- a CT never anonymized, exported with `check_burned_in=True`, returned
  an empty summary, wrote one `EXPORT` row calling the plan empty
  ("nothing matched the export plan"), left the counters unset, and
  graded **PASS**;
- two patients anonymized, one given an identifier back: "1 of 1
  requested", **PASS**.

The fix is the #479 pattern: one `WARNING` row per withheld instance,
through `log_audit(action_type="WARNING")`, which grades through the
existing section-4 term. `WARNING` rather than `ERROR`, because nothing
was written wrong -- an instance was held back. No new grade term: the
rows are the one expression of the fact, and they survive a reopen where
a counter would not.

**The subset is checked first.** An instance the caller never selected
was not withheld from anything, and until this change the identifier
test ran first and logged "Skipping" for it too; a row inheriting that
order would have graded a run on an instance nobody asked for.
"""
import logging
import os
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter.io_handlers import DicomExporter, ExportError, ExportSummary
from isocenter.session import DicomSession

ROOT = "1.2.826.0.1.3680043.10.536"

#: Both parallel paths. `audit()` -- the scan behind the filter --
#: clones the graph whichever it picks, through a `copy` in one and a
#: pickle in the other. The export batch itself always runs in processes
#: (#185), so the axis is the scan's.
MODES = ["threads", "processes"]


@pytest.fixture
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write_ct(folder, patient_id, suffix, name):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = patient_id
    ds.PatientName = name
    ds.StudyInstanceUID = f"{ROOT}.{suffix}"
    ds.SeriesInstanceUID = f"{ROOT}.{suffix}.1"
    ds.SOPInstanceUID = f"{ROOT}.{suffix}.1.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    os.makedirs(folder, exist_ok=True)
    ds.save_as(os.path.join(folder, f"{suffix}.dcm"))
    return ds.SOPInstanceUID


def _instances(session):
    return {i.sop_instance_uid: (p, i) for p in session.store.patients
            for st in p.studies for se in st.series for i in se.instances}


def _rows(session, action_type):
    """Rows of one type, read through the barrier: `log_audit` enqueues."""
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


def _report(session, tmp_path, name="report.md"):
    path = tmp_path / name
    session.generate_report(str(path))
    return path.read_text(encoding="utf-8")


def _grade(text):
    lines = [line for line in text.splitlines() if "Validation Status" in line]
    assert len(lines) == 1, text
    return lines[0]


def _written(out):
    return sorted(f for _r, _d, files in os.walk(str(out))
                  for f in files if f.endswith(".dcm"))


def _two_patients_one_identified(tmp_path, level):
    """Case b: anonymize both, then give one an identifier back.

    `level` picks where the identifier goes, so the row's level word is
    measured rather than assumed. Returns (session, withheld uid, kept
    uid, the value put back).
    """
    src = tmp_path / "src"
    a = _write_ct(str(src), "PAT-536-A", "11", "Alpha^Test")
    b = _write_ct(str(src), "PAT-536-B", "12", "Beta^Test")
    session = DicomSession(persistence_file=str(tmp_path / "b.db"))
    session.ingest(str(src))
    session.anonymize()
    patient, inst = _instances(session)[a]
    if level == "instance":
        value = "St Elsewhere Hospital"
        inst.set_attr("0008,0080", value)
    else:
        value = "Doe^Jane"
        patient.patient_name = value
        patient.mark_modified()
    return session, a, b, value


@pytest.mark.parametrize("mode", MODES, indirect=True)
@pytest.mark.parametrize("level", ["instance", "patient"])
def test_each_withheld_instance_writes_a_warning_row(tmp_path, mode, level):
    """One row per withheld instance, naming it and the level, never the value.

    The level is the only thing about the identifier the row may say.
    The value is PHI, and the Patient ID beside it is the pseudonym the
    cohort is keyed on -- neither belongs in a trail that renders into
    the report a recipient reads.
    """
    session, withheld, kept, value = _two_patients_one_identified(
        tmp_path, level)
    try:
        patient, _inst = _instances(session)[withheld]
        patient_id = patient.patient_id
        out = tmp_path / "out"
        summary = session.export(str(out), check_burned_in=True,
                                 use_compression=False, show_progress=False)
        warnings = _rows(session, "WARNING")
    finally:
        session.close()

    assert summary.written_uids == [kept], summary
    assert _written(out) == [f"{kept}.dcm"]

    assert len(warnings) == 1, warnings
    uid, details = warnings[0]
    assert uid == withheld
    assert withheld in details, details
    assert "withheld" in details, details
    assert f"its {level} still carries an identifier" in details, details
    assert value not in details, details
    assert patient_id not in details, details


def test_a_withheld_export_grades_review_required(tmp_path):
    """Graded through section 4, and section 4 names the instance (Q3)."""
    session, withheld, _kept, _value = _two_patients_one_identified(
        tmp_path, "instance")
    try:
        session.export(str(tmp_path / "out"), check_burned_in=True,
                       use_compression=False, show_progress=False)
        text = _report(session, tmp_path)
    finally:
        session.close()

    assert "REVIEW_REQUIRED" in _grade(text), text
    section_5 = text.split("## 5. Validation & Verification", 1)[1]
    assert ("**Grade Basis:** REVIEW_REQUIRED, for 1 reason(s):\n"
            "    *   1 row(s) in section 4 (Exceptions & Errors)\n"
            in section_5), section_5
    section_4 = text.split("## 4.", 1)[1].split("## 5.", 1)[0]
    assert withheld in section_4 and "withheld" in section_4, section_4


def test_a_withheld_row_is_one_table_row_whatever_the_folder_holds(tmp_path):
    """The folder is the caller's, and may hold a pipe or a newline.

    Section 4 renders `details` into a markdown table cell, so the row is
    flattened and pipe-escaped as `_audit_unread_instances` does. A folder
    name is the realistic carrier: a `|` is legal on POSIX, and so is a
    line break. `\\r\\n`, so a flattening that replaced `\\n` alone would
    leave a `\\r` to split the section-4 row.
    """
    session, withheld, _kept, _value = _two_patients_one_identified(
        tmp_path, "instance")
    folder = str(tmp_path / "out|a\r\nb")
    try:
        session.export(folder, check_burned_in=True, use_compression=False,
                       show_progress=False)
        warnings = _rows(session, "WARNING")
        text = _report(session, tmp_path)
    finally:
        session.close()

    # The whitespace join the code uses, not a `.replace` of the newline:
    # the join also collapses any run of spaces a temporary directory's
    # own path may hold, and a `.replace` would then expect them intact.
    def flattened(text):
        return " ".join(text.split()).replace("|", "\\|")

    [(uid, details)] = warnings
    assert uid == withheld
    assert details == flattened(
        f"DICOM export to {folder} withheld instance {withheld}: its "
        f"instance still carries an identifier the pre-export scan raised "
        f"(check_burned_in=True)."), details
    assert "\n" not in details and "\r" not in details, details
    assert "out\\|a b withheld" in details, details
    section_4 = text.split("## 4.", 1)[1].split("## 5.", 1)[0]
    rows = [line for line in section_4.splitlines() if withheld in line]
    assert len(rows) == 1 and flattened(folder) in rows[0], section_4


def test_the_counters_count_withheld_as_requested(tmp_path):
    """"1 of 1 requested" answered for the plan, not for the cohort asked for."""
    session, _withheld, _kept, _value = _two_patients_one_identified(
        tmp_path, "instance")
    try:
        session.export(str(tmp_path / "out"), check_burned_in=True,
                       use_compression=False, show_progress=False)
        text = _report(session, tmp_path)
        exports = _rows(session, "EXPORT")
    finally:
        session.close()

    assert "| Instances Written | 1 of 2 requested |" in text, text
    assert len(exports) == 1, exports
    assert exports[0][1].endswith(
        "wrote 1 of 1 planned instances from 2 patients; 1 more withheld "
        "by the pre-export scan (check_burned_in=True)."), exports


def test_a_fully_withheld_export_is_not_an_empty_plan(tmp_path):
    """Case a, the issue verbatim: nothing matched is not what happened.

    Two instances under one never-anonymized patient, so the identifier
    is on a level both share: one row per *instance* is two rows, where a
    row per patient would be one.
    """
    src = tmp_path / "src"
    uids = sorted([_write_ct(str(src), "PAT-536-A", "1", "Alpha^Test"),
                   _write_ct(str(src), "PAT-536-A", "2", "Alpha^Test")])
    session = DicomSession(persistence_file=str(tmp_path / "a.db"))
    try:
        session.ingest(str(src))
        summary = session.export(str(tmp_path / "out"), check_burned_in=True,
                                 use_compression=False, show_progress=False)
        exports = _rows(session, "EXPORT")
        warnings = _rows(session, "WARNING")
        text = _report(session, tmp_path)
    finally:
        session.close()

    # Returned, not raised: nothing failed, an instance was held back.
    assert isinstance(summary, ExportSummary)
    assert summary.written == 0 and summary.failures == []

    assert sorted(u for u, _d in warnings) == uids, warnings
    assert all("its patient still carries an identifier" in d
               for _u, d in warnings), warnings
    assert len(exports) == 1, exports
    detail = exports[0][1]
    assert "nothing matched" not in detail, detail
    assert detail.endswith(
        "wrote 0 of 2 requested instances; all 2 were withheld by the "
        "pre-export scan (check_burned_in=True)."), detail
    assert "| Instances Written | 0 of 2 requested |" in text, text
    assert "REVIEW_REQUIRED" in _grade(text), text


def test_an_instance_outside_the_subset_is_not_withheld(tmp_path, caplog):
    """The subset first: an instance nobody selected was not held back."""
    src = tmp_path / "src"
    a = _write_ct(str(src), "PAT-536-A", "21", "Alpha^Test")
    b = _write_ct(str(src), "PAT-536-B", "22", "Beta^Test")
    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        session.ingest(str(src))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.export(str(tmp_path / "out"), check_burned_in=True,
                           subset=[a], use_compression=False,
                           show_progress=False)
        warnings = _rows(session, "WARNING")
        text = _report(session, tmp_path)
    finally:
        session.close()

    assert [u for u, _d in warnings] == [a], warnings
    skipped = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("Skipping")]
    assert len(skipped) == 1 and a in skipped[0], skipped
    assert not any(b in message for message in skipped), skipped
    assert "| Instances Written | 0 of 1 requested |" in text, text


def test_a_true_empty_plan_keeps_its_text_and_no_counters(tmp_path):
    """A subset matching nothing, no identifiers: still "nothing matched"."""
    src = tmp_path / "src"
    _write_ct(str(src), "PAT-536-C", "31", "Gamma^Test")
    session = DicomSession(persistence_file=str(tmp_path / "c.db"))
    try:
        session.ingest(str(src))
        session.anonymize()
        folder = str(tmp_path / "out")
        session.export(folder, check_burned_in=True, subset=["NO-SUCH-UID"],
                       use_compression=False, show_progress=False)
        exports = _rows(session, "EXPORT")
        warnings = _rows(session, "WARNING")
        text = _report(session, tmp_path)
    finally:
        session.close()

    assert warnings == [], warnings
    assert [d for _u, d in exports] == [
        f"DICOM export to {folder}: wrote 0 of 0 planned instances; nothing "
        f"matched the export plan."], exports
    assert "Instances Written" not in text, text


def test_a_clean_export_row_text_is_unchanged(tmp_path):
    """K == 0 writes today's row byte for byte, and no WARNING row."""
    src = tmp_path / "src"
    _write_ct(str(src), "PAT-536-D", "41", "Delta^Test")
    _write_ct(str(src), "PAT-536-E", "42", "Epsilon^Test")
    session = DicomSession(persistence_file=str(tmp_path / "d.db"))
    try:
        session.ingest(str(src))
        session.anonymize()
        folder = str(tmp_path / "out")
        summary = session.export(folder, check_burned_in=True,
                                 use_compression=False, show_progress=False)
        exports = _rows(session, "EXPORT")
        warnings = _rows(session, "WARNING")
    finally:
        session.close()

    assert summary.written == 2, summary
    assert warnings == [], warnings
    assert [d for _u, d in exports] == [
        f"DICOM export to {folder}: wrote 2 of 2 planned instances from 2 "
        f"patients."], exports


def test_a_second_export_does_not_keep_the_first_ones_withheld_count(
        tmp_path):
    """The count is per run (#196): export 2 withholds nothing."""
    session, _withheld, _kept, _value = _two_patients_one_identified(
        tmp_path, "instance")
    try:
        session.export(str(tmp_path / "out1"), check_burned_in=True,
                       use_compression=False, show_progress=False)
        session.export(str(tmp_path / "out2"), use_compression=False,
                       show_progress=False)
        text = _report(session, tmp_path)
    finally:
        session.close()

    assert "| Instances Written | 2 of 2 requested |" in text, text


def test_a_failed_batch_beside_a_withheld_instance_counts_both(
        tmp_path, monkeypatch):
    """Every planned write fails and one more was withheld.

    Two numbers meet here and answer different questions: the report's
    denominator is the cohort asked for (planned plus withheld), and
    `ExportError.attempted` is what the batch tried to write. A fix that
    folded the withheld count into `attempted` would tell a caller the
    batch failed on an instance it never touched.

    `export_batch` is called in the parent process, so the monkeypatch
    reaches it.
    """
    session, withheld, kept, _value = _two_patients_one_identified(
        tmp_path, "instance")

    def _all_failed(*_args, **_kwargs):
        return ExportSummary(failures=[(kept, "disk full")])

    try:
        monkeypatch.setattr(DicomExporter, "export_batch", _all_failed)
        with pytest.raises(ExportError) as raised:
            session.export(str(tmp_path / "out"), check_burned_in=True,
                           use_compression=False, show_progress=False)
        text = _report(session, tmp_path)
        exports = _rows(session, "EXPORT")
    finally:
        session.close()

    assert raised.value.attempted == 1, raised.value.attempted
    assert [uid for uid, _detail in raised.value.failures] == [kept]
    assert withheld not in [uid for uid, _d in raised.value.failures]
    assert "| Instances Written | 0 of 2 requested |" in text, text
    assert len(exports) == 1, exports
    assert exports[0][1].endswith(
        "wrote 0 of 1 planned instances from 2 patients; 1 more withheld "
        "by the pre-export scan (check_burned_in=True)."), exports

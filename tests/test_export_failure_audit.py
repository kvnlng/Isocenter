"""An instance that failed to export must reach the compliance report (#181).

`_export_instance_worker` catches everything and hands the parent an
`ExportOutcome(ok=False, error=exc)`. The parent read `ok` to count
successes and dropped the rest, so a failed write produced no audit row
of any kind: `get_audit_errors()` came back empty, the report graded the
run `PASS`, printed `Total Instances` from the object graph, and stated
*"No exceptions or errors were recorded."* An export that wrote **zero**
of three files certified the same document as a clean one.

The tests below force failures from two structurally different origins,
because the defect is in the sink and not in any one mechanism:

* the **validator** arm is pure data -- a CT instance missing a Type 1
  element, so `IODValidator` raises inside `_finalize_dataset`;
* the **filesystem** arm makes the output root unwritable, so the
  worker's `makedirs` raises `PermissionError` for every instance.

Both run through `session.export()`, whose workers are always separate
processes (`_run_export_batch` passes `maxtasksperchild=25`, and
`parallel._use_threads` returns False whenever that is set). A
monkeypatch in the parent would not reach them, which is why both arms
break the data or the filesystem instead.
"""
import os
import sqlite3
from datetime import date

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

# Everything IODValidator demands of a CT image. Dropping one of them is
# the whole of the "validator" arm.
CT_REQUIRED = (
    ("0008,0020", "20230101"), ("0008,0030", "120000"),
    ("0008,0060", "CT"),
    ("0018,0050", "1.0"), ("0018,0060", "120"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
)

#: The element the broken instance goes without: Pixel Spacing, Type 1.
DROPPED_TAG = "0028,0030"


def _session(tmp_path, break_instances=()):
    """Three exportable CT instances, `break_instances` of them invalid."""
    session = DicomSession(str(tmp_path / "fail.db"))

    patient = Patient("PAT1", "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)

    for n in range(3):
        inst = Instance(f"1.2.826.0.1.{n}", CT_STORAGE, n + 1)
        inst.file_path = None
        for tag, value in CT_REQUIRED:
            if n in break_instances and tag == DROPPED_TAG:
                continue
            inst.set_attr(tag, value)
        inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
        series.instances.append(inst)

    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save()
    return session


def _audit(db_path, action_type):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


def _run(tmp_path, arm):
    """Export three instances under `arm`, and report what came of it.

    Every arm anonymizes first, so `audit_summary` is non-empty and the
    baseline grade is `PASS` -- an empty audit summary grades
    `REVIEW_REQUIRED` on its own, and a test that skipped this would
    pass without the fix.
    """
    session = _session(tmp_path,
                       break_instances=(1,) if arm == "validator" else ())
    out = tmp_path / "out"
    out.mkdir()
    report = tmp_path / "report.md"

    try:
        session.anonymize()
        if arm == "filesystem":
            os.chmod(out, 0o500)
        try:
            session.export(str(out), format="dicom", show_progress=False)
        finally:
            os.chmod(out, 0o700)
        session.generate_report(str(report))
        db_path = session.store_backend.db_path
        errors = session.store_backend.get_audit_errors()
    finally:
        session.close()

    return {
        "files": sorted(p.name for p in out.rglob("*.dcm")),
        "errors": errors,
        "error_rows": _audit(db_path, "ERROR"),
        "report": report.read_text(encoding="utf-8"),
    }


@pytest.fixture(params=["validator", "filesystem"])
def failed_export(request, tmp_path):
    if request.param == "filesystem" and os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this arm relies on")
    result = _run(tmp_path, request.param)
    result["arm"] = request.param
    return result


def test_the_arms_actually_fail_the_writes_they_claim_to(failed_export):
    """Guard against an arm that quietly writes every file anyway."""
    expected = 2 if failed_export["arm"] == "validator" else 0
    assert len(failed_export["files"]) == expected, failed_export["files"]


def test_a_failed_write_is_recorded_in_the_audit_log(failed_export):
    """One row per instance that did not reach disk, naming the instance."""
    rows = failed_export["error_rows"]
    expected = 1 if failed_export["arm"] == "validator" else 3
    assert len(rows) == expected, rows
    assert all(uid for uid, _details in rows), rows
    assert all("|" not in details and "\n" not in details
               for _uid, details in rows), (
        "the detail is rendered into a markdown table row")


def test_the_report_does_not_certify_a_failed_export_as_pass(failed_export):
    """The grade is the headline of the document; it must move."""
    content = failed_export["report"]
    assert "| **Validation Status** | **REVIEW_REQUIRED** |" in content, content
    assert "*No exceptions or errors were recorded.*" not in content, content


def test_the_report_names_the_failure_in_the_exceptions_section(failed_export):
    """`docs/analytics.md` promises this section lists errors. It must."""
    content = failed_export["report"]
    assert "## 4. Exceptions & Errors" in content
    assert "ERROR" in content.split("## 4. Exceptions & Errors")[1], content


def test_the_report_counts_what_was_written_not_only_what_was_indexed(
        failed_export):
    """`Total Instances | 3` beside zero delivered files is the false line."""
    written = 2 if failed_export["arm"] == "validator" else 0
    assert f"| Instances Written | {written} of 3 requested |" in (
        failed_export["report"]), failed_export["report"]


def test_a_clean_export_still_grades_pass_and_reports_a_full_count(tmp_path):
    """The control: the grade moves for failures, not for exporting."""
    result = _run(tmp_path, "clean")

    assert len(result["files"]) == 3, result["files"]
    assert result["error_rows"] == []
    assert "| **Validation Status** | **PASS** |" in result["report"]
    assert "*No exceptions or errors were recorded.*" in result["report"]
    assert "| Instances Written | 3 of 3 requested |" in result["report"]

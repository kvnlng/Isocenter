"""An image that declares Burned In Annotation YES, and was not
zone-redacted, costs the run its PASS (#1108).

The #1108 architect's baseline measured PASS over a burned-in name, on
pydicom's `RG3_UNCR.dcm`, which carries no `(0028,0301)` at all. With the
element set, measured on `main` at f31758d3: ingest -> audit -> anonymize
-> export -> report graded REVIEW_REQUIRED for `YES` and for `yes`, through
the report-time check `SqliteStore.check_unsafe_attributes()` (a
`COMPLIANCE_CHECK` row in section 4, grade condition 2). Its `LIKE` is
case-insensitive for ASCII, which is why `yes` is caught. So the owner's
ruling on #1108 (Q-A) added no grade condition; these tests pin the
coverage that already exists, so it cannot be lost silently.

A zone `redact()` applied writes `(0028,0301) NO`, so the same image
redacted passes. As found, not changed: the check reads the store, not
the files written, so it also flags a `YES` instance an export's `subset`
left out.
"""
import os

import pydicom
import pytest

from isocenter.session import DicomSession
from support.project_secret import load_fixed_secret
from support.serial_less_burned_cr import OVER, write_inputs


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _declaring(directory, value):
    write_inputs(directory)
    path = os.path.join(directory, "none.dcm")
    ds = pydicom.dcmread(path)
    ds.BurnedInAnnotation = value
    ds.save_as(path)
    return directory


def _grade(tmp_path, value, rules):
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        load_fixed_secret(session)
        session.ingest(_declaring(str(tmp_path / "input"), value))
        session.configuration.rules = rules
        session.anonymize(session.audit())
        if rules:
            assert session.redact(show_progress=False) == 1
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        report = str(tmp_path / "r.md")
        session.generate_report(report)
    with open(report, encoding="utf-8") as handle:
        text = handle.read()
    grade = "PASS" if "| **Validation Status** | **PASS** |" in text else (
        "REVIEW_REQUIRED"
        if "| **Validation Status** | **REVIEW_REQUIRED** |" in text else None)
    return grade, text


@pytest.mark.parametrize("value", ["YES", "yes"])
def test_a_declared_yes_with_no_redaction_grades_review_required(tmp_path, value):
    grade, text = _grade(tmp_path, value, [])
    assert grade == "REVIEW_REQUIRED"
    assert "COMPLIANCE_CHECK" in text
    assert "BurnedInAnnotation FLAGGED as YES" in text


def test_the_same_image_zone_redacted_passes(tmp_path):
    """Paired with the case above on the same fixture: the zone's
    `(0028,0301) NO` is what lifts it, not a fixture that never flagged."""
    grade, text = _grade(tmp_path, "YES", [
        {"serial_number": "", "redaction_zones": [list(OVER)]}])
    assert "BurnedInAnnotation FLAGGED" not in text
    assert grade == "PASS", text[:3000]

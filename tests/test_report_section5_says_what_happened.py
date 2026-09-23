"""Section 5 of the compliance report says what the session did (#481).

Section 5 ("Validation & Verification") was two report fields nothing ever
set -- `validation_issues`, always `0`, and `verification_details`, always
empty so the renderer printed "Standard automated checks performed." --
plus a methodology sentence that always said "Metadata was remediated
according to the tag policy in force; pixel data was scanned against the
configured machine redaction zones". Measured on e184933: a session whose
scan could not read an instance and one that never scanned rendered the
same section 5, "Identified Issues: 0" beside a `REVIEW_REQUIRED` grade
whose section 4 listed the issue.

What these tests hold:

- **The grade and section 5 come from one list.** `generate_report` builds
  the reasons the run is not `PASS`, grades `PASS` only when the list is
  empty, and section 5 renders the list, so section 5 cannot say "no
  issues" beside a `REVIEW_REQUIRED`, nor list an issue beside a `PASS`.
  Every term of the grade is a reason, including the two that have no row
  anywhere in the report: an empty audit trail and a verb this session
  performed whose rows never arrived (#254).
- **Section 5 counts section 4's rows**, whatever wrote them -- including
  the `WARNING` row a scan failure writes once #479 lands, which is why the
  invariant is asserted on the rendered table rather than on any one
  emitter.
- **The pixel-scan line describes `scan_pixel_content()` runs in this
  session**, every exit included: the early return with nothing configured
  to scan, the normal return, and the `PixelScanError` raise. It names the
  method, because `discover_redaction_zones()` also runs OCR, and it says
  "in this session", because the record is transient on the session, like
  `_actions_performed` and `_last_export_written` -- a reopened store's
  report shows an earlier session's audit rows in section 4 and must not
  claim to know what that session scanned.
- **The metadata line is read from the audit trail**, the same source
  section 2 prints, so it cannot say "remediated" over a session whose
  trail holds no remediation row.

**Why this file imports what it does.** OCR is faked with `ocr_present`
(`conftest.py`), threads only, as in
`test_scan_reports_what_it_could_not_read.py`; the frame's fill decides
whether its read fails, never a call counter.
"""
import re
from datetime import date

import numpy as np
import pytest

from isocenter import pixel_analysis
from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.session import DicomSession

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
SERIAL = "SN-481"
ZONE = [0, 20, 0, 20]
#: Outside `ZONE`, so every frame read raises one finding.
WORD = {"text": ["LEAKTEXT"], "conf": [90], "left": [200], "top": [200],
        "width": [50], "height": [50]}
#: Two regions per frame, so a scan that read one instance found two: the
#: counts differ, and a swap of `read` and `findings` cannot pass unseen.
TWO_WORDS = {"text": ["LEAK", "TEXT"], "conf": [90, 90], "left": [200, 100],
             "top": [200, 100], "width": [50, 50], "height": [50, 50]}
PASS_LINE = "| **Validation Status** | **PASS** |"
REVIEW_LINE = "| **Validation Status** | **REVIEW_REQUIRED** |"
OLD_CLAIM = "pixel data was scanned against the configured machine redaction zones"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _instance(uid, readable=True):
    instance = Instance(uid, SC_SOP_CLASS, 1)
    instance.file_path = None
    instance.set_pixel_data(np.full((32, 32), 1, dtype=np.uint8))
    if not readable:
        instance.pixel_array = None

        def broken_loader():
            raise OSError("sidecar read failed: [Errno 5] Input/output error")
        instance._pixel_loader = broken_loader  # pylint: disable=protected-access
    return instance


def _session(tmp_path, instances, zones=True):
    session = DicomSession(str(tmp_path / "s5.db"))
    patient = Patient("P481", "Original^Name")
    study = Study("1.2.826.0.1.481", date(2023, 1, 1))
    series = Series("1.2.826.0.1.481.1", "OT", 1)
    series.equipment = Equipment("Acme", "Model", SERIAL)
    series.instances.extend(instances)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.rules = [
        {"serial_number": SERIAL, "redaction_zones": [ZONE] if zones else []}]
    return session


def _report(session, tmp_path):
    session.store_backend.flush_audit_queue()
    out = tmp_path / "report.md"
    session.generate_report(str(out))
    return out.read_text(encoding="utf-8")


def _section4_rows(text):
    body = text.split("## 4. Exceptions & Errors", 1)[1].split("## 5.", 1)[0]
    # The `| :--- |` alignment rule starts with "| " too, and counting it
    # made one data row read as two; drop it, then the header.
    rows = [line for line in body.splitlines()
            if line.startswith("| ") and not line.startswith("| :")]
    return rows[1:]


def _section2(text):
    return text.split("## 2. Processing Audit", 1)[1].split("## 3.", 1)[0]


def _section5(text):
    return text.split("## 5. Validation & Verification", 1)[1].split("---\n", 1)[0]


def _scan_line(text):
    line = [l for l in _section5(text).splitlines() if "**Pixel Scan" in l]
    assert len(line) == 1, _section5(text)
    return line[0]


def _assert_section5_agrees_with_section4(text):
    """The invariant, on the rendered report: never 0 beside a row."""
    rows = _section4_rows(text)
    s5 = _section5(text)
    if rows:
        assert f"{len(rows)} row(s) in section 4" in s5, (rows, s5)
        assert REVIEW_LINE in text
    else:
        assert "in section 4" not in s5, s5


# --- the pixel-scan line -----------------------------------------------------

def test_a_session_that_never_scanned_says_so(tmp_path):
    """Arm B of the issue. Red before #481: "pixel data was scanned".

    Kills: the pixel-scan line rendering its "ran" wording without a record.
    """
    with _session(tmp_path, [_instance("1.2.481.1")]) as session:
        session.anonymize()
        text = _report(session, tmp_path)
    assert OLD_CLAIM not in text
    assert "Identified Issues" not in text
    assert "No `scan_pixel_content()` ran in this session" in _scan_line(text)
    assert PASS_LINE in text


def test_a_scan_that_read_everything_says_what_it_read_and_found(tmp_path, ocr_present):
    """Kills: the normal return's record dropped, and its counts swapped."""
    ocr_present.image_to_data = lambda *a, **k: WORD
    with _session(tmp_path, [_instance("1.2.481.1"), _instance("1.2.481.2")]) as session:
        session.anonymize()
        report = session.scan_pixel_content()
        assert len(report.findings) == 2
        text = _report(session, tmp_path)
    line = _scan_line(text)
    assert "read 2 of 2 instance(s)" in line, line
    assert "2 text region(s) outside the configured redaction zones" in line, line
    assert "0 instance(s) could not be read" in line, line
    _assert_section5_agrees_with_section4(text)


def test_a_scan_that_could_not_read_one_instance_says_so(tmp_path, ocr_present):
    """Arm A of the issue: one instance's loader raises `OSError`.

    Kills: the failure count read from anything but the pass's failures,
    and `read` swapped with `findings` -- which is why this arm answers
    two words, not one: with one, both counts are 1.
    """
    ocr_present.image_to_data = lambda *a, **k: TWO_WORDS
    broken = _instance("1.2.481.2", readable=False)
    with _session(tmp_path, [_instance("1.2.481.1"), broken]) as session:
        session.anonymize()
        # Named by the UID it holds now: the pass replaced 1.2.481.2 (#544).
        assert broken.sop_instance_uid != "1.2.481.2"
        report = session.scan_pixel_content()
        assert [uid for uid, _ in report.failures] == [broken.sop_instance_uid]
        assert len(report.findings) == 2
        text = _report(session, tmp_path)
    line = _scan_line(text)
    assert "read 1 of 2 instance(s)" in line, line
    assert "found 2 text region(s)" in line, line
    assert "1 instance(s) could not be read" in line, line
    _assert_section5_agrees_with_section4(text)


def test_a_scan_that_read_nothing_and_raised_is_still_recorded(tmp_path, ocr_present):
    """Kills: the record written after the `PixelScanError` raise, not before.

    Missed, a scan that failed every instance would read as "no scan ran"
    beside #479's rows for the very instances it could not read.
    """
    with _session(tmp_path, [_instance("1.2.481.2", readable=False)]) as session:
        session.anonymize()
        with pytest.raises(pixel_analysis.PixelScanError):
            session.scan_pixel_content()
        text = _report(session, tmp_path)
    line = _scan_line(text)
    assert "read 0 of 1 instance(s)" in line, line
    assert "1 instance(s) could not be read" in line, line
    _assert_section5_agrees_with_section4(text)


def test_a_scan_with_nothing_configured_to_read_says_that(tmp_path, ocr_present):
    """The early return: the machine's rule has no zones (a scaffold).

    Kills: the early return's record dropped, which reads as "no scan ran"
    for a call that ran and found nothing it was configured to read.
    """
    with _session(tmp_path, [_instance("1.2.481.1")], zones=False) as session:
        session.anonymize()
        session.scan_pixel_content()
        text = _report(session, tmp_path)
    line = _scan_line(text)
    assert "read 0 of 0 instance(s)" in line, line
    assert "1 instance(s) skipped" in line, line


# --- the grade basis ---------------------------------------------------------

def test_a_pass_says_nothing_cost_it(tmp_path):
    with _session(tmp_path, [_instance("1.2.481.1")]) as session:
        session.anonymize()
        text = _report(session, tmp_path)
    assert PASS_LINE in text
    assert "**Grade Basis:** PASS" in _section5(text)
    _assert_section5_agrees_with_section4(text)


def _warning_row(session):
    # The shape #479's rows take; written directly so this test holds
    # before and after that branch lands.
    session.store_backend.log_audit(
        "WARNING", "1.2.481.2",
        "scan_pixel_content() could not read 1.2.481.2 in full, so its "
        "burned-in text was not (or not all) checked: OSError: sidecar read failed")


def _data_loss(session):
    session.store_backend.log_audit(
        "DATA_LOSS", "1.2.481.1", "Dropped private element (0009,0010) LO",
        loss_scope="PRIVATE")


def _decline(session):
    session.store_backend.log_audit(
        "REMEDIATION_DECLINED", "1.2.826.0.1.481",
        "Remediation declined for 1.2.826.0.1.481: invalid date format")


def _unattested(session):
    # A verb this session performed whose rows never reached the trail
    # (#254). Its rows are what `anonymize()` would have written.
    session._actions_performed.add("REDACTION")  # pylint: disable=protected-access


@pytest.mark.parametrize("inject, reason", [
    (_warning_row, "1 row(s) in section 4"),
    (_data_loss, "1 graded data loss(es) in section 3.1"),
    (_decline, "1 declined remediation(s) in section 3.3"),
    (_unattested, "REDACTION ran in this session"),
], ids=["section-4-row", "graded-loss", "decline", "unattested-verb"])
def test_every_reason_the_run_is_not_pass_is_in_section_5(tmp_path, inject, reason):
    """Red before #481: "Identified Issues: 0" beside REVIEW_REQUIRED.

    Kills: any grade term removed from the reasons list -- there is no
    second boolean for it to survive in.
    """
    with _session(tmp_path, [_instance("1.2.481.1")]) as session:
        session.anonymize()
        inject(session)
        text = _report(session, tmp_path)
    assert REVIEW_LINE in text
    s5 = _section5(text)
    assert "**Grade Basis:** REVIEW_REQUIRED" in s5, s5
    assert reason in s5, s5
    _assert_section5_agrees_with_section4(text)


def test_an_empty_audit_trail_is_named_as_the_reason(tmp_path):
    """The one reason with no row anywhere: nothing was attested at all."""
    with _session(tmp_path, [_instance("1.2.481.1")]) as session:
        text = _report(session, tmp_path)
    assert REVIEW_LINE in text
    assert "the audit trail holds no rows" in _section5(text)


# --- the metadata line -------------------------------------------------------

def test_the_metadata_line_is_read_from_the_audit_trail(tmp_path):
    """Red before #481: "Metadata was remediated" for a session that did not.

    Kills: the line rendered from anything but REMEDIATION_* rows.
    """
    with _session(tmp_path, [_instance("1.2.481.1")]) as session:
        bare = _report(session, tmp_path)
        session.anonymize()
        done = _report(session, tmp_path)
    assert "Metadata was remediated" not in bare
    assert "No `REMEDIATION_*` row is in the audit trail" in _section5(bare)
    counts = [int(n) for n in re.findall(
        r"\*\*Metadata Remediation:\*\* (\d+) `REMEDIATION_\*` row", _section5(done))]
    assert counts and counts[0] > 0, _section5(done)


# --- after #479: the rows a failed scan writes ------------------------------

def _warning_rows(text):
    return [row for row in _section4_rows(text) if "| WARNING |" in row]


def test_a_real_scan_failure_is_counted_where_section_4_lists_it(tmp_path, ocr_present):
    """#479 writes one `WARNING` row per unread instance; section 5 counts it.

    The invariant helper passes vacuously over an empty section 4, so this
    first proves the row is there -- a test that only called the helper
    would pass on a base without #479 for the wrong reason.

    Kills: the section-4 reason removed, and the no-scan wording rendered
    beside a scan that ran.
    """
    ocr_present.image_to_data = lambda *a, **k: WORD
    broken = _instance("1.2.481.2", readable=False)
    with _session(tmp_path, [_instance("1.2.481.1"), broken]) as session:
        session.anonymize()
        session.scan_pixel_content()
        text = _report(session, tmp_path)
    rows = _warning_rows(text)
    # The UID the instance holds after the pass (#544).
    assert len(rows) == 1 and broken.sop_instance_uid in rows[0], _section4_rows(text)
    assert REVIEW_LINE in text
    assert "Identified Issues" not in text
    assert OLD_CLAIM not in text
    s5 = _section5(text)
    assert "1 row(s) in section 4" in s5, s5
    assert "No `scan_pixel_content()` ran" not in s5, s5
    _assert_section5_agrees_with_section4(text)


def test_a_rescan_that_reads_everything_leaves_the_earlier_row_and_says_so(
        tmp_path, ocr_present):
    """Failed, then read on a rescan: the row stays, and section 5 says why.

    #479's rows are permanent, so the grade stays REVIEW_REQUIRED after a
    clean second run. Section 5 lists both runs and says a later read does
    not remove the earlier row, so the Grade Basis and a clean final run do
    not read as a contradiction.

    Kills: the "does not remove that row" sentence dropped, and a pixel-scan
    line that describes only the last run.
    """
    ocr_present.image_to_data = lambda *a, **k: WORD
    broken = _instance("1.2.481.2", readable=False)
    with _session(tmp_path, [_instance("1.2.481.1"), broken]) as session:
        session.anonymize()
        session.scan_pixel_content()
        broken.set_pixel_data(np.full((32, 32), 1, dtype=np.uint8))
        second = session.scan_pixel_content()
        assert second.failures == []
        text = _report(session, tmp_path)
    assert len(_warning_rows(text)) == 1, _section4_rows(text)
    assert REVIEW_LINE in text
    line = _scan_line(text)
    assert "ran 2 times in this session" in line, line
    assert "run 1: read 1 of 2 instance(s)" in line, line
    assert "run 2: read 2 of 2 instance(s)" in line, line
    assert "a later run that reads it does not remove that row" in line, line
    assert "1 row(s) in section 4" in _section5(text)
    _assert_section5_agrees_with_section4(text)


def test_a_discovery_failure_is_counted_and_not_called_a_pixel_scan(
        tmp_path, ocr_present):
    """`discover_redaction_zones()` writes #479's rows too, and is not a scan.

    Section 5 counts discovery's row among section 4's, and its pixel-scan
    line -- which names `scan_pixel_content()` -- still says no such scan
    ran, which is true: discovery looks for zones, it does not verify them.

    Kills: the section-4 reason removed.
    """
    ocr_present.image_to_data = lambda *a, **k: WORD
    with _session(tmp_path, [_instance("1.2.481.1"),
                             _instance("1.2.481.2", readable=False)]) as session:
        session.anonymize()
        session.discover_redaction_zones(SERIAL)
        text = _report(session, tmp_path)
    rows = _warning_rows(text)
    assert len(rows) == 1 and "discover_redaction_zones()" in rows[0], rows
    assert REVIEW_LINE in text
    assert "1 row(s) in section 4" in _section5(text)
    assert "No `scan_pixel_content()` ran in this session" in _scan_line(text)
    _assert_section5_agrees_with_section4(text)


def test_the_metadata_count_excludes_declines(tmp_path):
    """`metadata_remediations` is the `REMEDIATION_*` rows, and a decline
    is not one of them.

    `REMEDIATION_DECLINED` is deliberately outside
    `REMEDIATION_ACTION_TYPES`; counting it would call a value that stayed
    in the data a remediation. Green before the review round: it is here to
    pin the exact count against section 2's own rows, the other reader of
    the same trail.

    Kills: `REMEDIATION_DECLINED` added to the counted action types.
    """
    with _session(tmp_path, [_instance("1.2.481.1")]) as session:
        # As a graph loaded from a damaged store would carry it: the
        # SHIFT_DATE arm declines a value it cannot parse.
        session.store.patients[0].studies[0].study_date = "notadate"
        session.anonymize()
        text = _report(session, tmp_path)
    rows = dict(re.findall(r"^\| (REMEDIATION_\w+) \| (\d+) \|$", _section2(text), re.M))
    assert rows.get("REMEDIATION_DECLINED") == "1", rows
    expected = sum(int(n) for action, n in rows.items() if action != "REMEDIATION_DECLINED")
    assert expected > 0, rows
    counts = [int(n) for n in re.findall(
        r"\*\*Metadata Remediation:\*\* (\d+) `REMEDIATION_\*` row", _section5(text))]
    assert counts == [expected], (counts, rows)

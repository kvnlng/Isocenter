"""The grade demands evidence for what this session did, not for anything.

`generate_report` graded `PASS` whenever `audit_summary` was non-empty
and nothing had failed -- an action-type-blind check that asks whether
the audit log heard about *anything*, not whether it heard about *what
this session did*. With #247 fixed, an ordinary redacting run writes its
`REDACTION` rows; but a run whose rows are lost to a second defect (a
dropped batch of the #219 shape) still graded `PASS` on the strength of
whatever unrelated rows survived -- the exact structure of #247's second
reading, one defect further away (#254).

The session's memory of its own verbs is transient and in-memory on
purpose (`Session._actions_performed`); these tests simulate the loss by
deleting rows from `audit_log` after the barrier, which is what a
dropped batch looks like from the report's side of the table.

Executor levers are not parametrised here: the grading comparison and
the verb recording are both parent-side, and
`test_redaction_audit_accounting.py` already covers the emitter under
both executors. Threads keeps these off the process-spawn path.
"""
import datetime
import sqlite3

import pytest

from isocenter.builders import DicomBuilder
from isocenter.session import DicomSession

#: Inside the fixture's 32x32 image, so the zone really lands.
IN_IMAGE_ZONE = [0, 8, 0, 8]

PASS_ROW = "**Validation Status** | **PASS**"
REVIEW_ROW = "**Validation Status** | **REVIEW_REQUIRED**"


def _grade(session, tmp_path):
    """Generate the markdown report and return its text."""
    output = tmp_path / "compliance.md"
    session.generate_report(str(output), format="markdown")
    return output.read_text(encoding="utf-8")


def _purge_rows(session, action_type_like):
    """Delete already-flushed audit rows, simulating a lost batch.

    Flush first: the writer thread queues rows, and a DELETE that runs
    before the queue drains removes nothing and the test goes vacuous.
    """
    session.store_backend.flush_audit_queue()
    with session.store_backend._get_connection() as conn:
        deleted = conn.execute(
            "DELETE FROM audit_log WHERE action_type LIKE ?",
            (action_type_like,)).rowcount
        conn.commit()
    assert deleted > 0, (
        f"no {action_type_like!r} rows existed to lose; this test is "
        "simulating a loss that did not happen")


@pytest.fixture
def anonymizing_session(tmp_path):
    """A session holding one identified patient, ready to anonymize."""
    sess = DicomSession(str(tmp_path / "anon.db"))
    patient = (DicomBuilder.start_patient("P123", "John Doe")
               .add_study("S1", datetime.date(2023, 1, 1))
               .add_series("SE1", "CT", 1)
               .add_instance("I1", "1.2.3", 1)
               .end_instance().end_series().end_study().build())
    sess.store.patients.append(patient)
    yield sess
    sess.close()


def test_a_redacting_run_whose_redaction_rows_are_lost_does_not_grade_pass(
        reloaded_redaction_session, monkeypatch, tmp_path):
    """Lost REDACTION rows plus one unrelated row must not read as PASS.

    This is #254's residue case: the unrelated `EXPORT` row keeps
    `audit_summary` non-empty, which is all the old check asked, so the
    grade vouched for a redaction the audit log never heard about.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="lost_redaction")

    assert session.redact(show_progress=False) == 1, (
        "the zone was in the image; this run must redact")
    _purge_rows(session, "REDACTION")
    session.store_backend.log_audit(
        "EXPORT", "1.2.3.lost", "unrelated evidence")

    content = _grade(session, tmp_path)
    session.close()

    assert REVIEW_ROW in content, (
        "a session that redacted, with no REDACTION row in its audit "
        "summary, graded on the strength of unrelated rows")


def test_an_anonymizing_run_whose_remediation_rows_are_lost_does_not_grade_pass(
        anonymizing_session, tmp_path):
    """Same shape as the redaction case, for the other verb."""
    session = anonymizing_session
    assert session.anonymize() > 0, (
        "the patient carries a name; this run must remediate something")
    _purge_rows(session, "REMEDIATION%")
    session.store_backend.log_audit(
        "EXPORT", "1.2.3", "unrelated evidence")

    content = _grade(session, tmp_path)

    assert REVIEW_ROW in content, (
        "a session that anonymized, with no REMEDIATION_* row in its "
        "audit summary, graded on the strength of unrelated rows")


def test_an_ordinary_redacting_run_still_grades_pass(
        reloaded_redaction_session, monkeypatch, tmp_path):
    """Green on both sides of #254, deliberately: this holds still the
    behaviour #255 landed -- a clean redact-only session grades PASS,
    because its own REDACTION rows are the evidence the grade demands."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="clean_redaction")

    assert session.redact(show_progress=False) == 1

    content = _grade(session, tmp_path)
    session.close()

    assert PASS_ROW in content, (
        "a clean redacting run carries its own REDACTION evidence and "
        "must keep its PASS")


def test_an_ordinary_anonymizing_run_still_grades_pass(
        anonymizing_session, tmp_path):
    """Green on both sides, holding the other verb's clean path still."""
    session = anonymizing_session
    assert session.anonymize() > 0

    content = _grade(session, tmp_path)

    assert PASS_ROW in content, (
        "a clean anonymizing run carries its own REMEDIATION_* evidence "
        "and must keep its PASS")


def test_a_session_that_never_redacted_is_not_penalized_for_absent_rows(
        tmp_path):
    """No verb performed, no evidence demanded.

    A session that only exported has no REDACTION or REMEDIATION_* rows
    and legitimately never will; demanding them would fail every
    non-redacting pipeline.
    """
    session = DicomSession(str(tmp_path / "no_verbs.db"))
    session.store_backend.log_audit("EXPORT", "1.2.3", "Exported")

    content = _grade(session, tmp_path)
    session.close()

    assert PASS_ROW in content, (
        "a session that performed neither verb was penalized for "
        "evidence rows that legitimately do not exist")


def test_a_redact_call_that_targeted_nothing_demands_no_evidence(
        reloaded_redaction_session, monkeypatch, tmp_path):
    """`redact()` that would have emitted nothing must expect nothing.

    A rule matching no machine emits no REDACTION row (#255's emitter
    writes one row per pass that *targeted* something), so the boundary
    for "performed" is "would have emitted" -- not "was called".
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="targeted_nothing")
    session.configuration.rules = [
        {"serial_number": "SN_NOBODY", "redaction_zones": [IN_IMAGE_ZONE]}]

    assert session.redact(show_progress=False) == 0, (
        "the rule matches no machine; if it applied, this test is "
        "asking a different question")
    session.store_backend.log_audit("EXPORT", "1.2.3", "Exported")

    content = _grade(session, tmp_path)
    session.close()

    assert PASS_ROW in content, (
        "a redact() call that targeted nothing emits no row and must "
        "not be graded as if it owed one")


def test_an_anonymize_call_that_applied_nothing_demands_no_evidence(
        tmp_path):
    """The other verb's boundary: zero applied, zero expected."""
    session = DicomSession(str(tmp_path / "anon_nothing.db"))

    assert session.anonymize() == 0, (
        "an empty store has nothing to remediate; if it applied, this "
        "test is asking a different question")
    session.store_backend.log_audit("EXPORT", "1.2.3", "Exported")

    content = _grade(session, tmp_path)
    session.close()

    assert PASS_ROW in content, (
        "an anonymize() call that applied nothing emits no row and must "
        "not be graded as if it owed one")

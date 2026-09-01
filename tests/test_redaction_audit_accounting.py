"""A redaction pass writes its own audit row, whichever executor ran.

On `abd51bc` the serial path (`RedactionService.redact_machine_instances`)
wrote one `REDACTION` row per machine and the public `session.redact()`
wrote none: a successful parallel redaction reached the compliance report
as "*No audit logs found*", and the run graded `REVIEW_REQUIRED` only
through the `not audit_summary` arm -- the right grade for the wrong
reason, flipping to `PASS` the moment anything else wrote a row (#247).

The accounting unit, decided rather than inherited: **one row per
rule-pass**, keyed on the serial spelling the rule was configured with,
written **in the parent** (#126 -- a worker's audit thread is torn down
without `stop()`, so its rows can vanish), **after** outcomes are known.
The serial path used to write its row before the pass, as intent
("Redacting N images..."); a compliance report is an account of what
happened, not of what was about to be attempted, so both paths now spell
the outcome -- and spell it identically, because two spellings for one
behaviour is how the two paths diverged in the first place.

Executor coverage mirrors `test_redaction_attestation.py`: `redact()`
takes threads on a free-threaded build and processes elsewhere, so
parallel-path tests run under both levers.
"""
import sqlite3

import pytest

from isocenter.services import RedactionService

LEVERS = ["ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES"]

#: Inside the fixture's 32x32 image, so the zone really lands.
IN_IMAGE_ZONE = [0, 8, 0, 8]


def _redaction_rows(db_path):
    """Rows straight out of sqlite, after `close()`.

    Not `get_audit_summary()`: the session is closed by the time these
    tests read, and raw rows also assert the `details` text, which is
    what keeps the two paths spelling one outcome.
    """
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='REDACTION'").fetchall()


@pytest.mark.parametrize("lever", LEVERS)
def test_a_successful_parallel_redaction_writes_a_redaction_row(
        reloaded_redaction_session, monkeypatch, lever):
    """The operation the report exists to attest appears in the report.

    **Detection.** Measured on `65dcb2e` (#247): one instance, one zone,
    redaction applied and verified -- `audit summary: {}`. Section 2 of
    the compliance report read "No audit logs found" for a session that
    had just redacted an image.
    """
    monkeypatch.setenv(lever, "1")
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name=f"audit_{lever}")
    db_path = session.persistence_file

    applied = session.redact(show_progress=False)
    assert applied == 1, "the zone was in the image; this run must redact"
    session.close()

    rows = _redaction_rows(db_path)
    assert len(rows) == 1, (
        f"a successful redaction pass wrote {len(rows)} REDACTION rows; "
        "one rule-pass is one row, and zero is #247 -- the report cannot "
        "attest an operation the audit log never heard about")
    entity_uid, details = rows[0]
    assert entity_uid == "SN_RELOAD", (
        f"the row is keyed on {entity_uid!r}, not on the serial the rule "
        "was configured with")
    assert "1 of 1" in details and "1 zone" in details, (
        f"the row does not account for the pass it attests: {details!r}")


@pytest.mark.parametrize("lever", LEVERS)
def test_a_wholly_skipped_pass_still_accounts_for_itself(
        reloaded_redaction_session, monkeypatch, lever):
    """A second pass under the same config is a pass that applied nothing.

    The attestation skip (`_ISOCENTER_REDACTION_HASH` matching) is the
    ordinary steady state of a store that is redacted, saved, and
    redacted again. The report should say that pass happened and applied
    nothing, rather than stay silent -- silence is what #247 looked like.
    """
    monkeypatch.setenv(lever, "1")
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name=f"skip_{lever}")
    db_path = session.persistence_file

    assert session.redact(show_progress=False) == 1
    assert session.redact(show_progress=False) == 0, (
        "the second pass must skip on the attestation hash; if it "
        "re-applied, this test is asking a different question")
    session.close()

    rows = _redaction_rows(db_path)
    assert len(rows) == 2, (
        f"two passes wrote {len(rows)} rows; every pass accounts for "
        "itself, including one that applied nothing")
    details = [d for _uid, d in rows]
    assert any("0 of 1" in d for d in details), (
        f"the skipped pass does not say it applied nothing: {details}")


def test_the_serial_and_parallel_paths_spell_one_outcome(
        reloaded_redaction_session, monkeypatch):
    """Identical work, identical row -- whichever executor ran.

    Whether a redacting session's report mentions redaction at all used
    to depend on which path ran (#247). The row is the attestation, so
    the two paths must produce byte-identical accounting for identical
    work; anything less and the report's section 2 becomes a record of
    call sites rather than of redactions. Same philosophy as
    `test_api_coherence.py`, which pins the two export paths to
    identical trees.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    parallel_session, _ = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="coherence_parallel")
    parallel_db = parallel_session.persistence_file
    assert parallel_session.redact(show_progress=False) == 1
    parallel_session.close()

    serial_session, _ = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="coherence_serial")
    serial_db = serial_session.persistence_file
    service = RedactionService(
        serial_session.store, serial_session.store_backend)
    service.redact_machine_instances(
        "SN_RELOAD", [tuple(IN_IMAGE_ZONE)], show_progress=False)
    serial_session.close()

    assert _redaction_rows(parallel_db) == _redaction_rows(serial_db), (
        "the two paths did the same work and accounted for it "
        "differently; one spelling per behaviour")

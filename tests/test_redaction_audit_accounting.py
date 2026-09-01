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

from isocenter.services import RedactionError, RedactionService

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
    assert details == "Applied 1 of 1 candidate images with 1 zones", (
        f"the row does not account for the pass it attests: {details!r}; "
        "the details text is a published shape (section 2 renders it), "
        "so this pins the exact spelling rather than substrings of it")


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


def test_two_rules_sharing_a_serial_write_two_rows(
        reloaded_redaction_session, monkeypatch):
    """The unit is the rule-pass, not the serial spelling.

    `load_config` takes rules verbatim from user YAML with no serial
    de-duplication, so two rules carrying one serial is an ordinary
    config. Keying the accounting on the serial collapses them into one
    row that keeps the first rule's zone count and double-counts the
    instance as two candidates -- while the serial path, called once per
    rule, writes two correct rows. Found by review of the first cut of
    this change, which did exactly that.

    Processes lever only, deliberately. Both rules' tasks are pickled
    at dispatch, so each worker redacts its own pre-redaction copy and
    both mutations come home -- deterministic. Under threads the two
    workers share the live instance, and whichever reads
    `sop_instance_uid` after the other's `regenerate_uid()` builds a
    mutation the parent cannot match and discards; that pre-existing
    race (#228 territory) makes the same input intermittently apply
    once or twice, and it is filed on its own rather than absorbed here
    as flakiness.
    """
    lever = "ISOCENTER_FORCE_PROCESSES"
    monkeypatch.setenv(lever, "1")
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name=f"dup_{lever}")
    db_path = session.persistence_file
    session.configuration.rules.append(
        {"serial_number": "SN_RELOAD",
         "redaction_zones": [[0, 4, 0, 4], [8, 12, 8, 12]]})

    applied = session.redact(show_progress=False)
    assert applied == 2, (
        "both rules' zones land and the second rule's config hash "
        "differs from the first's, so both passes must apply")
    session.close()

    rows = _redaction_rows(db_path)
    assert rows == [
        ("SN_RELOAD", "Applied 1 of 1 candidate images with 1 zones"),
        ("SN_RELOAD", "Applied 1 of 1 candidate images with 2 zones"),
    ], (
        f"two rule-passes over one serial wrote {rows}; each pass "
        "accounts for itself with its own zone count, in rule order")


@pytest.mark.parametrize("lever", LEVERS)
def test_a_wildcard_rule_writes_one_row_under_its_own_spelling(
        reloaded_redaction_session, monkeypatch, lever):
    """`"*"` is a rule-pass like any other, keyed as configured.

    The row answers "which configured pass ran"; per-instance
    attribution lives on the instances themselves (the attestation
    hash), so the wildcard's row carries the wildcard spelling rather
    than fanning out per matched machine.
    """
    monkeypatch.setenv(lever, "1")
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name=f"wild_{lever}")
    db_path = session.persistence_file
    session.configuration.rules = [
        {"serial_number": "*", "redaction_zones": [IN_IMAGE_ZONE]}]

    assert session.redact(show_progress=False) == 1
    session.close()

    assert _redaction_rows(db_path) == [
        ("*", "Applied 1 of 1 candidate images with 1 zones")]


def test_the_rows_are_written_before_the_failure_raise(
        reloaded_redaction_session, monkeypatch):
    """A caller that catches `RedactionError` still holds the accounting.

    The ordering is asserted in two comments, the CHANGELOG, and the PR
    that landed it -- and, measured during review, nothing else: moving
    the emitter after the raise on both paths left the whole suite
    green. This is the test that goes red when that happens.

    Threads lever only: the failing worker is a monkeypatched class
    attribute, and a spawned child re-imports the class unpatched, so
    under processes the patch never runs. The ordering under test is
    parent-side and executor-independent.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="fail_ordering")
    db_path = session.persistence_file

    from isocenter.services import RedactionOutcome

    def always_fails(task):
        uid = task["instance"].sop_instance_uid
        return RedactionOutcome(ok=False, sop_instance_uid=uid,
                                error="synthetic failure for the pin")

    monkeypatch.setattr(
        RedactionService, "execute_redaction_task",
        staticmethod(always_fails))

    with pytest.raises(RedactionError):
        session.redact(show_progress=False)
    session.close()

    assert _redaction_rows(db_path) == [
        ("SN_RELOAD", "Applied 0 of 1 candidate images with 1 zones")], (
        "the pass's row must reach the audit log before the raise; a "
        "caller that catches RedactionError otherwise holds a report "
        "with no accounting for the pass that just failed")


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

"""`DATA_LOSS` entries have to reach the report a human reads (#146).

#36, #125, and #137 all settled on the same pattern: warn, *and* write a
`DATA_LOSS` audit entry, because the log line alone is not a compliance
trail. The entries were written correctly and carried the tag and its
VR -- into a table the compliance report did not surface.

`get_audit_summary()` groups by `action_type`, so the report showed a
bare `DATA_LOSS | 3` with nothing saying what was lost.
`get_audit_errors()` filters to `('ERROR', 'WARNING')`, so the detail
never reached the Exceptions section. A count with no detail is worse
than nothing: it invites the reader to assume the number is benign.

The researcher who hits this reads the report, not `sqlite3 audit_log`.
"""
import os

import pytest

from isocenter import Session

TEST_DB = "test_data_loss_report.db"
REPORT_FILE = "test_data_loss_report.md"


@pytest.fixture
def clean_env():
    for f in (TEST_DB, REPORT_FILE):
        if os.path.exists(f):
            os.remove(f)
    yield
    for f in (TEST_DB, REPORT_FILE):
        if os.path.exists(f):
            os.remove(f)


def _report_with(losses):
    s = Session(TEST_DB)
    for entity, details in losses:
        s.store_backend.log_audit("DATA_LOSS", entity, details)
    s.store_backend.flush_audit_queue()
    s.generate_report(REPORT_FILE)
    s.close()
    with open(REPORT_FILE, "r") as f:
        return f.read()


def test_the_report_names_what_was_lost(clean_env):
    content = _report_with([
        ("1.2.3.4", "Dropped private binary element 0009,1002 (VR OB)"),
    ])

    assert "Data Loss" in content
    assert "0009,1002" in content, "the tag must reach the report"
    assert "OB" in content, "the VR is what says whether it was a serial number or a megabyte"
    assert "1.2.3.4" in content, "the instance must be identifiable"


def test_every_loss_is_listed_not_just_counted(clean_env):
    """The defect was a count with no detail. Two distinct losses must
    produce two rows, not `DATA_LOSS | 2`."""
    content = _report_with([
        ("1.2.3.4", "Dropped private binary element 0009,1002 (VR OB)"),
        ("1.2.3.5", "Dropped standard binary element 6000,3000 (VR OW)"),
    ])

    assert "0009,1002" in content
    assert "6000,3000" in content


def test_a_report_with_no_losses_says_so_explicitly(clean_env):
    """Silence reads as "not checked". The section states the negative
    for the same reason the Exceptions section does."""
    content = _report_with([])

    assert "Data Loss" in content
    assert "No data loss" in content


def test_data_loss_is_not_reclassified_as_an_exception(clean_env):
    """Pins the shape of the fix, not just its effect.

    Folding `DATA_LOSS` into `get_audit_errors()` would surface the
    detail in one line -- and flip every ingest of a file with an
    overlay to `REVIEW_REQUIRED`, because `validation_status` keys off
    `exceptions` being empty. That is a real decision about blast radius
    and it is not this one; see the test below.
    """
    content = _report_with([
        ("1.2.3.4", "Dropped private binary element 0009,1002 (VR OB)"),
    ])

    exceptions_section = content.split("Exceptions & Errors", 1)[1]
    assert "0009,1002" not in exceptions_section, (
        "the loss leaked into the exceptions section; it should have its "
        "own section so validation_status semantics are untouched")


def test_validation_status_is_deliberately_unchanged_by_data_loss(clean_env):
    """The open half of #146, pinned so it stays visible in the code.

    A session that dropped a vendor block still validates `PASS`. That
    is the *current* behaviour and this change does not alter it -- the
    question of whether a de-identification run that silently discarded
    data should be allowed to call itself PASS is genuinely open, and
    answering it here would have been a much larger blast radius smuggled
    in under a reporting fix.

    If that decision lands, this test is the one to change, and its
    failure is the intended signal rather than a regression.
    """
    content = _report_with([
        ("1.2.3.4", "Dropped private binary element 0009,1002 (VR OB)"),
    ])

    assert "**PASS**" in content, (
        "validation_status changed; if that was deliberate, update this "
        "test and say so in the changelog -- see #146")

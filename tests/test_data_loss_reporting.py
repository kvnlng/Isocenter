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

The grade is the second half, and it discriminates by group: a lost
*private* element flips `validation_status` to `REVIEW_REQUIRED`, a lost
*standard* one does not. See
`test_a_standard_tag_loss_still_grades_pass` for why that asymmetry is
deliberate rather than an oversight.
"""
import os
import sqlite3

import pytest

from isocenter import Session
from isocenter.io_handlers import LOSS_SCOPE_PRIVATE, LOSS_SCOPE_STANDARD

TEST_DB = "test_data_loss_report.db"
REPORT_FILE = "test_data_loss_report.md"

PRIVATE_LOSS = ("1.2.3.4",
                "Dropped private binary element 0009,1002 (VR OB)",
                LOSS_SCOPE_PRIVATE)
STANDARD_LOSS = ("1.2.3.5",
                 "Dropped standard binary element 6000,3000 (VR OW)",
                 LOSS_SCOPE_STANDARD)


@pytest.fixture
def clean_env():
    for f in (TEST_DB, REPORT_FILE):
        if os.path.exists(f):
            os.remove(f)
    yield
    for f in (TEST_DB, REPORT_FILE):
        if os.path.exists(f):
            os.remove(f)


def _report_with(losses, db_path=TEST_DB):
    s = Session(db_path)
    for entity, details, scope in losses:
        s.store_backend.log_audit("DATA_LOSS", entity, details,
                                  loss_scope=scope)
    s.store_backend.flush_audit_queue()
    s.generate_report(REPORT_FILE)
    s.close()
    with open(REPORT_FILE, "r") as f:
        return f.read()


def test_the_report_names_what_was_lost(clean_env):
    content = _report_with([PRIVATE_LOSS])

    assert "Data Loss" in content
    assert "0009,1002" in content, "the tag must reach the report"
    assert "OB" in content, "the VR is what says whether it was a serial number or a megabyte"
    assert "1.2.3.4" in content, "the instance must be identifiable"


def test_every_loss_is_listed_not_just_counted(clean_env):
    """The defect was a count with no detail. Two distinct losses must
    produce two rows, not `DATA_LOSS | 2`."""
    content = _report_with([PRIVATE_LOSS, STANDARD_LOSS])

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

    The grade now moves on a private loss, but the loss is still not an
    error: nothing failed. Folding `DATA_LOSS` into `get_audit_errors()`
    would have been the one-line way to move the grade, and it would
    have moved it for *every* loss -- overlays included -- while filing
    a routine drop under "Exceptions & Errors". The section and the
    grade are two separate decisions and this keeps them separable.
    """
    content = _report_with([PRIVATE_LOSS])

    exceptions_section = content.split("Exceptions & Errors", 1)[1]
    assert "0009,1002" not in exceptions_section, (
        "the loss leaked into the exceptions section; it belongs in its "
        "own section, whatever it does to validation_status")


def test_a_private_tag_loss_grades_review_required(clean_env):
    """The half of #146 that was open until now.

    A dropped private element is not routine. It is a vendor block the
    caller may well have asked to keep -- `remove_private_tags=False`
    cannot preserve what never reached the object graph -- and nobody
    can tell from the outside whether those bytes were a serial number
    or a megabyte of telemetry. A run that discarded one does not get to
    call itself PASS.

    This test replaces `test_validation_status_is_deliberately_unchanged
    _by_data_loss`, which named itself as the test to change when this
    decision landed. It did.
    """
    content = _report_with([PRIVATE_LOSS])

    assert "**REVIEW_REQUIRED**" in content, content


def test_a_standard_tag_loss_still_grades_pass(clean_env):
    """The asymmetry is deliberate; do not "simplify" it into one rule.

    Overlay Data `(60xx,3000)` and the palette LUTs `(0028,120x)` are
    dropped on ordinary images, by the thousand, with no vendor intent
    behind them. Flipping the grade on those would mark most real
    cohorts REVIEW_REQUIRED and turn the grade into noise -- which is
    the same failure mode #146 opened against a bare `DATA_LOSS: 3`.
    """
    content = _report_with([STANDARD_LOSS])

    assert "**PASS**" in content, content
    assert "6000,3000" in content, "reported, just not graded"


def test_one_private_loss_among_standard_ones_still_grades_review_required(
        clean_env):
    """The grade is a floor, not a majority vote."""
    content = _report_with([STANDARD_LOSS, PRIVATE_LOSS, STANDARD_LOSS])

    assert "**REVIEW_REQUIRED**" in content, content


def test_the_report_says_which_losses_are_graded(clean_env):
    """A grade the reader cannot trace back to a row is the same defect
    as a count with no detail. The scope column is what connects
    REVIEW_REQUIRED to the element that caused it."""
    content = _report_with([PRIVATE_LOSS, STANDARD_LOSS])

    loss_section = content.split("Data Loss", 1)[1].split("Exceptions", 1)[0]
    assert LOSS_SCOPE_PRIVATE in loss_section, loss_section
    assert LOSS_SCOPE_STANDARD in loss_section, loss_section


def test_a_database_written_before_the_scope_column_existed_still_opens(
        clean_env, tmp_path):
    """Stores in the wild predate `loss_scope` and must keep working.

    Their `DATA_LOSS` rows carry no scope at all, so there is nothing to
    grade on and they read as PASS -- the behaviour they were written
    under. Building the 0.8.x table shape by hand rather than deleting a
    column is what makes this exercise the `ALTER TABLE` migration
    instead of the current schema.
    """
    legacy = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(legacy)
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            action_type TEXT,
            entity_uid TEXT,
            details TEXT
        )
    """)
    conn.execute(
        "INSERT INTO audit_log (timestamp, action_type, entity_uid, details) "
        "VALUES ('2024-01-01T00:00:00', 'DATA_LOSS', '1.2.3.4', "
        "'Dropped private binary element 0009,1002 (VR OB)')")
    conn.commit()
    conn.close()

    content = _report_with([], db_path=legacy)

    assert "0009,1002" in content, "the pre-existing row must still be read"
    assert "**PASS**" in content, (
        "a row with no recorded scope cannot be graded and must not be "
        "guessed at")

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

The Scope column is asserted per *row*, not per section (#157). Hand
mutation-testing found that swapping every scope for its opposite left
this file green: checking that both scopes appear somewhere under the
heading is satisfied by any permutation of the column, and a column
that can lie points the reader at the wrong element -- the same defect
as the bare `DATA_LOSS: 3` count, one layer up.
"""
import os
import sqlite3

import pytest

from isocenter import Session
from isocenter.io_handlers import (LOSS_SCOPE_PRIVATE, LOSS_SCOPE_STANDARD,
                                   loss_scope_for_tag)

TEST_DB = "test_data_loss_report.db"
REPORT_FILE = "test_data_loss_report.md"

PRIVATE_LOSS = ("1.2.3.4",
                "Dropped private binary element 0009,1002 (VR OB)",
                LOSS_SCOPE_PRIVATE)
STANDARD_LOSS = ("1.2.3.5",
                 "Dropped standard binary element 6000,3000 (VR OW)",
                 LOSS_SCOPE_STANDARD)
# A second private loss, deliberately not the same element as
# `PRIVATE_LOSS`: the legacy fixture row below is that element, and a
# test that mixes recorded and unrecorded scopes cannot tell the two
# rows apart if they name the same tag.
OTHER_PRIVATE_LOSS = ("1.2.3.9",
                      "Dropped private binary element 0011,1001 (VR OB)",
                      LOSS_SCOPE_PRIVATE)


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


def _legacy_store(tmp_path, name="legacy.db"):
    """A store with the 0.8.x `audit_log` shape and one ungraded row.

    The table is written out by hand, with no `loss_scope` column at
    all, rather than by letting `Session` create the current schema and
    dropping the column afterwards. That is the whole point: it is what
    makes every caller exercise `SqliteStore._add_missing_columns`, and
    disabling that `ALTER TABLE` is caught by these tests and nothing
    else in the suite. Do not "simplify" this to the current schema --
    the migration would lose its only coverage and stay green (#157).

    Args:
        tmp_path: pytest's per-test directory.
        name (str): Filename, so one test can build two distinct stores.

    Returns:
        str: Path to the store. Its single `DATA_LOSS` row names
        `0009,1002` and has a NULL scope.
    """
    path = str(tmp_path / name)
    conn = sqlite3.connect(path)
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
    return path


def _loss_table(content):
    """The Data Loss table, split into header, delimiter and row cells.

    Per *cell*, because the gap #157 was opened about is that a scope
    appearing somewhere in the section says nothing about which row
    carries it: with one private and one standard loss, any permutation
    of the column satisfies a membership check.

    The `| :--- |` delimiter is identified by its content rather than by
    its position, so a mutation that drops it cannot silently shift the
    first data row into the header slot. It is *returned* rather than
    discarded because it is the row that declares the column count:
    under GFM a delimiter whose cell count disagrees with the header
    means the block is not a table at all, and the section renders as
    literal pipes with no Scope column for anyone to read -- the same
    reader-facing failure as an unlabelled header, which was live and
    unobservable while this helper filtered the line away (#157).

    Returns:
        tuple: (header_cells, delimiter_cells, [row_cells, ...]).
        `delimiter_cells` is None if the table has no delimiter row.
    """
    section = content.split("Data Loss", 1)[1].split("Exceptions", 1)[0]
    lines = [ln.strip() for ln in section.splitlines()
             if ln.strip().startswith("|")]
    delimiter = None
    cells = []
    for line in lines:
        row = [cell.strip() for cell in line.strip("|").split("|")]
        if set(line) <= set("|:- "):
            if delimiter is None:
                delimiter = row
            continue
        cells.append(row)
    return cells[0], delimiter, cells[1:]


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
    REVIEW_REQUIRED to the element that caused it.

    Asserted per row, not per section. This test used to check that
    both scopes appeared *somewhere* under the heading, which with one
    private and one standard loss is satisfied by every permutation of
    the column -- including the one that points the reader at the wrong
    element, which is the entire failure the column exists to prevent
    (#157).
    """
    content = _report_with([PRIVATE_LOSS, STANDARD_LOSS])

    _header, _delimiter, rows = _loss_table(content)
    by_tag = {tag: row[3]
              for row in rows
              for tag in ("0009,1002", "6000,3000") if tag in row[2]}

    assert by_tag == {"0009,1002": LOSS_SCOPE_PRIVATE,
                      "6000,3000": LOSS_SCOPE_STANDARD}, rows


def test_the_loss_table_is_well_formed(clean_env):
    """The column has to be labelled, and labelled where it is read.

    A header that has lost its last cell while the rows still emit four
    renders as a table whose scopes sit under "Element" -- markdown
    does not complain, and a reader tracing the grade reads the wrong
    column. The named header pins which cell is the scope; the
    cell-count equality pins that no row disagrees with it.
    """
    content = _report_with([PRIVATE_LOSS, STANDARD_LOSS])

    header, delimiter, rows = _loss_table(content)

    assert header == ["Timestamp", "Instance", "Element", "Scope"], header
    assert delimiter is not None, (
        "no delimiter row: GFM does not recognise the block as a table "
        "without one, so the section renders as literal pipe characters")
    assert len(delimiter) == len(header), (
        "the delimiter declares the column count and disagrees with the "
        "header, which un-makes the table and takes the Scope column "
        "with it", header, delimiter)
    assert rows, content
    for row in rows:
        assert len(row) == len(header), (header, row)


def test_a_loss_with_no_recorded_scope_reads_as_unrecorded(clean_env,
                                                           tmp_path):
    """An empty cell is not the same statement as "unrecorded".

    A migrated row has no scope to print, and a blank there reads as an
    omission the reader is invited to fill in -- most likely with
    "standard", which is exactly the silent downgrade the NULL exists
    to refuse. The word is the whole point of the cell (#157).
    """
    legacy = _legacy_store(tmp_path)

    content = _report_with([], db_path=legacy)

    _header, _delimiter, rows = _loss_table(content)
    assert [row[3] for row in rows] == ["unrecorded"], rows


def test_a_database_written_before_the_scope_column_existed_still_opens(
        clean_env, tmp_path):
    """Stores in the wild predate `loss_scope` and must keep working.

    Their `DATA_LOSS` rows carry no scope at all, so there is nothing to
    grade on and they read as PASS -- the behaviour they were written
    under. Building the 0.8.x table shape by hand rather than deleting a
    column is what makes this exercise the `ALTER TABLE` migration
    instead of the current schema.
    """
    legacy = _legacy_store(tmp_path)

    content = _report_with([], db_path=legacy)

    assert "0009,1002" in content, "the pre-existing row must still be read"
    assert "**PASS**" in content, (
        "a row with no recorded scope cannot be graded and must not be "
        "guessed at")


def test_an_ungraded_row_and_a_graded_one_keep_their_own_scopes(
        clean_env, tmp_path):
    """A store that predates the column and then records new losses.

    The realistic upgrade path, and the one case where both kinds of
    row are on the page at once: the migrated row cannot be graded and
    must stay `unrecorded`, the new one must carry the scope its own
    emitter recorded, and the grade must come from the row that has
    one. Neither row may borrow the other's scope.
    """
    legacy = _legacy_store(tmp_path)

    content = _report_with([OTHER_PRIVATE_LOSS], db_path=legacy)

    _header, _delimiter, rows = _loss_table(content)
    by_tag = {tag: row[3]
              for row in rows
              for tag in ("0009,1002", "0011,1001") if tag in row[2]}

    assert by_tag == {"0009,1002": "unrecorded",
                      "0011,1001": LOSS_SCOPE_PRIVATE}, rows
    assert "**REVIEW_REQUIRED**" in content, (
        "the graded row still decides the grade; an ungraded neighbour "
        "does not excuse it")


@pytest.mark.parametrize("tag,expected", [
    ("0009,1002", LOSS_SCOPE_PRIVATE),
    ("0011,1001", LOSS_SCOPE_PRIVATE),
    ("6000,3000", LOSS_SCOPE_STANDARD),
    ("0028,1201", LOSS_SCOPE_STANDARD),
    ("7fe0,0010", LOSS_SCOPE_STANDARD),
])
def test_loss_scope_is_decided_by_group_parity(tag, expected):
    """The grading rule itself, tested where it is written.

    It was reachable only through the emitters until now, which is a
    long way from the assertion for a function whose two return values
    are the difference between PASS and REVIEW_REQUIRED.
    """
    assert loss_scope_for_tag(tag) == expected


@pytest.mark.parametrize("tag", ["", "notatag", "gggg,eeee", "0009;1002"])
def test_an_unparseable_tag_raises_rather_than_defaulting(tag):
    """The documented contract, and the reason for it.

    `loss_scope_for_tag` deliberately does not catch this: every caller
    holds a tag it has already parsed, so an unparseable one is a bug,
    and a `except ValueError: return LOSS_SCOPE_STANDARD` would answer
    it by grading a possibly-private loss as routine -- turning a
    crash, which someone fixes, into a PASS nobody questions.
    """
    with pytest.raises(ValueError):
        loss_scope_for_tag(tag)

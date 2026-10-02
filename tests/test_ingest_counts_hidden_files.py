"""`ingest()` counts the files it does not read because their name starts
with `.` (#795, owner ruling Q2 A).

Walking a directory, `import_files` skips every file whose name starts
with `.`, so `.DS_Store` and AppleDouble `._*` files do not become
rejected files with `ERROR` rows that cost every Mac-touched folder its
`PASS`. It counted them nowhere: measured on b440bca0, a valid MR named
`.mr_hidden.dcm` was absent from the summary, the log, the audit trail and
the console, while `IngestSummary`'s docstring said every file takes one
of four routes.

The skip stays; `IngestSummary.hidden` counts it. A directory whose name
starts with `.` is still walked, and a file named directly is still read.
"""
import os
import shutil
import sqlite3

from pydicom.data import get_testdata_file

from isocenter import Session

HIDDEN_LINE = "2 file(s) whose name starts with '.' were not read"


def _folder(with_junk=True):
    os.makedirs("input/.hiddendir")
    shutil.copy(get_testdata_file("CT_small.dcm"), "input/ct.dcm")
    shutil.copy(get_testdata_file("MR_small.dcm"), "input/.mr_hidden.dcm")
    shutil.copy(get_testdata_file("rtdose.dcm"), "input/.hiddendir/dose.dcm")
    with open("input/.DS_Store", "wb") as fh:
        fh.write(b"\x00\x00\x00\x01Bud1" + b"\x00" * 64)
    if with_junk:
        with open("input/notes.txt", "w", encoding="utf-8") as fh:
            fh.write("not dicom")


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute(
            "SELECT action_type, details FROM audit_log").fetchall()


def test_hidden_files_are_counted_not_read(capsys):
    _folder()
    with Session("s.db") as session:
        summary = session.ingest("input")
        out = capsys.readouterr().out
        rows = _rows(session)
        n_instances = sum(len(se.instances) for p in session.store.patients
                          for st in p.studies for se in st.series)

    # Absolute values first, so `hidden` and `skipped` cannot agree at 0.
    assert summary.hidden == 2
    assert summary.skipped == 0
    assert summary.ingested == 2
    assert [path for path, _ in summary.failures] == [
        os.path.join("input", "notes.txt")]
    assert summary.declined == 0
    # The CT, and the RT Dose from the hidden *directory*; not the MR.
    assert n_instances == 2

    assert len(rows) == 1, rows
    assert rows[0][0] == "ERROR" and "notes.txt" in rows[0][1]
    assert not any(".mr_hidden" in d or ".DS_Store" in d for _, d in rows)

    assert HIDDEN_LINE in out


def test_the_early_return_counts_them_too(capsys):
    """Every non-hidden file already known: `import_files` returns before
    reading anything, and that return carries `hidden` as well."""
    _folder(with_junk=False)
    with Session("s.db") as session:
        first = session.ingest("input")
        assert (first.ingested, first.hidden) == (2, 2)
        capsys.readouterr()
        second = session.ingest("input")
        out = capsys.readouterr().out
    assert second.skipped == 2
    assert second.hidden == 2
    assert second.ingested == 0
    assert second.failures == []
    assert HIDDEN_LINE in out


def test_a_hidden_directory_is_walked_and_not_counted():
    os.makedirs("input/.d")
    shutil.copy(get_testdata_file("CT_small.dcm"), "input/.d/x.dcm")
    with Session("s.db") as session:
        summary = session.ingest("input")
    assert summary.ingested == 1
    assert summary.hidden == 0


def test_a_dotfile_named_directly_is_read():
    os.makedirs("input")
    shutil.copy(get_testdata_file("CT_small.dcm"), "input/.ct.dcm")
    with Session("s.db") as session:
        summary = session.ingest(os.path.join("input", ".ct.dcm"))
    assert summary.ingested == 1
    assert summary.hidden == 0


def test_no_hidden_file_prints_no_line(capsys):
    os.makedirs("input")
    shutil.copy(get_testdata_file("CT_small.dcm"), "input/ct.dcm")
    with Session("s.db") as session:
        summary = session.ingest("input")
        out = capsys.readouterr().out
    assert summary.ingested == 1
    assert summary.hidden == 0
    assert "starts with '.'" not in out


def test_hidden_is_the_last_field():
    """Positional construction of the four existing fields still means
    what it meant: the new field goes last."""
    from isocenter.io_handlers import IngestSummary
    summary = IngestSummary(3, [], 1, 2)
    assert (summary.ingested, summary.declined, summary.skipped,
            summary.hidden) == (3, 1, 2, 0)
    assert repr(summary) == ("IngestSummary(ingested=3, failures=[], "
                             "declined=1, skipped=2, hidden=0)")

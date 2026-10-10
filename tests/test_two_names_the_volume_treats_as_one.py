"""Two exported instances whose file names the volume resolves to one file (#1020).

`export()` names a file by its SOP Instance UID. A conformant UID is digits
and dots, but a UID is whatever the source wrote, and a letter passes the
file-name rule (`io_handlers.export_file_name`). Measured on `main` at
07bdbcad, 3.12.14 and 3.14.7t, macOS, case-insensitive APFS: two CTs of one
series whose UIDs are `SOPa` and `SOPA`, no pass, gave **one** `.dcm`, named
by one spelling and holding either instance's content (it varied by run),
with both UIDs in `ExportSummary.written_uids`, no failure, no row, `PASS`.
On a case-sensitive APFS image the same pair is two files. The U+212B /
U+00C5 pair is one file on both. So no text key is right on every volume:
`lower()` misses collisions, and NFC + `casefold()` reports two that a
case-sensitive volume does not have.

What the owner ruled (2026-10-09), and what this file pins:

- **Q1 A: the volume is asked, after the write.** Among the tasks this run
  delivered, two different planned paths that are one file (same device and
  inode) are one collision: one `ERROR` row, `REVIEW_REQUIRED`. No name is
  folded as text.
- **Q2 A: `written_uids` holds only the instance whose content the file
  carries**, read back from the file's `(0008,0018)`; the others are in
  `failures`. If that cannot be read, the whole group moves and the row
  says so.
- The worker's temporary name carries the thread's id beside the pid: on a
  free-threaded build `write_tree()`'s workers are threads of one process,
  and two of them writing two such names shared one temporary file.

**Which tests depend on the volume.** Two do, and neither skips:
`test_a_folded_pair_is_one_error_row_and_one_written_uid` and
`test_the_surviving_uid_is_the_one_in_the_file` first ask the test's own
`tmp_path` whether it resolves `SOPa.dcm` and `SOPA.dcm` to one file, and
assert the collision where it does and two files, no row and `PASS` where it
does not (a Linux runner). Every arm of the detection is also pinned by a
test that runs the same on any volume: the `_collide` tests build one file
under two names with a hard link, which is one inode everywhere, and
`test_a_collision_is_asked_of_the_volume_not_of_the_text` replaces the one
function that asks (`session._delivered_file_identity`).

This file names `isocenter.session` and `isocenter.io_handlers`, so both
probe rows are charged with it.
"""
import os
import sqlite3
import threading
import types
from pathlib import Path

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter import io_handlers
from isocenter import session as session_module
from isocenter.io_handlers import DicomExporter, ExportError, ExportSummary
from isocenter.session import DicomSession

#: Read through `session`, never imported from its own module: this file
#: belongs to the `session` and `io_handlers` probe rows only.
ReversibilityService = session_module.ReversibilityService

ROOT = "1.2.826.0.1.3680043.10.9999.1020"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    """`write_tree()` in threads; `session.export()` always spawns."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(path, sop_uid, number=1):
    """CT_small in one fixed patient, study and series, under `sop_uid`
    exactly as given (pydicom's own check of a UI value switched off)."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PAT-1020"
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.InstanceNumber = number
    ds[0x00080018] = pydicom.DataElement(
        0x00080018, "UI", sop_uid, validation_mode=pydicom.config.IGNORE)
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with pydicom.config.disable_value_validation():
        ds.save_as(str(path))


def _source(tmp_path, *uids):
    folder = tmp_path / "in"
    for number, uid in enumerate(uids, start=1):
        _write(folder / f"{number}.dcm", uid, number)
    return str(folder)


def _folds(tmp_path):
    """Whether this volume resolves `SOPa.dcm` and `SOPA.dcm` to one file."""
    probe = tmp_path / "does_it_fold"
    probe.mkdir()
    (probe / "SOPa.dcm").write_bytes(b"x")
    return (probe / "SOPA.dcm").exists()


def _dcm_files(folder):
    return sorted(os.path.join(root, name)
                  for root, _, names in os.walk(folder) for name in names
                  if name.endswith(".dcm"))


def _all_files(folder):
    return sorted(os.path.join(root, name)
                  for root, _, names in os.walk(folder) for name in names)


def _rows(db_path, action="ERROR"):
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type = ? "
            "ORDER BY rowid", (action,)).fetchall()


def _grade(session, tmp_path):
    report = tmp_path / "report.md"
    session.generate_report(str(report))
    text = report.read_text(encoding="utf-8")
    return [g for g in ("PASS", "REVIEW_REQUIRED", "FAIL") if f"**{g}**" in text]


def _folded(uids, survivor):
    """The row for instances written to one file, whole."""
    n = len(uids)
    others = "other was" if n == 2 else f"other {n - 1} were"
    return (f"{n} exported instances ({', '.join(sorted(uids))}) were written "
            f"under file names this volume resolves to one file: each "
            f"successful write replaced the one before it, and the folder "
            f"holds one file for all {n} of them. That file carries "
            f"{survivor}; the {others} not delivered.")


def _folded_unread(uids):
    n = len(uids)
    return (f"{n} exported instances ({', '.join(sorted(uids))}) were written "
            f"under file names this volume resolves to one file: each "
            f"successful write replaced the one before it, and the folder "
            f"holds one file for all {n} of them. Which of them that file "
            f"carries could not be read from it, so none of the {n} is "
            f"reported as written.")


def _export(tmp_path, *uids, name="s"):
    """Ingest and export with no pass. Returns `(summary, out, db, grade)`."""
    db = tmp_path / f"{name}.db"
    out = tmp_path / f"{name}_out"
    with DicomSession(str(db)) as session:
        session.ingest(_source(tmp_path, *uids))
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        grade = _grade(session, tmp_path)
    return summary, out, db, grade


# --------------------------------------------------------------------------
# The pair, through `export()`, on the volume the test runs on
# --------------------------------------------------------------------------

def test_a_folded_pair_is_one_error_row_and_one_written_uid(tmp_path):
    """The issue's pair, no pass. Where the volume folds the two names:
    one `.dcm`; `written_uids` is exactly the UID that file holds (not
    merely one UID: dropping both would also leave fewer than two); the
    other is in `failures` with the row's sentence; one `ERROR` row keyed
    on the survivor; `REVIEW_REQUIRED`. Red before #1020 on such a volume:
    both UIDs written, no failure, no row, `PASS`. Where the volume keeps
    the names apart: two files, both UIDs, no row, `PASS`, before and now.

    Kills: the loser left in `written_uids`; both dropped; no row; a row
    on a volume that wrote two files (a text key)."""
    folds = _folds(tmp_path)
    summary, out, db, grade = _export(tmp_path, "SOPa", "SOPA")
    files = _dcm_files(out)
    held = sorted(str(pydicom.dcmread(f).SOPInstanceUID) for f in files)
    if not folds:
        assert held == ["SOPA", "SOPa"]
        assert sorted(summary.written_uids) == ["SOPA", "SOPa"]
        assert summary.failures == [] and _rows(db) == []
        assert grade == ["PASS"]
        return
    [survivor] = held
    [loser] = sorted({"SOPa", "SOPA"} - {survivor})
    sentence = _folded(["SOPa", "SOPA"], survivor)
    assert summary.written_uids == [survivor]
    assert summary.failures == [(loser, sentence)]
    assert (summary.written, summary.failed) == (1, 1)
    assert _rows(db) == [(survivor, sentence)]
    assert grade == ["REVIEW_REQUIRED"]
    [(_, export_row)] = _rows(db, "EXPORT")
    assert "wrote 1 of 2 planned instances" in export_row
    assert [f for f in _all_files(out) if f.endswith(".tmp")] == []


@pytest.mark.parametrize("holds", ["SOPa", "SOPA"])
def test_the_surviving_uid_is_the_one_in_the_file(tmp_path, monkeypatch, holds):
    """Which instance's write lands last is the pool's to decide (under
    processes the plan's last won nearly every run here, so a test that
    waits for both outcomes does not see both). The survivor is therefore
    read from the file, and this test decides what the file holds: between
    the batch and the report, the one file is rewritten in place to hold
    `holds`, once for each of the two. `written_uids` follows the file both
    times, through `export()`. On a volume that keeps the names apart
    nothing is rewritten and each of the two files holds its own UID.

    Kills, on a folding volume, on every run: the plan's first, or its
    last, kept as the survivor; the UID read back compared after folding
    its case. (`test_the_read_back_uid_is_compared_as_written` holds the
    same on every volume.)"""
    folds = _folds(tmp_path)
    report = DicomSession._report_export_collisions

    def with_the_file_decided(self, tasks, summary):
        if folds:
            _write(tasks[0].output_path, holds)
        return report(self, tasks, summary)

    monkeypatch.setattr(DicomSession, "_report_export_collisions",
                        with_the_file_decided)
    summary, out, db, _ = _export(tmp_path, "SOPa", "SOPA")
    held = sorted(str(pydicom.dcmread(f).SOPInstanceUID)
                  for f in _dcm_files(out))
    if not folds:
        assert held == ["SOPA", "SOPa"]
        assert sorted(summary.written_uids) == held
        return
    [loser] = {"SOPa", "SOPA"} - {holds}
    sentence = _folded(["SOPa", "SOPA"], holds)
    assert held == [holds], "setup: the one file holds what the test wrote"
    assert summary.written_uids == [holds]
    assert summary.failures == [(loser, sentence)]
    assert _rows(db) == [(holds, sentence)]


@pytest.mark.parametrize("holds", ["SOPa", "SOPA"])
def test_the_read_back_uid_is_compared_as_written(tmp_path, holds):
    """Two UIDs that differ only in letter case, under two hard-linked
    names of one file, on any volume: the member whose UID the file holds,
    letter for letter, stays. Kills: the UID read back, or the members',
    folded in case before they are compared (which keeps one fixed member
    whatever the file holds)."""
    tasks, summary = _collide(tmp_path, holds, "SOPa", "SOPA")
    replaced, rows = _report(tmp_path, tasks, summary)
    [loser] = {"SOPa", "SOPA"} - {holds}
    sentence = _folded(["SOPa", "SOPA"], holds)
    assert summary.written_uids == [holds]
    assert summary.failures == [(loser, sentence)]
    assert rows == [(holds, sentence)]
    assert replaced == {id(task) for task in tasks
                        if task.instance.sop_instance_uid == loser}


def test_a_pair_the_default_pass_renames_is_two_files(tmp_path):
    """After `audit()` and `anonymize()` under the default configuration
    each instance has its own `2.25.` UID: two files, no row. Kills: the
    collision asked of the source UIDs, or of the plan before the pass."""
    db, out = tmp_path / "s.db", tmp_path / "out"
    with DicomSession(str(db)) as session:
        session.ingest(_source(tmp_path, "SOPa", "SOPA"))
        session.anonymize(session.audit())
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
    assert len(_dcm_files(out)) == 2
    assert len(set(summary.written_uids)) == 2 and summary.failures == []
    assert _rows(db) == []


# --------------------------------------------------------------------------
# The volume is what is asked: the same on every volume
# --------------------------------------------------------------------------

def test_a_collision_is_asked_of_the_volume_not_of_the_text(tmp_path, monkeypatch):
    """`session._delivered_file_identity` is the one place the volume is
    asked. Replaced so that two conformant names, which no volume folds,
    report one file: the row is written. Replaced so that every path
    reports a file of its own, over `SOPa`/`SOPA`: no row, on a folding
    volume too. A key computed from the names' text gets both wrong.

    Kills: grouping by a folded path string (option A of the issue);
    grouping by the exact path string alone."""
    a, b = f"{ROOT}.1.1.1", f"{ROOT}.1.1.2"
    monkeypatch.setattr(session_module, "_delivered_file_identity",
                        lambda path: (1, 1))
    summary, out, db, grade = _export(tmp_path, a, b, name="same")
    assert len(_dcm_files(out)) == 2, "setup: the volume wrote two files"
    [survivor] = summary.written_uids
    [(loser, sentence)] = summary.failures
    assert {survivor, loser} == {a, b}
    assert sentence == _folded([a, b], survivor)
    assert _rows(db) == [(survivor, sentence)]
    assert grade == ["REVIEW_REQUIRED"]

    seen = {}
    monkeypatch.setattr(session_module, "_delivered_file_identity",
                        lambda path: (1, seen.setdefault(path, len(seen) + 1)))
    work = tmp_path / "apart"
    work.mkdir()
    summary, _, db, grade = _export(work, "SOPa", "SOPA", name="apart")
    assert sorted(summary.written_uids) == ["SOPA", "SOPa"]
    assert summary.failures == [] and _rows(db) == []
    assert grade == ["PASS"]
    assert len(seen) == 2, "setup: the volume was asked about both paths"


def test_the_identity_is_the_files_device_and_inode(tmp_path):
    """Two names of one file answer alike, and two files do not. Kills:
    the identity taken from the path, or from the inode without its
    device."""
    one, link, other = tmp_path / "one", tmp_path / "link", tmp_path / "other"
    one.write_bytes(b"1")
    other.write_bytes(b"2")
    os.link(one, link)
    identity = session_module._delivered_file_identity
    assert identity(str(one)) == identity(str(link))
    assert identity(str(one)) != identity(str(other))
    stat = os.stat(one)
    assert identity(str(one)) == (stat.st_dev, stat.st_ino)
    assert identity(str(tmp_path / "missing")) is None


def _task(uid, path, sequences=None):
    return types.SimpleNamespace(
        output_path=str(path),
        instance=types.SimpleNamespace(sop_instance_uid=uid,
                                       sequences=sequences or {}))


def _collide(tmp_path, holds, *uids, delivered=None):
    """One file, holding `holds`, under one hard-linked name per UID: a
    real inode shared by different names on any volume. Returns the tasks
    and a summary saying every one in `delivered` (default: all) was
    written."""
    folder = tmp_path / "linked"
    folder.mkdir()
    first = folder / "0.dcm"
    if holds is None:
        first.write_bytes(b"not a DICOM file")
    else:
        _write(first, holds)
    tasks = [_task(uids[0], first)]
    for position, uid in enumerate(uids[1:], start=1):
        os.link(first, folder / f"{position}.dcm")
        tasks.append(_task(uid, folder / f"{position}.dcm"))
    written = list(uids if delivered is None else delivered)
    return tasks, ExportSummary(written_uids=written, failures=[])


def _report(tmp_path, tasks, summary):
    """Run the report in a session; return its answer and the rows."""
    db = tmp_path / "report.db"
    with DicomSession(str(db)) as session:
        replaced = session._report_export_collisions(tasks, summary)
    return replaced, _rows(db)


@pytest.mark.parametrize("survivor", ["1.2.3.1", "1.2.3.2", "1.2.3.3"])
def test_the_instance_the_file_holds_stays_and_the_others_fail(tmp_path, survivor):
    """Three names of one file. Whichever UID the file holds stays in
    `written_uids`; the other two move to `failures`, each under its own
    UID with the row's sentence; one row, keyed on the survivor. The plan's
    order is the same in all three cases. Kills: the first or last of the
    group kept; one loser moved and not the other; a row per loser."""
    uids = ["1.2.3.1", "1.2.3.2", "1.2.3.3"]
    tasks, summary = _collide(tmp_path, survivor, *uids)
    replaced, rows = _report(tmp_path, tasks, summary)
    sentence = _folded(uids, survivor)
    losers = [uid for uid in uids if uid != survivor]
    assert summary.written_uids == [survivor]
    assert summary.failures == [(uid, sentence) for uid in losers]
    assert rows == [(survivor, sentence)]
    assert replaced == {id(task) for task in tasks
                        if task.instance.sop_instance_uid != survivor}


def test_a_file_that_cannot_be_read_back_moves_the_whole_group(tmp_path):
    """The one file cannot be read as DICOM, so nothing says whose it is:
    every member moves to `failures`, the row says so and is keyed
    `MULTIPLE`. No member is reported as replaced: the file is there, and
    what it carries is not known. Kills: the plan's first kept on a
    failed read; the read's exception escaping the export."""
    uids = ["1.2.3.1", "1.2.3.2"]
    tasks, summary = _collide(tmp_path, None, *uids)
    replaced, rows = _report(tmp_path, tasks, summary)
    sentence = _folded_unread(uids)
    assert summary.written_uids == []
    assert summary.failures == [(uid, sentence) for uid in uids]
    assert rows == [("MULTIPLE", sentence)]
    assert replaced == set()


def test_a_file_holding_none_of_the_groups_uids_moves_the_whole_group(tmp_path):
    """A file that reads back under a UID no member of the group has is
    no member's delivery. Kills: any member kept on a UID that is not
    its own."""
    uids = ["1.2.3.1", "1.2.3.2"]
    tasks, summary = _collide(tmp_path, "1.2.3.9", *uids)
    _, rows = _report(tmp_path, tasks, summary)
    assert summary.written_uids == []
    assert summary.failures == [(uid, _folded_unread(uids)) for uid in uids]
    assert rows == [("MULTIPLE", _folded_unread(uids))]


def test_two_members_sharing_the_surviving_uid_leave_it_once(tmp_path):
    """Two instances of one UID under two names of one file (two patients'
    folders resolving to one, say): the UID is in `written_uids` twice and
    the folder holds one file. One stays; the other is a failure under the
    same UID. Neither is reported as replaced: the file names their UID,
    which does not say which of the two it holds, so the disclosure that
    follows still counts both. Kills: losers chosen by UID, which finds
    none here; a same-UID loser's token left out of the disclosure."""
    uid = "1.2.3.1"
    tasks, summary = _collide(tmp_path, uid, uid, uid)
    replaced, rows = _report(tmp_path, tasks, summary)
    sentence = _folded([uid, uid], uid)
    assert summary.written_uids == [uid]
    assert summary.failures == [(uid, sentence)]
    assert rows == [(uid, sentence)]
    assert replaced == set()


def test_only_what_this_run_delivered_is_grouped(tmp_path):
    """A hard link already in the folder is two paths and one inode, and
    no collision of this run when the run wrote only one of them (the
    other task failed, and has its own row). Kills: grouping over every
    planned path that exists."""
    tasks, summary = _collide(tmp_path, "1.2.3.1", "1.2.3.1", "1.2.3.2",
                              delivered=["1.2.3.1"])
    replaced, rows = _report(tmp_path, tasks, summary)
    assert summary.written_uids == ["1.2.3.1"]
    assert summary.failures == [] and rows == [] and replaced == set()


def test_a_volume_that_reports_no_inode_gives_no_identity(tmp_path, monkeypatch):
    """Some network volumes answer `st_ino == 0` for every file. That is
    no identity: two such files are not one file. `os.stat` is replaced
    for this one call only. Kills: `(st_dev, 0)` returned, which would
    make every file of such a volume one collision."""
    path = tmp_path / "file"
    path.write_bytes(b"1")
    with monkeypatch.context() as patch:
        patch.setattr(session_module.os, "stat",
                      lambda p: types.SimpleNamespace(st_dev=7, st_ino=0))
        assert session_module._delivered_file_identity(str(path)) is None


def test_files_with_no_identity_are_not_grouped(tmp_path, monkeypatch):
    """A file gone by the time it is asked about, or a volume with no
    inodes, answers None for every path, and None is not a group. Kills:
    paths with no identity grouped under the one key None."""
    tasks, summary = _collide(tmp_path, "1.2.3.1", "1.2.3.1", "1.2.3.2")
    monkeypatch.setattr(session_module, "_delivered_file_identity",
                        lambda path: None)
    replaced, rows = _report(tmp_path, tasks, summary)
    assert sorted(summary.written_uids) == ["1.2.3.1", "1.2.3.2"]
    assert summary.failures == [] and rows == [] and replaced == set()


def test_a_uid_holding_a_pipe_is_escaped_in_the_row(tmp_path):
    """The detail is rendered into a markdown table row, as its neighbour
    is. (`export_file_name` refuses such a UID before any write; the
    report does not rest on that.)"""
    tasks, summary = _collide(tmp_path, "1.2|3", "1.2|3", "1.2.4")
    _, rows = _report(tmp_path, tasks, summary)
    [(key, detail)] = rows
    assert key == "1.2|3"
    assert detail == _folded(["1.2|3", "1.2.4"], "1.2|3").replace("|", "\\|")


# --------------------------------------------------------------------------
# What it must not disturb
# --------------------------------------------------------------------------

def test_two_instances_sharing_a_uid_still_get_their_own_sentence(tmp_path):
    """Two instances given one UID are planned to one path string: the
    existing row, whole and unchanged, and no second row from the new arm
    (one path is not two names). `written_uids` is as before too: this is
    #993's, not #1020's. Kills: the "different paths" condition dropped
    (a second row); the old sentence reworded."""
    uid_a, uid_b = f"{ROOT}.1.1.1", f"{ROOT}.1.1.2"
    db, out = tmp_path / "s.db", tmp_path / "out"
    with DicomSession(str(db)) as session:
        session.ingest(_source(tmp_path, uid_a, uid_b))
        instances = [i for p in session.store.patients for st in p.studies
                     for se in st.series for i in se.instances]
        next(i for i in instances if i.sop_instance_uid == uid_b) \
            .sop_instance_uid = uid_a
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
    assert len(_dcm_files(out)) == 1
    assert summary.written_uids == [uid_a, uid_a]
    assert summary.failures == []
    assert _rows(db) == [(uid_a, (
        f"2 exported instances share SOP Instance UID {uid_a} and were "
        f"written to one file: each successful write overwrote the previous "
        f"one, and the folder holds one file for all 2 of them."))]


def test_a_file_that_was_there_before_the_export_is_not_a_collision(tmp_path):
    """Re-exporting into a folder that holds an earlier export is allowed,
    and stays so. The second export finds, in place of one of its files, a
    hard link to the other: two planned paths, one inode, before it
    writes. Each write is a rename over the name, which gives the name a
    file of its own, so afterwards there are two files and no row.

    Kills: detection by an exclusive create (the second export would
    fail); the volume asked before the write."""
    a, b = f"{ROOT}.1.1.1", f"{ROOT}.1.1.2"
    db, out = tmp_path / "s.db", tmp_path / "out"
    with DicomSession(str(db)) as session:
        session.ingest(_source(tmp_path, a, b))
        session.export(str(out), use_compression=False, show_progress=False)
        first, second = _dcm_files(out)
        os.remove(second)
        os.link(first, second)
        assert os.stat(first).st_ino == os.stat(second).st_ino, "setup"
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
    assert sorted(summary.written_uids) == [a, b] and summary.failures == []
    assert _rows(db) == []
    first, second = _dcm_files(out)
    assert os.stat(first).st_ino != os.stat(second).st_ino
    assert sorted(str(pydicom.dcmread(f).SOPInstanceUID)
                  for f in (first, second)) == [a, b]


# --------------------------------------------------------------------------
# The disclosure that reads `written_uids` afterwards
# --------------------------------------------------------------------------

def _a_token():
    tag = ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ
    return {tag: types.SimpleNamespace(items=[object()])}


def test_a_replaced_instance_is_not_disclosed_as_delivered(tmp_path, monkeypatch):
    """`check_reversibility`'s disclosure counts an instance as delivered
    when a worker wrote it **or its planned file exists**. A replaced
    instance's planned name does exist: it is the survivor's file. Its
    token is not in the folder, so it is not counted. Here the replaced
    instance alone carries a token: no disclosure. And when the survivor
    carries it, the count is of one delivered instance, not two.

    Kills: the replaced task counted through `os.path.exists`."""
    monkeypatch.setattr(ReversibilityService, "holds_an_earlier_layout_token",
                        staticmethod(lambda instance: False))
    tasks, summary = _collide(tmp_path, "1.2.3.1", "1.2.3.1", "1.2.3.2")
    tasks[1].instance.sequences = _a_token()
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        replaced = session._report_export_collisions(tasks, summary)
        assert session._report_recoverable_identities(
            tasks, summary.written_uids, replaced) == 0
        tasks[0].instance.sequences = _a_token()
        assert session._report_recoverable_identities(
            tasks, summary.written_uids, replaced) == 1
    [(key, detail)] = _rows(db, "REVERSIBLE_EXPORT")
    assert key == "1.2.3.1"
    assert detail.startswith("1 of 1 exported instances carry")


def test_a_loser_sharing_the_survivors_uid_is_still_disclosed(tmp_path, monkeypatch):
    """Two instances of one UID in one file: the file's UID does not say
    which of them it holds, so a token on either is disclosed. Kills:
    every loser reported as replaced."""
    monkeypatch.setattr(ReversibilityService, "holds_an_earlier_layout_token",
                        staticmethod(lambda instance: False))
    tasks, summary = _collide(tmp_path, "1.2.3.1", "1.2.3.1", "1.2.3.1")
    tasks[1].instance.sequences = _a_token()
    with DicomSession(str(tmp_path / "s.db")) as session:
        replaced = session._report_export_collisions(tasks, summary)
        assert session._report_recoverable_identities(
            tasks, summary.written_uids, replaced) == 1


def test_an_unread_group_is_still_disclosed(tmp_path, monkeypatch):
    """When the one file could not be read, nothing says which instance it
    carries, so every member still counts as delivered for the
    disclosure: an over-claim costs a disclosure, an under-claim gets a
    re-identifiable file treated as safe. Kills: the whole unread group
    excluded."""
    monkeypatch.setattr(ReversibilityService, "holds_an_earlier_layout_token",
                        staticmethod(lambda instance: False))
    tasks, summary = _collide(tmp_path, None, "1.2.3.1", "1.2.3.2")
    tasks[1].instance.sequences = _a_token()
    with DicomSession(str(tmp_path / "s.db")) as session:
        replaced = session._report_export_collisions(tasks, summary)
        assert summary.written_uids == []
        assert session._report_recoverable_identities(
            tasks, summary.written_uids, replaced) == 1


def test_export_hands_the_replaced_instances_to_the_disclosure(tmp_path, monkeypatch):
    """The three tests above call the disclosure with the set in hand; this
    one goes through `export()`, which must pass it. Two instances of one
    locked patient, each carrying a token. Exported once as the volume has
    them (two files): `2 of 2 exported instances carry …`. Exported again
    with the volume answering "one file" for both paths: one instance
    stays, the other is replaced, and the row reads `1 of 1`. The replaced
    instance's planned file does exist (here, really), which is what the
    disclosure would count it by if `export()` did not say it was replaced.

    The same on every volume: the one function that asks the volume is
    replaced. Kills: `export()` calling the disclosure without the set the
    collision report returned."""
    a, b = f"{ROOT}.1.1.1", f"{ROOT}.1.1.2"
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.enable_reversible_anonymization(str(tmp_path / "test.key"))
        session.ingest(_source(tmp_path, a, b))
        session.lock_identities("PAT-1020")
        session.export(str(tmp_path / "apart"), use_compression=False,
                       show_progress=False)
        monkeypatch.setattr(session_module, "_delivered_file_identity",
                            lambda path: (1, 1))
        summary = session.export(str(tmp_path / "one"), use_compression=False,
                                 show_progress=False)
    assert len(_dcm_files(tmp_path / "one")) == 2, \
        "setup: the replaced instance's planned file exists"
    [survivor] = summary.written_uids
    details = [detail for _, detail in _rows(db, "REVERSIBLE_EXPORT")]
    assert [d.split(" exported instances carry")[0] for d in details] \
        == ["2 of 2", "1 of 1"]
    assert _rows(db, "REVERSIBLE_EXPORT")[1][0] == survivor


# --------------------------------------------------------------------------
# Every instance in a group whose file cannot be read back
# --------------------------------------------------------------------------

def test_an_export_of_one_unread_group_raises_and_says_what_is_true(
        tmp_path, monkeypatch):
    """Every planned instance is in one group, and the group's file cannot
    be read back as any of them: none is reported written, so `export()`
    raises `ExportError`, as any export that reports nothing written does,
    after its records: the `MULTIPLE` row, the `EXPORT` row `wrote 0 of 2
    planned instances`, `REVIEW_REQUIRED`. A file **is** in the folder, so
    the exception does not say "nothing reached disk", which it said of
    every case before #1020; it says none is reported as written.

    The same on every volume: the volume's answer and the read-back are
    both replaced. Kills: the group's members left in `written_uids` (no
    raise); the raise placed before the rows; the old sentence."""
    a, b = f"{ROOT}.1.1.1", f"{ROOT}.1.1.2"
    monkeypatch.setattr(session_module, "_delivered_file_identity",
                        lambda path: (1, 1))
    monkeypatch.setattr(session_module, "_sop_instance_uid_in",
                        lambda path: None)
    db, out = tmp_path / "s.db", tmp_path / "out"
    with DicomSession(str(db)) as session:
        session.ingest(_source(tmp_path, a, b))
        with pytest.raises(ExportError) as raised:
            session.export(str(out), use_compression=False, show_progress=False)
        grade = _grade(session, tmp_path)
    sentence = _folded_unread([a, b])
    assert raised.value.attempted == 2
    assert sorted(raised.value.failures) == [(a, sentence), (b, sentence)]
    message = str(raised.value)
    assert "wrote 0 of 2 planned instances; 2 failed and none is reported " \
           "as written. First: " in message
    assert "nothing reached disk" not in message
    assert _dcm_files(out), "setup: a file is in the folder"
    assert _rows(db) == [("MULTIPLE", sentence)]
    [(_, export_row)] = _rows(db, "EXPORT")
    assert "wrote 0 of 2 planned instances" in export_row
    assert grade == ["REVIEW_REQUIRED"]


# --------------------------------------------------------------------------
# The temporary name
# --------------------------------------------------------------------------

def _contexts(tmp_path, *uids):
    """The `ExportContext`s `write_tree()` would hand its workers."""
    session = DicomSession(str(tmp_path / "c.db"))
    session.ingest(_source(tmp_path, *uids))
    contexts = []
    for patient in session.store.patients:
        contexts += DicomExporter._generate_export_contexts(
            patient, patient.studies, str(tmp_path / "tree"))
    return session, contexts


@pytest.mark.parametrize("uids", [("SOPa", "SOPA"), ("SOPa", "SOPa")],
                         ids=["names_differing_in_case", "one_name_twice"])
def test_two_threads_writing_at_once_have_a_temp_name_each(
        tmp_path, monkeypatch, uids):
    """`write_tree()` runs its workers as threads on a free-threaded
    build, and wherever `ISOCENTER_FORCE_THREADS` says so. Threads share a
    pid, so `<path>.<pid>.tmp` was one name for two instances of one UID,
    and one file on a folding volume for `SOPa`/`SOPA`: measured on 3.14t,
    one `write_tree()` in four over the pair raised `[Errno 2] No such
    file or directory: '…SOPa.dcm.<pid>.tmp'`, the loser of the rename.

    Two threads are held inside `save_as` at the same moment, so the two
    names are both in use at once whatever the volume: they must differ
    even with letter case ignored. Kills: the thread's id dropped from the
    name."""
    session, contexts = _contexts(tmp_path, "SOPa", "SOPA")
    try:
        if uids[0] == uids[1]:
            contexts[1].instance.sop_instance_uid = contexts[0].instance.sop_instance_uid
            contexts[1].output_path = contexts[0].output_path
        assert len(contexts) == 2
        both_in = threading.Barrier(2, timeout=30)
        names = []
        real = pydicom.dataset.Dataset.save_as

        def save_as(self, filename, *args, **kwargs):
            names.append(str(filename))
            both_in.wait()
            return real(self, filename, *args, **kwargs)

        monkeypatch.setattr(pydicom.dataset.Dataset, "save_as", save_as)
        outcomes = []
        threads = [threading.Thread(
            target=lambda c=c: outcomes.append(io_handlers._export_instance_worker(c)))
            for c in contexts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert len(names) == 2 and all(n.endswith(".tmp") for n in names)
        assert len({n.casefold() for n in names}) == 2, names
        assert [o.ok for o in outcomes] == [True, True], \
            [o.error for o in outcomes]
        assert [f for f in _all_files(tmp_path / "tree")
                if f.endswith(".tmp")] == []
    finally:
        session.close()


def test_the_temp_name_ends_in_tmp_beside_its_file(tmp_path, monkeypatch):
    """The temporary file stays in the destination directory (the rename
    is atomic only there), begins with the file's own name, and ends
    `.tmp`: `<name>.<pid>.<thread id>.tmp`. Kills: the temp moved out of
    the folder; the suffix changed under whoever sweeps `*.tmp`."""
    session, contexts = _contexts(tmp_path, f"{ROOT}.1.1.1")
    try:
        [context] = contexts
        names = []
        real = pydicom.dataset.Dataset.save_as

        def save_as(self, filename, *args, **kwargs):
            names.append(str(filename))
            return real(self, filename, *args, **kwargs)

        monkeypatch.setattr(pydicom.dataset.Dataset, "save_as", save_as)
        outcome = io_handlers._export_instance_worker(context)
        assert outcome.ok, outcome.error
        assert names == [f"{context.output_path}.{os.getpid()}."
                         f"{threading.get_ident()}.tmp"]
        assert Path(names[0]).parent == Path(context.output_path).parent
    finally:
        session.close()

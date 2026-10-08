"""An exported file is named by its SOP Instance UID, and an instance whose
UID cannot name a file is refused, not written (GHSA-2rc2-r9r5-x7hm).

Both write doors named the file `f"{sop_instance_uid}.dcm"` and joined it
to the series folder. The folder names go through
`ConfigLoader.clean_filename`; the file name did not. Measured on
1.0.0rc15 (33d8811a), Python 3.12.14 and 3.14.7t: a source file whose
`(0008,0018)` is `../../../../../escaped`, exported to
`work/export_here/out` with no `anonymize()`, was written as
`work/escaped.dcm`, replaced a file already there, and was reported in
`written_uids` with no failure and no row. `DicomExporter.write_tree()`
did the same.

Every test here writes inside its own `tmp_path`: the export folder is
`tmp_path/work/export_here/out`, five directories deep with the three
the export adds, so under the defect the stray file lands in
`tmp_path/work`.

That no third door formats a name of its own is
`tests/test_api_coherence.py`'s
`test_the_exported_file_name_is_built_in_one_place`.

**Why this file imports what it does.** `isocenter.io_handlers` and
`isocenter.session` are named, so their probe rows are charged. The
helper and its exception are read off the module inside each test, so
that at a tree without them the door tests still run and fail on what
the export did.
"""
import os
import sqlite3
import types
from pathlib import Path

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter import io_handlers
from isocenter.io_handlers import DicomExporter, ExportError
from isocenter.session import DicomSession

ROOT = "1.2.826.0.1.3680043.10.9999.7"
GOOD = f"{ROOT}.1.1.1"
ESCAPING = "../../../../../escaped"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    """`write_tree()` in threads; `session.export()` always spawns, so the
    refusal also crosses a process boundary in every session test."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(path, sop_uid):
    """CT_small in one fixed patient, study and series, under `sop_uid`
    exactly as given: pydicom's own check of a UI value is switched off,
    as a file from anywhere else is not held to it."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PAT-7"
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds[0x00080018] = pydicom.DataElement(
        0x00080018, "UI", sop_uid, validation_mode=pydicom.config.IGNORE)
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with pydicom.config.disable_value_validation():
        ds.save_as(str(path))


def _layout(tmp_path):
    work = tmp_path / "work"
    return work, work / "export_here" / "out"


def _files(folder):
    """Every file under `folder`, dotfiles and temp files included."""
    return sorted(os.path.join(root, name)
                  for root, _, names in os.walk(folder) for name in names)


def _directories(folder):
    return sorted(os.path.join(root, name)
                  for root, names, _ in os.walk(folder) for name in names)


def _rows(db_path, action):
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type = ?",
            (action,)).fetchall()


def _held_uids(session):
    return sorted(i.sop_instance_uid for p in session.store.patients
                  for st in p.studies for se in st.series
                  for i in se.instances)


def _assert_only_the_sibling_is_on_disk(work, out):
    files = _files(work)
    assert len(files) == 1, files
    assert Path(files[0]).is_relative_to(out), files
    assert os.path.basename(files[0]) == f"{GOOD}.dcm"
    # No directory beside the export folder either.
    assert _directories(work)[0] == str(work / "export_here")
    assert all(Path(d).is_relative_to(work / "export_here")
               for d in _directories(work))


def _assert_the_detail_names_no_path(detail, tmp_path):
    for fragment in ("escaped.dcm", str(tmp_path), "Subject_", "export_here"):
        assert fragment not in detail, detail
    assert "SOP Instance UID (0008,0018)" in detail
    assert "\n" not in detail


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("uid", [
    "1.2.840.10008.5.1.4.1.1.2",
    "1.3.6.1.4.1.5962.1.1.1.1.1.20040119072730.12322",
    "2.25.166995460729138110036113970345358325126",
    "0",
    # Not conformant, and not refused: nothing in them is more than a
    # letter, a digit, `_`, `-` or `.`.
    "SOP1", "sop-1_a", "1..2", "-x", "é1",
])
def test_a_uid_of_safe_characters_names_its_file_unchanged(uid):
    """Byte for byte what both doors wrote before: the UID and `.dcm`."""
    instance = types.SimpleNamespace(sop_instance_uid=uid)
    assert io_handlers.export_file_name(instance) == uid + ".dcm"


@pytest.mark.parametrize("uid", [
    ESCAPING,
    "/tmp/escaped",
    "a/b",
    "..\\..\\escaped",
    "a\\b",
    "1.2\x003",
    "1.2 3",
    " 1.2.3",
    "1.2.3 ",
    "1.2\n3",
    "1.2\t3",
    "1.2:3",
    "a*b",
    "a|b",
    " ",
    # Written inside the folder before (`...dcm`, `.hidden.dcm`): refused
    # as dotfiles, which a listing of the export does not show.
    "..",
    ".",
    ".hidden",
])
def test_a_uid_a_file_name_cannot_carry_is_refused(uid):
    instance = types.SimpleNamespace(sop_instance_uid=uid)
    with pytest.raises(io_handlers.FileNameRefused) as refused:
        io_handlers.export_file_name(instance)
    # A `ValueError`, and its text never repeats the UID: the row names
    # the instance once, and a second copy inside the reason would read
    # as the path of a file. (`" "` and `"."` are in any sentence.)
    assert isinstance(refused.value, ValueError)
    assert uid in (" ", ".") or uid not in str(refused.value)
    assert "SOP Instance UID (0008,0018)" in str(refused.value)
    assert "|" not in str(refused.value)


@pytest.mark.parametrize("uid, name", [("", ".dcm"), (None, "None.dcm")])
def test_an_absent_uid_is_not_this_rules_to_refuse(uid, name):
    """An empty UID fails at the write, keyed `UNKNOWN`
    (`test_written_uids_names_only_uids.py`), and a `None` one at the save
    before it. Both keep that refusal and its words."""
    instance = types.SimpleNamespace(sop_instance_uid=uid)
    assert io_handlers.export_file_name(instance) == name


# --------------------------------------------------------------------------
# `session.export()`
# --------------------------------------------------------------------------

def test_export_refuses_the_instance_and_writes_the_rest(tmp_path):
    """No `anonymize()`: the escaping instance is a failure with one
    `ERROR` row, its sibling is written under its own name, nothing is
    written outside the folder, and a file already at the path the name
    reached is left as it was."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", ESCAPING)
    _write(tmp_path / "in" / "b.dcm", GOOD)
    work.mkdir()
    bystander = work / "escaped.dcm"
    bystander.write_bytes(b"the user's own file")

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        assert _held_uids(session) == sorted([ESCAPING, GOOD])
        summary = session.export(str(out), show_progress=False)
        session.generate_report(str(tmp_path / "report.md"))

    assert bystander.read_bytes() == b"the user's own file"
    bystander.unlink()
    _assert_only_the_sibling_is_on_disk(work, out)

    assert summary.written_uids == [GOOD]
    assert [uid for uid, _ in summary.failures] == [ESCAPING]
    detail = summary.failures[0][1]
    _assert_the_detail_names_no_path(detail, tmp_path)
    # The run does not grade PASS over an instance it did not deliver.
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "REVIEW_REQUIRED" in report and detail in report

    assert _rows(tmp_path / "s.db", "ERROR") == [(ESCAPING, detail)]
    (_, export_row), = _rows(tmp_path / "s.db", "EXPORT")
    assert "wrote 1 of 2 planned instances" in export_row


def test_export_refuses_it_after_a_pass_that_keeps_the_uid(tmp_path):
    """`audit()` and `anonymize()` under a `KEEP` rule on `(0008,0018)`:
    the pass leaves the UID, and the export refuses the instance."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", ESCAPING)
    _write(tmp_path / "in" / "b.dcm", GOOD)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.configuration.set_phi_tag("0008,0018", "KEEP")
        session.anonymize(session.audit())
        assert _held_uids(session) == sorted([ESCAPING, GOOD])
        summary = session.export(str(out), show_progress=False)

    _assert_only_the_sibling_is_on_disk(work, out)
    assert summary.written_uids == [GOOD]
    assert [uid for uid, _ in summary.failures] == [ESCAPING]
    assert len(_rows(tmp_path / "s.db", "ERROR")) == 1


def test_an_export_of_nothing_but_refused_instances_raises(tmp_path):
    """One instance, refused: `ExportError`, as for any export that
    planned files and delivered none, and neither the file nor the
    directories its name asks for exist."""
    work, out = _layout(tmp_path)
    reaching = "../../../../../newdir/sub/escaped"
    _write(tmp_path / "in" / "a.dcm", reaching)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        with pytest.raises(ExportError) as error:
            session.export(str(out), show_progress=False)

    assert error.value.attempted == 1
    assert [uid for uid, _ in error.value.failures] == [reaching]
    assert _files(work) == []
    assert not (work / "newdir").exists()
    assert _rows(tmp_path / "s.db", "ERROR") == error.value.failures
    (_, export_row), = _rows(tmp_path / "s.db", "EXPORT")
    assert "wrote 0 of 1 planned instances" in export_row


def test_an_absolute_path_in_the_uid_is_refused(tmp_path):
    """`os.path.join` drops everything before an absolute component, so
    such a UID was written at the path it spells (here one inside
    `tmp_path`). It begins with `/`, not `.`: the rule's first clause is
    the one that refuses it."""
    work, out = _layout(tmp_path)
    target = tmp_path / "elsewhere" / "escaped"
    _write(tmp_path / "in" / "a.dcm", str(target))
    _write(tmp_path / "in" / "b.dcm", GOOD)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        summary = session.export(str(out), show_progress=False)

    assert not (tmp_path / "elsewhere").exists()
    _assert_only_the_sibling_is_on_disk(work, out)
    assert summary.written_uids == [GOOD]
    assert [uid for uid, _ in summary.failures] == [str(target)]


def test_a_uid_the_pass_replaces_is_exported_as_before(tmp_path):
    """The default configuration replaces `(0008,0018)`, so the same
    source file is written, inside the folder, under its `2.25.` UID."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", ESCAPING)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        (replaced,) = _held_uids(session)
        summary = session.export(str(out), show_progress=False)

    assert replaced.startswith("2.25.")
    assert summary.written_uids == [replaced] and summary.failures == []
    (written,) = _files(work)
    assert Path(written).is_relative_to(out)
    assert os.path.basename(written) == f"{replaced}.dcm"


# --------------------------------------------------------------------------
# `DicomExporter.write_tree()`
# --------------------------------------------------------------------------

def test_write_tree_refuses_the_instance_and_writes_the_rest(tmp_path):
    """The serializer raises for any instance it could not write, after
    the others: the sibling is on disk, and nothing is outside the
    folder."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", ESCAPING)
    _write(tmp_path / "in" / "b.dcm", GOOD)
    work.mkdir()
    bystander = work / "escaped.dcm"
    bystander.write_bytes(b"the user's own file")

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        (patient,) = session.store.patients
        with pytest.raises(RuntimeError) as error:
            DicomExporter.write_tree(patient, str(out), show_progress=False)

    assert "Export incomplete. 1 failed." in str(error.value)
    _assert_the_detail_names_no_path(str(error.value), tmp_path)
    assert bystander.read_bytes() == b"the user's own file"
    bystander.unlink()
    _assert_only_the_sibling_is_on_disk(work, out)


def test_a_uid_assigned_by_the_caller_is_refused_the_same_way(tmp_path):
    """Not only a source file's: a UID the caller assigns reaches the same
    name, and a backslash, which ingest never holds in this key (#747),
    arrives this way. The UID does not begin with a dot, so only the
    rule's first clause can refuse it at a door: `../` is refused by
    either."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", f"{ROOT}.1.1.2")
    _write(tmp_path / "in" / "b.dcm", GOOD)
    assigned = "sub\\..\\escaped"

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        (target,) = [i for p in session.store.patients for st in p.studies
                     for se in st.series for i in se.instances
                     if i.sop_instance_uid != GOOD]
        target.sop_instance_uid = assigned
        summary = session.export(str(out), show_progress=False)

    _assert_only_the_sibling_is_on_disk(work, out)
    assert summary.written_uids == [GOOD]
    assert [uid for uid, _ in summary.failures] == [assigned]

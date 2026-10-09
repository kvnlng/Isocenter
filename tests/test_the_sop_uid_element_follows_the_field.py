"""Assigning `instance.sop_instance_uid` also sets `(0008,0018)`: the
element follows the field (#936, owner ruling Q1 A, 2026-10-08).

Measured on `main` at fd359eb3, on 3.12.14 and 3.14.7t. The export names a
file by the field and fills it from `attributes`, and pydicom's writer
then sets Media Storage SOP Instance UID from the dataset's own
`(0008,0018)`. `isocenter.entities.Instance.__setattr__` moved the field
and the revision and left the element, so after one assignment:

- `instance.sop_instance_uid = '1.2.3.4'` exported `1.2.3.4.dcm` carrying
  the **earlier** UID in `(0008,0018)` and in the file meta: a file whose
  name and content disagree, with no row.
- `instance.sop_instance_uid = ''` exported the dotfile `.dcm`, carrying
  the earlier UID, and `written_uids` held `''`.
- a save and a reopen gave the field and the element apart, as they were.

Every library writer that moves the UID sets both (`_take_sop_uid`, the
redaction parent, the redaction withdrawal). Only a caller's own
assignment left them apart. Now the assignment writes the element first,
then the field, then the revision, once. An emptied UID therefore reaches
the write with no `(0008,0018)`, and fails there as a hand-built UID-less
instance always has (#613): keyed `UNKNOWN`, one `ERROR` row, no file. No
new exception, and `''` is still not refused by the save (#721's control).
"""
import copy
import os
import pickle
import sqlite3

import pydicom
import pytest

from isocenter import Session
from isocenter import entities
from isocenter import services

from support.ct_small_files import study_uid, write_ct

ELEMENT = "0008,0018"
FIRST = f"{study_uid(936)}.1.1"
OTHER = f"{study_uid(937)}.1.1"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path):
    """Two CTs of one patient, ingested and saved."""
    write_ct(tmp_path / "in" / "a.dcm", "PID-936", 936)
    write_ct(tmp_path / "in" / "b.dcm", "PID-936", 937)
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    session.save(sync=True)
    return session


def _instance(session, uid=FIRST):
    [instance] = [i for p in session.store.patients for st in p.studies
                  for se in st.series for i in se.instances
                  if i.sop_instance_uid == uid]
    return instance


def _files(folder):
    """Every file under `folder`, dotfiles included: the defect's file was
    `.dcm`, which `glob` skips."""
    return sorted(os.path.join(root, name)
                  for root, _, names in os.walk(folder) for name in names)


def _rows(session, *kinds):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        rows = conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log").fetchall()
    return [row for row in rows if row[0] in kinds]


# ---------------------------------------------------------------------------
# The assignment itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1.2.3.4", "", " ", None],
                         ids=["a_uid", "empty", "blank", "none"])
def test_the_element_holds_the_assigned_value_at_once(tmp_path, value):
    """And the revision moves once: the element is written directly, so
    one assignment is one edit."""
    with _session(tmp_path) as session:
        instance = _instance(session)
        assert instance.attributes[ELEMENT] == FIRST
        before = instance._revision
        instance.sop_instance_uid = value
        assert instance.sop_instance_uid == value
        assert instance.attributes[ELEMENT] == value
        assert ELEMENT in instance.attributes
        assert instance._revision == before + 1
        assert instance.has_unsaved_changes


def test_assigning_the_value_already_held_changes_nothing(tmp_path):
    """Control, green on main. Not an edit, so an element written around
    the entity is not brought back by it: the standing rule that a write
    around the entity is not seen."""
    with _session(tmp_path) as session:
        instance = _instance(session)
        instance.attributes[ELEMENT] = "9.9.9"
        before = instance._revision
        instance.sop_instance_uid = FIRST
        assert instance._revision == before
        assert instance.attributes[ELEMENT] == "9.9.9"


def test_a_new_uid_is_written_over_an_element_written_around_the_entity(tmp_path):
    """The element follows whatever it held before, not only when it
    equalled the old field. A hook that wrote the element "only when it
    equals the old field" (the condition the comment in `__setattr__`
    forbids) leaves `9.9.9` here, and the export would again write a file
    named by one UID and carrying another."""
    with _session(tmp_path) as session:
        instance = _instance(session)
        instance.attributes[ELEMENT] = "9.9.9"
        instance.sop_instance_uid = "1.2.3.4"
        assert instance.attributes[ELEMENT] == "1.2.3.4"


def test_the_assignment_records_no_source_uid(tmp_path):
    """Control, green on main: `SOURCE_SOP_UID_ATTR` is `_take_sop_uid`'s
    to write, for a UID the library moved. A caller's assignment is not
    that (`docs/api/stability.md`)."""
    with _session(tmp_path) as session:
        instance = _instance(session)
        instance.sop_instance_uid = "1.2.3.4"
        assert entities.SOURCE_SOP_UID_ATTR not in instance.attributes


def test_construction_pickle_and_copy_do_not_come_through_the_hook(tmp_path):
    """The first assignment is the dataclass `__init__`, into an unset
    slot with no `attributes` yet; pickle and deepcopy restore a slots
    dataclass with `object.__setattr__`. A copy of an instance whose
    element was written around the entity is that instance, not a
    corrected one: the scan's clone and a worker's copy must not diverge
    from the graph."""
    built = entities.Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.2", 1)
    assert built.attributes[ELEMENT] == "1.2.3"
    built.attributes[ELEMENT] = "9.9.9"
    for clone in (pickle.loads(pickle.dumps(built)), copy.deepcopy(built)):
        assert clone.sop_instance_uid == "1.2.3"
        assert clone.attributes[ELEMENT] == "9.9.9"
        assert clone._revision == built._revision


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_a_reopened_store_gives_the_field_and_the_element_together(tmp_path):
    """On main the row was keyed on the field and its `attributes_json`
    held the earlier UID."""
    with _session(tmp_path) as session:
        _instance(session).sop_instance_uid = "1.2.3.4"
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as reopened:
        instance = _instance(reopened, "1.2.3.4")
        assert instance.attributes[ELEMENT] == "1.2.3.4"


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------

def test_an_assigned_uid_is_exported_under_itself(tmp_path):
    """On main `1.2.3.4.dcm` carried `FIRST` in both elements."""
    with _session(tmp_path) as session:
        _instance(session).sop_instance_uid = "1.2.3.4"
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
        assert sorted(summary.written_uids) == sorted(["1.2.3.4", OTHER])
        assert summary.failures == []
        [path] = [f for f in _files(tmp_path / "out")
                  if os.path.basename(f) == "1.2.3.4.dcm"]
        ds = pydicom.dcmread(path)
        assert ds.SOPInstanceUID == "1.2.3.4"
        assert ds.file_meta.MediaStorageSOPInstanceUID == "1.2.3.4"
        assert _rows(session, "ERROR", "WARNING") == []


def test_an_emptied_uid_fails_at_the_write_and_no_dotfile_is_written(tmp_path):
    """On main: the dotfile `.dcm` carrying `FIRST`, `''` in
    `written_uids`, no failure, no row. Now what a hand-built UID-less
    instance does (#613)."""
    with _session(tmp_path) as session:
        _instance(session).sop_instance_uid = ""
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
        assert summary.written_uids == [OTHER]
        [(key, reason)] = summary.failures
        assert key == "UNKNOWN"
        assert reason == (
            "Export failed for an instance with no SOP Instance UID: "
            "ValueError: Validation Errors: ['[Type 1 Error] Missing "
            "0008,0018 in Common']")
        assert [os.path.basename(f) for f in _files(tmp_path / "out")] == \
            [f"{OTHER}.dcm"]
        [(kind, uid, details)] = _rows(session, "ERROR")
        assert (kind, uid) == ("ERROR", "UNKNOWN")
        assert details == reason
        assert FIRST not in details
        report = tmp_path / "report.md"
        session.generate_report(str(report))
        status = next(line for line in report.read_text().splitlines()
                      if "Validation Status" in line)
        assert "**REVIEW_REQUIRED**" in status


# ---------------------------------------------------------------------------
# The redaction withdrawal, which assigns the field and then restores the
# element from what it captured
# ---------------------------------------------------------------------------

def test_a_withdrawn_attestation_restores_the_element_it_captured(tmp_path):
    """`_withdraw_attestation` assigns the field, which now writes the
    element too, and then restores `attributes` from what
    `_capture_attestation` saw. When the two were apart at the capture (an
    element written around the entity), the captured element is what is
    left: the instance as it was found."""
    with _session(tmp_path) as session:
        instance = _instance(session)
        instance.attributes[ELEMENT] = "9.9.9"
        captured = services._capture_attestation(instance)
        instance.regenerate_uid("2.25.936")
        assert instance.attributes[ELEMENT] == "2.25.936"
        services._withdraw_attestation(instance, captured)
        assert instance.sop_instance_uid == FIRST
        assert instance.attributes[ELEMENT] == "9.9.9"
        assert entities.SOURCE_SOP_UID_ATTR not in instance.attributes

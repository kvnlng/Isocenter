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
#: As `bytes`, the name was `b'../…/escaped'.dcm`: its first component,
#: `b'..`, is a directory the write created inside the series folder, so
#: this needs seven `../` to leave the export folder where the text needs
#: five. Under the defect the stray file is `work/escaped'.dcm`.
ESCAPING_BYTES = b"../../../../../../../escaped"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    """`write_tree()` in threads; `session.export()` always spawns, so the
    refusal also crosses a process boundary in every session test."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(path, sop_uid, vr="UI"):
    """CT_small in one fixed patient, study and series, under `sop_uid`
    exactly as given: pydicom's own check of a UI value is switched off,
    as a file from anywhere else is not held to it. `vr` is the VR the
    file states for `(0008,0018)`; under `OB` pydicom reads the value as
    `bytes`, and since #1022 ingest refuses the file."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PAT-7"
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds[0x00080018] = pydicom.DataElement(
        0x00080018, vr, sop_uid, validation_mode=pydicom.config.IGNORE)
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


@pytest.mark.parametrize("uid", [
    ESCAPING_BYTES,
    [ESCAPING],
    (ESCAPING,),
    b"1.2.3",
    None,
    7,
])
def test_a_uid_that_is_not_text_is_refused(uid):
    """The name is `f"{uid}.dcm"`, and the text of a `bytes` or a list is
    its repr, separators and all (`b'../x'.dcm`). So the type is judged
    before the text, and every UID that is not a `str` is refused: one
    whose repr would be harmless (`b"1.2.3"`, `7`, `None`) too, because
    `b'1.2.3'.dcm` and `None.dcm` are not the instance's UID either. The
    text names the type, never the value."""
    instance = types.SimpleNamespace(sop_instance_uid=uid)
    with pytest.raises(io_handlers.FileNameRefused) as refused:
        io_handlers.export_file_name(instance)
    text = str(refused.value)
    assert "SOP Instance UID (0008,0018)" in text
    assert type(uid).__name__ in text
    assert "escaped" not in text and "/" not in text
    # A pass is not offered as the repair: the `ingest()` of such a file
    # raised (#721) and saved nothing.
    assert "anonymize" not in text


def test_an_empty_uid_is_refused():
    """`""` named the hidden file `.dcm` (owner's ruling, 2026-10-08): the
    dot clause's own case, with nothing after the dot. No source file
    arrives so (ingest refuses a file without the UID), so the text does
    not offer a pass as the repair."""
    instance = types.SimpleNamespace(sop_instance_uid="")
    with pytest.raises(io_handlers.FileNameRefused) as refused:
        io_handlers.export_file_name(instance)
    text = str(refused.value)
    assert "SOP Instance UID (0008,0018)" in text and "is empty" in text
    assert "anonymize" not in text


def test_a_str_subclass_is_judged_as_text():
    """`pydicom.uid.UID` is a `str`: what ingest holds for a UI element.
    The type clause is `isinstance`, so it reaches the text clauses."""
    held = pydicom.uid.UID(ESCAPING, validation_mode=pydicom.config.IGNORE)
    assert type(held) is not str
    with pytest.raises(io_handlers.FileNameRefused) as refused:
        io_handlers.export_file_name(types.SimpleNamespace(sop_instance_uid=held))
    assert "holds a character" in str(refused.value)
    good = pydicom.uid.UID(GOOD)
    assert io_handlers.export_file_name(
        types.SimpleNamespace(sop_instance_uid=good)) == GOOD + ".dcm"


def test_a_refused_instance_has_no_path_at_all(tmp_path):
    """`export_output_path` answers `""` for a refused instance, never
    the series folder: the parent asks `os.path.exists` of every planned
    path, and a folder a sibling created would answer True for each
    refused instance of the series."""
    folder = tmp_path / "Series_1"
    folder.mkdir()
    instance = types.SimpleNamespace(sop_instance_uid=ESCAPING)
    path, refusal = io_handlers.export_output_path(str(folder), instance)
    assert path == "" and isinstance(refusal, io_handlers.FileNameRefused)
    clean = types.SimpleNamespace(sop_instance_uid=GOOD)
    assert io_handlers.export_output_path(str(folder), clean) == (
        str(folder / f"{GOOD}.dcm"), None)


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


# --------------------------------------------------------------------------
# A UID that is not text, at both doors
# --------------------------------------------------------------------------
#
# A source file reached this until #1022: `(0008,0018)` stated `OB` or
# `OW` was held as `bytes`. `session.export()` never met one after #721,
# because its leading save refuses a UID that is not a `str`;
# `write_tree()` has no save. Measured at 215cc152 on 3.12.14 and 3.14.7t,
# each `write_tree` case below returned normally with the file outside
# the export folder. Since #1022 ingest refuses such a file by the VR it
# states, so the first two tests pin that no source file arrives here,
# and the third pins the serializer's refusal for a UID assigned by hand.

def _instances(store):
    return [i for p in store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _assert_write_tree_refuses_one(store, work, out, tmp_path):
    (patient,) = store.patients
    with pytest.raises(RuntimeError) as error:
        DicomExporter.write_tree(patient, str(out), show_progress=False)
    assert "Export incomplete. 1 failed." in str(error.value)
    assert "SOP Instance UID (0008,0018) cannot name a file" in str(error.value)
    assert str(tmp_path) not in str(error.value)
    _assert_only_the_sibling_is_on_disk(work, out)


def _not_text(vr):
    return (f"ValueError: SOP Instance UID (0008,0018) is written as {vr}, "
            f"not as text; the file is linked by it, and no text is chosen "
            f"for it.")


def test_a_source_uid_stated_ob_no_longer_reaches_the_serializer(tmp_path):
    """The session route. Until #1022 `ingest()` raised #721's `ValueError`
    and left the instance in `session.store` holding `bytes`, and at
    215cc152 a caller who went on to the serializer got a file outside
    the folder and no error. Since #1022 the worker refuses that file by
    the VR it states, so no source file puts `bytes` in this key: the
    store holds the sibling alone and the serializer writes it. The
    serializer's own refusal of `bytes` is pinned below by a UID the
    caller assigns, which is now the only way one arrives."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", ESCAPING_BYTES, vr="OB")
    _write(tmp_path / "in" / "b.dcm", GOOD)

    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert summary.ingested == 1
        assert summary.failures == [(str(tmp_path / "in" / "a.dcm"), _not_text("OB"))]
        assert [i.sop_instance_uid for i in _instances(session.store)] == [GOOD]
        (patient,) = session.store.patients
        DicomExporter.write_tree(patient, str(out), show_progress=False)
    _assert_only_the_sibling_is_on_disk(work, out)


@pytest.mark.parametrize("vr", ["OB", "OW"])
def test_it_does_not_reach_the_serializer_with_no_session_at_all(tmp_path, vr):
    """The importer into a bare store, then the serializer: no save, so
    until #1022 no exception anywhere before the write, and the file
    landed outside the folder. This is what the fixture generators in
    `scripts/` do. The importer's worker now refuses the file too."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", ESCAPING_BYTES, vr=vr)
    _write(tmp_path / "in" / "b.dcm", GOOD)

    store = io_handlers.DicomStore()
    summary = io_handlers.DicomImporter.import_files([str(tmp_path / "in")], store)
    assert summary.failures == [(str(tmp_path / "in" / "a.dcm"), _not_text(vr))]
    assert [i.sop_instance_uid for i in _instances(store)] == [GOOD]
    (patient,) = store.patients
    DicomExporter.write_tree(patient, str(out), show_progress=False)
    _assert_only_the_sibling_is_on_disk(work, out)


@pytest.mark.parametrize("assigned", [
    ESCAPING_BYTES, [ESCAPING_BYTES.decode()]], ids=["bytes", "list"])
def test_write_tree_refuses_a_uid_the_caller_assigned_as_not_text(
        tmp_path, assigned):
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", f"{ROOT}.1.1.2")
    _write(tmp_path / "in" / "b.dcm", GOOD)

    store = io_handlers.DicomStore()
    io_handlers.DicomImporter.import_files([str(tmp_path / "in")], store)
    (target,) = [i for i in _instances(store) if i.sop_instance_uid != GOOD]
    target.sop_instance_uid = assigned
    _assert_write_tree_refuses_one(store, work, out, tmp_path)


def test_export_refuses_a_uid_that_is_not_text_when_the_save_did_not(tmp_path):
    """`session.export()`'s leading save refuses a UID that is not a `str`
    (#721), so this door is closed twice. The save sees an instance only
    when it has an unsaved change, though: a `bytes` UID placed around
    the tracked setter is not one, and the export then wrote it outside
    the folder and listed the `bytes` in `written_uids`. Contrived, and
    here so the door's own refusal is pinned without the save's help."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", f"{ROOT}.1.1.2")
    _write(tmp_path / "in" / "b.dcm", GOOD)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        (target,) = [i for i in _instances(session.store)
                     if i.sop_instance_uid != GOOD]
        object.__setattr__(target, "sop_instance_uid", ESCAPING_BYTES)
        summary = session.export(str(out), show_progress=False)

    _assert_only_the_sibling_is_on_disk(work, out)
    assert summary.written_uids == [GOOD]
    (_, detail), = summary.failures
    assert "it is held as bytes" in detail


# --------------------------------------------------------------------------
# An empty UID, at both doors
# --------------------------------------------------------------------------
#
# Measured at 215cc152 and on 1.0.0rc15: an ingested instance later
# assigned `""` was written as the hidden file `.dcm` in its series
# folder, and `export()` listed `''` in `written_uids`. (#613's hand-built
# instance, which has no source file, failed at the write instead.)

def test_export_refuses_an_empty_uid(tmp_path):
    """Refused like the others: a failure and one `ERROR` row, both keyed
    `UNKNOWN`, as every export line keys an instance with no UID."""
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", f"{ROOT}.1.1.2")
    _write(tmp_path / "in" / "b.dcm", GOOD)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        (target,) = [i for i in _instances(session.store)
                     if i.sop_instance_uid != GOOD]
        target.sop_instance_uid = ""
        summary = session.export(str(out), show_progress=False)

    _assert_only_the_sibling_is_on_disk(work, out)
    assert summary.written_uids == [GOOD]
    (key, detail), = summary.failures
    assert key == "UNKNOWN"
    assert detail.startswith(
        "Export failed for an instance with no SOP Instance UID: ")
    assert "cannot name a file: it is empty" in detail
    assert _rows(tmp_path / "s.db", "ERROR") == [("UNKNOWN", detail)]
    (_, export_row), = _rows(tmp_path / "s.db", "EXPORT")
    assert "wrote 1 of 2 planned instances" in export_row


def test_write_tree_refuses_an_empty_uid(tmp_path):
    work, out = _layout(tmp_path)
    _write(tmp_path / "in" / "a.dcm", f"{ROOT}.1.1.2")
    _write(tmp_path / "in" / "b.dcm", GOOD)

    store = io_handlers.DicomStore()
    io_handlers.DicomImporter.import_files([str(tmp_path / "in")], store)
    (target,) = [i for i in _instances(store) if i.sop_instance_uid != GOOD]
    target.sop_instance_uid = ""
    _assert_write_tree_refuses_one(store, work, out, tmp_path)


# --------------------------------------------------------------------------
# Two refused instances in one series
# --------------------------------------------------------------------------

def test_two_refused_instances_of_one_series_are_two_failures(tmp_path):
    """Each has its own `ERROR` row and nothing else is said of them. A
    refused instance's planned path is `""`; were it the series folder,
    which the sibling's write creates, the two would share a path that
    exists and draw the row for instances written to one file."""
    work, out = _layout(tmp_path)
    second = "../../../../../escaped-too"
    _write(tmp_path / "in" / "a.dcm", ESCAPING)
    _write(tmp_path / "in" / "b.dcm", GOOD)
    _write(tmp_path / "in" / "c.dcm", second)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        summary = session.export(str(out), show_progress=False)

    _assert_only_the_sibling_is_on_disk(work, out)
    assert summary.written_uids == [GOOD]
    assert sorted(uid for uid, _ in summary.failures) == sorted(
        [ESCAPING, second])
    assert sorted(uid for uid, _ in _rows(tmp_path / "s.db", "ERROR")) == sorted(
        [ESCAPING, second])
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        said = conn.execute(
            "SELECT action_type, details FROM audit_log WHERE action_type "
            "NOT IN ('ERROR', 'EXPORT', 'INGEST')").fetchall()
    assert not [row for row in said if "one file" in (row[1] or "")], said
    assert not [row for row in said if row[0] == "WARNING"], said

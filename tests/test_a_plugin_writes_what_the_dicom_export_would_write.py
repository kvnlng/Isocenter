"""A plugin exporter gets the dataset the DICOM export would write (#783).

Before #783 a registered exporter was called as `export(session, folder,
**options)` and read the session's graph. Measured at a946a1e6 on 3.12.14
and 3.14.7t, such a plugin wrote pixels under a configured zone when
`redact()` had not run, kept the Icon Image Sequence after `redact()`, could
read the source SOP Instance UID kept in `_ISOCENTER_SOURCE_SOP_UID`, and
wrote no de-identification markers; it received `check_burned_in`,
`patient_ids` and unknown options raw; and no `EXPORT` row was written.

What the owner ruled (2026-11-20):

- Q4/Q6: a plugin's `write(dataset, item, folder, **options)` gets the
  pydicom Dataset the DICOM export would write, before compression, built
  by the same worker code, and never the session.
- Q5: `write` runs once per instance, in the export worker; the class must
  live in an importable module (`tests/support/plugin_exporters.py`).
- Q7: such an export can grade PASS; the "not attested" row is gone, and the
  `EXPORT` row names the class.
- Q8/Q9: `register()` refuses the 1.0 shape (`TypeError`, naming `write`)
  and the names `dicom` and `wfdb` (`ValueError`).

This file names `isocenter.session`, `isocenter.io_handlers` and
`isocenter.exporters`, so their probe rows are charged with it.
"""
import json
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import exporters
from isocenter.io_handlers import ExportError
from isocenter.session import DicomSession

from support import plugin_exporters as plugins

ROOT = "1.2.826.0.1.3680043.10.9999.7831"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


@pytest.fixture(autouse=True)
def _registry():
    before = dict(exporters._REGISTRY)
    for name, cls in (("recorder", plugins.Recorder),
                      ("raising", plugins.Raising),
                      ("declining", plugins.Declining),
                      ("onefile", plugins.OneFile),
                      ("aggregate", plugins.Aggregate),
                      ("writethenraise", plugins.WriteThenRaise)):
        exporters.register(name, cls)
    yield
    exporters._REGISTRY.clear()
    exporters._REGISTRY.update(before)


def _icon():
    icon = Dataset()
    icon.Rows, icon.Columns = 8, 8
    icon.SamplesPerPixel = 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit = 7
    icon.PixelRepresentation = 0
    icon.PixelData = bytes(range(64))
    return icon


def _write(path, number, patient_id="PAT-7831", icon=False):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = patient_id
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.SOPInstanceUID = f"{ROOT}.1.1.{number}"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.InstanceNumber = number
    frame = np.full((ds.Rows, ds.Columns), 200, dtype=np.int16)
    ds.PixelData = frame.tobytes()
    if icon:
        ds.IconImageSequence = Sequence([_icon()])
        ref = Dataset()
        ref.ReferencedSOPClassUID = ds.SOPClassUID
        ref.ReferencedSOPInstanceUID = f"{ROOT}.9.9"
        ref.IconImageSequence = Sequence([_icon()])
        ds.ReferencedImageSequence = Sequence([ref])
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    ds.save_as(str(path))


def _session(tmp_path, count=1, **kwargs):
    src = tmp_path / "in"
    for number in range(1, count + 1):
        _write(src / f"{number}.dcm", number, **kwargs)
    session = DicomSession(str(tmp_path / "s.db"))
    session.ingest(str(src))
    return session


def _records(out):
    found = []
    for root, _dirs, names in os.walk(str(out)):
        for name in sorted(names):
            if name.endswith(".json"):
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    found.append(json.load(f))
    return found


def _rows(db, action):
    with sqlite3.connect(str(db)) as conn:
        return [details for (details,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = ? "
            "ORDER BY rowid", (action,))]


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    [line] = [ln for ln in path.read_text(encoding="utf-8").splitlines()
              if ln.startswith("| **Validation Status** |")]
    return line


def _zone_everything(session):
    session.configuration.add_rule("*", redaction_zones=[[0, 128, 0, 128]])


# --------------------------------------------------------------------------
# The dataset a plugin is handed
# --------------------------------------------------------------------------

def test_a_plugin_gets_pixels_with_the_configured_zones_applied(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        _zone_everything(session)
        session.export(str(out), format="recorder")
    [record] = _records(out)
    assert record["pixel_max"] == 0
    assert record["dataset_type"] == "FileDataset"


def test_a_plugin_gets_unredacted_pixels_when_no_zone_is_configured(tmp_path):
    """The control: the zero above is the zone's, not the export's."""
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.export(str(out), format="recorder")
    [record] = _records(out)
    assert record["pixel_max"] == 200


def test_a_plugin_gets_no_icon_after_redact(tmp_path):
    """Its own icon and a foreign one in Referenced Image Sequence."""
    out = tmp_path / "out"
    with _session(tmp_path, icon=True) as session:
        _zone_everything(session)
        session.redact()
        session.export(str(out), format="recorder")
    [record] = _records(out)
    assert record["icons"] == 0


def test_a_plugin_gets_its_icons_when_nothing_is_redacted(tmp_path):
    """The control for the test above."""
    out = tmp_path / "out"
    with _session(tmp_path, icon=True) as session:
        session.export(str(out), format="recorder")
    [record] = _records(out)
    assert record["icons"] == 2


def test_a_plugin_gets_no_bookkeeping_key_and_no_source_uid(tmp_path):
    """The source SOP Instance UID that `anonymize()` replaced stays in the
    graph, in `entities.SOURCE_SOP_UID_ATTR`. It is in no value of the
    dataset a plugin is handed, at any depth or in the file meta.

    The review of #1197 found the first version of this test could not
    fail: it compared the written UID with the source, which `anonymize()`
    alone makes differ, and read `isalnum()` over hex tags. This one goes
    red when `_write_through_plugin` adds the bookkeeping value back as a
    private element before `write()` (the reviewer's mutant)."""
    from isocenter.entities import SOURCE_SOP_UID_ATTR

    out = tmp_path / "out"
    source = f"{ROOT}.1.1.1"
    with _session(tmp_path) as session:
        session.audit()
        session.anonymize()
        [instance] = [i for p in session.store.patients for st in p.studies
                      for se in st.series for i in se.instances]
        # The premise: the graph still holds the source UID, so a dataset
        # built carelessly from it could carry it.
        assert instance.attributes.get(SOURCE_SOP_UID_ATTR) == source
        session.export(str(out), format="recorder")
    [record] = _records(out)
    assert record["texts"], "the recorder saw no values at all"
    assert not [text for text in record["texts"] if source in text]


def test_a_plugin_gets_the_de_identification_markers(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.audit()
        session.anonymize()
        session.export(str(out), format="recorder")
    [record] = _records(out)
    assert record["patient_identity_removed"] == "YES"
    assert record["deid_method"].startswith("isocenter/")


def test_a_plugin_gets_an_empty_id_for_a_synthetic_key(tmp_path):
    """#584: a file with no Patient ID is held under a key no output may
    carry. The stamp and the item both say `''`."""
    out = tmp_path / "out"
    with _session(tmp_path, patient_id="") as session:
        session.export(str(out), format="recorder")
    [record] = _records(out)
    assert record["patient_id"] == ""
    assert record["item_patient_id"] == ""
    assert record["item_folder_names"][0] == "Subject_UnknownPatient"


def test_a_plugin_is_never_handed_the_session(tmp_path):
    """`write` runs in an export worker process, with the dataset, an item
    and the folder: nothing that reaches the store."""
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.export(str(out), format="recorder", note="hello")
    [record] = _records(out)
    assert record["pid"] != os.getpid()
    assert record["folder"] == str(out)
    assert record["options"] == {"note": "hello"}


# --------------------------------------------------------------------------
# The session's gates, for a plugin
# --------------------------------------------------------------------------

def test_check_burned_in_withholds_for_a_plugin(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        summary = session.export(str(out), format="recorder",
                                 check_burned_in=True)
    assert summary.written_uids == []
    assert _records(out) == []
    withheld = [d for d in _rows(tmp_path / "s.db", "WARNING")
                if "withheld instance" in d]
    assert len(withheld) == 1 and withheld[0].startswith(
        f"recorder export to {out} withheld instance"), withheld


def test_a_bad_patient_ids_is_refused_for_a_plugin(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        with pytest.raises(TypeError):
            session.export(str(out), format="recorder",
                           patient_ids="PAT-7831")
    assert not out.exists()


def test_an_unknown_option_is_refused_before_a_plugin_runs(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        saves = []
        real = session.save
        session.save = lambda *a, **k: (saves.append(1), real(*a, **k))
        with pytest.raises(TypeError, match="'bogus'"):
            session.export(str(out), format="recorder", bogus=1)
        del session.save
    assert saves == []
    assert not out.exists()


def test_an_option_that_cannot_reach_a_worker_is_refused(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        with pytest.raises(TypeError, match="note"):
            session.export(str(out), format="recorder",
                           note=lambda: None)
    assert not out.exists()


def test_a_plugin_export_writes_its_export_row_naming_the_class(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path, count=2) as session:
        summary = session.export(str(out), format="recorder")
    assert summary.written_uids == [f"{ROOT}.1.1.1", f"{ROOT}.1.1.2"]
    assert _rows(tmp_path / "s.db", "EXPORT") == [
        f"recorder export to {out} by support.plugin_exporters.Recorder: "
        f"wrote 2 of 2 planned instances from 1 patients."]


def test_a_plugin_that_raises_fails_its_instance_with_one_error_row(
        tmp_path, monkeypatch):
    """Two instances, each reaching `write`, which raises: one `ERROR` row
    each, and `ExportError` counts both as attempted."""
    out = tmp_path / "out"
    with _session(tmp_path, count=2) as session:
        with pytest.raises(ExportError) as raised:
            session.export(str(out), format="raising")
    errors = _rows(tmp_path / "s.db", "ERROR")
    assert len(errors) == 2, errors
    assert all("RuntimeError: the plugin failed" in e for e in errors)
    assert raised.value.attempted == 2


def test_a_plugin_that_fails_every_instance_raises_export_error_last(
        tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        with pytest.raises(ExportError):
            session.export(str(out), format="raising")
    assert _rows(tmp_path / "s.db", "EXPORT") == [
        f"raising export to {out} by support.plugin_exporters.Raising: "
        f"wrote 0 of 1 planned instances from 1 patients."]


def test_a_uid_that_cannot_name_a_file_is_refused_before_write(tmp_path):
    """GHSA-2rc2-r9r5-x7hm: the refusal is raised in the worker before the
    plugin's `write` sees the dataset."""
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        [instance] = [i for p in session.store.patients for st in p.studies
                      for se in st.series for i in se.instances]
        instance.sop_instance_uid = ".refused"
        with pytest.raises(ExportError) as raised:
            session.export(str(out), format="recorder")
    assert _records(out) == []
    assert "FileNameRefused" in str(raised.value)


def test_a_plugin_that_writes_nothing_is_neither_written_nor_failed(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        summary = session.export(str(out), format="declining")
    assert summary.written_uids == [] and summary.failures == []
    assert _rows(tmp_path / "s.db", "EXPORT") == [
        f"declining export to {out} by support.plugin_exporters.Declining: "
        f"wrote 0 of 1 planned instances from 1 patients; 1 not written: "
        f"the exporter wrote nothing for them."]


def test_one_path_for_two_instances_is_a_collision(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path, count=2) as session:
        with pytest.raises(ExportError):
            session.export(str(out), format="onefile")
    errors = _rows(tmp_path / "s.db", "ERROR")
    assert len(errors) == 1, errors
    assert "resolves to one file" in errors[0]


def test_a_plugin_export_of_a_locked_store_writes_the_disclosure(tmp_path):
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization(str(tmp_path / "test.key"))
        session.lock_identities("PAT-7831")
        session.export(str(out), format="recorder")
    assert len(_rows(tmp_path / "s.db", "REVERSIBLE_EXPORT")) == 1


@pytest.mark.parametrize("fmt", ["aggregate", "writethenraise"])
def test_every_instance_handed_to_write_counts_for_the_disclosure(
        tmp_path, fmt):
    """Owner ruling Q-A (2026-11-21): an instance whose dataset `write()`
    was handed is delivered, whatever `write()` did. `aggregate` puts the
    token in one shared file and returns None; `writethenraise` writes a
    file and then raises. Before the ruling neither drew the disclosure,
    because it read only `written_uids` and the planned paths."""
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization(str(tmp_path / "test.key"))
        session.lock_identities("PAT-7831")
        try:
            session.export(str(out), format=fmt)
        except ExportError:
            assert fmt == "writethenraise"
    # The premise: the plugin was handed the token and kept it.
    kept = [json.loads(line) for name in os.listdir(str(out))
            for line in open(os.path.join(str(out), name), encoding="utf-8")]
    assert kept and all(entry["has_token"] for entry in kept)
    assert _rows(tmp_path / "s.db", "REVERSIBLE_EXPORT") == [
        "1 of 1 exported instances carry encrypted original identities "
        "(0400,0500). They are recoverable with the session key; treat the "
        "export as re-identifiable by any holder of it."]


def test_an_instance_refused_before_write_is_not_delivered(tmp_path):
    """The other side of Q-A: an instance the worker refused before
    `write()` was never handed, so it draws no disclosure."""
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization(str(tmp_path / "test.key"))
        session.lock_identities("PAT-7831")
        [instance] = [i for p in session.store.patients for st in p.studies
                      for se in st.series for i in se.instances]
        instance.sop_instance_uid = ".refused"
        with pytest.raises(ExportError):
            session.export(str(out), format="aggregate")
    assert _rows(tmp_path / "s.db", "REVERSIBLE_EXPORT") == []


def test_a_plugin_that_writes_nothing_prints_no_failure_line(
        tmp_path, capsys):
    """Review of #1197: "Export finished with failures: 0/1" was printed
    for an export with no failure, because the line was keyed on
    `written < planned`."""
    with _session(tmp_path) as session:
        summary = session.export(str(tmp_path / "out"), format="declining")
    assert summary.failures == []
    assert "finished with failures" not in capsys.readouterr().out


def test_a_plugin_that_fails_prints_the_failure_line(tmp_path, capsys):
    with _session(tmp_path) as session:
        with pytest.raises(ExportError):
            session.export(str(tmp_path / "out"), format="raising")
    assert "Export finished with failures: 0/1" in capsys.readouterr().out


def test_the_burned_in_text_policy_rows_name_the_plugin(tmp_path):
    """Review of #1197: the policy gate's withheld row said "DICOM export"
    for every format. It names the format, as the `check_burned_in` rows
    do."""
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.configuration.burned_in_text = {
            "action": "withhold", "scope": "all", "min_confidence": 80}
        summary = session.export(str(out), format="recorder")
    assert summary.written_uids == []
    withheld = [d for d in _rows(tmp_path / "s.db", "WARNING")
                if "withheld instance" in d]
    assert withheld == [
        f"recorder export to {out} withheld instance {ROOT}.1.1.1: its "
        f"pixels were not checked for burned-in text under this "
        f"configuration's burned_in_text policy; run redact()."]


def _ambiguous_source(tmp_path, name):
    """A waveform with no Waveform Bits Allocated: the export resolves its
    ambiguous VRs and writes one `WARNING` row about the source."""
    from test_an_unresolvable_ambiguous_vr_is_written import (
        _dataset, _save, _waveform)
    from pydicom.uid import ExplicitVRLittleEndian

    ds = _waveform(_dataset(), declare_bits=False, minmax=False,
                   padding=True)
    return _save(tmp_path, ds, ExplicitVRLittleEndian, name)


def test_a_plugin_export_writes_the_rows_about_the_source_data(tmp_path):
    """Owner ruling Q-B (2026-11-21): the rows that report on the source
    data are written for a plugin export too, in the DICOM export's
    words. Before, they were written by the DICOM serializer only."""
    rows = {}
    for fmt in ("dicom", "recorder"):
        db = tmp_path / f"{fmt}.db"
        with DicomSession(str(db)) as session:
            session.ingest(_ambiguous_source(tmp_path, f"src-{fmt}"))
            session.export(str(tmp_path / f"out-{fmt}"), format=fmt)
        rows[fmt] = [d for d in _rows(db, "WARNING")
                     if d.startswith("Ambiguous value representations")]
    assert len(rows["dicom"]) == 1, rows
    assert rows["recorder"] == rows["dicom"]


def test_a_plugin_is_handed_an_instance_the_iod_check_refuses(tmp_path):
    """The DICOM export fails an instance its IOD check refuses, because it
    will not write a file that does not conform. A plugin writes its own
    format: its instance is handed over, with a `WARNING` row naming what
    the check found (Q-B)."""
    outcomes = {}
    for fmt in ("dicom", "recorder"):
        work = tmp_path / fmt
        with _session(work) as session:
            [instance] = [i for p in session.store.patients
                          for st in p.studies for se in st.series
                          for i in se.instances]
            del instance.attributes["0028,0030"]  # Pixel Spacing, Type 1
            instance.mark_modified()
            try:
                outcomes[fmt] = session.export(str(work / "out"), format=fmt)
            except ExportError as error:
                outcomes[fmt] = error
        outcomes[fmt + "-rows"] = _rows(work / "s.db", "WARNING")
    assert isinstance(outcomes["dicom"], ExportError)  # the premise
    assert outcomes["recorder"].written_uids == [f"{ROOT}.1.1.1"]
    iod = [d for d in outcomes["recorder-rows"]
           if "does not conform to its IOD" in d]
    assert len(iod) == 1 and "0028,0030" in iod[0], outcomes["recorder-rows"]


def test_a_plugin_export_grades_pass(tmp_path):
    """Q7: no row says the plugin is not attested."""
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.audit()
        session.anonymize()
        session.export(str(out), format="recorder")
        grade = _grade(session, tmp_path)
    assert "PASS" in grade and "REVIEW_REQUIRED" not in grade, grade
    assert not [d for d in _rows(tmp_path / "s.db", "WARNING")
                if "not attested" in d]


# --------------------------------------------------------------------------
# What register() refuses
# --------------------------------------------------------------------------

def test_register_refuses_the_1_0_shape():
    with pytest.raises(TypeError, match="write"):
        exporters.register("old", plugins.OldShape)


def test_register_refuses_a_class_that_inherits_write():
    with pytest.raises(TypeError, match="write"):
        exporters.register("inherits", plugins.InheritsWrite)


@pytest.mark.parametrize("name", ["dicom", "wfdb"])
def test_register_refuses_a_built_in_name(name):
    with pytest.raises(ValueError, match=name):
        exporters.register(name, plugins.Recorder)
    assert exporters._REGISTRY[name] is not plugins.Recorder


def test_register_refuses_a_class_no_worker_can_import():
    class Local(exporters.Exporter):
        def write(self, dataset, item, folder, **options):
            return None

    with pytest.raises(TypeError, match="import"):
        exporters.register("local", Local)


def test_register_refuses_an_option_the_session_owns():
    with pytest.raises(TypeError, match="patient_ids"):
        exporters.register("owns", plugins.OwnsAnOption)


def test_register_refuses_a_built_in_class_under_another_name():
    """The built-ins implement no `write`: they are installed at import,
    never through `register`."""
    from isocenter.exporters.dicom import DicomFormatExporter

    with pytest.raises(TypeError, match="write"):
        exporters.register("dicom-again", DicomFormatExporter)


@pytest.mark.parametrize("cls, name", [
    (plugins.OptionNamedDataset, "dataset"),
    (plugins.OptionNamedItem, "item")])
def test_register_refuses_an_option_named_for_an_argument_of_write(
        _registry, cls, name):
    """Review of #1197: `write(dataset, item, folder, **options)` would get
    the name twice, and every instance would fail in the worker."""
    with pytest.raises(TypeError, match=f"'{name}'.*its own arguments"):
        exporters.register("clash", cls)
    assert "clash" not in exporters._REGISTRY


_SCRIPT = '''\
from isocenter import exporters

{top}class InScript(exporters.Exporter):
{indent}    def write(self, dataset, item, folder, **options):
{indent}        return None


if __name__ == "__main__":
{guarded}    try:
        exporters.register("in-script", InScript)
    except TypeError as error:
        print("REFUSED", error)
    else:
        print("REGISTERED")
'''


@pytest.mark.parametrize("guarded", [True, False],
                         ids=["under-the-main-guard", "at-the-top"])
def test_register_refuses_a_class_under_a_scripts_main_guard(
        tmp_path, guarded):
    """Review of #1197: a spawned worker imports the script under another
    name, so the block under its main guard does not run there. A class
    defined in it passed the pickle round trip in this process, and the
    export then failed with `BrokenProcessPool`. The class at the top of
    the same script is admitted: that is what makes the refusal a
    judgement about the guard and not about scripts."""
    import subprocess
    import sys

    if guarded:
        text = _SCRIPT.format(
            top="if __name__ == \"__main__\":\n    ", indent="    ",
            guarded="")
    else:
        text = _SCRIPT.format(top="", indent="", guarded="")
    script = tmp_path / "script.py"
    script.write_text(text, encoding="utf-8")
    done = subprocess.run([sys.executable, str(script)], cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    if guarded:
        assert done.stdout.startswith("REFUSED"), done.stdout
        assert "an export worker process could not import it" in done.stdout
    else:
        assert done.stdout.strip() == "REGISTERED", done.stdout

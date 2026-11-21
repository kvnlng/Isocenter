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
                      ("onefile", plugins.OneFile)):
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
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.audit()
        session.anonymize()
        source = f"{ROOT}.1.1.1"
        session.export(str(out), format="recorder")
    [record] = _records(out)
    assert record["sop_instance_uid"] != source
    assert all(tag[:4].isalnum() for tag in record["tags"])


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

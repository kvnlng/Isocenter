"""The built-in formats are told apart from a plugin by class identity (#527, #783).

Until #783 this file was `test_a_third_party_export_is_not_attested.py`:
every export gate lived inside the two built-in formats, so an exporter
registered through `exporters.register` ran behind none of them, and owner
ruling Q2 of 2026-09-23 had `Session.export()` write one "not attested"
`WARNING` row per such run. #783 runs every gate for a plugin too, and the
owner ruled on 2026-11-20 (Q7) that such an export can grade PASS, so the
row is gone. What stays is the question this file always pinned: which
classes are the built-ins.

`Session.export()` sends a format to the built-in path only when the
exporter's class **is** `DicomFormatExporter` or `WfdbExporter`; every other
class runs through the plugin path and gets the dataset the DICOM export
would write. Not by `__module__`, which a plugin can spell as it likes and a
subclass inherits; not by `isinstance`, because a subclass may override
anything; not by format name, which `register()` now refuses for `dicom`
and `wfdb` (Q9); and `is`, never `in (...)`, because a tuple's `in` falls
back to `==`.
"""
import os

import pytest

from isocenter import exporters
from isocenter.exporters.dicom import DicomFormatExporter
from isocenter.exporters.wfdb import WfdbExporter
from isocenter.session import DicomSession

from support import plugin_exporters as plugins
from test_report_export_boundary import _write_src

#: The row 1.0 wrote for a plugin. Kept to show it is not written now.
NOT_ATTESTED = "not attested by Isocenter"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")


@pytest.fixture
def _clean_registry():
    before = dict(exporters._REGISTRY)
    yield
    exporters._REGISTRY.clear()
    exporters._REGISTRY.update(before)


def _session(tmp_path, name="s.db"):
    src = tmp_path / "src"
    if not src.exists():
        src.mkdir()
        _write_src(str(src))
    session = DicomSession(persistence_file=str(tmp_path / name))
    session.ingest(str(src))
    return session


def _marked(session):
    return [details for _, action, details
            in session.store_backend.get_audit_errors()
            if action == "WARNING" and NOT_ATTESTED in details]


def _report(session, tmp_path, name="report.md"):
    path = tmp_path / name
    session.generate_report(str(path))
    return path.read_text(encoding="utf-8")


def _grade(text):
    [line] = [ln for ln in text.splitlines()
              if ln.startswith("| **Validation Status** |")]
    return line


def _documented_path(session, tmp_path, fmt):
    session.audit()
    session.anonymize()
    return session.export(str(tmp_path / f"out-{fmt}"), format=fmt)


def _recorded(out):
    found = []
    for root, _dirs, names in os.walk(str(out)):
        found.extend(os.path.join(root, n) for n in names
                     if n.endswith(".json"))
    return found


@pytest.mark.parametrize("cls", [plugins.SubOfDicom, plugins.Impostor],
                         ids=["subclass", "equal-to-everything"])
def test_a_class_that_is_not_a_built_in_runs_as_a_plugin(
        tmp_path, _clean_registry, cls):
    """Kills `isinstance` (the subclass) and `type(x) in (...)` (the
    metaclass whose `__eq__` answers True) in place of identity: either
    would send the class down the built-in path, which writes `.dcm`
    files through `DicomFormatExporter.export`."""
    if cls is plugins.Impostor:
        assert cls == DicomFormatExporter  # the premise
    exporters.register("candidate", cls)
    out = tmp_path / "out"
    with _session(tmp_path) as session:
        session.export(str(out), format="candidate")
    assert _recorded(out), "the plugin's write() never ran"
    assert not [n for _r, _d, names in os.walk(str(out)) for n in names
                if n.endswith(".dcm")]


def test_a_class_that_spells_the_built_in_module_is_refused(_clean_registry):
    """Kills a `__module__` test: a class claiming the built-in's module
    is not there to import, so `register()` refuses it."""
    with pytest.raises(TypeError, match="import"):
        exporters.register("spoof", plugins.Spoofed)


def test_an_unknown_format_writes_nothing(tmp_path):
    with _session(tmp_path) as session:
        with pytest.raises(ValueError, match="Unknown export format"):
            session.export(str(tmp_path / "out"), format="nifti")
    assert not (tmp_path / "out").exists()


def test_no_export_writes_the_not_attested_row(tmp_path, _clean_registry):
    """Q7: the row 1.0 wrote before a plugin ran is gone."""
    exporters.register("recorder", plugins.Recorder)
    with _session(tmp_path) as session:
        _documented_path(session, tmp_path, "recorder")
        rows = _marked(session)
        text = _report(session, tmp_path)
    assert rows == []
    assert "PASS" in _grade(text) and "REVIEW_REQUIRED" not in _grade(text), text


def test_the_documented_dicom_path_grades_pass(tmp_path):
    with _session(tmp_path) as session:
        _documented_path(session, tmp_path, "dicom")
        rows = _marked(session)
        text = _report(session, tmp_path)
    assert rows == []
    assert "PASS" in _grade(text) and "REVIEW_REQUIRED" not in _grade(text), text


def test_the_documented_wfdb_path_grades_pass(tmp_path):
    from scripts.generate_waveform_test_data import write_fixture

    src = tmp_path / "wsrc"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-1", patient_name="Doe^Jane")
    with DicomSession(persistence_file=str(tmp_path / "w.db")) as session:
        session.ingest(str(src))
        _documented_path(session, tmp_path, "wfdb")
        text = _report(session, tmp_path)
    assert "PASS" in _grade(text) and "REVIEW_REQUIRED" not in _grade(text), text


def test_the_two_built_ins_are_the_classes_registered_at_import():
    """The identity test is only right while these are what `dicom` and
    `wfdb` resolve to out of the box, and `register()` cannot change
    that (Q9)."""
    assert type(exporters.get_exporter("dicom")) is DicomFormatExporter
    assert type(exporters.get_exporter("wfdb")) is WfdbExporter

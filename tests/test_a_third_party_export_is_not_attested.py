"""A third-party exporter's run writes one WARNING row, so it never grades PASS (#527).

Every export gate lives inside the two built-in formats: the burned-in
re-audit (#536), the recoverable-identity disclosure, the
de-identification markers (#554), the owner stamps and the synthetic-key
rule (#584), the #555/#686/#725 notices, and the `EXPORT` and
export-time `DATA_LOSS` rows. `Session.export()` resolves the format and
dispatches; an exporter registered through `exporters.register` runs
behind none of it. Before this, a plugin that wrote the identity token
or a raw MRN left the report at `PASS`, carrying only the "generated
before any export" note -- the defect #527's ruling called the one that
must be impossible before the contract is public.

Owner ruling Q2 (2026-09-23): one `WARNING` audit row per third-party
export, written before dispatch, so the report grades
`REVIEW_REQUIRED` through the existing `WARNING` condition. The cost is
stated plainly on `docs/api/exporters.md`: in 1.0 no third-party export
grades PASS, however well the plugin behaves. #783 is the 1.1 work that
moves the gates above dispatch and would let a plugin earn one.

"Third-party" is decided by class identity against the two built-in
classes -- not by `__module__`, which a plugin can spell as it likes and
a subclass inherits, and not by format name, which anyone can register
over.
"""
import pytest

from isocenter import exporters
from isocenter.exporters.dicom import DicomFormatExporter
from isocenter.exporters.wfdb import WfdbExporter
from isocenter.session import DicomSession

from test_report_export_boundary import BOUNDARY_NOTE_MARKER, _write_src

#: The row's own words. The test reads them rather than counting every
#: WARNING, so a notice some other gate writes cannot stand in for it.
NOT_ATTESTED = "not attested by Isocenter"


@pytest.fixture
def _clean_registry():
    """`register()` mutates the module-global registry. The cases that
    register over `dicom` replace the real exporter, so restoring it is
    not tidiness: every later test in the process exports through it."""
    before = dict(exporters._REGISTRY)
    yield
    exporters._REGISTRY.clear()
    exporters._REGISTRY.update(before)


class Toy:
    """A plugin that behaves: it writes nothing and says so."""

    calls = 0

    def export(self, session, folder, **options):
        type(self).calls += 1
        return []


class Raising:
    def export(self, session, folder, **options):
        raise RuntimeError("the plugin failed")


class Sub(DicomFormatExporter):
    """Inherits the built-in's `export` and its `__module__` is this
    file's -- but a subclass may override anything, so it is not the
    code Isocenter verifies."""


class Spoofed:
    """Claims the built-in's module. Only class identity sees through it."""

    __module__ = DicomFormatExporter.__module__

    def export(self, session, folder, **options):
        return []


class _EqualToEverything(type):
    def __eq__(cls, other):
        return True

    __hash__ = type.__hash__


class Impostor(metaclass=_EqualToEverything):
    """Compares equal to every class, so a built-in test that falls back
    to `==` -- `type(x) in (A, B)` does -- takes it for one. Only `is`
    sees through it (review of #786)."""

    def export(self, session, folder, **options):
        return []


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
    # The table row, not the boundary note, which names the status too.
    [line] = [ln for ln in text.splitlines()
              if ln.startswith("| **Validation Status** |")]
    return line


def _documented_path(session, tmp_path, fmt):
    session.audit()
    session.anonymize()
    return session.export(str(tmp_path / f"out-{fmt}"), format=fmt)


def test_a_third_party_export_writes_one_row_and_grades_review_required(
        tmp_path, _clean_registry):
    """The documented path, a plugin at the end. Kills the row deleted,
    and the row written at a level the grade does not read."""
    exporters.register("toy", Toy)
    out = str(tmp_path / "out-toy")
    with _session(tmp_path) as session:
        session.audit()
        session.anonymize()
        result = session.export(out, format="toy", level=3)
        rows = _marked(session)
        text = _report(session, tmp_path)

    assert result == []
    assert len(rows) == 1, rows
    # The row names what ran, where, and under what name.
    assert "'toy'" in rows[0], rows[0]
    assert out in rows[0], rows[0]
    assert f"{Toy.__module__}.{Toy.__qualname__}" in rows[0], rows[0]
    assert "REVIEW_REQUIRED" in _grade(text), text
    # The report still does not know the export happened: no EXPORT row
    # was written for it, so the #153 note stands. The page says both.
    assert BOUNDARY_NOTE_MARKER in text, text


def test_each_third_party_export_writes_its_own_row(tmp_path, _clean_registry):
    exporters.register("toy", Toy)
    with _session(tmp_path) as session:
        session.export(str(tmp_path / "a"), format="toy")
        session.export(str(tmp_path / "b"), format="toy")
        rows = _marked(session)
    assert len(rows) == 2, rows


def test_a_raising_exporter_still_leaves_the_row(tmp_path, _clean_registry):
    """Written before dispatch. Kills the row moved after the call."""
    exporters.register("boom", Raising)
    with _session(tmp_path) as session:
        with pytest.raises(RuntimeError, match="the plugin failed"):
            session.export(str(tmp_path / "out"), format="boom")
        rows = _marked(session)
    assert len(rows) == 1, rows


def test_an_unknown_format_writes_no_row(tmp_path):
    """Nothing was dispatched, so there is nothing to disclaim: the
    `ValueError` from `get_exporter` comes first."""
    with _session(tmp_path) as session:
        with pytest.raises(ValueError, match="Unknown export format"):
            session.export(str(tmp_path / "out"), format="nifti")
        rows = _marked(session)
    assert rows == []


def test_a_subclass_of_the_dicom_exporter_is_third_party(
        tmp_path, _clean_registry):
    """Kills `isinstance` in place of identity."""
    exporters.register("dicom-sub", Sub)
    with _session(tmp_path) as session:
        session.export(str(tmp_path / "out"), format="dicom-sub",
                       show_progress=False)
        rows = _marked(session)
    assert len(rows) == 1, rows


def test_a_class_that_spells_the_built_in_module_is_third_party(
        tmp_path, _clean_registry):
    """Kills a `__module__` test in place of identity."""
    exporters.register("spoof", Spoofed)
    with _session(tmp_path) as session:
        session.export(str(tmp_path / "out"), format="spoof")
        rows = _marked(session)
    assert len(rows) == 1, rows


def test_a_class_that_compares_equal_to_the_built_ins_is_third_party(
        tmp_path, _clean_registry):
    """Kills `type(exporter) not in (...)`, which asks `==`, not `is`."""
    assert Impostor == DicomFormatExporter  # the premise
    exporters.register("impostor", Impostor)
    with _session(tmp_path) as session:
        session.export(str(tmp_path / "out"), format="impostor")
        rows = _marked(session)
    assert len(rows) == 1, rows


def test_another_class_registered_as_dicom_is_third_party(
        tmp_path, _clean_registry):
    """Kills a test on the format name: `dicom` is only a name."""
    exporters.register("dicom", Toy)
    with _session(tmp_path) as session:
        session.export(str(tmp_path / "out"), format="dicom")
        rows = _marked(session)
    assert len(rows) == 1, rows


@pytest.mark.parametrize("name", ["dicom", "dicom-again"])
def test_the_dicom_exporter_under_any_name_is_built_in(
        tmp_path, _clean_registry, name):
    """Re-registered under its own name or another, the class is the
    code Isocenter verifies, so it writes no row."""
    exporters.register(name, DicomFormatExporter)
    with _session(tmp_path) as session:
        session.export(str(tmp_path / "out"), format=name,
                       show_progress=False)
        rows = _marked(session)
    assert rows == []


def test_the_documented_dicom_path_writes_no_row_and_grades_pass(tmp_path):
    """Kills the condition inverted, or widened to every exporter."""
    with _session(tmp_path) as session:
        _documented_path(session, tmp_path, "dicom")
        rows = _marked(session)
        text = _report(session, tmp_path)
    assert rows == []
    assert "PASS" in _grade(text) and "REVIEW_REQUIRED" not in _grade(text), text


def test_the_documented_wfdb_path_writes_no_row_and_grades_pass(tmp_path):
    """The wfdb built-in is the second identity; kills it left out."""
    from scripts.generate_waveform_test_data import write_fixture

    src = tmp_path / "wsrc"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-1", patient_name="Doe^Jane")
    with DicomSession(persistence_file=str(tmp_path / "w.db")) as session:
        session.ingest(str(src))
        _documented_path(session, tmp_path, "wfdb")
        rows = _marked(session)
        text = _report(session, tmp_path)
    assert rows == []
    assert "PASS" in _grade(text) and "REVIEW_REQUIRED" not in _grade(text), text


def test_the_two_built_ins_are_the_classes_registered_at_import():
    """The identity set is only right while these are what `dicom` and
    `wfdb` resolve to out of the box."""
    assert type(exporters.get_exporter("dicom")) is DicomFormatExporter
    assert type(exporters.get_exporter("wfdb")) is WfdbExporter

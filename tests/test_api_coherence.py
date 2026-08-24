"""The public API must offer one way to do each thing.

Pre-1.0 cleanup: duplicate export layouts, duplicate sanitizers, and dead
parameters are removed rather than deprecated.
"""
import inspect
import os

import pytest

from gantry.session import DicomSession


def test_export_does_not_accept_a_dead_version_parameter():
    """`version` was accepted, documented as unused, and never read.

    A parameter that silently does nothing is worse than no parameter:
    a caller passing it reasonably believes it took effect.
    """
    signature = inspect.signature(DicomSession._export_dicom)
    assert "version" not in signature.parameters, (
        "`version` is still accepted by _export_dicom; it is never read, so "
        "any caller passing it is silently ignored")


def test_only_one_public_export_entry_point_builds_directory_trees():
    """`generate_export_from_db` was a third folder-naming scheme.

    It duplicated the format strings inline rather than sharing a helper,
    and nothing but a test ever called it.
    """
    from gantry.io_handlers import DicomExporter

    assert not hasattr(DicomExporter, "generate_export_from_db"), (
        "generate_export_from_db still exists; it is a third, "
        "independently-maintained directory layout with no production caller")


def test_both_public_export_paths_produce_the_same_tree(tmp_path):
    """`DicomExporter.save_patient` and `session.export()` must agree.

    Both are public and shipped. Two layouts means "where does Gantry put
    files" has no single answer for a library user.

    Derives both trees from real exports rather than hardcoding names, so
    it cannot drift out of step with the naming logic it guards.
    """
    from gantry.io_handlers import DicomExporter
    from gantry.session import DicomSession
    from scripts.generate_waveform_test_data import write_fixture

    source = tmp_path / "src"
    source.mkdir()
    write_fixture(str(source / "ecg.dcm"), num_samples=50)

    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        session.ingest(str(source))
        patient = session.store.patients[0]

        via_session = tmp_path / "via_session"
        session.export(str(via_session), format="dicom")

        via_exporter = tmp_path / "via_exporter"
        DicomExporter.save_patient(patient, str(via_exporter))
    finally:
        session.close()

    def tree(root):
        return sorted(
            str(p.relative_to(root).parent)
            for p in root.rglob("*.dcm"))

    session_tree = tree(via_session)
    exporter_tree = tree(via_exporter)

    assert session_tree, "session.export() produced no .dcm files"
    assert exporter_tree, "save_patient produced no .dcm files"
    assert session_tree == exporter_tree, (
        f"the two public export paths disagree:\n"
        f"  session.export(): {session_tree}\n"
        f"  save_patient():   {exporter_tree}")


def test_export_folder_naming_is_case_insensitive_to_description_tag_keys():
    """Series/Study Description keys may be spelled with either hex-letter
    casing depending on how the object graph was built.

    Real DICOM ingestion always lowercases tag keys
    (`io_handlers.populate_attrs`'s
    `f"{elem.tag.group:04x},{elem.tag.element:04x}"`), but object graphs
    built directly by callers -- e.g. `scripts/generate_test_dataset.py`,
    which sets Series Description via `inst_builder.set_attribute(
    "0008,103E", ...)` and then calls `DicomExporter.save_patient` -- are
    free to spell the tag with uppercase hex letters. `export_folder_names`
    must find the description either way: a mismatch here is the same trap
    `privacy.py`'s `PHIRedactor._normalize_tag_keys` guards against for
    PHI-tag config keys (see its comment on "0008,103E"), except here it
    would silently drop a real caller's Series Description from the
    exported folder name rather than disabling a redaction rule.
    """
    import datetime
    from gantry.io_handlers import export_folder_names
    from gantry.entities import Patient, Study, Series, Instance

    def build(series_desc_tag):
        patient = Patient("PID_CI", "CI Test")
        study = Study("STUDY_CI_UID", datetime.date(2025, 6, 1))
        patient.studies.append(study)
        series = Series("SERIES_CI_UID", "CT", 1)
        study.series.append(series)
        inst = Instance("SOP_CI_UID", "1.2.840.10008.5.1.4.1.1.2", 0)
        inst.attributes = {
            "0008,1030": "Some Study",
            series_desc_tag: "Some Series",
        }
        series.instances.append(inst)
        return patient, study, series

    lower_patient, lower_study, lower_series = build("0008,103e")
    upper_patient, upper_study, upper_series = build("0008,103E")

    _, _, folder_lower = export_folder_names(lower_patient, lower_study, lower_series)
    _, _, folder_upper = export_folder_names(upper_patient, upper_study, upper_series)

    assert "Some_Series" in folder_lower, (
        f"lowercase '0008,103e' Series Description tag was not found; "
        f"got folder name {folder_lower!r}")
    assert "Some_Series" in folder_upper, (
        f"uppercase '0008,103E' Series Description tag was not found; "
        f"got folder name {folder_upper!r}")
    assert folder_lower == folder_upper, (
        f"same Series Description under different tag-key casing produced "
        f"different folder names: {folder_lower!r} != {folder_upper!r}")


def test_one_sanitizer_for_folder_names():
    """Two folder-name sanitizers is one too many.

    `wfdb._sanitize` is deliberately excluded -- WFDB record names are
    bare ASCII tokens with different rules, documented as such.
    """
    from gantry.io_handlers import DicomExporter

    assert not hasattr(DicomExporter, "_sanitize"), (
        "DicomExporter._sanitize still exists alongside "
        "ConfigLoader.clean_filename; both sanitize folder names")


def test_a_zero_series_number_is_not_renamed_to_unknown():
    """`_sanitize(0)` returned 'Unknown' because `0` is falsy.

    A series number of 0 is a real value, not a missing one.
    """
    from gantry.config_manager import ConfigLoader

    assert ConfigLoader.clean_filename(0) == "0"


def test_export_offers_one_name_per_behaviour():
    """`safe` and `compression` were aliases for parameters that already
    existed, so two spellings produced the same effect."""
    signature = inspect.signature(DicomSession._export_dicom)
    for alias, canonical in (("safe", "check_burned_in"),
                             ("compression", "use_compression")):
        assert alias not in signature.parameters, (
            f"`{alias}` is still accepted; it is an alias for `{canonical}`")
        assert canonical in signature.parameters, (
            f"`{canonical}` is missing -- the alias was removed but the "
            "canonical parameter did not survive")


def test_use_compression_none_means_no_compression(tmp_path):
    """`use_compression=None` must mean "do not compress", not "compress".

    Under the old legacy-mapping block, `compression=None` failed the
    `if compression is not None` guard and fell through, leaving
    `use_compression` at its default of `True` -- so a caller who typed
    `None` for "no compression" silently got JPEG2000 compression anyway
    (this is exactly what `tests/benchmarks/run_stress_test.py` did: its
    "uncompressed" arm computed `compression=None` and got J2K anyway).
    Now that `compression`/`use_compression` are one parameter, `None` is
    simply falsy and must produce an uncompressed export.
    """
    import numpy as np
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless

    input_dir = tmp_path / "input"
    input_dir.mkdir()

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.7'
    ds.file_meta.MediaStorageSOPInstanceUID = '1.2.3.4.5.6'

    ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.7'
    ds.SOPInstanceUID = '1.2.3.4.5.6'
    ds.PatientName = "Test^Patient"
    ds.PatientID = "123456"
    ds.StudyInstanceUID = "1.2.3.4.5"
    ds.SeriesInstanceUID = "1.2.3.4.5.1"

    ds.Rows = 64
    ds.Columns = 64
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PhotometricInterpretation = "MONOCHROME2"

    arr = np.random.randint(0, 255, (64, 64), dtype=np.uint8)
    ds.PixelData = arr.tobytes()

    ds.is_little_endian = True
    ds.is_implicit_VR = True
    ds.preamble = b"\0" * 128
    pydicom.dcmwrite(str(input_dir / "test.dcm"), ds, write_like_original=False)

    session = DicomSession(":memory:")
    session.ingest(str(input_dir))

    out = tmp_path / "output"
    session.export(str(out), use_compression=None, show_progress=False)

    exported_file = None
    for root, _, files in os.walk(out):
        for f in files:
            if f.endswith(".dcm"):
                exported_file = os.path.join(root, f)
                break

    assert exported_file is not None, "Exported file not found"

    out_ds = pydicom.dcmread(exported_file)
    assert out_ds.file_meta.TransferSyntaxUID != JPEG2000Lossless, (
        "use_compression=None produced a JPEG2000-compressed export; "
        "None must mean 'no compression', not 'use the default'")


def test_session_can_be_used_as_a_context_manager(tmp_path):
    """close() releases a process pool and two threads; forgetting it
    leaks worker subprocesses. `with` is how Python spells that."""
    from concurrent.futures import BrokenExecutor

    with DicomSession(persistence_file=str(tmp_path / "ctx.db")) as session:
        assert session.store is not None, "session unusable inside `with`"
        executor = session._executor

    try:
        executor.submit(int, "1")
        raised = None
    except (RuntimeError, BrokenExecutor) as exc:
        raised = exc

    assert raised is not None, (
        "the process pool still accepts work after the `with` block; "
        "__exit__ did not call close()")


def test_context_manager_closes_the_session_when_the_body_raises(tmp_path):
    """A leak on the error path is the one that matters -- that is
    precisely when a caller's own `close()` gets skipped."""
    from concurrent.futures import BrokenExecutor

    session = DicomSession(persistence_file=str(tmp_path / "boom.db"))
    executor = session._executor

    class Boom(Exception):
        pass

    try:
        with session:
            raise Boom("failure inside the with-body")
    except Boom:
        pass
    else:
        raise AssertionError("__exit__ swallowed the exception; it must not")

    try:
        executor.submit(int, "1")
        raised = None
    except (RuntimeError, BrokenExecutor) as exc:
        raised = exc

    assert raised is not None, (
        "the process pool survived an exception in the with-body")


def test_close_is_idempotent(tmp_path):
    """Calling close() a second time must not raise.

    `PersistenceManager.shutdown()`, `SqliteStore.stop()`, and
    `ProcessPoolExecutor.shutdown()` already guard against redundant
    shutdown internally, so close() itself is already safe to call twice.
    This pins that property explicitly: a caller who calls close() inside
    a `with DicomSession(...) as session:` block must not get an error
    when `__exit__` calls close() again on the way out.
    """
    session = DicomSession(persistence_file=str(tmp_path / "idempotent.db"))
    session.close()
    session.close()  # must not raise


def test_close_still_shuts_down_the_executor_if_an_earlier_step_raises(tmp_path):
    """close() runs persistence_manager.shutdown(), store_backend.stop(),
    and _executor.shutdown() as a bare sequence. If the first step raises
    and the other two never run, the ProcessPoolExecutor leaks its worker
    processes for the life of the interpreter -- a `with` block does not
    help, because __exit__ just calls the same broken close().

    This forces a failure in the FIRST step and asserts the executor was
    still shut down, which only passes if close() is internally
    exception-safe (every step runs regardless of an earlier failure).
    """
    from concurrent.futures import BrokenExecutor

    session = DicomSession(persistence_file=str(tmp_path / "leak.db"))
    executor = session._executor

    def boom():
        raise RuntimeError("persistence shutdown exploded")

    session.persistence_manager.shutdown = boom

    with pytest.raises(RuntimeError, match="persistence shutdown exploded"):
        session.close()

    try:
        executor.submit(int, "1")
        raised = None
    except (RuntimeError, BrokenExecutor) as exc:
        raised = exc

    assert raised is not None, (
        "the process pool survived close() after an earlier shutdown "
        "step raised -- close() is not exception-safe")

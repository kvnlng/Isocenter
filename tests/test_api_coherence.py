"""The public API must offer one way to do each thing.

Pre-1.0 cleanup: duplicate export layouts, duplicate sanitizers, and dead
parameters are removed rather than deprecated.
"""
import inspect
import os

import pytest

from isocenter.session import DicomSession


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
    from isocenter.io_handlers import DicomExporter

    assert not hasattr(DicomExporter, "generate_export_from_db"), (
        "generate_export_from_db still exists; it is a third, "
        "independently-maintained directory layout with no production caller")


def test_both_public_export_paths_produce_the_same_tree(tmp_path):
    """`DicomExporter.write_tree` and `session.export()` must agree.

    Both are public and shipped. Two layouts means "where does Isocenter put
    files" has no single answer for a library user.

    Derives both trees from real exports rather than hardcoding names, so
    it cannot drift out of step with the naming logic it guards.
    """
    from isocenter.io_handlers import DicomExporter
    from isocenter.session import DicomSession
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
        DicomExporter.write_tree(patient, str(via_exporter))
    finally:
        session.close()

    def tree(root):
        # Full relative paths, not just `.parent`. Comparing folders only
        # is how #50 survived this test: the two paths agreed on every
        # directory and disagreed on the filename inside it, so a tree
        # built by one could not be diffed against a tree built by the
        # other while this assertion stayed green.
        return sorted(
            str(p.relative_to(root))
            for p in root.rglob("*.dcm"))

    session_tree = tree(via_session)
    exporter_tree = tree(via_exporter)

    assert session_tree, "session.export() produced no .dcm files"
    assert exporter_tree, "write_tree produced no .dcm files"
    assert session_tree == exporter_tree, (
        f"the two public export paths disagree:\n"
        f"  session.export(): {session_tree}\n"
        f"  write_tree():   {exporter_tree}")


def test_export_folder_naming_is_case_insensitive_to_description_tag_keys():
    """Series/Study Description keys may be spelled with either hex-letter
    casing depending on how the object graph was built.

    Real DICOM ingestion always lowercases tag keys
    (`io_handlers.populate_attrs`'s
    `f"{elem.tag.group:04x},{elem.tag.element:04x}"`), but object graphs
    built directly by callers -- e.g. `scripts/generate_test_dataset.py`,
    which sets Series Description via `inst_builder.set_attribute(
    "0008,103E", ...)` and then calls `DicomExporter.write_tree` -- are
    free to spell the tag with uppercase hex letters. `export_folder_names`
    must find the description either way: a mismatch here is the same trap
    `privacy.py`'s `PHIRedactor._normalize_tag_keys` guards against for
    PHI-tag config keys (see its comment on "0008,103E"), except here it
    would silently drop a real caller's Series Description from the
    exported folder name rather than disabling a redaction rule.
    """
    import datetime
    from isocenter.io_handlers import export_folder_names
    from isocenter.entities import Patient, Study, Series, Instance

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
    from isocenter.io_handlers import DicomExporter

    assert not hasattr(DicomExporter, "_sanitize"), (
        "DicomExporter._sanitize still exists alongside "
        "ConfigLoader.clean_filename; both sanitize folder names")


def test_clean_filename_does_not_treat_a_falsy_value_like_0_as_missing():
    """`ConfigLoader.clean_filename` must not treat a falsy-but-real value
    like the integer `0` as missing.

    This does NOT pin a series-number regression: the deleted
    `DicomExporter._sanitize` was applied to the PatientID and to the
    Study/Series Description strings in the legacy folder-naming path,
    never to the series number itself (which was inserted into the
    folder name
    unsanitized), so there is no historical "series number 0 renamed to
    Unknown" bug to regress against. This pins a general property of
    `clean_filename` in isolation.
    """
    from isocenter.config_manager import ConfigLoader

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


def test_the_scan_for_phi_alias_is_gone():
    """`scan_for_phi` was a pure alias for `audit()` -- its own docstring
    said "Legacy alias for audit()", it took the same argument and
    returned the same object, and it added nothing.

    Pinned by name rather than by signature comparison: a signature check
    would keep passing if someone reintroduced the alias with a *changed*
    signature, which is a worse state than the one being removed. What
    matters is that the second spelling does not exist.
    """
    assert not hasattr(DicomSession, "scan_for_phi"), (
        "`scan_for_phi` is back; it is an alias for `audit()`, and pre-1.0 "
        "duplicate spellings are deleted rather than deprecated")
    assert callable(DicomSession.audit), (
        "`audit` is missing -- the alias was removed but the canonical "
        "method did not survive")


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


# --- export_folder_names' fallbacks must not invent words (#53) -------

def _bare_graph(study_uid="1.2.3.4.5.9999", series_uid="1.2.3.4.5.8888",
                series_number=7):
    from isocenter.entities import Patient, Series, Study
    series = Series(series_instance_uid=series_uid, modality="CT",
                    series_number=series_number)
    study = Study(study_instance_uid=study_uid, study_date="20230101",
                  series=[series])
    patient = Patient(patient_id="PAT1", patient_name="DOE^JOHN",
                      studies=[study])
    return patient, study, series


def test_a_study_with_no_uid_is_not_labelled_with_a_sliced_placeholder():
    """`(uid or "Unknown")[-5:]` is `"nknow"`.

    The suffix exists to disambiguate two studies that share a date and
    a description. With no UID there is nothing to disambiguate *with*,
    so the honest token says the UID is missing. `"nknow"` is a word
    from nowhere: it looks like real data, sorts among real suffixes,
    and tells a reader nothing.
    """
    from isocenter.io_handlers import export_folder_names

    _, study_folder, _ = export_folder_names(*_bare_graph(study_uid=None))

    assert "nknow" not in study_folder, study_folder
    assert "NoUID" in study_folder, study_folder


def test_a_series_with_no_uid_is_not_labelled_with_a_sliced_placeholder():
    from isocenter.io_handlers import export_folder_names

    _, _, series_folder = export_folder_names(*_bare_graph(series_uid=None))

    assert "nknow" not in series_folder, series_folder
    assert "NoUID" in series_folder, series_folder


def test_a_series_with_no_number_is_not_labelled_None():
    """`str(series.series_number)` is `"None"` when it is absent.

    Same defect as the sliced placeholder, one line down: a folder named
    `Series_None_CT_...` reads as a series numbered "None" rather than a
    series whose number was never recorded.
    """
    from isocenter.io_handlers import export_folder_names

    _, _, series_folder = export_folder_names(*_bare_graph(series_number=None))

    assert "_None_" not in series_folder, series_folder
    assert "NoNumber" in series_folder, series_folder


def test_a_uid_that_exists_still_contributes_its_suffix():
    """The fallbacks must not cost the disambiguation they exist beside."""
    from isocenter.io_handlers import export_folder_names

    _, study_folder, series_folder = export_folder_names(*_bare_graph())

    assert study_folder.endswith("9999"), study_folder
    assert series_folder.endswith("8888"), series_folder


def test_the_export_to_parquet_second_spelling_is_gone():
    """Two methods wrote Parquet (#55). `export_dataframe(".parquet")`
    and `export_to_parquet()` differed in source -- the in-memory graph
    versus a re-read of the database -- so they could disagree about
    what the cohort contained, and nothing said which was authoritative.

    Pinned by name rather than by signature, for the reason given in
    `test_the_scan_for_phi_alias_is_gone`: reintroducing the method with
    a changed signature is worse than the state being removed, and a
    signature check would not notice.
    """
    assert not hasattr(DicomSession, "export_to_parquet"), (
        "`export_to_parquet` is back; Parquet has one writer, "
        "`export_dataframe`, and pre-1.0 duplicate spellings are deleted "
        "rather than deprecated")
    assert callable(DicomSession.export_dataframe), (
        "`export_dataframe` is missing -- the duplicate was removed but "
        "the surviving writer did not")


def test_neither_public_write_path_reports_total_failure_as_success(tmp_path):
    """The asymmetry #191 named, and the half the tree test does not cover.

    `test_both_public_export_paths_produce_the_same_tree` pins that the
    two paths *agree about where files go*. It says nothing about what
    they do when no file goes anywhere, and until #191 they disagreed
    completely: `write_tree` raised `RuntimeError`, and `session.export()`
    returned `None` -- indistinguishable, at the call site, from an
    export that wrote every file.

    A **new** test rather than a modification of that one: the trees
    assertion is not wrong and does not go red here, and folding a
    failure contract into a layout test would make one red mean two
    things.

    `ExportError` subclasses `RuntimeError` precisely so both raises are
    catchable by one `except`; `write_tree`'s bare raise is deliberately
    left alone, because the two describe different behaviours -- "this
    serializer could not write" and "the pipeline delivered nothing".
    """
    import pytest as _pytest

    from isocenter.io_handlers import DicomExporter, ExportError
    from tests.test_export_failure_audit import _session

    session = _session(tmp_path, break_instances=(0, 1, 2))
    try:
        patient = session.store.patients[0]

        with _pytest.raises(RuntimeError):
            DicomExporter.write_tree(patient, str(tmp_path / "via_exporter"))

        with _pytest.raises(ExportError) as caught:
            session.export(str(tmp_path / "via_session"), show_progress=False)
    finally:
        session.close()

    assert isinstance(caught.value, RuntimeError), (
        "an existing `except RuntimeError` around a full run would stop "
        "catching the export that delivered nothing")

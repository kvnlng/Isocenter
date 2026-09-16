"""The public API must offer one way to do each thing.

Pre-1.0 cleanup: duplicate export layouts, duplicate sanitizers, and dead
parameters are removed rather than deprecated.
"""
import ast
import dataclasses
import inspect
import logging
import os

import pytest

import isocenter
from isocenter.entities import Equipment
from isocenter.persistence import SqliteStore
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


def _headers(path):
    """Every element of a written file but the pixel bytes, as comparable
    `(tag path, VR, value)` rows, file meta included."""
    import pydicom

    ds = pydicom.dcmread(str(path))
    rows = [("meta", str(e.tag), e.VR, repr(e.value)) for e in ds.file_meta]

    def walk(dataset, prefix):
        for element in dataset:
            if element.tag == 0x7FE00010:
                continue
            path_ = f"{prefix}{element.tag}"
            if element.VR == "SQ":
                rows.append((path_, "SQ", len(element.value)))
                for index, item in enumerate(element.value):
                    walk(item, f"{path_}[{index}]")
            else:
                rows.append((path_, element.VR, repr(element.value)))

    walk(ds, "")
    return rows


def test_both_public_export_paths_write_the_same_headers(tmp_path):
    """The two doors agree on what is *in* each file, not only where it
    goes (#570).

    Same tree, same filenames, and every element but the pixel bytes
    equal, file by file: over an ingested CT carrying a device serial,
    over the same graph after `anonymize()`, and over a hand-built graph
    whose study has no time and whose equipment came from the builder.
    Until #570 `write_tree` stamped its own set -- the literal Study Time
    `120000`, and the equipment from `Series.equipment`, which after
    `anonymize()` put the real serial back. Killing mutations: the
    literal restored; the equipment block restored (the ingested graph
    gains a zero-length Device Serial Number, the anonymized one the
    serial itself).
    """
    import numpy as np
    import pydicom
    from pydicom.data import get_testdata_file
    from isocenter import Builder
    from isocenter.io_handlers import DicomExporter
    from isocenter.session import DicomSession

    source = tmp_path / "src"
    source.mkdir()
    ct = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ct.DeviceSerialNumber = "SN-570"
    ct.save_as(str(source / "ct.dcm"))

    def compare(session, stage):
        via_session = tmp_path / f"{stage}_session"
        via_tree = tmp_path / f"{stage}_tree"
        session.export(str(via_session), use_compression=False,
                       show_progress=False)
        for patient in session.store.patients:
            DicomExporter.write_tree(patient, str(via_tree),
                                     show_progress=False)
        session_files = sorted(p.relative_to(via_session)
                               for p in via_session.rglob("*.dcm"))
        tree_files = sorted(p.relative_to(via_tree)
                            for p in via_tree.rglob("*.dcm"))
        assert session_files and session_files == tree_files, stage
        for name in session_files:
            assert _headers(via_session / name) == _headers(
                via_tree / name), (stage, str(name))

    with DicomSession(str(tmp_path / "ingested.db")) as session:
        session.ingest(str(source))
        compare(session, "ingested")
        session.anonymize()
        compare(session, "anonymized")

    series = (Builder.start_patient("PAT570", "Doe^Jane")
              .add_study("1.2.826.0.2.570", "20230102")
              .add_series("1.2.826.0.3.570", "OT", 2)
              .set_equipment("ACME", "Model-9", "SN-9"))
    context = series.add_instance("1.2.826.0.1.570.1",
                                  "1.2.840.10008.5.1.4.1.1.7", 1)
    for tag, value in (("0008,0020", "20230102"), ("0028,0002", 1),
                       ("0028,0004", "MONOCHROME2")):
        context.set_attribute(tag, value)
    context.set_pixel_data(np.zeros((4, 4), np.uint8))
    patient = series.end_series().end_study().build()
    with DicomSession(str(tmp_path / "built.db")) as session:
        session.store.patients.append(patient)
        session.save()
        compare(session, "hand_built")


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

    with DicomSession(":memory:") as session:
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


# --- #290: one spelling of the equipment predicate ------------------------


def _equipment_constructions_outside_entities():
    """`(module, lineno)` for every `Equipment(...)` call outside `entities.py`.

    Guards the four spellings that existed -- `Equipment(...)` by bare
    name and `<module>.Equipment(...)` by attribute -- and no more. A
    `type(eq)(...)`, a `dataclasses.replace(eq, ...)`, or a local alias
    (`E = Equipment; E(...)`) would slip past it, and that is accepted
    rather than solved: this test exists because four sites had the
    same three lines and one of them was missing its predicate, not
    because every conceivable construction must be routed through
    `from_parts`.
    """
    package_dir = os.path.dirname(isocenter.__file__)
    hits = []
    for root, _, files in os.walk(package_dir):
        for name in sorted(files):
            if not name.endswith(".py") or name == "entities.py":
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if ((isinstance(func, ast.Name) and func.id == "Equipment")
                        or (isinstance(func, ast.Attribute)
                            and func.attr == "Equipment")):
                    hits.append((os.path.relpath(path, package_dir),
                                 node.lineno))
    return hits


def test_equipment_has_one_constructor_outside_entities():
    """Every `Equipment` outside `entities.py` is built by `Equipment.from_parts` (#290).

    The predicate "a series has equipment iff it has a manufacturer or a
    model name" was spelled at three sites and omitted at a fourth
    (`SeriesBuilder.set_equipment`), and the whole suite stayed green
    with manufacturer and model swapped at both hydration sites. One
    constructor means one place for the rule to be right or wrong.
    """
    hits = _equipment_constructions_outside_entities()

    assert hits == [], (
        "Equipment(...) is constructed directly outside entities.py at "
        f"{hits}; route it through Equipment.from_parts so the predicate "
        "has one spelling")


def test_from_parts_is_bound_to_equipment_in_field_order():
    """`Equipment.from_parts` takes the dataclass's fields, in the dataclass's order (#290).

    The order pin is structural -- derived from `dataclasses.fields`
    rather than a second hardcoded list -- so a field added to
    `Equipment` and not to `from_parts` is a red test, not a second
    place to update.
    """
    assert inspect.ismethod(Equipment.from_parts)
    assert Equipment.from_parts.__self__ is Equipment

    params = list(inspect.signature(Equipment.from_parts).parameters)
    assert params == [f.name for f in dataclasses.fields(Equipment)]


# --- #142: the published shape of the store's streaming reader -----------


def test_get_flattened_instances_is_published_store_api():
    """`SqliteStore.get_flattened_instances` has the shape the docs publish (#142).

    Reachable as `session.store_backend.get_flattened_instances(...)`,
    rendered on the docs site by an unfiltered `::: isocenter.persistence`,
    and named by the 0.9.1 CHANGELOG as the migration path for callers
    of the deleted `export_to_parquet`. #142 weighed deleting it and
    kept it. #26 ruled it *documented but internal* (#379): the facade is
    what gets frozen, and `SqliteStore` is a seam behind it -- so it
    stays rendered and stays pinned here, and a change goes through a
    red test and a CHANGELOG entry naming both spellings rather than a
    2.0. A characterization pin, green today, same class as
    `test_the_page_size_default_is_not_a_public_knob`: a renamed or
    reordered parameter is an API change and goes through a red test.
    `page_size` is in the same tier as the method, by the same ruling.
    """
    params = list(inspect.signature(
        SqliteStore.get_flattened_instances).parameters)

    assert params == ["self", "patient_ids", "instance_uids", "page_size"]


# --- #678: the two write doors read `patient_ids` the same way ----------
#
# `export_folder_names` answers "where does the export write" for both
# doors and `export_stamp_attributes` answers "what does it stamp".
# `patient_ids` is the third question -- *who* does it write -- and it
# was answered twice, differently: `_export_dicom` read `is not None`
# and `WfdbExporter.export` read the argument's truthiness, so an empty
# container meant "nobody" on one door and "everybody" on the other
# (#678). These tests are about the doors agreeing, which is why they
# live here rather than in `tests/test_wfdb_privacy.py` where the
# two-patient waveform fixture does; the wfdb half of #678 is pinned
# there as well, against the audit row.

_COH_A, _COH_B = "COH-A", "COH-B"


def _two_patient_waveform_session(tmp_path, name):
    """One session, two patients, one waveform-bearing instance each.

    Waveform-bearing so the same store can be exported through both
    doors: the `wfdb` exporter writes only waveform instances, and the
    `dicom` exporter writes any instance.

    Deliberately not anonymized -- `record_name_for` builds the wfdb
    record name from `patient.patient_id`, so leaving the ingested ids
    alone keeps these assertions about the filter rather than about
    which pseudonym anonymization happened to mint.
    """
    from scripts.generate_waveform_test_data import write_fixture

    source = tmp_path / f"src_{name}"
    source.mkdir()
    write_fixture(str(source / "a.dcm"), num_samples=64,
                  patient_id=_COH_A, patient_name="Alpha^Ann")
    write_fixture(str(source / "b.dcm"), num_samples=64,
                  patient_id=_COH_B, patient_name="Beta^Bob")

    session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
    session.ingest(str(source))
    assert {p.patient_id for p in session.store.patients} == {_COH_A, _COH_B}, (
        "the fixture did not ingest both patients; every assertion below "
        "would pass vacuously on a one-patient store")
    return session


def _owner_by_uid(session):
    return {instance.sop_instance_uid: patient.patient_id
            for patient in session.store.patients
            for study in patient.studies
            for series in study.series
            for instance in series.instances}


def _patients_written(session, folder, fmt, **options):
    """Which patients reached disk, read off what each door reports.

    Per format, because the two doors return different shapes: the wfdb
    exporter returns `.hea` paths whose basenames start with the patient
    id, and the dicom exporter returns an `ExportSummary` whose
    `written_uids` are mapped back through the graph. `show_progress`
    goes to the dicom door only -- the wfdb door raises `TypeError` for
    it, correctly, since #410.
    """
    owner = _owner_by_uid(session)
    if fmt == "dicom":
        options = {**options, "show_progress": False}
    result = session.export(str(folder), format=fmt, **options)
    if fmt == "dicom":
        return {owner[uid] for uid in result.written_uids}
    names = {os.path.basename(path) for path in result}
    return {pid for pid in set(owner.values())
            if any(name.startswith(f"{pid}_") for name in names)}


@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_only_none_means_every_patient_on_both_export_formats(tmp_path, fmt):
    """`patient_ids=[]` writes nothing and `None` writes everyone (#678).

    The coherence half of #678, and the assertion that would have caught
    it when #142 fixed the same defect on
    `SqliteStore.get_flattened_instances`: three of the four readers of
    an empty `patient_ids` in the package meant "nobody" and the fourth
    meant "everybody". Measured on 0.9.8, `format="wfdb"` with
    `patient_ids=[]` wrote both patients' records while `format="dicom"`
    with the same argument wrote nothing.

    Both halves are asserted per format, because "nothing was written"
    is also what a door broken for every argument produces.
    """
    with _two_patient_waveform_session(tmp_path, f"none_{fmt}") as session:
        everyone = _patients_written(session, tmp_path / f"all_{fmt}", fmt,
                                     patient_ids=None)
        nobody = _patients_written(session, tmp_path / f"empty_{fmt}", fmt,
                                   patient_ids=[])

    assert everyone == {_COH_A, _COH_B}, (
        f"format={fmt!r} with patient_ids=None wrote {sorted(everyone)}; "
        "None is the one spelling of every patient")
    assert nobody == set(), (
        f"format={fmt!r} with patient_ids=[] wrote {sorted(nobody)}; an "
        "empty container is a filter that selected nobody, and a caller "
        "whose cohort query came back empty got the whole cohort (#678)")


@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_a_generator_of_patient_ids_selects_by_id_not_by_store_order(
        tmp_path, fmt):
    """An iterator must not be consumed by the first membership test.

    `patient.patient_id not in patient_ids` exhausts a generator on the
    first patient it walks, so every later patient is compared against
    an empty iterator. Measured on 0.9.8 on both doors: a generator
    yielding the *second* patient in store order exported **nothing**,
    and one yielding the first exported that patient -- so which
    patients survived depended on the order the store happened to hold
    them in, not on the ids the caller named.

    The second patient is the discriminating case; a generator yielding
    the first passes today by accident and proves nothing. Both doors
    normalise through `io_handlers.normalize_patient_id_subset`, which
    is why this is parametrised over the two formats rather than written
    twice.
    """
    with _two_patient_waveform_session(tmp_path, f"gen_{fmt}") as session:
        second = _patients_written(session, tmp_path / f"gen2_{fmt}", fmt,
                                   patient_ids=(x for x in [_COH_B]))
        both = _patients_written(session, tmp_path / f"genboth_{fmt}", fmt,
                                 patient_ids=iter([_COH_A, _COH_B]))

    assert second == {_COH_B}, (
        f"format={fmt!r} with a generator yielding {_COH_B!r} wrote "
        f"{sorted(second)}; the membership test consumed the iterator "
        "before it reached that patient")
    assert both == {_COH_A, _COH_B}, (
        f"format={fmt!r} with an iterator of both ids wrote "
        f"{sorted(both)}; the first membership test ate the rest")


@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_a_bare_string_patient_ids_names_one_patient_on_both_formats(
        tmp_path, fmt, caplog):
    """A bare `str` selects exactly that id, and never a substring match.

    `"COH-A" in "COH-ACOH-B"` is True, so a caller who wrote a string
    where a list was meant got a fuzzy match that looked like it worked.
    Measured on 0.9.8, identically on both doors: `patient_ids="COH-A"`
    exported A, `patient_ids="COH-ACOH-B"` exported **both** patients,
    and a common prefix exported none.

    Chosen over a refusal because the write path warns and writes the
    closest honest output rather than refusing: one id is the only
    reading of a bare string, and it is the reading the caller meant.
    The warning is the other half -- a caller who passed the wrong type
    hears about it even though the export succeeded. `bytes` is refused
    instead; `test_bytes_as_patient_ids_is_refused_on_both_formats`
    says why.
    """
    with _two_patient_waveform_session(tmp_path, f"str_{fmt}") as session:
        # Inside the session, not around its construction:
        # `DicomSession()` resets the `isocenter` logger's handlers, and
        # a level set before that is set on a logger the session
        # replaces the handlers of.
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            one = _patients_written(session, tmp_path / f"str1_{fmt}", fmt,
                                    patient_ids=_COH_A)
        concatenated = _patients_written(
            session, tmp_path / f"str2_{fmt}", fmt,
            patient_ids=_COH_A + _COH_B)

    assert one == {_COH_A}, (
        f"format={fmt!r} with patient_ids={_COH_A!r} wrote "
        f"{sorted(one)}; a bare string names exactly one patient id")
    assert concatenated == set(), (
        f"format={fmt!r} with patient_ids={_COH_A + _COH_B!r} wrote "
        f"{sorted(concatenated)}; no patient carries that id, and a "
        "substring match handed the caller patients they never named")
    warned = [record.getMessage() for record in caplog.records
              if record.levelno >= logging.WARNING
              and "patient_ids" in record.getMessage()]
    assert warned, (
        "a bare string passed as `patient_ids` was read as one id and "
        f"logged nothing about it; records were {caplog.messages}")


@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_bytes_as_patient_ids_is_refused_on_both_formats(tmp_path, fmt):
    """`bytes` is refused, because best-effort would select nobody.

    A `str` can be read as one patient id. `b"COH-A"` cannot: every
    `patient_id` in the graph is a `str`, so wrapping the bytes would
    silently select **no** patient and report a clean zero export --
    exactly the silence #678 is about. There is no encoding to decode it
    under either. Measured on 0.9.8 both doors already raised
    `TypeError`, but from inside the walk and with the message
    `a bytes-like object is required, not 'str'`, which names neither
    the option nor the mistake; the refusal now names both and is raised
    before anything is written.
    """
    with _two_patient_waveform_session(tmp_path, f"bytes_{fmt}") as session:
        with pytest.raises(TypeError) as caught:
            _patients_written(session, tmp_path / f"bytes_{fmt}", fmt,
                              patient_ids=b"COH-A")

    message = str(caught.value)
    assert "patient_ids" in message, (
        f"format={fmt!r} refused bytes with {message!r}, which does not "
        "name the option the caller got wrong")
    assert "bytes" in message, (
        f"format={fmt!r} refused bytes with {message!r}, which does not "
        "name the type it refused")

"""The public API must offer one way to do each thing.

Pre-1.0 cleanup: duplicate export layouts, duplicate sanitizers, and dead
parameters are removed rather than deprecated.
"""
import inspect

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

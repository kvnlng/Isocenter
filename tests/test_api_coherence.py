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

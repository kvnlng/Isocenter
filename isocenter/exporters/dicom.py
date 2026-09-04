"""Built-in DICOM export format.

A thin adapter over the existing `DicomExporter` so the established
export path keeps its exact behavior while gaining a registry entry.
"""
from . import Exporter, register


class DicomFormatExporter(Exporter):
    """Writes cleaned DICOM files, preserving the legacy export behavior."""

    def export(self, session, folder: str, **options):
        """Delegate to the session's existing DICOM export implementation.

        Returns:
            io_handlers.ExportSummary: what `_export_dicom` returns on
                every path, including its empty-plan early return. It
                raises `io_handlers.ExportError` when zero of N planned
                instances reached disk and at least one failed.

        Unannotated, like `Exporter.export` since #191. This method
        carried `-> List[str]` while returning `None`, and leaving it
        there once `_export_dicom` began returning an `ExportSummary`
        would restate the same wrong promise one layer down from the
        base class that dropped it. `wfdb.py` keeps its `List[str]`
        because that one is true.
        """
        return session._export_dicom(folder, **options)


register("dicom", DicomFormatExporter)

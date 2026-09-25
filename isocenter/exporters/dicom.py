"""Built-in DICOM export format.

A thin registry adapter over `DicomSession._export_dicom`, which does all
the work.
"""
from . import Exporter, register


class DicomFormatExporter(Exporter):
    """Writes cleaned DICOM files through the session's DICOM export path."""

    def export(self, session, folder: str, **options):
        """Delegate to the session's existing DICOM export implementation.

        Returns:
            io_handlers.ExportSummary: what `_export_dicom` returns on
                every path, including its empty-plan early return. It
                raises `io_handlers.ExportError` when zero of N planned
                instances reached disk and at least one failed.

        Unannotated, like `Exporter.export`: the return is an
        `ExportSummary`, not the `List[str]` `wfdb.py` returns.
        """
        return session._export_dicom(folder, **options)


register("dicom", DicomFormatExporter)

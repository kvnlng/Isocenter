"""Built-in DICOM export format.

A thin adapter over the existing `DicomExporter` so the established
export path keeps its exact behavior while gaining a registry entry.
"""
from typing import List

from . import Exporter, register


class DicomFormatExporter(Exporter):
    """Writes cleaned DICOM files, preserving the legacy export behavior."""

    def export(self, session, folder: str, **options) -> List[str]:
        """Delegate to the session's existing DICOM export implementation."""
        return session._export_dicom(folder, **options)


register("dicom", DicomFormatExporter)

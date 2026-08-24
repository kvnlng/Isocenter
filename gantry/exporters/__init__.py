"""Export format registry.

Each exporter turns a `DicomSession`'s in-memory object graph into files
on disk in one output format. Formats register themselves here and are
selected via `DicomSession.export(folder, format=...)`.
"""
from typing import Any, Dict, List

_REGISTRY: Dict[str, Any] = {}


class Exporter:
    """Interface every export format implements.

    Implementations must not mutate the session's object graph — export is
    a read operation over already-de-identified data.
    """

    def export(self, session, folder: str, **options) -> List[str]:
        """Write the session to `folder`.

        Args:
            session (DicomSession): The active session.
            folder (str): Output directory. Created if absent.
            **options: Format-specific options.

        Returns:
            List[str]: Paths written.
        """
        raise NotImplementedError


def register(name: str, exporter_cls) -> None:
    """Register an export format under `name`.

    Raises:
        TypeError: If `exporter_cls` has no `export` attribute.
    """
    if not hasattr(exporter_cls, "export"):
        raise TypeError(
            f"{exporter_cls!r} cannot be registered as the {name!r} exporter: "
            "it has no 'export' method.")
    _REGISTRY[name] = exporter_cls


def get_exporter(name: str):
    """Instantiate the exporter registered under `name`.

    Raises:
        ValueError: If no such format is registered.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise ValueError(
            f"Unknown export format {name!r}. Available formats: {known}.")
    return cls()


def available_formats() -> List[str]:
    """Return the registered format names, sorted."""
    return sorted(_REGISTRY)


from . import dicom, wfdb  # noqa: E402,F401  (registers the built-in formats)

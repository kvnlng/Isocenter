"""Export format registry.

Each exporter turns a `DicomSession`'s in-memory object graph into files
on disk in one output format. Formats register themselves here and are
selected via `DicomSession.export(folder, format=...)`.

**Provisional until 1.1** (#527). `Exporter`, `register`, `get_exporter`
and `available_formats` are documented but internal (tier 2): 1.1 may
replace them rather than extend them, with a CHANGELOG entry naming both
spellings. A third-party exporter runs behind none of the export gates,
which all live inside the two built-in formats, and each of its runs
writes one `WARNING` audit row so the report grades `REVIEW_REQUIRED`.
`docs/api/exporters.md` is the plugin author's page; #783 is the 1.1 work.
"""
from typing import Any, Dict, List

_REGISTRY: Dict[str, Any] = {}


class Exporter:
    """Interface every export format implements.

    Provisional until 1.1 (#527): this interface may be replaced, not
    extended, in 1.1. A plugin written against 1.0 pins
    `isocenter>=1.0,<1.1`.

    Implementations must not mutate the session's object graph -- export is
    a read operation over already-de-identified data. For a class other
    than the two built-ins, that is the whole of the boundary: `export()`
    applies none of the built-ins' gates (burned-in re-audit, the
    configured redaction zones, the drop of nested icons that may show
    redacted pixels, the filter that writes only `gggg,eeee` attribute keys,
    identity disclosure, de-identification markers, owner stamps, `EXPORT`
    and `DATA_LOSS` rows) before or after calling it, and writes one
    `WARNING` audit row saying the output is not attested by Isocenter.
    Call `redact()` first, write no nested pixel payload such as an Icon
    Image Sequence `(0088,0200)` item (nothing scans or redacts one), and
    write no `_`-prefixed key from `attributes`: one holds the source SOP
    Instance UID that UID replacement removed.
    """

    def export(self, session, folder: str, **options):
        """Write the session to `folder`.

        Args:
            session (DicomSession): The active session.
            folder (str): Output directory. Created if absent.
            **options (dict): Format-specific options.

        Returns:
            Any: The format's own result object. `dicom` returns an
                `io_handlers.ExportSummary`; `wfdb` returns a `List[str]`
                of paths. **Whatever the shape, it must let a caller
                detect that nothing was written**: an empty list, a zero
                count, a raise.
        """
        raise NotImplementedError


def register(name: str, exporter_cls) -> None:
    """Register an export format under `name`.

    Provisional until 1.1 (#527): this function may be replaced, not
    extended, in 1.1. It checks only that `exporter_cls` has an `export`
    attribute; 1.1 may check more. Registering any class other than the
    two built-ins -- a subclass of one included -- makes each export in
    that format write one `WARNING` audit row, because Isocenter cannot
    attest what the class writes.

    Args:
        name (str): The format name `export(format=...)` selects by.
            Registering over an existing name replaces it.
        exporter_cls (type): A class whose instances have
            `export(session, folder, **options)`.

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


# MUST stay at the bottom of the file, after Exporter/register/get_exporter/
# available_formats are all defined. isocenter/exporters/dicom.py and
# isocenter/exporters/wfdb.py both do `from . import Exporter, register` at
# their own top level, which re-enters this (partially initialized)
# package module. Moving this import to the top of the file -- before
# those names exist -- makes that re-entrant import fail with:
#   ImportError: cannot import name 'Exporter' from partially initialized
#   module 'isocenter.exporters' (most likely due to a circular import)
# There is no isort/flake8/pre-commit config in this repo enforcing
# import order, so a routine "tidy up the imports" pass can silently
# break every export path. Leave it here.
from . import dicom, wfdb  # noqa: E402,F401  (registers the built-in formats)

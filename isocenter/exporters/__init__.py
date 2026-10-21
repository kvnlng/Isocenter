"""Export format registry.

Each exporter writes a session's in-memory object graph to files on
disk, in one output format. Each format registers itself here, and
`session.export(folder, format=...)` selects a format by its name.
"""
from typing import Any, Dict, List

_REGISTRY: Dict[str, Any] = {}


class Exporter:
    """Interface every export format implements.

    Provisional until 1.1. In 1.1, this interface may be replaced, not
    extended. A plugin written against 1.0 pins `isocenter>=1.0,<1.1`.

    An implementation must not change the session's object graph, because
    an export only reads it. For a class other than the two built-ins,
    `export()` applies none of the built-ins' gates before or after it
    calls the class. The Exporter registry page lists those gates under
    "What a third-party exporter receives". `export()` also writes one
    `WARNING` audit row, which says that Isocenter does not attest the
    output.
    """

    def export(self, session, folder: str, **options):
        """Write the session to `folder`.

        Args:
            session (DicomSession): The active session.
            folder (str): The output directory. It is created if it does
                not exist.
            **options (dict): Format-specific options.

        Returns:
            Any: The format's own result object. `dicom` returns an
            `ExportSummary`, and `wfdb` returns a `List[str]`
            of paths. **Whatever the shape, it must let a caller
            detect that nothing was written**, for example by an empty
            list, a zero count or a raise.
        """
        raise NotImplementedError


def register(name: str, exporter_cls) -> None:
    """Register an export format under `name`.

    Provisional until 1.1. This function checks only that `exporter_cls`
    has an `export` attribute, and 1.1 may check more.

    If you register any class other than the two built-ins, each export in
    that format writes one `WARNING` audit row, because Isocenter cannot
    attest what the class writes. A subclass of a built-in is such a class
    too.

    Args:
        name (str): The format name that `export(format=...)` selects by.
            A registration under an existing name replaces the earlier one.
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

    Args:
        name (str): The format name, e.g. `'dicom'` or `'wfdb'`.

    Returns:
        Exporter: A new instance of the registered class.

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
    """Return the registered format names, sorted.

    Returns:
        List[str]: The names, sorted.
    """
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

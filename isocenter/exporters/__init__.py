"""Export format registry.

Each exporter writes an export in one output format. Each format
registers itself here, and `session.export(folder, format=...)` selects a
format by its name. The two built-in formats are `dicom` and `wfdb`. A
plugin implements `Exporter.write`, and is handed, once per instance, the
dataset that the DICOM export would write (#783).
"""
import pickle
import sys
from typing import Any, Dict, List

from ..io_handlers import ExportItem  # noqa: F401  (the contract's item)

_REGISTRY: Dict[str, Any] = {}

#: The names `register()` refuses (#783, owner ruling Q9): the built-in
#: formats'. Otherwise a package imported for another reason could take
#: over the default `session.export(folder)`.
_BUILT_IN_NAMES = frozenset({"dicom", "wfdb"})

#: The options `session.export()` reads itself, for every format that
#: takes them. A plugin's `options` may not name one.
_SESSION_OPTIONS = frozenset({"patient_ids", "subset", "check_burned_in",
                              "check_reversibility", "show_progress"})


class Exporter:
    """Interface an export plugin implements.

    A plugin implements `write`. `session.export()` calls it once per
    instance, in an export worker process. Before that, it runs every
    export gate, as for the `dicom` format, and it hands `write` the
    dataset that the DICOM export would write. Such an export can grade
    `PASS`, and its `EXPORT` audit row names the class.

    The class must be defined at the top level of a module that a new
    process can import, because the worker imports it there. `register()`
    checks that. The class declares its own option names in `options`.

    Documented but internal: a minor release can change this contract,
    with a `Breaking` CHANGELOG entry. 1.1 replaced the 1.0 shape,
    `export(session, folder, **options)`.

    Attributes:
        options (frozenset): The option names this format takes beyond
            the session's own (`patient_ids`, `subset`, `check_burned_in`,
            `check_reversibility`, `show_progress`). Each value must
            pickle, because it goes to the worker.
    """

    options: frozenset = frozenset()

    def write(self, dataset, item, folder: str, **options):
        """Write one instance.

        Args:
            dataset (pydicom.dataset.FileDataset): The instance as the
                DICOM export would write it, before compression. The
                configured zones are applied to a copy of the pixels,
                nested icons are dropped, the owner stamps and the
                de-identification markers are written, and no bookkeeping
                key is in it.
            item (ExportItem): The instance's SOP Instance UID, the
                folder names that the built-in formats write under, and
                the Patient ID that the export carries.
            folder (str): The export folder that `export()` was given.
            **options (dict): The options that this class names in
                `options`.

        Returns:
            Optional[str]: The path of the file that holds this instance,
            or None when nothing is written for it. None is neither a
            write nor a failure.

        Raises:
            Exception: Anything. The instance then fails with one `ERROR`
                audit row, and the export goes on.
        """
        raise NotImplementedError


def _install_built_in(name: str, exporter_cls) -> None:
    """Register a built-in format. Only `dicom.py` and `wfdb.py` call it."""
    _REGISTRY[name] = exporter_cls


def _importable_by_a_worker(exporter_cls) -> bool:
    """Whether a spawned worker can import `exporter_cls` by module and name.

    Asked here, in this process, by the round trip a worker's import makes.
    A class that does not come back as itself (defined in a function, or
    under a `__module__` it is not in) would fail every instance in the
    worker, and so does one in a `__main__` that has no file (the
    interactive prompt, a notebook). Not covered: a module this process
    imports through a `sys.path` entry that a worker does not get.
    """
    try:
        same = pickle.loads(pickle.dumps(exporter_cls)) is exporter_cls
    except Exception:  # pylint: disable=broad-except
        return False
    if same and exporter_cls.__module__ == "__main__":
        return bool(getattr(sys.modules.get("__main__"), "__file__", None))
    return same


def register(name: str, exporter_cls) -> None:
    """Register an export format under `name`.

    `session.export(folder, format=name)` then runs the class's `write`
    once per instance, behind every export gate.

    Args:
        name (str): The format name that `export(format=...)` selects by.
            A registration under an existing name replaces the earlier
            one, except that `dicom` and `wfdb` are refused.
        exporter_cls (type): A subclass of `Exporter` that implements
            `write(dataset, item, folder, **options)` itself, defined at
            the top level of an importable module.

    Raises:
        ValueError: If `name` is `dicom` or `wfdb`, the built-in formats'
            names.
        TypeError: If `exporter_cls` is not a class or does not implement
            `write`. A class written for 1.0, with only
            `export(session, folder, **options)`, is told so. Also if its
            `options` is not a `frozenset` of `str` or names an option
            that `export()` takes itself, or if an export worker process
            could not import it (a class defined in a function, a
            notebook cell or the interactive prompt).
    """
    if name in _BUILT_IN_NAMES:
        raise ValueError(
            f"{name!r} is a built-in export format's name, and cannot be "
            f"registered over (#783). Register {exporter_cls!r} under "
            f"another name.")
    if not isinstance(exporter_cls, type):
        raise TypeError(
            f"{exporter_cls!r} cannot be registered as the {name!r} "
            f"exporter: it is not a class.")
    # `is`, not `==` or `hasattr`: a class that inherits `Exporter.write`
    # implements nothing, and would fail every instance in a worker.
    write = getattr(exporter_cls, "write", None)
    if write is None or write is Exporter.write:
        old = callable(getattr(exporter_cls, "export", None))
        raise TypeError(
            f"{exporter_cls!r} cannot be registered as the {name!r} "
            f"exporter: it does not implement "
            f"write(self, dataset, item, folder, **options)."
            + (" It implements export(session, folder, **options), the "
               "1.0 shape, which 1.1 replaced (#783): an exporter is "
               "handed the dataset the DICOM export would write, one "
               "instance at a time, and never the session." if old else ""))
    options = getattr(exporter_cls, "options", frozenset())
    if (not isinstance(options, frozenset)
            or not all(isinstance(option, str) for option in options)):
        raise TypeError(
            f"{exporter_cls!r} cannot be registered as the {name!r} "
            f"exporter: its options must be a frozenset of str, not "
            f"{options!r}.")
    owned = sorted(options & _SESSION_OPTIONS)
    if owned:
        raise TypeError(
            f"{exporter_cls!r} cannot be registered as the {name!r} "
            f"exporter: its options name {', '.join(map(repr, owned))}, "
            f"which session.export() reads itself.")
    if not _importable_by_a_worker(exporter_cls):
        raise TypeError(
            f"{exporter_cls!r} cannot be registered as the {name!r} "
            f"exporter: an export worker process could not import it. "
            f"Define it at the top level of a module that the worker can "
            f"import, not in a function, a notebook cell or the "
            f"interactive prompt.")
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


# MUST stay at the bottom of the file, after Exporter/_install_built_in/
# register/get_exporter/available_formats are all defined.
# isocenter/exporters/dicom.py and isocenter/exporters/wfdb.py both do
# `from . import Exporter, _install_built_in` at their own top level,
# which re-enters this (partially initialized) package module. Moving
# this import to the top of the file -- before those names exist -- makes
# that re-entrant import fail with:
#   ImportError: cannot import name 'Exporter' from partially initialized
#   module 'isocenter.exporters' (most likely due to a circular import)
# There is no isort/flake8/pre-commit config in this repo enforcing
# import order, so a routine "tidy up the imports" pass can silently
# break every export path. Leave it here.
from . import dicom, wfdb  # noqa: E402,F401  (registers the built-in formats)

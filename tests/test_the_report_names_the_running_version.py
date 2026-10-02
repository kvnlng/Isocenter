"""The compliance report's System Version is the running code's
`isocenter.__version__`, the same name the export's `(0012,0063)` stamp
reads (#806).

The report read `importlib.metadata.version("isocenter")`, which answers
"what is installed under this name". In an editable or `PYTHONPATH`
install that can be another tree's, or stale: measured on b440bca0, one
run's report said `Isocenter v0.9.8` while its exported `(0012,0063)`
said `isocenter/1.0.0rc8`. Two claims from one run about which code
produced it, disagreeing.

An ordinary install has metadata equal to `__version__`, so a test that
does not make the two differ passes with the bug in place. Each test
here patches `importlib.metadata.version` for the name `isocenter` only
(patching every name would also answer pydicom's and numpy's own
version checks).
"""
import ast
import glob
import importlib.metadata
import os
import re
import shutil

import pydicom
import pytest
from pydicom.data import get_testdata_file

import isocenter
from isocenter import Session

INSTALLED = "0.0.1-installed-metadata"

_PACKAGE = os.path.dirname(os.path.abspath(isocenter.__file__))


def _run_pipeline():
    os.makedirs("input")
    shutil.copy(get_testdata_file("CT_small.dcm"), "input/ct.dcm")
    with open("config.yaml", "w", encoding="utf-8") as fh:
        fh.write('privacy_profile: "basic@2026c"\n')
    with Session("s.db") as session:
        session.ingest("input")
        session.load_config("config.yaml")
        session.anonymize(session.audit())
        session.export("out", use_compression=False)
        session.generate_report("report.md")
    with open("report.md", encoding="utf-8") as fh:
        report = fh.read()
    exported = glob.glob("out/**/*.dcm", recursive=True)
    assert len(exported) == 1, exported
    stamp = str(pydicom.dcmread(exported[0])[0x0012, 0x0063].value)
    return report, stamp


def _system_version(report):
    found = re.search(r"\*\*System Version:\*\* Isocenter v(\S+)", report)
    assert found, "the report carries no System Version line"
    return found.group(1)


def test_the_report_names_the_running_version_not_the_installed_one(monkeypatch):
    original = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata, "version",
        lambda name: INSTALLED if name == "isocenter" else original(name))

    report, stamp = _run_pipeline()

    # Absolute first, so the comparison below cannot hold at two wrong values.
    assert _system_version(report) == isocenter.__version__
    assert INSTALLED not in report
    assert stamp.startswith(f"isocenter/{isocenter.__version__};"), stamp
    assert stamp.startswith(f"isocenter/{_system_version(report)};"), stamp


def test_no_installed_metadata_does_not_make_the_report_say_zero(monkeypatch):
    original = importlib.metadata.version

    def _absent(name):
        if name == "isocenter":
            raise importlib.metadata.PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib.metadata, "version", _absent)

    report, _ = _run_pipeline()

    assert _system_version(report) == isocenter.__version__
    assert "v0.0.0" not in report


def _imports_metadata(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.startswith("importlib.metadata") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("importlib.metadata"):
                return True
            if node.module == "importlib" and any(
                    a.name == "metadata" for a in node.names):
                return True
    return False


def test_no_module_reads_the_installed_metadata():
    """The version the package reports is `isocenter._version`'s. A read
    of `importlib.metadata` anywhere in the package is a second answer
    that can disagree with it, which is #806 coming back elsewhere."""
    offenders = []
    for path in sorted(glob.glob(os.path.join(_PACKAGE, "**", "*.py"),
                                 recursive=True)):
        if not path.endswith(".py"):
            continue
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        if _imports_metadata(tree):
            offenders.append(os.path.relpath(path, os.path.dirname(_PACKAGE)))
    assert offenders == [], (
        f"{offenders} import importlib.metadata; read isocenter.__version__ "
        "instead, as the (0012,0063) stamp does (#806)")


def test_the_source_guard_sees_an_import_of_metadata():
    """The guard's own detector, so an empty result above is not a blind one."""
    for src in ("import importlib.metadata",
                "from importlib.metadata import version",
                "from importlib import metadata"):
        assert _imports_metadata(ast.parse(src)), src
    assert not _imports_metadata(ast.parse("import importlib"))

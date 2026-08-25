"""One version, declared once, agreed on everywhere it appears.

The value Isocenter reported at runtime used to come from
`importlib.metadata.version("isocenter")`, which reads *installed*
metadata. In an editable install that had drifted from `setup.py` the
package reported a version it was not -- and that string is stamped into
`annotations.json` as producer provenance, so a wrong version becomes a
wrong claim inside a delivered dataset.

These tests pin the rule rather than the current number: whatever the
version is, every file that states it states the same one, and the
runtime value comes from the source tree rather than from whatever
happens to be installed alongside it.
"""
import pathlib
import re
import subprocess
import sys

import pytest

import isocenter

ROOT = pathlib.Path(__file__).resolve().parent.parent


def declared_in(path: pathlib.Path, pattern: str) -> str:
    """The version a given file declares, or fails the test saying so."""
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.MULTILINE)
    assert match, f"no version declaration matching {pattern!r} in {path.name}"
    return match.group(1)


def test_the_package_reports_the_version_its_source_declares():
    """`isocenter.__version__` comes from the source tree, not the install.

    This is the property the old `importlib.metadata` lookup could not
    provide: it answered "what is installed under this name", which is a
    different question and, in an editable checkout, a different answer.
    """
    declared = declared_in(ROOT / "isocenter" / "_version.py",
                           r'__version__\s*=\s*["\']([^"\']+)["\']')

    assert isocenter.__version__ == declared


def test_setup_py_builds_with_the_same_version_as_the_package():
    """The distribution and the package it contains cannot disagree.

    Asks setuptools what it would build rather than reading the file:
    `setup.py` derives the version now, so the value that matters is the
    one it computes, not the text it contains. A wheel built with one
    version and importing as another is installable, and wrong in a way
    nothing checks at install time.
    """
    built = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT, capture_output=True, text=True, check=True)

    assert built.stdout.strip() == isocenter.__version__


def test_the_citation_file_declares_the_same_version():
    """CITATION.cff is what tooling copies into bibliographies.

    A version mismatch here is not cosmetic: it is a citation pointing at
    a release that does not contain the work being cited.
    """
    assert declared_in(ROOT / "CITATION.cff",
                       r'^version:\s*(\S+)') == isocenter.__version__


def test_the_changelog_documents_the_declared_version():
    """A released version with no changelog entry has no record.

    Only enforced once the version has been released -- an unreleased
    bump sits under [Unreleased] until the release step moves it, which
    the runbook does in the same commit as the bump.
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"[{isocenter.__version__}]" in changelog, (
        f"CHANGELOG.md has no [{isocenter.__version__}] section. If this is "
        "an unreleased bump, move [Unreleased] to it as the release "
        "process describes.")


def test_the_version_is_a_release_number_not_a_placeholder():
    """`0.0.0` was the old fallback for "not installed", and it shipped."""
    assert isocenter.__version__ != "0.0.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+([.-]?\w+)?", isocenter.__version__), (
        f"{isocenter.__version__!r} is not a recognisable version number")

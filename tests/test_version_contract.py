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


def top_heading_problem(changelog: str, version: str):
    """Why `changelog`'s first section heading is wrong, or None.

    Two shapes are right. On `main` the first section is `[Unreleased]`.
    On a release branch there is no `[Unreleased]`; the first section is
    the release the branch carries, and it is the version `_version.py`
    declares. Anything else is the shape a merge of `release/X.Y` into
    `main` leaves behind, which `RELEASING.md` forbids: the merge carries
    the release commit's renamed heading across, so `main` loses
    `[Unreleased]` and its unreleased entries sit under a released
    version's heading, with no conflict to stop it.
    """
    match = re.search(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)
    if match is None:
        return "CHANGELOG.md has no `## [...]` section heading at all"
    top = match.group(1)
    if top == "Unreleased" or top == version:
        return None
    return (f"CHANGELOG.md's first section is [{top}], which is neither "
            f"[Unreleased] (main) nor [{version}], the version "
            "isocenter/_version.py declares (a release branch). A merge of "
            "a release branch into main leaves this shape: forward-port "
            "fixes by cherry-pick instead, as RELEASING.md says")


def test_the_changelog_opens_with_unreleased_or_the_declared_release():
    """`main` keeps `[Unreleased]` on top; a release branch opens with its release.

    A cheap guard, not a complete one. It cannot tell `main` from a
    release branch, so a merge that also carried `_version.py` to the
    same number passes it. What it does catch is the common case: a
    patch release merged forward, whose top heading (`[0.9.9]`) names a
    version `main`'s `_version.py` does not.
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    problem = top_heading_problem(changelog, isocenter.__version__)

    assert problem is None, problem


@pytest.mark.parametrize("changelog, version, right", [
    ("# Changelog\n\n## [Unreleased]\n\n## [0.9.8] - 2026-09-20\n",
     "0.9.8", True),
    ("# Changelog\n\n## [0.9.9] - 2026-09-25\n\n## [0.9.8] - 2026-09-20\n",
     "0.9.9", True),
    ("# Changelog\n\n## [0.9.9] - 2026-09-25\n- fix C\n"
     "## [0.9.8] - 2026-09-20\n- entry B\n", "0.9.8", False),
    ("# Changelog\n\n## [0.9.8] - 2026-09-20\n\n## [Unreleased]\n",
     "0.9.9", False),
    ("# Changelog\n\nNo sections.\n", "0.9.8", False),
], ids=["main", "release-branch", "merged-forward-patch",
        "unreleased-not-on-top", "no-headings"])
def test_the_top_heading_check_tells_the_shapes_apart(changelog, version,
                                                      right):
    """The check above, on the shapes it has to tell apart, including the
    CHANGELOG a merge of `release/0.9` into `main` produced when simulated
    in review of #704."""
    assert (top_heading_problem(changelog, version) is None) is right


def test_the_version_is_a_release_number_not_a_placeholder():
    """`0.0.0` was the old fallback for "not installed", and it shipped."""
    assert isocenter.__version__ != "0.0.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+([.-]?\w+)?", isocenter.__version__), (
        f"{isocenter.__version__!r} is not a recognisable version number")


def test_the_release_runbook_points_at_the_file_that_declares_the_version():
    """The runbook is a file that says where the version lives.

    It said "Bump `version` in `setup.py`" for as long as that was true,
    and stayed saying it after the declaration moved to `_version.py`.
    Following it would edit a file that no longer holds the number, and
    produce a tag the build job rejects. Same drift this module exists to
    catch, one level up: the instruction and the code disagreed.

    The runbook is `RELEASING.md` since the release-branch procedure;
    `docs/developer_guide.md` points at it rather than restating it.
    """
    runbook = (ROOT / "RELEASING.md").read_text(encoding="utf-8")

    assert "isocenter/_version.py" in runbook, (
        "the release runbook does not name the file that declares the "
        "version; if the declaration moved again, move this line with it")
    assert "Bump `version` in `setup.py`" not in runbook, (
        "the release runbook still tells you to bump the version in "
        "setup.py, which no longer declares it")


def test_the_zenodo_license_is_an_id_zenodo_recognises():
    """`.zenodo.json` mints the DOI record's metadata; a bad id is silent.

    Zenodo resolves licences against its own vocabulary, not SPDX, and
    those ids are lowercase: `apache-2.0` returns 200 from
    `/api/vocabularies/licenses/`, `Apache-2.0` returns 404 (checked
    2026-09-06, when the licence changed from `agpl-3.0-or-later` for
    #348). The file once carried the SPDX spelling, which reads correctly
    to everyone except the service that has to match it.

    Checked as a literal rather than over the network: a test that calls
    Zenodo fails when Zenodo is down, which says nothing about this repo.
    If Zenodo changes its vocabulary, this line is what gets updated.
    """
    import json

    deposit = json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))

    assert deposit["license"] == "apache-2.0", (
        "the licence id in .zenodo.json is not the one Zenodo's vocabulary "
        "uses; the deposit would not carry the licence it names")


def test_the_zenodo_deposit_does_not_pin_a_version():
    """The GitHub integration fills `version` in from the release tag.

    Hardcoding it here would give a third place for the number to live and
    a third place for it to drift -- the failure this module exists for.
    """
    import json

    deposit = json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))

    assert "version" not in deposit, (
        "the deposit pins a version; let the release tag supply it")


def test_the_readme_badge_shows_the_doi_the_citation_file_declares():
    """Two copies of one DOI, in the two places people read.

    `CITATION.cff` is what GitHub's "Cite this repository" button and
    reference managers read; the README badge is what a human sees first.
    A DOI is exactly the kind of value that gets updated in one place --
    a version DOI pasted into the badge after a release, say -- and the
    disagreement is invisible until someone cites the wrong record.
    """
    import re

    declared = declared_in(ROOT / "CITATION.cff", r'^doi:\s*"?([^"\s]+)"?')
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    in_readme = set(re.findall(r'10\.5281/zenodo\.\d+', readme))
    ours = {d for d in in_readme if d != "10.5281/zenodo.21077528"}  # Murmur's

    assert ours, "the README names no Isocenter DOI"
    assert ours == {declared}, (
        f"CITATION.cff declares {declared} but the README carries {ours}")


def test_the_declared_doi_is_the_concept_doi():
    """The concept DOI resolves to the latest version; a version DOI freezes.

    Zenodo mints both for every release, one digit apart, so pasting the
    wrong one is a plausible slip rather than a far-fetched one. A
    citation carrying a version DOI silently stops tracking the software
    the moment the next release lands.

    Pinned as a literal because it must not change: a new value here
    means someone replaced the concept record, which is exactly the
    change that should require reading this comment first.
    """
    declared = declared_in(ROOT / "CITATION.cff", r'^doi:\s*"?([^"\s]+)"?')

    assert declared == "10.5281/zenodo.22104298", (
        "the DOI in CITATION.cff is not Isocenter's concept DOI; a version "
        "DOI here would stop the citation following the work")

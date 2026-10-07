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
    same number passes it; `test_this_checkout_is_not_a_release_merged_forward`
    below asks git about that shape (#931). What this one does catch is
    the common case: a patch release merged forward, whose top heading
    (`[0.9.9]`) names a version `main`'s `_version.py` does not.
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


# --- a release merged forward with its version files (#931) ---------------
#
# A merge of `release/X.Y` into `main` that brings `_version.py` along
# leaves a tree every check above accepts: its first heading is its own
# version, as a release branch's is. No tracked fact can tell the two
# apart, because a merge carries every file `main` did not change exactly
# as the release branch has it. What differs is history, so the owner
# ruled (Q6 A, recorded on #935) that git is asked two things of a tree
# that opens its changelog with its own version:
#
# (i)  is that version's tag, where the clone has it, in HEAD's history?
#      A squash of a tagged release onto another line says no.
# (ii) was `isocenter/_version.py` last set on HEAD's own first-parent
#      line? After a merge commit it was set on the side that was merged.
#
# Both hold at every tag from v0.9.6 to v1.0.0rc13 and on both release
# branches (C5's spec, §6.1 D), because a PR into `release/X.Y` merges by
# squash, so a release branch is a line.
#
# **Not caught**, and not catchable from here: a squash of a release
# commit that has no tag yet (neither fact differs); a merge commit whose
# resolution of `_version.py` matches neither parent (the merge itself is
# then the commit that set it); and any clone with no tags and no history,
# where both questions are silent.

_VERSION_LINE = r'__version__\s*=\s*["\']([^"\']+)["\']'


def merged_forward_problem(release_shaped, tag_present, tag_is_ancestor,
                           version_commit_on_first_parent, version="X.Y.Z"):
    """Why this checkout is a release merged onto another line, or None.

    Args:
        release_shaped (bool): The changelog's first heading is the
            version `_version.py` declares.
        tag_present (bool): The clone has the tag `v<version>`.
        tag_is_ancestor (bool): That tag is an ancestor of HEAD.
        version_commit_on_first_parent (bool): The commit that last
            changed `isocenter/_version.py` is on HEAD's first-parent chain.
        version (str): The declared version, for the message.

    Returns:
        Optional[str]: The failure message, or None.
    """
    if not release_shaped:
        # `main`, or a work branch off it. One that took `main`'s release
        # record by a merge commit has its version set off the
        # first-parent chain and is right.
        return None
    opening = (f"This tree declares {version} and opens CHANGELOG.md with "
               f"[{version}], as a release branch does, and ")
    advice = (" RELEASING.md, \"Never merge a release branch into `main`\": "
              "fixes travel by cherry-pick and the release record by a copy "
              "commit.")
    if tag_present and not tag_is_ancestor:
        return (opening + f"v{version} is not in its history: the release's "
                "files were copied onto another line (a squash of the "
                "release branch into `main`?)." + advice)
    if not version_commit_on_first_parent:
        return (opening + "isocenter/_version.py was last set by a commit "
                "merged in from a side branch: a release branch merged "
                "into `main`?" + advice + " If this is a release branch, "
                "it took its release commit, or its target, by a merge "
                "commit; RELEASING prescribes a squash into `release/X.Y` "
                "and a rebase onto it.")
    return None


def _git_at(root, *args):
    # A run started by a git hook must ask about `root`, not the hook's
    # repository.
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    return subprocess.run(["git", "-C", str(root), *args], env=env,
                          capture_output=True, text=True, check=False)


def history_facts(root):
    """What `merged_forward_problem` asks, read from the checkout at `root`.

    Returns:
        Optional[dict]: `version` and the four booleans, or None when
            `root` is not the top of a git work tree with a commit (a
            `git archive` copy, an sdist, a copy inside another checkout).
    """
    root = pathlib.Path(root).resolve()
    top = _git_at(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or pathlib.Path(top.stdout.strip()).resolve() != root:
        return None
    if _git_at(root, "rev-parse", "-q", "--verify", "HEAD^{commit}").returncode:
        return None
    version = declared_in(root / "isocenter" / "_version.py", _VERSION_LINE)
    heading = re.search(r"^## \[([^\]]+)\]",
                        (root / "CHANGELOG.md").read_text(encoding="utf-8"),
                        re.MULTILINE)
    tag = f"refs/tags/v{version}"
    tag_present = _git_at(root, "rev-parse", "-q", "--verify", tag).returncode == 0
    tag_is_ancestor = tag_present and _git_at(
        root, "merge-base", "--is-ancestor", tag, "HEAD").returncode == 0
    # git's default history simplification: at a merge that took the file
    # whole from one parent, the log follows that parent.
    setter = _git_at(root, "log", "-1", "--format=%H", "HEAD", "--",
                     "isocenter/_version.py").stdout.strip()
    chain = _git_at(root, "rev-list", "--first-parent", "HEAD").stdout.split()
    return {
        "version": version,
        "release_shaped": bool(heading) and heading.group(1) == version,
        "tag_present": tag_present,
        "tag_is_ancestor": tag_is_ancestor,
        # No commit holds the file: there is no history to read it from.
        "version_commit_on_first_parent": not setter or setter in chain,
    }


def _problem_at(root):
    facts = history_facts(root)
    assert facts is not None, f"git cannot read {root}"
    return merged_forward_problem(
        facts["release_shaped"], facts["tag_present"], facts["tag_is_ancestor"],
        facts["version_commit_on_first_parent"], facts["version"])


@pytest.mark.parametrize(
    "shaped, present, ancestor, first_parent, right", [
        (True, True, True, True, True),
        (True, False, False, True, True),
        (True, True, False, True, False),
        (True, True, True, False, False),
        (True, False, False, False, False),
        (True, True, False, False, False),
        (False, True, False, False, True),
        (False, False, False, True, True),
    ], ids=["a-release-branch-at-or-past-its-tag",
            "a-release-commit-not-yet-tagged",
            "a-squash-of-a-tagged-release",
            "a-merge-commit",
            "a-merge-commit-in-a-clone-with-no-tags",
            "both",
            "a-work-branch-that-merged-main",
            "main"])
def test_the_merged_forward_check_tells_the_shapes_apart(
        shaped, present, ancestor, first_parent, right):
    """Kills: either condition dropped; the tag asked for where the clone
    has none; the release-shaped gate dropped, which turns a work branch
    that merged `main` red."""
    problem = merged_forward_problem(shaped, present, ancestor, first_parent,
                                     "1.1.0rc1")
    assert (problem is None) is right, problem
    if problem:
        assert "1.1.0rc1" in problem and "Never merge" in problem


_SCRATCH_CHANGELOG = """# Changelog

## [Unreleased]

### Fixed
- **Entry A, merged before the cut.**

## [1.0.0] - 2026-10-26

### Fixed
- **Entry Z.**
"""


class _Line:
    """A scratch repository shaped like this one at a line's first cut:
    `main` at 1.0.0, and `release/1.1` holding a tagged release commit."""

    def __init__(self, path):
        self.path = path
        (path / "isocenter").mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.write(_SCRATCH_CHANGELOG, "1.0.0")
        self.commit("the cut")
        self.git("checkout", "-q", "-b", "release/1.1")
        self.write(_SCRATCH_CHANGELOG.replace(
            "## [Unreleased]", "## [1.1.0rc1] - 2026-11-01"), "1.1.0rc1")
        self.commit("release: 1.1.0rc1")
        self.git("tag", "v1.1.0rc1")
        self.git("checkout", "-q", "main")

    def git(self, *args, check=True):
        done = _git_at(self.path, "-c", "user.name=scratch",
                       "-c", "user.email=scratch@example.invalid",
                       "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                       *args)
        assert not check or done.returncode == 0, done.stderr
        return done.stdout.strip()

    def write(self, changelog, version):
        (self.path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        (self.path / "isocenter" / "_version.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def work_on_main(self, with_an_entry):
        if with_an_entry:
            (self.path / "CHANGELOG.md").write_text(_SCRATCH_CHANGELOG.replace(
                "- **Entry A, merged before the cut.**\n",
                "- **Entry A, merged before the cut.**\n"
                "- **Entry B, merged to main after the cut.**\n"),
                encoding="utf-8")
        (self.path / "later.txt").write_text("later work\n", encoding="utf-8")
        self.commit("feat: later work")

    def changelog(self):
        return (self.path / "CHANGELOG.md").read_text(encoding="utf-8")

    def version(self):
        return declared_in(self.path / "isocenter" / "_version.py", _VERSION_LINE)


@pytest.mark.parametrize("how", ["merge", "squash"])
@pytest.mark.parametrize("main_had", ["an entry", "none"])
def test_a_release_merged_onto_main_is_seen(tmp_path, how, main_had):
    """A line's first release merged into `main`: git merges it silently,
    version files and all. Kills: a check that reads the changelog's lines
    (misses "none"); a check that asks the first-parent line only (misses
    the squash); a check that asks the tag only (misses the merge commit,
    whose history holds the tag)."""
    line = _Line(tmp_path / "repo")
    line.work_on_main(with_an_entry=main_had == "an entry")
    assert _problem_at(line.path) is None      # `main` before the merge
    if how == "merge":
        line.git("merge", "--no-ff", "-m", "merge release/1.1", "release/1.1")
    else:
        line.git("merge", "--squash", "release/1.1")
        line.commit("squash of release/1.1")
    # The shape every older check accepts: no conflict, the release's
    # heading on top, and a version that agrees with it.
    assert line.version() == "1.1.0rc1"
    assert top_heading_problem(line.changelog(), line.version()) is None
    assert ("Entry B" in line.changelog()) is (main_had == "an entry")

    facts = history_facts(line.path)
    assert facts["release_shaped"] and facts["tag_present"]
    # Each way of merging is caught by its own condition.
    assert facts["tag_is_ancestor"] is (how == "merge")
    assert facts["version_commit_on_first_parent"] is (how == "squash")
    problem = _problem_at(line.path)
    assert problem is not None
    assert ("not in its history" in problem) is (how == "squash")
    assert ("from a side branch" in problem) is (how == "merge")

    # With no tags in the clone the merge commit is still seen; the squash
    # is not, and that is the bound the comment above states.
    line.git("tag", "-d", "v1.1.0rc1")
    assert (_problem_at(line.path) is None) is (how == "squash")


def test_a_release_branch_and_its_tag_are_not_merged_forward(tmp_path):
    """Kills: a check that is red on every release-shaped tree."""
    line = _Line(tmp_path / "repo")
    line.work_on_main(with_an_entry=True)
    line.git("checkout", "-q", "release/1.1")
    assert history_facts(line.path) == {
        "version": "1.1.0rc1", "release_shaped": True, "tag_present": True,
        "tag_is_ancestor": True, "version_commit_on_first_parent": True}
    assert _problem_at(line.path) is None
    # A fix on top of the tag, as a release branch has between releases.
    (line.path / "fix.txt").write_text("fix\n", encoding="utf-8")
    line.commit("fix: after the tag")
    assert _problem_at(line.path) is None
    # Detached at the tag, as the publish run checks it out.
    line.git("checkout", "-q", "--detach", "v1.1.0rc1")
    assert _problem_at(line.path) is None
    # A work branch off `main` that took a release record by a merge
    # commit is not release-shaped, and is not asked.
    line.git("checkout", "-q", "main")
    line.git("checkout", "-q", "-b", "record")
    line.write(_SCRATCH_CHANGELOG.replace(
        "## [1.0.0]", "## [1.1.0rc1] - 2026-11-01\n\n## [1.0.0]"), "1.1.0rc1")
    line.commit("release: bring the 1.1.0rc1 record back")
    line.git("checkout", "-q", "main")
    line.git("checkout", "-q", "-b", "work")
    line.git("merge", "--no-ff", "-m", "merge the record", "record")
    facts = history_facts(line.path)
    assert not facts["release_shaped"]
    assert not facts["version_commit_on_first_parent"]
    assert _problem_at(line.path) is None


def test_a_copy_with_no_git_has_no_history_to_ask(tmp_path):
    """A `git archive` copy or an sdist, and such a copy unpacked inside
    another checkout, whose history is not this tree's."""
    assert history_facts(tmp_path) is None
    line = _Line(tmp_path / "repo")
    inner = line.path / "unpacked"
    (inner / "isocenter").mkdir(parents=True)
    assert history_facts(inner) is None


def test_this_checkout_is_not_a_release_merged_forward():
    """The two questions, asked of this checkout. Silent where the clone
    has no tag for the declared version (i) or one commit of history (ii);
    skipped where git cannot read the tree at all."""
    facts = history_facts(ROOT)
    if facts is None:
        pytest.skip(f"{ROOT} is not the top of a git work tree, so there is "
                    "no history to ask")
    # Where git reads the tree the four answers were gathered from it: a
    # check that skipped everywhere would not get here.
    assert facts["version"] == isocenter.__version__
    answers = [facts[key] for key in (
        "release_shaped", "tag_present", "tag_is_ancestor",
        "version_commit_on_first_parent")]
    assert all(isinstance(answer, bool) for answer in answers), facts
    problem = merged_forward_problem(*answers, facts["version"])
    assert problem is None, problem


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

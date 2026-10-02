"""A released section of CHANGELOG.md is the text its release published.

The trap. A bunch PR's branch takes `main`'s release record-back (RELEASING.md,
"Cutting a release", step 8) by merge. The record-back moved entries out of
`[Unreleased]` into a new `## [1.0.0rcN] - date` section; the branch had added
its own entries to `[Unreleased]` in the meantime. Git merges that cleanly and
puts the branch's new entries under the released heading, where they read as
part of a release that never contained them. It happened twice, at rc10 and
rc11, and only the reviews caught it.

The rule this pins, for each `## [X]` heading other than `[Unreleased]` whose
tag `vX` is present:

* **The base** is the section as the newest present tag's CHANGELOG.md holds
  it: the first heading, reading this file top-down (newest first), whose tag
  is present and whose CHANGELOG.md has a `## [X]` heading. Not always `vX`
  itself, because a released section is amended on purpose, and an amendment
  that has shipped is the published record. Measured on `main` at c0552934:
  13 of 31 tagged sections differ from their own tag, and every one equals its
  text in v1.0.0rc11. Two were amended on `main` since 0.9.0 (fd77f8f0 struck
  #765's limit inside `[1.0.0rc1]`; b223f6ab corrected a PS3.5 citation inside
  `[0.9.0]`), each picked into the next cut. Everything at 0.7.0 and before
  was reformatted after its tag (Gantry to Isocenter, blank lines), and
  v0.7.0's own CHANGELOG.md has no `[0.7.0]` heading. No record-back commit has
  edited a section that was already there. The newest tag is read from this
  file's heading order rather than `sort -V` (which puts `v1.0.0` before
  `v1.0.0rc1`) or tag dates (which misorder a patch to an older line).
* **Byte-identical passes.** So does an *in-place rewrite*: the same number of
  lines and the same heading line, some lines changed. That is the shape of
  both amendments above, made before the next cut carries them, and never the
  shape of the defect, which inserts lines. Any added or removed line is red,
  with the diff against the base tag.
* **Skipped**, naming why: the tag `vX` is not present (a fresh clone without
  tags, CI's shallow checkout, or a version never tagged: 0.6.1, 0.5.4, 0.5.1,
  0.4.0), `git` cannot read this tree, or no present tag's CHANGELOG.md holds
  the heading.

What this does not see: a misplaced entry that rewrites an existing line
instead of adding one, and any change to `[Unreleased]`.

Paths are absolute from this file, so the per-test `tmp_path` cwd (#707) does
not matter and the test needs no `repo_root` marker.
"""
import difflib
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = "CHANGELOG.md"
_HEADING = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def sections(text):
    """{version: its section's text, heading line included}, in file order.

    A section runs from its `## [X]` line to the next `## [` line or the end
    of the file. `[Unreleased]` is included; callers drop it.
    """
    starts = [(m.group(1), m.start()) for m in _HEADING.finditer(text)]
    ends = [start for _, start in starts[1:]] + [len(text)]
    return {version: text[start:end]
            for (version, start), end in zip(starts, ends)}


def released_section_problem(current, base):
    """None when `current` may stand for `base`, else why it may not.

    Byte-identical, or an in-place rewrite: the same line count and the
    same heading line. An added or removed line is the defect's shape.
    """
    if current == base:
        return None
    now, then = current.splitlines(), base.splitlines()
    if now and then and now[0] != then[0]:
        return f"its heading changed from {then[0]!r} to {now[0]!r}"
    if len(now) != len(then):
        return (f"it has {len(now)} lines where the release has {len(then)}: "
                "a line was added or removed, not rewritten in place")
    return None


def _git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=False)


@lru_cache(maxsize=None)
def _tag_present(version):
    return _git("rev-parse", "-q", "--verify",
                f"refs/tags/v{version}").returncode == 0


@lru_cache(maxsize=None)
def _sections_at_tag(version):
    shown = _git("show", f"v{version}:{CHANGELOG}")
    return sections(shown.stdout) if shown.returncode == 0 else None


def _working_sections():
    return sections((ROOT / CHANGELOG).read_text(encoding="utf-8"))


def _released_versions():
    return [v for v in _working_sections() if v != "Unreleased"]


@pytest.fixture(scope="module")
def git_reads_this_tree():
    if _git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip(f"git cannot read {ROOT}, so no tag can be compared")


@pytest.mark.parametrize("version", _released_versions())
def test_a_released_section_is_the_text_its_release_published(
        version, git_reads_this_tree):
    if not _tag_present(version):
        pytest.skip(f"tag v{version} is not present here, so [{version}] "
                    "has no released text to compare with")
    current = _working_sections()[version]
    base_tag = next(
        (v for v in _working_sections()
         if v != "Unreleased" and _tag_present(v)
         and version in (_sections_at_tag(v) or {})),
        None)
    if base_tag is None:
        pytest.skip(f"no present tag's {CHANGELOG} has a [{version}] heading")
    base = _sections_at_tag(base_tag)[version]
    assert current.startswith(f"## [{version}]") and base.startswith(
        f"## [{version}]"), "the section parser lost its heading"

    problem = released_section_problem(current, base)
    if problem:
        diff = "".join(difflib.unified_diff(
            base.splitlines(keepends=True), current.splitlines(keepends=True),
            f"v{base_tag}:{CHANGELOG} [{version}]",
            f"working tree {CHANGELOG} [{version}]"))
        pytest.fail(
            f"[{version}] in {CHANGELOG} is not the text v{base_tag} released: "
            f"{problem}. A new entry belongs under [Unreleased]; a branch that "
            "took main's release record-back by merge can have put its own "
            "entries under the released heading.\n" + diff)


def test_the_parser_finds_the_headings_the_file_has():
    """An empty parse would parametrize nothing and pass silently."""
    text = (ROOT / CHANGELOG).read_text(encoding="utf-8")
    found = list(sections(text))
    assert found[0] == "Unreleased"
    assert found == [line[4:line.index("]")]
                     for line in text.splitlines() if line.startswith("## [")]
    assert "1.0.0rc1" in found and "0.1.0" in found
    assert "".join(sections(text).values()) == text[text.index("## ["):]


BASE = ("## [1.0.0rc9] - 2026-10-01\n"
        "\n"
        "### Fixed\n"
        "\n"
        "- **An entry.** Its body.\n"
        "  - **Output:** none.\n"
        "\n")


def test_an_identical_section_passes():
    assert released_section_problem(BASE, BASE) is None


def test_an_entry_merged_under_a_released_heading_is_red():
    misplaced = BASE.replace(
        "### Fixed\n\n",
        "### Fixed\n\n- **A later bunch's entry.** Never released.\n")
    assert "a line was added or removed" in released_section_problem(
        misplaced, BASE)


def test_a_line_taken_out_of_a_released_section_is_red():
    shorter = BASE.replace("  - **Output:** none.\n", "")
    assert "a line was added or removed" in released_section_problem(
        shorter, BASE)


def test_a_changed_heading_is_red():
    redated = BASE.replace("2026-10-01", "2026-10-02")
    assert "its heading changed" in released_section_problem(redated, BASE)


def test_an_amendment_in_place_passes():
    """The shape of fd77f8f0's amendment of [1.0.0rc1]: strike and annotate."""
    amended = BASE.replace("Its body.", "~~Its body.~~ (Closed by #1.)")
    assert amended != BASE
    assert released_section_problem(amended, BASE) is None

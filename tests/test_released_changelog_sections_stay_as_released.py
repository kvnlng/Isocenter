"""A released section of CHANGELOG.md is the text its release published.

The trap. A bunch PR's branch takes `main`'s release record-back (RELEASING.md,
"Cutting a release", step 8) by merge. The record-back moved entries out of
`[Unreleased]` into a new `## [1.0.0rcN] - date` section; the branch had added
its own entries to `[Unreleased]` in the meantime. Git merges that cleanly and
puts the branch's new entries under the released heading, where they read as
part of a release that never contained them. It happened twice, at rc10 and
rc11, and only the reviews caught it.

The rule this pins, for each `## [X]` heading other than `[Unreleased]`:

* **The base** is the section as the newest present tag's CHANGELOG.md holds
  it: the first heading, reading this file top-down (newest first), whose tag
  is present and whose CHANGELOG.md has a `## [X]` heading. Not always `vX`
  itself, because a released section is amended on purpose, and an amendment
  that has shipped is the published record. Measured on `main` at c0552934,
  with every tag present: 32 sections are tagged; 9 differ from their own tag
  (1.0.0rc1, 0.9.0, 0.6.0, 0.5.0, 0.4.1, 0.3.0, 0.2.0, 0.1.0) and v0.7.0's
  own CHANGELOG.md has no `[0.7.0]` heading, 10 in all, and every one equals
  its text in v1.0.0rc11. Two were amended on `main` since 0.9.0 (fd77f8f0
  struck #765's limit inside `[1.0.0rc1]`; b223f6ab corrected a PS3.5
  citation inside `[0.9.0]`), each picked into the next cut; the older ones
  were reformatted after their tags (Gantry to Isocenter, blank lines), though
  not `[0.5.3]` or `[0.5.2]`. No record-back commit has edited a section that
  was already there. The newest tag is read from this file's heading order
  rather than `sort -V` (which puts `v1.0.0` before `v1.0.0rc1`) or tag dates
  (which misorder a patch to an older line). A version never tagged (0.6.1,
  0.5.4, 0.5.1, 0.4.0) has a base all the same, in any later tag.
* **Byte-identical passes.** So does a *correction*: the same number of lines
  and the same heading line, and each changed line one that `CORRECTIONS`
  names, by the sha256 of the line as released and the text it now holds.
  That is the shape of both amendments above, made before the next cut
  carried them (both shipped before the table, so no row is needed for them).
  Any other rewrite in place is red, and so is any added or removed line,
  with the diff against the base tag. Until 2026-10-17 any rewrite in place
  passed; the table narrows that to the lines a correction names, so a
  released section changes only on purpose.
* **Skipped**, naming why: no present tag's CHANGELOG.md holds the heading (a
  fresh clone without tags; CI's checkout was one until #966 made it fetch
  every tag, and there the section being released still has none before
  its tag is pushed), or `git` cannot read this tree. So `test_the_live_check_reads_a_tag` pins, where the tags are present,
  that the check reads one and acts on what it finds: without it, a check that
  skipped everywhere or compared the tree with itself would stay green.

**The second base is `origin/main`** (#956). The tag is the authority, and the
newest released section is the one a clone most often has no tag for: release
tags sit on `release/X.Y`, which `main` does not reach, so `git fetch origin
main` brings the record-back and never its tag. On 2026-10-07 the branch of
#958 (d7e1cc98) merged the rc13 record-back (5f0c1ca5) with no conflict, and
git put 37 lines, three `### Fixed` entries, at the end of `[1.0.0rc13]`.
Run over that merge, the tag check is red where `v1.0.0rc13` is present and
skips `[1.0.0rc13]`, the one section that matters, where it is not: 45 passed,
1 skipped. So each released section is also compared, by the same rule, with
the same section of `refs/remotes/origin/main:CHANGELOG.md`: the record-back
is what put the released text there, and a branch off `main` adds nothing to
it. This needs no tag. What it can and cannot see:

* a worktree shares its checkout's refs, so the merge gate sees `origin/main`
  as the last `git fetch origin main` left it. A stale one that lacks the
  heading skips that section, by name; fetch before trusting a skip;
* at `HEAD` == `origin/main` it compares the file with itself and passes,
  which is right: what is on `main` is the tags' question, asked at the cut;
* a release branch's own new section (a release commit, a fix after it) is
  not on `main` until its record-back, and is skipped by name;
* CI's depth-1 checkout of a tag, a clone whose remote has another name and
  the sdist (no git) have no such ref: every case skips, naming it.

`test_the_main_check_reads_origin_main` pins that this check asks git for
that ref and no other, and acts on the answer.

What this does not see: any change to `[Unreleased]`.

Paths are absolute from this file, so the per-test `tmp_path` cwd (#707) does
not matter and the test needs no `repo_root` marker.
"""
import difflib
import hashlib
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = "CHANGELOG.md"
_HEADING = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)

#: The corrections made to a released section after its release, on
#: purpose: {version: {sha256 of the line as released: the line as it
#: now reads}}. Keyed by hash, so the table never holds the text it
#: corrects. A rewrite in place of any other line is red.
CORRECTIONS = {
    # The SCRUB examples, replaced with invented strings of the same
    # shape (owner ruling of 2026-10-17).
    "1.0.0rc21": {
        '4b9b76fe4e3c7257f7c28f9665408f0997eb215a1defeb69d1f0ca736edac0e9': (
            '  - **Now** a rule `{action: SCRUB, target: [names, dates, '
            'ids]}` keeps the value and removes each span of the kinds '
            '`target` names, with a connecting word directly before it '
            '(`for`, `at`, `by`, `per`, `from`, and a title before a '
            'name), then tidies separators and whitespace. `MR HEPA UNIT '
            'for 912-48-3071` becomes `MR HEPA UNIT`, `MG BIOPSY SPECIMEN '
            '20190314` becomes `MG BIOPSY SPECIMEN`, and `Report for '
            'Breast` is unchanged. The new module '
            '`isocenter/text_scrub.py` holds the rule; it only deletes '
            'characters and never logs.'),
        'a79115732e37d18b8c71ba0be837c2b1dcc2b26e3b7e84785a52896c014b7833': (
            "  - **The kinds.** `dates` and `ids` are #1121's regular "
            "expressions, `dates` without OCR's digit confusables "
            '(`text_kinds.date_spans(ocr=False)`; the burned-in default '
            'is unchanged). A dotted triple whose last field has two '
            'digits (`Version 3.10.21`) reads as a date, by owner ruling '
            'Q8 (a); `0.2.0` and `dcmqi 1.2.4` do not. `names` is the '
            'family, given and middle names of the Person Names the '
            "instance itself holds, at any depth, and its patient's, "
            'matched whole-word: no model (owner ruling Q2 (a)), because '
            "spaCy's `PERSON`, measured on header text, emptied `SAG T1` "
            'and `MAMMO SCREENING BILATERAL` and missed a mixed-case full '
            'name. A name that is also a clinical word (`LONG`) is '
            "removed from that patient's descriptions. `orgs` (owner "
            "rulings Q11 (b), R14) is spaCy's `ORG`, `FAC` and `GPE` on "
            'the value title-cased; it needs the `nlp` extra and '
            '`en_core_web_sm`, and is unavailable on the free-threaded '
            'build, which has no spaCy. It over-removes: measured with '
            'en_core_web_sm 3.8.0 and spaCy 3.8.16, it changed 14 of 47 '
            'typical clinical descriptions on the corpus the design was '
            'measured on (13 on the one the tests carry; 28 read as '
            'written and title-cased together, which does not ship), and '
            'turned `MR HEPA UNIT for 912-48-3071` into `MR UNIT`. The '
            'model is not pinned, so these figures may change; '
            '`docs/configuration.md` lists them.'),
        '8bb3ed31256d07d7c113e58bebbb99f0dcf012ec518dc8c9d42fb790cb19676f': (
            '  - **Not scrubbed:** an identifier shorter than 6 '
            'characters or with fewer than 5 digits (`A1234`), a site the '
            'model does not find (`TWMC`), and words that are not '
            'identifiers. `REMOVE` remains the exact answer.'),
    },
}


def sections(text):
    """{version: its section's text, heading line included}, in file order.

    A section runs from its `## [X]` line to the next `## [` line or the end
    of the file. `[Unreleased]` is included; callers drop it.
    """
    starts = [(m.group(1), m.start()) for m in _HEADING.finditer(text)]
    ends = [start for _, start in starts[1:]] + [len(text)]
    return {version: text[start:end]
            for (version, start), end in zip(starts, ends)}


def released_section_problem(current, base, corrections=None):
    """None when `current` may stand for `base`, else why it may not.

    Byte-identical, or each changed line one `corrections` names: {sha256
    of the line in `base`: the line `current` must hold there}. An added
    or removed line is the defect's shape, and no row permits one.
    """
    if current == base:
        return None
    now, then = current.splitlines(), base.splitlines()
    if now and then and now[0] != then[0]:
        return f"its heading changed from {then[0]!r} to {now[0]!r}"
    if len(now) != len(then):
        return (f"it has {len(now)} lines where the release has {len(then)}: "
                "a line was added or removed, not rewritten in place")
    corrections = corrections or {}
    for number, (was, is_now) in enumerate(zip(then, now), start=1):
        if was != is_now and corrections.get(
                hashlib.sha256(was.encode("utf-8")).hexdigest()) != is_now:
            return (f"line {number} was rewritten in place, and CORRECTIONS "
                    "names no such correction: a released section is "
                    "corrected only through that table")
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


def problem_for(version, working):
    """(base tag, None or the failure message) for `[version]` of `working`.

    `working` is {version: section} for the CHANGELOG under test. The base
    tag is None when no present tag's CHANGELOG.md holds the heading.
    """
    base_tag = next(
        (v for v in working
         if v != "Unreleased" and _tag_present(v)
         and version in (_sections_at_tag(v) or {})),
        None)
    if base_tag is None:
        return None, None
    current, base = working[version], _sections_at_tag(base_tag)[version]
    assert current.startswith(f"## [{version}]") and base.startswith(
        f"## [{version}]"), "the section parser lost its heading"
    problem = released_section_problem(current, base, CORRECTIONS.get(version))
    if problem is None:
        return base_tag, None
    diff = "".join(difflib.unified_diff(
        base.splitlines(keepends=True), current.splitlines(keepends=True),
        f"v{base_tag}:{CHANGELOG} [{version}]",
        f"working tree {CHANGELOG} [{version}]"))
    return base_tag, (
        f"[{version}] in {CHANGELOG} is not the text v{base_tag} released: "
        f"{problem}. A new entry belongs under [Unreleased]; a branch that "
        "took main's release record-back by merge can have put its own "
        "entries under the released heading.\n" + diff)


@pytest.mark.parametrize("version", _released_versions())
def test_a_released_section_is_the_text_its_release_published(
        version, git_reads_this_tree):
    base_tag, failure = problem_for(version, _working_sections())
    if base_tag is None:
        pytest.skip(f"no present tag's {CHANGELOG} has a [{version}] heading, "
                    "so it has no released text to compare with")
    if failure:
        pytest.fail(failure)


def test_the_live_check_reads_a_tag(git_reads_this_tree):
    """The check above reads a tag, compares with it and acts on the answer.

    Each half goes red on a mutant that left every parametrized case green
    (review of #927): `_tag_present` never true (all skip), the base read
    from HEAD (tree compared with itself), the problem dropped.
    """
    # Listed, not asked of `_tag_present`, which is under test here.
    tags = _git("tag", "-l", "v*").stdout.split()
    if "v1.0.0rc10" not in tags:
        pytest.skip("tag v1.0.0rc10 is not present here")
    assert _tag_present("1.0.0rc10") and _tag_present("1.0.0rc1")
    assert not _tag_present("0.6.1"), "0.6.1 was never tagged"

    working = _working_sections()
    # [0.5.0] gained blank lines after v0.5.0 was cut: its own tag's text
    # is not the tree's, so a base read from the tree would pass for it.
    assert _sections_at_tag("0.5.0")["0.5.0"] != working["0.5.0"]
    base_tag, failure = problem_for("0.5.0", working)
    assert base_tag is not None and base_tag != "0.5.0" and failure is None

    misfiled = dict(working)
    misfiled["1.0.0rc10"] = working["1.0.0rc10"].replace(
        "\n### ", "\n- **A later bunch's entry.** Never released.\n\n### ", 1)
    assert misfiled["1.0.0rc10"] != working["1.0.0rc10"]
    base_tag, failure = problem_for("1.0.0rc10", misfiled)
    assert base_tag is not None
    assert failure is not None and "A later bunch's entry" in failure

    # A never-tagged version is checked against a later tag, not skipped.
    base_tag, failure = problem_for("0.6.1", working)
    assert base_tag is not None and failure is None


MAIN = "refs/remotes/origin/main"


@lru_cache(maxsize=None)
def _sections_at_main():
    """`origin/main`'s sections, or None where that ref does not resolve."""
    shown = _git("show", f"{MAIN}:{CHANGELOG}")
    return sections(shown.stdout) if shown.returncode == 0 else None


def main_problem_for(version, working, at_main):
    """(whether `at_main` holds `[version]`, None or the failure message).

    `working` and `at_main` are {version: section}: the CHANGELOG under test
    and `origin/main`'s.
    """
    if version not in at_main:
        return False, None
    current, base = working[version], at_main[version]
    problem = released_section_problem(current, base, CORRECTIONS.get(version))
    if problem is None:
        return True, None
    diff = "".join(difflib.unified_diff(
        base.splitlines(keepends=True), current.splitlines(keepends=True),
        f"origin/main:{CHANGELOG} [{version}]",
        f"working tree {CHANGELOG} [{version}]"))
    return True, (
        f"[{version}] in {CHANGELOG} is not the text origin/main holds: "
        f"{problem}. A new entry belongs under [Unreleased]; a merge of "
        "main that raised no conflict can have put this branch's entries "
        "under the released heading (RELEASING.md, \"Changes land on "
        "`main`\", step 3).\n" + diff)


@pytest.mark.parametrize("version", _released_versions())
def test_a_released_section_is_the_text_main_holds(
        version, git_reads_this_tree):
    at_main = _sections_at_main()
    if at_main is None:
        pytest.skip(f"{MAIN}:{CHANGELOG} cannot be read here (a shallow or "
                    "tag checkout, a remote of another name, a main "
                    "without the file), so there is no main to compare "
                    f"[{version}] with")
    held, failure = main_problem_for(version, _working_sections(), at_main)
    if not held:
        pytest.skip(f"{MAIN}'s {CHANGELOG} has no [{version}] heading: a "
                    "section main does not hold yet (a release branch's "
                    "own, a record-back's), or a stale ref "
                    "(`git fetch origin`)")
    if failure:
        pytest.fail(failure)


def test_the_main_check_reads_origin_main(git_reads_this_tree, monkeypatch):
    """The check above asks git for `origin/main`, and acts on the answer.

    A base read from `HEAD` would leave every case here green on a branch
    whose released sections are `main`'s, the misfiled one included (it
    differs from `HEAD` as well), so what is pinned is the question put to
    git. A reader that never resolves would skip every case.
    """
    # Asked of git directly, not of `_sections_at_main`, which is under test.
    if _git("rev-parse", "-q", "--verify", MAIN).returncode != 0:
        pytest.skip(f"{MAIN} is not present here")
    if _git("cat-file", "-e", f"{MAIN}:{CHANGELOG}").returncode != 0:
        pytest.skip(f"{MAIN} has no {CHANGELOG} (another project's main, "
                    "or one from before the file)")

    asked = []
    real_git = _git

    def recording_git(*args):
        asked.append(args)
        return real_git(*args)

    monkeypatch.setitem(globals(), "_git", recording_git)
    _sections_at_main.cache_clear()
    try:
        at_main = _sections_at_main()
    finally:
        _sections_at_main.cache_clear()
    assert asked == [("show", "refs/remotes/origin/main:CHANGELOG.md")]
    assert at_main is not None

    working = _working_sections()
    # An old main (a fork's, from before 1.0.0rc1) still holds the older
    # headings and is compared on those; one that shares none has nothing
    # to be compared on, which is a skip and not a failure (review of #970).
    newest = next(
        (v for v in working if v in at_main and v != "Unreleased"), None)
    if newest is None:
        pytest.skip(f"{MAIN}'s {CHANGELOG} holds none of this file's "
                    "released headings, so no section can be compared")
    # The shape of 2026-10-07: git put the branch's entries at the end of
    # the newest released section, just above the next heading.
    misfiled = dict(working)
    misfiled[newest] = at_main[newest] + (
        "- **A later bunch's entry.** Never released.\n\n")
    held, failure = main_problem_for(newest, misfiled, at_main)
    assert held
    assert failure is not None and "A later bunch's entry" in failure
    assert main_problem_for(newest, {newest: at_main[newest]},
                            at_main) == (True, None)
    # And the parametrized case itself fails on it, not only its helper.
    monkeypatch.setitem(globals(), "_working_sections", lambda: misfiled)
    with pytest.raises(pytest.fail.Exception, match="A later bunch's entry"):
        test_a_released_section_is_the_text_main_holds(newest, None)

    # A section main does not hold has nothing to be compared with.
    assert main_problem_for("9.9.9", {"9.9.9": "## [9.9.9]\n"},
                            at_main) == (False, None)


def test_every_entry_under_a_released_heading_is_red():
    """Not green by accident: with nothing left under `[Unreleased]`.

    A check that only asked whether `[Unreleased]` held the branch's
    entries, or whether the headings were in order, would pass this file.
    """
    at_main = sections("## [Unreleased]\n\n" + BASE)
    all_misfiled = sections(
        "## [Unreleased]\n\n" + BASE.replace(
            "### Fixed\n\n",
            "### Fixed\n\n- **A later bunch's entry.** Never released.\n"))
    assert all_misfiled["Unreleased"] == at_main["Unreleased"]
    held, failure = main_problem_for("1.0.0rc9", all_misfiled, at_main)
    assert held and "a line was added or removed" in failure


def _parser_problem(text):
    """Assert `sections` parses `text` whole: every heading, every byte."""
    found = list(sections(text))
    assert found == [line[4:line.index("]")]
                     for line in text.splitlines() if line.startswith("## [")]
    assert "1.0.0rc1" in found and "0.1.0" in found
    assert "".join(sections(text).values()) == text[text.index("## ["):]


def test_the_parser_finds_the_headings_the_file_has():
    """An empty parse would parametrize nothing and pass silently."""
    _parser_problem((ROOT / CHANGELOG).read_text(encoding="utf-8"))


def test_the_parser_reads_a_release_commits_file():
    """A release commit renames `[Unreleased]`, and the file still parses.

    The parser test asserted `[Unreleased]` was the first heading, so it
    failed at the 1.0.0rc12 release commit (#929), where the rename is the
    point: every release commit would have been red. Which first heading is
    right, `[Unreleased]` on `main` or the declared version on a release
    branch, is `test_version_contract.top_heading_problem`'s question, not
    the parser's.
    """
    text = (ROOT / CHANGELOG).read_text(encoding="utf-8")
    if "## [Unreleased]\n" in text:
        text = text.replace("## [Unreleased]\n", "## [9.9.9] - 2099-01-01\n", 1)
    # A heading whose bytes drift (a trailing space, CRLF) would make the
    # replace a no-op and this test a copy of the one above.
    assert "Unreleased" not in sections(text)
    _parser_problem(text)


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


def _sha256(line):
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


ENTRY = "- **An entry.** Its body."
AMENDED = "- **An entry.** ~~Its body.~~ (Closed by #1.)"


def test_an_amendment_in_place_that_no_correction_names_is_red():
    """The shape of fd77f8f0's amendment of [1.0.0rc1], strike and annotate,
    is red unless `CORRECTIONS` names it: a released section changes only
    through that table. Kills the in-place rewrite passing on line count."""
    amended = BASE.replace(ENTRY, AMENDED)
    assert amended != BASE
    problem = released_section_problem(amended, BASE)
    assert problem is not None and "line 5" in problem, problem
    assert "CORRECTIONS" in problem, problem


def test_a_correction_the_table_names_passes():
    amended = BASE.replace(ENTRY, AMENDED)
    assert released_section_problem(
        amended, BASE, {_sha256(ENTRY): AMENDED}) is None


def test_a_correction_speaks_for_its_one_line_and_its_one_text():
    """Kills: a row that permits any text on its line, or any line once a
    row matches."""
    table = {_sha256(ENTRY): AMENDED}
    other_text = BASE.replace(ENTRY, AMENDED + " And more.")
    assert released_section_problem(other_text, BASE, table) is not None
    other_line = BASE.replace(ENTRY, AMENDED).replace(
        "**Output:** none.", "**Output:** some.")
    assert released_section_problem(other_line, BASE, table) is not None
    # A row cannot let a line in or out either.
    longer = BASE.replace(ENTRY, AMENDED + "\n- **More.**")
    assert "a line was added or removed" in released_section_problem(
        longer, BASE, table)


def test_the_table_holds_hashes_and_corrected_lines_only():
    """Each key is a sha256 of the line as released, so the table never
    carries the text it corrects."""
    for version, table in CORRECTIONS.items():
        assert table, version
        for key, line in table.items():
            assert re.fullmatch(r"[0-9a-f]{64}", key), (version, key)
            assert "\n" not in line and _sha256(line) != key, (version, key)


def test_each_correction_names_a_line_its_release_published(
        git_reads_this_tree):
    """Every row names a line of its version's own tag, and that line now
    reads as the row says. Fails, never skips, where the tag is present: a
    row that matches nothing would sit there permitting a later edit."""
    working = _working_sections()
    for version, table in CORRECTIONS.items():
        if not _tag_present(version):
            pytest.skip(f"tag v{version} is not present here")
        released = _sections_at_tag(version)[version].splitlines()
        now = working[version].splitlines()
        assert len(now) == len(released), version
        for key, line in table.items():
            at = [i for i, was in enumerate(released) if _sha256(was) == key]
            assert at, f"[{version}]: no line of v{version} hashes to {key}"
            assert all(now[i] == line for i in at), (version, key)

"""A line number this tree cites must exist, and must still hold the code (#310).

This project cites source lines constantly -- in comments, in test
docstrings, and in CLAUDE.md. The citations are load-bearing: five tests
in `tests/test_remediation_invariants.py` are near-identical, and each
one says which of five near-identical `entity.mark_modified()` calls it
defends *by line number*. Naming only the arm would not distinguish them,
and #310 records that naming the arm alone is exactly how the count of
that cluster drifted to three in CLAUDE.md while there were five.

Nothing read any of them. One was already wrong when this file was
written: `isocenter/configuration.py` cited line 523 of
`config_manager.py`, in a file 272 lines long. (Written that way round
on purpose: spelled in this file's own citation grammar it would be
swept, and this guard would be red on its own docstring.)

Three rules, and the reason each stops where it does.

**Rule 1 -- a citation naming a repository file must be in range.**
Grammar: `` `path.py:N` `` and `path.py line N`, with an optional `-M`
end for a range. The path may be wrapped in a **balanced** pair of
backticks, and the whole citation may sit inside one -- but half a pair
is malformed prose and is not a citation (#325). A citation naming a file that is *not*
in this repository is **skipped**: the tree cites pydicom's
`filereader.py`, `filebase.py` and `filewriter.py`, and this guard has no
business grading a third-party line number it cannot see and cannot fix.
Because skipping is the design, the test also asserts that a healthy
number of citations were actually *graded* -- a broken resolver would
otherwise skip everything and report a clean pass, which is the
"a silent skip reads as a pass" failure written down twice already in
this tree (#162, `tests/test_doc_anchors.py`'s fourth deferral).

**Rule 2 -- the content pin.** Grammar: `` `<CODE>` at path.py line N ``
(a backticked code span, then the word "at", then the file -- bare or in
its own balanced backtick pair, #325 -- then the word "line"). The cited line, stripped, must equal `<CODE>` exactly. This is
what #310 actually asks for: an in-range check alone still passes after
someone inserts a line above 201 and every one of the five citations
starts pointing one line high.

The grammar is deliberately narrow, and the boundary was checked against
the prose that already exists. `tests/test_redaction_attestation.py`
writes `` `scan_burned_in_annotations` (`services.py:211`) `` and
``at `services.py:207` `` -- the `:N` spelling, no "line", and the
backticked span names a *symbol* rather than a line of code. Those get
Rule 1 only. Getting this boundary wrong would make the guard red on
correct prose, which is how a guard gets deleted.

**Rule 3 -- the count.** Inserting a *sixth* `entity.mark_modified()`
below line 324 leaves all five existing citations true and only
CLAUDE.md's "five" wrong, and count drift is precisely what happened
before. So: the set of line numbers cited for `entity.mark_modified()` in
`remediation.py` must equal the set of lines in that file whose stripped
text is `entity.mark_modified()`. Set equality on the *numbers*, not a
count of citations -- there are two citations per line today (the test
docstring and CLAUDE.md) and a count comparison would be red forever.
Nothing hardcodes 5.

**Excluded from the sweep: `CHANGELOG.md` and `docs/superpowers/`.** Both
are dated records. CLAUDE.md's Conventions section says a dated spec is
never silently rewritten to match today's code, and a changelog entry
describes the tree as it stood on its date. A stale citation in either is
a fact about history, not a defect, and must not turn CI red -- because
the only way to make it green would be to falsify the record. `build/`
and `dist/` are excluded as build artefacts: they are stale copies of the
package, so grading them reports every defect twice.

**Stated deferral: a bare `line <N>` citation naming no file is not
checked.** `tests/test_remediation_dates.py:86` is one -- it refers to
"a shape worse than the one at line 392" with no file named. Which file
line 392 belongs to could not be determined: the test file itself, the
module under test and `git log -S` all fail to settle it. It is left
exactly as written rather than guessed at, and the gap is recorded here
rather than in a comment nobody would find. A future rule could require
every `line N` to name a file; it would need that citation rewritten
first, by someone who knows what it meant.

No `scripts/mutation_probe.py` `TARGETS` entry -- and #310 suggests the
opposite, so the reason matters. #310 proposes putting this in a file
already covered under `remediation.py`. That would re-run a pure text
check against every mutant of `remediation.py`: zero kill signal, paid on
each one. This file imports no target module, the same argument
`tests/test_documented_api_exists.py` makes for itself.
"""
import functools
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# Directories whose contents are not this tree's live prose.
_EXCLUDED_DIRS = (".git", ".claude", "__pycache__", "build", "dist",
                  ".venv", "site-packages", "node_modules")

# Dated records: see the module docstring. Rewriting a citation in either
# to make this guard green would falsify the record.
_EXCLUDED_PATHS = ("CHANGELOG.md",)
_EXCLUDED_PREFIXES = ("docs/superpowers/",)

# The cited path, optionally wrapped in a **balanced** pair of
# backticks (#325). Markdown prose wraps a filename in code ticks as
# naturally as it writes it bare, and requiring the path bare made the
# backticked spelling an unmarked way to opt a citation out of every
# rule below: not red, not graded, silently trusted. Two such citations
# had drifted 361 lines from the branches they name and nothing in this
# file could see either one (#329).
#
# Two traps, both measured before this was written. The whole group is
# optional rather than the opening backtick alone: a `` (?P<tick>`?) ``
# matching the empty string still counts as "participated" for
# `(?(tick)...)`, so the conditional never fires and the pattern matches
# nothing at all. And the closing backtick is conditional rather than an
# independent `` `? ``, because an independent one accepts *half* a pair
# -- malformed prose -- and being red on writing nobody meant is how a
# guard gets deleted rather than fixed.
_CITED_PATH = r"(?P<tick>`)?(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.py)(?(tick)`)"

# Rule 1: `path.py:N`, `path.py line N`, either optionally ending `-M`.
_FILE_CITATION = re.compile(
    _CITED_PATH + r"(?::|\s+line\s+)(?P<start>\d+)(?:-(?P<end>\d+))?")

# Rule 2: `<code>` at path.py line N. The word "line" is what separates
# this from the `:N` spelling used for symbol references; see the module
# docstring's boundary note.
_CONTENT_CITATION = re.compile(
    r"`(?P<code>[^`\n]+)`\s+at\s+" + _CITED_PATH + r"\s+line\s+(?P<number>\d+)")

MARK_MODIFIED = "entity.mark_modified()"


def _is_excluded(rel):
    parts = pathlib.PurePosixPath(rel).parts
    if any(part in _EXCLUDED_DIRS for part in parts):
        return True
    if rel in _EXCLUDED_PATHS:
        return True
    return rel.startswith(_EXCLUDED_PREFIXES)


@functools.lru_cache(maxsize=None)
def _tracked_paths(root):
    """The paths git tracks under `root`, or `None` (#324).

    Mirrors `tests/test_packaging_contract.py`'s
    `_tracked_paths_in_package()`, down to its treatment of an empty
    result: `None` means "git could not answer", never "nothing is
    tracked". An empty listing is not a valid answer for this
    repository, and a guard that goes green because git is missing is
    the defect that file's docstring names.

    `git ls-files`, not `git ls-tree HEAD`: a tracked file edited but
    not committed is prose this tree owns, and is precisely what this
    guard is for. `-z`, because `core.quotepath` quotes a non-ASCII
    path and the result is compared against paths taken off disk.

    Cached per root: four tests times two walks is otherwise ten
    subprocesses for one unchanging answer.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    tracked = frozenset(part for part in proc.stdout.split("\0") if part)
    return tracked or None


def _is_graded(root, rel):
    """Whether `rel` is prose or source this tree actually carries.

    The tracked set *intersects* the walk rather than replacing it.
    `git ls-files` still lists a file deleted from the working tree, and
    `_lines()` would raise `FileNotFoundError` on it; the walk is what
    keeps the answer to "does this exist" honest.

    When git cannot answer, everything the walk found is graded -- see
    `test_a_tree_git_cannot_answer_for_is_swept_whole`.
    """
    if _is_excluded(rel):
        return False
    tracked = _tracked_paths(root)
    return tracked is None or rel in tracked


def _prose_files(root):
    """Every `.py` and `.md` file in the tree whose citations we grade."""
    found = []
    for pattern in ("*.py", "*.md"):
        for path in root.rglob(pattern):
            rel = path.relative_to(root).as_posix()
            if not _is_graded(root, rel):
                continue
            found.append(path)
    return sorted(found)


def _source_index(root):
    """Basename -> the source files carrying it.

    Kept as a list rather than collapsed to one path so that an ambiguous
    citation can be reported as ambiguous. Resolving it to an arbitrary
    winner would grade a line number against a file the writer did not
    mean.
    """
    index = {}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if not _is_graded(root, rel):
            continue
        index.setdefault(path.name, []).append(path)
    return index


def _resolve(root, index, cited):
    """The file a citation names, `None` if it names none of ours.

    A citation carrying a directory (`isocenter/privacy.py`) is resolved
    as a path first, so a future duplicate basename does not make a
    perfectly unambiguous citation fail.
    """
    direct = root / cited
    if direct.is_file() and _is_graded(
            root, direct.relative_to(root).as_posix()):
        return direct
    return index.get(pathlib.PurePosixPath(cited).name)


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def check_file_citations(root=None):
    """Rule 1 over the tree. Returns `(offenders, graded)`."""
    root = root or REPO
    index = _source_index(root)
    offenders = []
    graded = 0
    for path in _prose_files(root):
        where = path.relative_to(root).as_posix()
        for lineno, text in enumerate(_lines(path), 1):
            for match in _FILE_CITATION.finditer(text):
                target = _resolve(root, index, match.group("path"))
                if target is None:
                    # Third-party, or a file that no longer exists.
                    # Deliberately not graded; see the module docstring.
                    continue
                if isinstance(target, list):
                    if len(target) > 1:
                        offenders.append(
                            f"{where}:{lineno}: {match.group(0)!r} is "
                            "ambiguous -- "
                            + ", ".join(
                                str(p.relative_to(root)) for p in target)
                            + " all match. Cite it with its directory.")
                        continue
                    target = target[0]
                graded += 1
                total = len(_lines(target))
                cited_name = target.relative_to(root).as_posix()
                for number in (match.group("start"), match.group("end")):
                    if number is None:
                        continue
                    if not 1 <= int(number) <= total:
                        offenders.append(
                            f"{where}:{lineno}: cites {match.group(0)!r} "
                            f"but {cited_name} is {total} lines long")
    return offenders, graded


def check_content_citations(root=None):
    """Rule 2 over the tree. Returns `(offenders, checked)`."""
    root = root or REPO
    index = _source_index(root)
    offenders = []
    checked = []
    for path in _prose_files(root):
        where = path.relative_to(root).as_posix()
        for lineno, text in enumerate(_lines(path), 1):
            for match in _CONTENT_CITATION.finditer(text):
                code = match.group("code")
                cited = match.group("path")
                number = match.group("number")
                target = _resolve(root, index, cited)
                if target is None:
                    continue
                if isinstance(target, list):
                    if len(target) != 1:
                        continue
                    target = target[0]
                lines = _lines(target)
                number = int(number)
                cited_name = target.relative_to(root).as_posix()
                checked.append((where, lineno, code, cited_name, number))
                actual = lines[number - 1].strip() if (
                    1 <= number <= len(lines)) else None
                if actual != code:
                    offenders.append(
                        f"{where}:{lineno}: says {code!r} is at "
                        f"{cited_name} line {number}, but that line "
                        f"holds {actual!r}")
    return offenders, checked


def _mark_modified_lines(root=None):
    root = root or REPO
    path = root / "isocenter" / "remediation.py"
    return {n for n, text in enumerate(_lines(path), 1)
            if text.strip() == MARK_MODIFIED}


def test_every_file_citation_is_in_range():
    """A cited line number must exist in the file it names.

    Red when this file was added, on
    `isocenter/configuration.py`, which cited line 523 of
    `config_manager.py` against a 272-line file. That citation is deleted rather than renumbered: what
    it originally pointed at is unrecoverable, and a renumbered guess
    would be a fresh false claim wearing the authority of a citation.
    """
    offenders, graded = check_file_citations()

    # The skip for third-party files is the design, which makes a broken
    # resolver indistinguishable from a clean tree. #299's precedent.
    assert graded >= 10, (
        f"only {graded} citations resolved to a file in this repository; "
        "the resolver is broken and this test would otherwise pass "
        "vacuously (#310)")

    assert not offenders, (
        "these citations name a line that does not exist (#310):\n    "
        + "\n    ".join(offenders))


def test_a_cited_line_still_holds_the_code_the_citation_quotes():
    """`` `code` at file.py line N `` must still be true of line N.

    An in-range check alone survives an insertion above line 201: all
    five `entity.mark_modified()` citations quietly start pointing one
    line high and nothing notices. This is the rule that goes red the
    moment that happens.
    """
    offenders, checked = check_content_citations()

    assert len(checked) >= 5, (
        f"only {len(checked)} content citations found; the grammar has "
        "stopped matching the prose that uses it and this test would "
        "otherwise pass vacuously (#310)")

    assert not offenders, (
        "these citations quote code that is no longer on the line they "
        "name (#310):\n    " + "\n    ".join(offenders))


def test_claude_md_cites_every_mark_modified_call_in_the_checkable_grammar():
    """CLAUDE.md must name each of the five lines checkably, not in prose.

    CLAUDE.md's dirty-tracking paragraph listed the lines as a bare
    comma-separated run of numbers, which no rule above can read: the
    numbers could all shift and the paragraph would stay green while
    every one of them was wrong. The requirement is derived from
    `remediation.py`, not hardcoded -- a sixth call makes this red
    without anyone editing a number here.
    """
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    # `finditer` and named groups, not `findall`: the tuple grew a
    # `tick` member in #325, and positional unpacking would have gone
    # wrong in shape rather than raising.
    cited = {
        int(match.group("number"))
        for match in _CONTENT_CITATION.finditer(text)
        if match.group("code") == MARK_MODIFIED
        and pathlib.PurePosixPath(match.group("path")).name
        == "remediation.py"}

    expected = _mark_modified_lines()
    assert expected, (
        "no `entity.mark_modified()` lines found in "
        "isocenter/remediation.py; the scan is broken (#310)")
    assert cited == expected, (
        "CLAUDE.md must cite every `entity.mark_modified()` call in "
        "`remediation.py` in the checkable "
        "``code` at remediation.py line N`` grammar, so a shifted line "
        "turns a test red rather than leaving the paragraph quietly "
        f"wrong (#310). Cited: {sorted(cited)}; actual calls at: "
        f"{sorted(expected)}")


def test_the_number_of_citations_matches_the_number_of_calls():
    """Every `entity.mark_modified()` call must be cited, and no other.

    Rule 3. The failure this catches is the one Rules 1 and 2 cannot: a
    *sixth* call inserted below line 324 leaves all five existing
    citations true and only the claim that there are five wrong -- which
    is the drift that happened before (#132 said three).

    Set equality on the line numbers, not a count of citations: there
    are two citations per line today (a test docstring and CLAUDE.md),
    so comparing counts would be red forever.
    """
    _, checked = check_content_citations()
    cited = {number for _, _, code, cited_file, number in checked
             if code == MARK_MODIFIED
             and cited_file == "isocenter/remediation.py"}

    assert cited == _mark_modified_lines(), (
        "the set of cited `entity.mark_modified()` lines has drifted "
        "from the set of calls in isocenter/remediation.py -- a call "
        "with no citation is one no test says it defends, and a "
        f"citation with no call is stale (#310). Cited: {sorted(cited)}; "
        f"actual: {sorted(_mark_modified_lines())}")


# --- The checkers' own negative cases ------------------------------------
#
# The fixture module is named `zzz_fixture_mod.py` rather than something
# realistic on purpose: these fixture strings also live as literals in
# THIS file, which the real sweep above reads. A fixture named after a
# real module would be resolved against the real module and graded, and
# the resulting failure would be genuinely confusing to debug.


def _tree(tmp_path, module_lines, prose):
    (tmp_path / "zzz_fixture_mod.py").write_text(
        "\n".join(module_lines) + "\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text(prose, encoding="utf-8")
    return tmp_path


def test_the_check_would_fail_on_an_off_by_one_citation(tmp_path):
    """Rule 2 must name the shifted citation and only the shifted one."""
    _tree(
        tmp_path,
        ["def f():", "    first()", "    second()"],
        "Right: `first()` at zzz_fixture_mod.py line 2.\n"
        "Shifted: `second()` at zzz_fixture_mod.py line 2.\n")

    offenders, checked = check_content_citations(tmp_path)

    assert len(checked) == 2
    assert len(offenders) == 1, offenders
    assert "'second()'" in offenders[0]
    assert offenders[0].startswith("notes.md:2:"), offenders[0]


def test_a_citation_naming_a_file_outside_the_repo_is_skipped(tmp_path):
    """The pydicom carve-out, pinned.

    The tree cites pydicom's `filereader.py:336` and two of its
    siblings. Grading a line number in a package this repository does
    not contain would be red on a dependency bump and unfixable here.
    """
    _tree(tmp_path, ["one"],
          "pydicom parses at filereader.py:336 and writes at "
          "filewriter.py:633.\n")

    offenders, graded = check_file_citations(tmp_path)

    assert (offenders, graded) == ([], 0)


def test_an_out_of_range_citation_of_a_repo_file_is_caught(tmp_path):
    """Rule 1's positive case, so the skip above is not the only path."""
    _tree(tmp_path, ["one", "two"],
          "See zzz_fixture_mod.py:99 and zzz_fixture_mod.py line 2.\n")

    offenders, graded = check_file_citations(tmp_path)

    assert graded == 2
    assert len(offenders) == 1, offenders
    assert "is 2 lines long" in offenders[0]


def test_the_symbol_spelling_is_not_read_as_a_content_pin(tmp_path):
    """`` `name` (`file.py:N`) `` is Rule 1 only.

    That spelling is used throughout `tests/test_redaction_attestation.py`
    to name a *symbol*, not a line of code, and it is correct prose. If
    Rule 2 matched it, the guard would be red on writing that is right,
    which is how a guard gets deleted rather than fixed.
    """
    _tree(tmp_path, ["def helper():", "    pass"],
          "`helper` (`zzz_fixture_mod.py:1`) is defined at "
          "`zzz_fixture_mod.py:1`.\n")

    content_offenders, checked = check_content_citations(tmp_path)
    file_offenders, graded = check_file_citations(tmp_path)

    assert (content_offenders, checked) == ([], [])
    assert (file_offenders, graded) == ([], 2)


def _git(tmp_path, *args):
    """Run git in `tmp_path`, skipping the test if there is no git."""
    try:
        return subprocess.run(["git", *args], cwd=tmp_path,
                              capture_output=True, text=True, check=True)
    except FileNotFoundError:
        pytest.skip("git is not installed; the tracked-set walk cannot "
                    "be exercised")
    return None


def test_an_untracked_file_is_not_graded(tmp_path, monkeypatch):
    """A file git does not track is not this tree's prose (#324).

    Both walks are a bare `rglob`, which grades whatever happens to be
    sitting in the working tree. An untracked scratch `.md` carrying a
    stale citation turns Rule 1 red on writing that is not in this
    repository, and an untracked scratch copy of a module makes
    `_source_index()` report a *correct* citation as ambiguous -- the
    same failure through a second door. Both are "red on writing that
    is right", which this file's own docstring names as how a guard
    gets deleted rather than fixed.

    Staged and never committed, deliberately. A tracked-but-uncommitted
    edit is exactly the state this guard exists to grade, so the
    question has to be `git ls-files` and not `git ls-tree HEAD`: there
    is no HEAD here to read.
    """
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)

    _tree(tmp_path, ["def f():", "    first()"],
          "In range: zzz_fixture_mod.py:2.\n")
    (tmp_path / "notes.md").rename(tmp_path / "tracked.md")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "zzz_fixture_mod.py", "tracked.md")

    (tmp_path / "untracked.md").write_text(
        "Scratch: see zzz_fixture_mod.py:99.\n", encoding="utf-8")

    offenders, graded = check_file_citations(tmp_path)

    assert offenders == [], (
        "an untracked scratch file was graded; a stale citation in a "
        "file this repository does not carry must not turn CI red")
    assert graded == 1, (
        f"expected only the tracked citation to be graded, got {graded}")


def test_a_tree_git_cannot_answer_for_is_swept_whole(tmp_path):
    """No repository -> the bare walk, not a skip (#324).

    An unpacked sdist has no `.git`, and pytest's own `tmp_path` is
    outside any repository on every platform this runs on. Falling back
    to the full walk is the only honest answer: skipping would report a
    clean pass on a tree nothing looked at, which is the failure this
    file already records twice (#162, `tests/test_doc_anchors.py`).

    Characterization, green on both sides of #324 -- the fallback is new
    machinery, and this is what pins that it is a fallback and not a
    skip. It is also what the four fixture tests above quietly depend
    on: if one of them ever goes red on a tracked-set intersection, this
    is the test that says why.
    """
    _tree(tmp_path, ["one", "two"], "Scratch: see zzz_fixture_mod.py:99.\n")

    offenders, graded = check_file_citations(tmp_path)

    assert graded == 1
    assert len(offenders) == 1, offenders
    assert "is 2 lines long" in offenders[0]


def test_a_backticked_path_is_still_content_pinned(tmp_path):
    """`` `code` at `file.py` line N `` is Rule 2, backticks and all (#325).

    Rule 2 required the path *bare*, and Rule 1 required `:` or the word
    `line` to follow `.py` immediately -- so a path wrapped in backticks
    fell through **both**. That spelling is the natural one in Markdown
    prose, and writing it was an unmarked way to opt a citation out of
    every rule this file has: not red, not graded, silently trusted.

    The pair must be balanced. Half a pair -- an opening backtick with
    no closing one -- is malformed prose, and grading it would make this
    guard red on writing nobody meant, which
    `test_the_symbol_spelling_is_not_read_as_a_content_pin` exists to
    prevent for the other spelling.
    """
    _tree(
        tmp_path,
        ["def f():", "    first()", "    second()"],
        "Right: `first()` at `zzz_fixture_mod.py` line 2.\n"
        "Shifted: `second()` at `zzz_fixture_mod.py` line 2.\n"
        "Half a pair: `second()` at `zzz_fixture_mod.py line 3.\n")

    offenders, checked = check_content_citations(tmp_path)

    assert len(checked) == 2, (
        "a backticked path must be graded by Rule 2, and half a pair "
        f"must not be; checked {checked}")
    assert len(offenders) == 1, offenders
    assert "'second()'" in offenders[0]
    assert offenders[0].startswith("notes.md:2:"), offenders[0]


def test_a_backticked_path_is_in_range_checked(tmp_path):
    """Rule 1 was blind to the same spelling (#325).

    A backticked path followed by the word "line" and a number names a
    file and a line and was invisible to every rule -- #329's two stale
    citations are written that way. Widening Rule 2 alone would leave a
    citation carrying no quoted code -- the commonest shape by far --
    still ungraded.
    """
    _tree(tmp_path, ["one", "two"],
          "Out of range: at `zzz_fixture_mod.py` line 99.\n"
          "In range: at `zzz_fixture_mod.py` line 2.\n")

    offenders, graded = check_file_citations(tmp_path)

    assert graded == 2, f"a backticked path must be graded; graded {graded}"
    assert len(offenders) == 1, offenders
    assert "is 2 lines long" in offenders[0]

"""A line number this tree cites must exist, and must still hold the code (#310).

This project cites source lines constantly -- in comments and in test
docstrings. The citations are load-bearing: five tests
in `tests/test_remediation_invariants.py` are near-identical, and each
one says which of five near-identical `entity.mark_modified()` calls it
defends *by line number*. Naming only the arm would not distinguish them,
and #310 records that naming the arm alone is exactly how the count of
that cluster drifted to three in prose while there were five.

Nothing read any of them. One was already wrong when this file was
written: `isocenter/configuration.py` cited line 523 of
`config_manager.py`, in a file 272 lines long. (Written that way round
on purpose: spelled in this file's own citation grammar it would be
swept, and this guard would be red on its own docstring.)

Four rules, and the reason each stops where it does.

**Rule 1 -- a citation naming a repository file must be in range.**
Grammar: `` `path.py:N` `` and `path.py line N`, with an optional `-M`
end for a range. The path may be wrapped in a **balanced** pair of
backticks, and the whole citation may sit inside one (#325). Half a
pair is not a spelling this rule adds: a *trailing* stray backtick
stops the citation matching at all, and a *leading* one is simply read
past, so `` `path.py line 3 `` grades as the bare citation it would be
without it. Both behaviours predate #325 -- the balanced pair is the
only thing that changed -- and Rule 2 below is the rule that refuses
half a pair outright. A citation naming a file that is *not*
in this repository is **skipped**: the tree cites pydicom's
`filereader.py`, `filebase.py` and `filewriter.py`, and this guard has no
business grading a third-party line number it cannot see and cannot fix.
Because skipping is the design, the test also asserts that a healthy
number of citations were actually *graded* -- a broken resolver would
otherwise skip everything and report a clean pass, which is the
"a silent skip reads as a pass" failure written down twice already in
this tree (#162, `tests/test_doc_anchors.py`'s fourth deferral). The
whitespace between the path, the word "line" and the number may contain
a line break, and a `#` comment continuation after it (#346, below).

**Rule 2 -- the content pin.** Grammar: `` `<CODE>` at path.py line N ``
(a backticked code span, then the word "at", then the file -- bare or in
its own balanced backtick pair, #325 -- then the word "line"). The cited
line, stripped, must equal `<CODE>` exactly. This is what #310 actually
asks for: an in-range check alone still passes after someone inserts a
line above 201 and every one of the five citations starts pointing one
line high. As with Rule 1, any of the gaps between the tokens may hold a
line break and a `#` continuation; the code span itself may not wrap.

**Wrapped citations are graded, and reported on the line they start
(#346).** Both scanners used to read one line at a time while both
grammars joined their tokens with `\\s+`, which crosses a newline, so a
citation written across a line break matched the regex and was never
handed to it. A wrap between "at" and the path was range-checked but
never content-checked -- reported in `graded`, looking pinned; a wrap
between the path and "line" was seen by neither rule; and a wrap inside
a `#` comment put a `#` between the tokens that no whitespace class
crosses, so joining the text was not enough on its own. Both scanners
now match the whole file and derive the line from the match offset, the
shape `tests/test_documented_env_vars.py`'s `_names_read` already uses,
and the inter-token gap admits a comment continuation. One known
widening, measured at zero in the tree: a bare path ending one line
followed by "line N" starting the next now reads as a citation even
across a sentence boundary. If that ever fires on prose that is right,
reword the prose -- a sentence shaped like a citation deserves to be one.

**Rule 2b -- a bare citation names the symbol on its line (#535).**
Grammar: `` `symbol` (`path.py:N`) `` -- a backticked span, an opening
parenthesis, then a Rule 1 citation in either spelling. The symbol must
appear on line N of the file, as a substring. This is what closes the
gap Rule 1 left: a citation quoting no code could only be range-checked,
and #535 found one naming lines inside a function it had nothing to do
with, graded clean. A bare citation with neither Rule 2's quoted code nor a
symbol before it is an offender outright, in the ordinary prose form
(`` the teardown is at path.py:N ``): there is nothing to compare its
line against, so it cannot be told from a stale one, and every such
citation in the tree at the time this rule was added had either a
natural symbol or a natural line of code to carry. Ranges (`:N-M`) are
exempt, because a range names a block and a block has no one symbol.

The grammar is deliberately narrow, and the boundary was checked against
the prose that already exists. `tests/test_redaction_attestation.py`
writes `` `scan_burned_in_annotations` (`services.py:N`) `` -- the `:N`
spelling, no "line", and the backticked span names a *symbol* rather
than a line of code. That gets Rule 1 and Rule 2b, never Rule 2: the
symbol is looked for *on* the line, not held equal to it. (The example
spells its number as `N` so that this docstring does not itself cite
line N of `services.py`; the same trick as the comment above `_GAP`.)
Getting this boundary wrong would make the guard red on correct prose,
which is how a guard gets deleted.

**Rule 3 -- the count.** Inserting a *sixth* `entity.mark_modified()`
below line 324 leaves all five existing citations true and only a
summary "five" elsewhere wrong, and count drift is precisely what
happened before. So: the set of line numbers cited for
`entity.mark_modified()` in `remediation.py` must equal the set of lines
in that file whose stripped text is `entity.mark_modified()`. Set
equality on the *numbers*, not a count of citations, so that a second
citation of the same line cannot make this red forever. Nothing
hardcodes 5.

**Excluded from the sweep: `CHANGELOG.md` and `docs/superpowers/`.** Both
are dated records. A dated spec is never silently rewritten to match
today's code, and a changelog entry
describes the tree as it stood on its date. A stale citation in either is
a fact about history, not a defect, and must not turn CI red -- because
the only way to make it green would be to falsify the record. `build/`
and `dist/` are excluded as build artefacts: they are stale copies of the
package, so grading them reports every defect twice.

**Stated deferral: a bare `line <N>` citation naming no file is not
checked.** The phrase `than the one at line 392`
(`tests/test_remediation_dates.py:73`) is one -- it refers to "a shape
worse than the one at line 392" with no file named. Which file line 392
belongs to could not be determined: the test file itself, the module
under test and `git log -S` all fail to settle it. It is left exactly
as written rather than guessed at, and the gap is recorded here rather
than in a comment nobody would find. A future rule could require every
`line N` to name a file; it would need that citation rewritten first,
by someone who knows what it meant.

**Stated deferral, narrowed by Rule 2b: Rule 1 is a range check and
nothing more.** A citation naming a file and a line but quoting no code
can only be graded against the file's length -- there is nothing to
compare the line *against*. So it catches the gross failure and nothing
else: the line-523 citation of a 272-line `config_manager.py` described
at the top of this docstring is the shape it does catch, and #329 found
two that it did not -- 361 lines adrift, on code that had nothing to do
with what they claimed, and graded as fine because the numbers were
still in range. (Both spelled here without the grammar, for the reason
given up there.) Rule 1 still does exactly that and no more --
`test_an_in_range_citation_of_the_wrong_line_is_not_caught` pins it --
but since #535 a citation that stops there is an offender under Rule
2b, so the shape #329 found cannot be written without also writing the
symbol Rule 2b will hold it to.

Rule 2's quoted code and Rule 2b's symbol are what close that gap
between them, and neither can be required of every citation alone: a
range citation names a block, which has no one line of code and no one
symbol, and is exempt from both. Widening any grammar to accept a bare
`:N` is rejected outright; it would match every ratio, timeout and port
number in the tree.

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
# `(?(tick)...)`, so the closing backtick becomes *mandatory* and every
# bare citation -- the great majority of them -- stops matching, leaving
# only the fully ticked spelling. And the closing backtick is
# conditional rather than an independent `` `? ``, because an
# independent one accepts *half* a pair in Rule 2 -- malformed prose --
# and being red on writing nobody meant is how a guard gets deleted
# rather than fixed.
_CITED_PATH = r"(?P<tick>`)?(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.py)(?(tick)`)"

# The whitespace between a citation's tokens, and the one thing a wrap
# inside a `#` comment puts there that `\s+` will not cross (#346). Six
# of the tree's Rule 1 citations sit on comment lines, and a citation
# wrapped in one reads `` `code` at\n# path.py line N `` once joined: a
# newline, a `#`, a space. Measured before this was written: scanning
# the joined text alone grades every wrap *except* that one, which is
# an unmarked opt-out left in a guard whose issue is unmarked opt-outs.
# The `#` is optional and may only follow whitespace, so a stray `#`
# mid-line between two tokens (`` `x` # at path.py line N ``) matches
# too -- harmless in Markdown, and measured against the live tree the
# widening added no match to any prose that existed before it.
# The code span itself stays `[^`\n]+`: a code span cannot wrap.
# (The examples in this comment spell the number as `N` so that they
# are not themselves citations; see the module docstring's own trick.)
_GAP = r"\s+(?:#[ \t]*)?"

# Rule 1: `path.py:N`, `path.py line N`, either optionally ending `-M`.
_FILE_CITATION = re.compile(
    _CITED_PATH + r"(?::|" + _GAP + r"line" + _GAP + r")"
    r"(?P<start>\d+)(?:-(?P<end>\d+))?")

# Rule 2: `<code>` at path.py line N. The word "line" is what separates
# this from the `:N` spelling used for symbol references; see the module
# docstring's boundary note.
_CONTENT_CITATION = re.compile(
    r"`(?P<code>[^`\n]+)`" + _GAP + r"at" + _GAP + _CITED_PATH
    + _GAP + r"line" + _GAP + r"(?P<number>\d+)")

# Rule 2b: the symbol a bare citation stands beside. `` `sym` (`path.py:N`)
# `` -- a backticked span, an opening parenthesis, then the citation.
# Matched against the text *ending* where Rule 1's match starts, so the
# `(` must be the last thing before it: no gap is admitted there, on
# purpose, because a stray `(` a sentence earlier must not lend its
# symbol to a citation it does not enclose. The optional backtick is the
# one that wraps a whole `` `path.py:N` `` citation (#325): Rule 1's
# `tick` group takes only a backtick that closes right after the path,
# so for the whole-citation wrap the match starts *inside* it.
_SYMBOL_PREFIX = re.compile(r"`(?P<symbol>[^`\n]+)`" + _GAP + r"\(`?$")

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
    """The *target* side of every check: `lines[number - 1]` and the
    Rule 1 total both come from here. `splitlines()`, not a count of
    `\\n` -- a file without a trailing newline would otherwise be one
    line short."""
    return path.read_text(encoding="utf-8").splitlines()


def _citations(path, pattern):
    """Every match of `pattern` in `path`, with the line it starts on.

    Matched over the file's whole text rather than line by line (#346).
    Both grammars join their tokens with whitespace that crosses a
    newline, so a citation written across a line break matched the
    regex and was never handed to it: the per-line sweep could not see
    a citation that no single line contained. That is the same trap
    `tests/test_documented_env_vars.py`'s `_names_read` closes for read
    calls, in the same words -- invisible is the one answer a guard must
    never give -- and this is the same shape: whole text, line number
    derived from the match offset, reported as the line the citation
    *starts* on.
    """
    text = path.read_text(encoding="utf-8")
    for match in pattern.finditer(text):
        yield text.count("\n", 0, match.start()) + 1, match


def check_file_citations(root=None):
    """Rule 1 over the tree. Returns `(offenders, graded)`."""
    root = root or REPO
    index = _source_index(root)
    offenders = []
    graded = 0
    for path in _prose_files(root):
        where = path.relative_to(root).as_posix()
        for lineno, match in _citations(path, _FILE_CITATION):
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
        for lineno, match in _citations(path, _CONTENT_CITATION):
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


def check_symbol_citations(root=None):
    """Rule 2b over the tree. Returns `(offenders, checked)`.

    `checked` lists the symbol-form citations that were held to their
    line; a bare citation with no symbol is an offender and is not in
    it. Ranges are skipped, and so is anything inside a Rule 2 span --
    a content pin's `path.py line N` is Rule 1's and Rule 2's business.
    """
    root = root or REPO
    index = _source_index(root)
    offenders = []
    checked = []
    for path in _prose_files(root):
        where = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        pinned = [(m.start(), m.end())
                  for m in _CONTENT_CITATION.finditer(text)]
        for lineno, match in _citations(path, _FILE_CITATION):
            if match.group("end") is not None:
                continue
            if any(start <= match.start() < end for start, end in pinned):
                continue
            target = _resolve(root, index, match.group("path"))
            if target is None:
                continue
            if isinstance(target, list):
                if len(target) != 1:
                    continue        # Rule 1 reports the ambiguity
                target = target[0]
            cited_name = target.relative_to(root).as_posix()
            number = int(match.group("start"))
            prefix = _SYMBOL_PREFIX.search(text, 0, match.start())
            if prefix is None:
                offenders.append(
                    f"{where}:{lineno}: {match.group(0)!r} quotes no code "
                    "and names no symbol, so nothing can tell it from a "
                    "stale one; write `code` at file line N, or "
                    "`symbol` (file:N) with the symbol on that line (#535)")
                continue
            symbol = prefix.group("symbol")
            lines = _lines(target)
            actual = lines[number - 1] if 1 <= number <= len(lines) else None
            checked.append((where, lineno, symbol, cited_name, number))
            if actual is None or symbol not in actual:
                offenders.append(
                    f"{where}:{lineno}: says `{symbol}` is on "
                    f"{cited_name} line {number}, but that line holds "
                    f"{actual.strip() if actual is not None else None!r}")
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


def test_the_number_of_citations_matches_the_number_of_calls():
    """Every `entity.mark_modified()` call must be cited, and no other.

    Rule 3. The failure this catches is the one Rules 1 and 2 cannot: a
    *sixth* call inserted below line 324 leaves all five existing
    citations true and only the claim that there are five wrong -- which
    is the drift that happened before (#132 said three).

    Set equality on the line numbers, not a count of citations, so
    that a second citation of the same line cannot make this red
    forever.
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


def test_every_bare_citation_names_its_symbol():
    """Rule 2b over the tree: no citation is left with only a range check.

    Red when added, on eleven bare citations, six of them stale (#535):
    every one had either a line of code to quote or a symbol to name,
    and now does. The count floor is the same vacuity guard as Rule 2's.
    """
    offenders, checked = check_symbol_citations()

    assert len(checked) >= 3, (
        f"only {len(checked)} symbol citations found; the grammar has "
        "stopped matching the prose that uses it and this test would "
        "otherwise pass vacuously (#535)")

    assert not offenders, (
        "these citations quote no code and name no symbol, or name a "
        "symbol that is no longer on the line they cite (#535):\n    "
        + "\n    ".join(offenders))


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


def test_an_in_range_citation_of_the_wrong_line_is_not_caught(tmp_path):
    """Rule 1's limit, executable (#329).

    Characterization: green on both sides of #329, because nothing here
    changes behaviour. It states what Rule 1 does *not* do, so that a
    future reader who assumes a citation is pinned because a guard
    exists can see the shape that walks past it. #329's two stale
    citations were exactly this: a number in range, on a line holding
    something else entirely, reported as clean.

    Closing it needs Rule 2's quoted code or Rule 2b's symbol, and the
    module docstring records why neither can be required of every
    citation. This is scoped to `check_file_citations` on purpose: the
    same fixture is an offender under Rule 2b (the test below), which
    is exactly the division of labour.
    """
    _tree(tmp_path,
          ["def f():", "    first()", "    second()"],
          "The teardown is at zzz_fixture_mod.py:2.\n")

    offenders, graded = check_file_citations(tmp_path)

    assert graded == 1, graded
    assert offenders == [], (
        "Rule 1 has started grading content; if that is deliberate, the "
        "module docstring's stated deferral is now wrong")


# --- Rule 2b: the symbol beside a bare citation (#535) ---------------------


def test_a_bare_citation_without_a_symbol_is_an_offender(tmp_path):
    """The shape #535 found: in range, quoting nothing, naming nothing."""
    _tree(tmp_path,
          ["def f():", "    first()", "    second()"],
          "The teardown is at zzz_fixture_mod.py:2.\n")

    offenders, checked = check_symbol_citations(tmp_path)

    assert checked == [], checked
    assert len(offenders) == 1, offenders
    assert "quotes no code and names no symbol" in offenders[0]
    assert offenders[0].startswith("notes.md:1:"), offenders[0]


def test_a_symbol_citation_whose_symbol_is_on_the_line_passes(tmp_path):
    """`` `sym` (`file.py:N`) `` in both Rule 1 spellings, held and clean."""
    _tree(tmp_path,
          ["def helper():", "    return 1"],
          "`helper` (`zzz_fixture_mod.py:1`) and "
          "`return 1` (zzz_fixture_mod.py line 2).\n")

    offenders, checked = check_symbol_citations(tmp_path)

    assert offenders == [], offenders
    assert [c[2:] for c in checked] == [
        ("helper", "zzz_fixture_mod.py", 1),
        ("return 1", "zzz_fixture_mod.py", 2)], checked


def test_a_symbol_citation_whose_symbol_moved_is_caught(tmp_path):
    """The mutant that skips the symbol check is what this kills."""
    _tree(tmp_path,
          ["def helper():", "    return 1", ""],
          "`helper` (`zzz_fixture_mod.py:2`) and "
          "`helper` (`zzz_fixture_mod.py:9`).\n")

    offenders, checked = check_symbol_citations(tmp_path)

    assert len(checked) == 2, checked
    assert len(offenders) == 2, offenders
    assert "says `helper` is on zzz_fixture_mod.py line 2, but that line " \
           "holds 'return 1'" in offenders[0], offenders[0]
    assert "line 9, but that line holds None" in offenders[1], offenders[1]


def test_a_range_citation_is_not_read_by_rule_2b(tmp_path):
    """A range names a block; a content pin is Rule 2's; neither is graded.

    The mutant that treats a range as bare reports the first citation
    here; the one that ignores Rule 2's spans reports the second.
    """
    _tree(tmp_path,
          ["one", "two", "three"],
          "The block at zzz_fixture_mod.py:1-3, and "
          "`two` at zzz_fixture_mod.py line 2.\n")

    assert check_symbol_citations(tmp_path) == ([], [])


def test_a_symbol_a_sentence_earlier_does_not_vouch_for_a_citation(tmp_path):
    """`` `sym` (see also foo) zzz.py:N `` is bare: the `(` must touch it."""
    _tree(tmp_path,
          ["def helper():", "    return 1"],
          "`helper` (defined early) is at zzz_fixture_mod.py:1.\n")

    offenders, checked = check_symbol_citations(tmp_path)

    assert checked == [], checked
    assert len(offenders) == 1 and "names no symbol" in offenders[0], offenders


# --- Citations written across a line break (#346) -------------------------
#
# Every prose file below starts with an `Intro.` line so that the wrapped
# citation *starts* on line 2 and *ends* on line 3. That is what
# separates a line number derived from `match.start()` (reports 2) from
# one derived from `match.end()` (3) and from a dropped `+ 1` (1); a
# line-1 citation would pass under the last of those by coincidence.


def test_a_content_citation_wrapped_after_at_is_still_content_pinned(
        tmp_path):
    """`` `code` at\\npath.py line N `` is graded by Rule 2 (#346).

    The regex joins its tokens with `\\s+`, which crosses a newline, and
    the scanner handed it one line at a time, so a citation wrapped
    anywhere inside matched the grammar and was never given the chance
    to. Rule 1 still saw this shape -- the path and `line N` sit
    together on the second line -- so it was reported in `graded`,
    looked pinned, and only the content check was missing. Red first:
    `checked == []`.
    """
    _tree(
        tmp_path,
        ["def f():", "    first()", "    second()"],
        "Intro.\n"
        "Wrapped: `second()` at\n"
        "zzz_fixture_mod.py line 2.\n")

    offenders, checked = check_content_citations(tmp_path)

    assert len(checked) == 1, (
        "a content citation wrapped after `at` was not graded by Rule 2; "
        f"checked {checked}")
    assert len(offenders) == 1, offenders
    assert "'second()'" in offenders[0]
    assert offenders[0].startswith("notes.md:2:"), (
        "the reported line must be the one the citation starts on: "
        f"{offenders[0]}")


def test_a_citation_wrapped_before_line_is_graded_by_both_rules(tmp_path):
    """`` path.py\\nline N `` was invisible to Rule 1 *and* Rule 2 (#346).

    Worse than the shape above: with the break between the path and
    the word `line`, neither rule's per-line scan could see a citation
    at all -- not red, not graded, not counted. Red first: Rule 2
    checks 0 and Rule 1 grades 0.
    """
    _tree(
        tmp_path,
        ["def f():", "    first()", "    second()"],
        "Intro.\n"
        "`second()` at zzz_fixture_mod.py\n"
        "line 2.\n"
        "See zzz_fixture_mod.py\n"
        "line 99.\n")

    content_offenders, checked = check_content_citations(tmp_path)
    file_offenders, graded = check_file_citations(tmp_path)

    assert len(checked) == 1, (
        f"Rule 2 did not see the wrapped citation; checked {checked}")
    assert len(content_offenders) == 1, content_offenders
    assert content_offenders[0].startswith("notes.md:2:"), content_offenders
    assert graded == 2, (
        f"Rule 1 did not see the two wrapped citations; graded {graded}")
    assert len(file_offenders) == 1, file_offenders
    assert "is 3 lines long" in file_offenders[0]


def test_a_citation_wrapped_inside_a_comment_is_graded(tmp_path):
    """A wrap inside a `#` comment puts a `#` between the tokens (#346).

    Six of the tree's forty-eight Rule 1 citations sit on comment lines.
    A citation wrapped there reads `` `code` at\\n# path.py line N ``:
    joined, the text between `at` and the path is a newline, a `#` and
    a space, and `\\s+` does not cross the `#`. So scanning the joined
    text is not enough on its own -- this test is red after that fix
    too, and is what earns the comment-continuation gap its place in
    both grammars.

    Written as `notes.py` rather than through `_tree`, because the wrap
    is a Python comment. `_source_index` will also index it as a source
    file; the basename is unique, so that is harmless.
    """
    (tmp_path / "zzz_fixture_mod.py").write_text(
        "def f():\n    first()\n    second()\n", encoding="utf-8")
    (tmp_path / "notes.py").write_text(
        "# Intro.\n"
        "# Wrapped: `second()` at\n"
        "# zzz_fixture_mod.py line 2.\n", encoding="utf-8")

    offenders, checked = check_content_citations(tmp_path)

    assert len(checked) == 1, (
        "a content citation wrapped across a comment continuation was "
        f"not graded; checked {checked}")
    assert len(offenders) == 1, offenders
    assert "'second()'" in offenders[0]
    assert offenders[0].startswith("notes.py:2:"), offenders[0]

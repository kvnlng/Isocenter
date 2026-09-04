"""A documented example that shows its output must produce that output (#304).

`tests/test_documented_api_exists.py` (#234) checks that every method a
fence *names* exists. Nothing checked that a fence which prints something
prints what the page says it prints, and the two claims that motivated
this file are exactly the shape a name check cannot see: README's deleted
zone-discovery section stated `to_dataframe()`'s column list and
`get_density_matrix()`'s return shape, both of which are properties of the
values, not of the names. A wrong one is a claim the code does not back,
which is what this milestone is about.

Four decisions, and the reasoning for each, because each has a cheaper
wrong answer.

**Which fences run: only the ones that opt in.** Most fences in this tree
start from a live `Session` over a real dataset. Running them would need
fixtures, disk and in some cases OCR, and a doc test that is slow or
flaky gets deleted. Opt-in keeps the check free and keeps its blind spot
*stated* rather than discovered.

**How a fence opts in: an HTML comment on the line before it** --
`<!-- runnable: none -->`, or a fixture name in place of `none`. It is
invisible on the rendered site, and, decisively, it leaves the fence's
info string as exactly ```` ```python ````. The obvious alternative, a
marker in the info string (```` ```python exec ````), would stop
`test_documented_api_exists.py`'s `_PYTHON_FENCE` from matching the fence
at all -- so opting a fence *in* to output checking would silently opt it
*out* of name checking. "A silent skip reads as a pass" is already
written down twice in this tree (#162, and `test_doc_anchors.py`'s fourth
deferral); do not reintroduce it through a new door.

That interaction is real and not hypothetical: a `>>>` fence is not valid
Python, so `ast.parse` raises `SyntaxError` on it and the name check's
`except SyntaxError: continue` swallowed the whole fence. #304 fixes that
in the same change, by running a fence through `doctest` first and
parsing the concatenated `.source` of its examples when it has any.

**Verbatim or normalised: stdlib `doctest`, with `NORMALIZE_WHITESPACE`
and `ELLIPSIS`.** No bespoke comparison code, for the reason
`test_doc_anchors.py` gives for using the real `markdown` slugger: a
hand-rolled comparator is a second, private definition of "matches" that
can disagree with the one every reader already knows.

**Cost: in memory, single-digit milliseconds.** A fixture must be
constructible with no disk, no network, no OCR, no spacy and no optional
import. `"none"` -- an empty namespace -- is the only one needed today.

**Marked today: README's 5b discovery fence, and nothing else.** That is
deliberate. It is the fence whose two claims were wrong before, and its
receiver is `result`, which is outside `test_documented_api_exists.py`'s
`ROOTS`, so its calls are checked for *output* here and not for name
resolution there. `docs/ocr.md`'s fences all begin from a live `session`
and stay unmarked; that blind spot is stated here rather than hidden.

No `scripts/mutation_probe.py` `TARGETS` entry, for the reason
`test_documented_api_exists.py`'s docstring gives: this file imports no
target module, so a `TARGETS`-covered home would re-run it against every
mutant of that module for zero kill signal.
"""
import doctest
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent

# Dated design records, not documentation -- same carve-out and same
# reason as `test_documented_api_exists.py`'s.
_EXCLUDED_DOCS = "docs/superpowers/"

# `<!-- runnable: NAME -->` on its own line, immediately before a
# ```python fence. The marker must be the last thing before the fence:
# a blank line between them means the marker is describing something
# else and the fence is not claimed.
_MARKED_FENCE = re.compile(
    r"<!--[ \t]*runnable:[ \t]*([A-Za-z0-9_-]+)[ \t]*-->[ \t]*\n"
    r"```python\n(.*?)```",
    re.DOTALL)

_OPTIONFLAGS = doctest.NORMALIZE_WHITESPACE | doctest.ELLIPSIS


def _no_globals():
    """The empty namespace. A marked fence must import what it uses."""
    return {}


#: Fixture name -> builder returning the fence's globals. A builder must
#: touch no disk, no network and no optional dependency; see the module
#: docstring. Adding one that does is how this check becomes the slow
#: doc test nobody runs.
FIXTURES = {
    "none": _no_globals,
}


def _documentation_files(root=None):
    root = root or REPO
    files = []
    readme = root / "README.md"
    if readme.is_file():
        files.append(readme)
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            if _EXCLUDED_DOCS in path.relative_to(root).as_posix():
                continue
            files.append(path)
    return files


def check_marked_fences(root=None):
    """Run every `<!-- runnable: ... -->` fence under `root`.

    Returns `(failures, marked)`: a list of human-readable failure
    strings and the number of marked fences found. The count is returned
    rather than logged because an empty walk has to be a failure at the
    call site -- a check that grades nothing reports the same "no
    failures" as one that grades everything.
    """
    root = root or REPO
    failures = []
    marked = 0
    for path in _documentation_files(root):
        text = path.read_text(encoding="utf-8")
        for match in _MARKED_FENCE.finditer(text):
            marked += 1
            name, body = match.group(1), match.group(2)
            where = path.relative_to(root).as_posix()
            # Line of the fence body's first line.
            line = text.count("\n", 0, match.start(2)) + 1

            builder = FIXTURES.get(name)
            if builder is None:
                # Not a skip. A marker naming a fixture that does not
                # exist is a fence nobody is checking, wearing the badge
                # of one that is.
                failures.append(
                    f"{where}:{line}: marker names fixture {name!r}, which "
                    f"is not in FIXTURES (have: "
                    f"{sorted(FIXTURES)}) -- the fence is unchecked")
                continue

            examples = doctest.DocTestParser().get_examples(body)
            if not examples:
                failures.append(
                    f"{where}:{line}: marked runnable but contains no `>>>` "
                    "examples, so it asserts nothing -- either write it as "
                    "a doctest or remove the marker")
                continue

            test = doctest.DocTestParser().get_doctest(
                body, builder(), f"{where}:{line}", str(path), line - 1)
            captured = []
            runner = doctest.DocTestRunner(optionflags=_OPTIONFLAGS)
            runner.run(test, out=captured.append, clear_globs=True)
            if runner.failures:
                failures.append(
                    f"{where}:{line}: {runner.failures} of "
                    f"{runner.tries} example(s) printed something other "
                    f"than what the page shows:\n"
                    + "".join(captured))
    return failures, marked


def test_every_marked_documentation_fence_produces_the_output_it_shows():
    """A fence that opts in must print what the page says it prints.

    This is the whole point of the file. It is red the moment a
    documented return value changes shape and the page does not.
    """
    failures, marked = check_marked_fences()

    # An empty walk is a broken check, not a clean bill of health --
    # #299's precedent, and the same assertion
    # `test_documented_api_exists.py` makes about its own file list.
    assert marked >= 1, (
        "no `<!-- runnable: ... -->` fence found in README.md or docs/, "
        "so this check graded nothing and would pass whatever the "
        "documentation claimed (#304)")

    assert not failures, (
        "these documented examples do not produce the output they "
        "show (#304):\n" + "\n".join(failures))


def _write(tmp_path, name, body):
    (tmp_path / "docs").mkdir(exist_ok=True)
    path = tmp_path / "docs" / name
    path.write_text(body, encoding="utf-8")
    return path


def test_the_check_would_fail_on_a_wrong_expected_output(tmp_path):
    """The check must name the wrong fence and only the wrong fence.

    Same shape as `test_doc_anchors.py`'s
    `test_the_check_would_fail_on_a_broken_anchor`: a checker whose
    negative case is never exercised is a checker that might be
    asserting nothing.
    """
    _write(tmp_path, "page.md", (
        "# Page\n\n"
        "<!-- runnable: none -->\n"
        "```python\n"
        ">>> 1 + 1\n"
        "2\n"
        "```\n\n"
        "<!-- runnable: none -->\n"
        "```python\n"
        ">>> 2 + 2\n"
        "5\n"
        "```\n"))

    failures, marked = check_marked_fences(tmp_path)

    assert marked == 2
    assert len(failures) == 1, failures
    assert "printed something other than what the page shows" in failures[0]
    # The right fence's body starts on line 5, the wrong one's on line 11.
    assert failures[0].startswith("docs/page.md:11:"), failures[0]


def test_an_unknown_fixture_name_stops_the_check(tmp_path):
    """A marker naming a fixture that does not exist is an error.

    Skipping it would give a fence the appearance of coverage and none
    of it -- the failure mode this file's docstring names twice.
    """
    _write(tmp_path, "page.md", (
        "<!-- runnable: a_session_with_pixels -->\n"
        "```python\n"
        ">>> 1 + 1\n"
        "2\n"
        "```\n"))

    failures, marked = check_marked_fences(tmp_path)

    assert marked == 1
    assert len(failures) == 1, failures
    assert "a_session_with_pixels" in failures[0]
    assert "not in FIXTURES" in failures[0]


def test_an_inert_marker_is_an_error(tmp_path):
    """A marked fence with no `>>>` in it asserts nothing.

    It is the most likely way this check quietly stops working: someone
    marks an ordinary fence, sees green, and believes its output is
    pinned.
    """
    _write(tmp_path, "page.md", (
        "<!-- runnable: none -->\n"
        "```python\n"
        "session.audit()\n"
        "```\n"))

    failures, marked = check_marked_fences(tmp_path)

    assert marked == 1
    assert len(failures) == 1, failures
    assert "no `>>>` examples" in failures[0]


def test_a_blank_line_between_marker_and_fence_does_not_claim_the_fence(
        tmp_path):
    """The marker must be immediately above the fence it claims.

    Otherwise an HTML comment written as prose about a paragraph would
    silently adopt whatever fence came next, and a later edit inserting
    a fence between them would move the claim without anyone touching
    the marker.
    """
    _write(tmp_path, "page.md", (
        "<!-- runnable: none -->\n"
        "\n"
        "```python\n"
        ">>> 2 + 2\n"
        "5\n"
        "```\n"))

    failures, marked = check_marked_fences(tmp_path)

    assert (failures, marked) == ([], 0)

"""Every tutorial page runs, whole, and shows what it prints (#27).

A tutorial is a claim of a different size from the fences
`tests/test_documented_output_matches.py` (#304) checks: not "this call
returns this shape" but "run these steps over these files and this is
what you get", a real session over real DICOM, from ingest to the
report's grade. #304's contract forbids that (a fixture there touches no
disk and costs milliseconds), so this is a sibling that **reuses its
parts rather than its contract**: the same stdlib `doctest`, with the
same `_OPTIONFLAGS` imported from it, so "matches" has one definition in
this tree.

The conventions a tutorial page follows (T2-T5 copy them; a page that
breaks one is red, never skipped):

1. **Which pages run: every `docs/tutorials/*.md`, whole,** plus any
   other page under `docs/` that carries the header marker below (that is
   how a guide's quick start joins). There is no per-fence opt-in. Code
   the runner would not run is refused, not passed over
   (`_shape_failures`): a fence that is indented (in a list item, an
   admonition or a tab), spelled `~~~` or with four backticks, or left
   unclosed; a `<pre>` block; a line in a `>>>` fence that is not an
   example; and any `doctest:` directive. A run page's fences start at
   column 0 with exactly three backticks. One gap is stated rather than
   closed: a four-space indented code block with no fence is not seen,
   because telling it from list-item prose needs a full Markdown parser.

2. **The header marker names the inputs:**
   `<!-- tutorial: inputs=CT_small.dcm,MR_small.dcm -->`, once per page.
   Each name is a file bundled *inside* the installed pydicom package
   (`pydicom/data/test_files/`), copied into `./input/` before the first
   fence runs. The working directory is the test's own `tmp_path` (the
   autouse chdir in `tests/conftest.py`, #707), so the page says
   `Session("tutorial.db")` and `session.ingest("input")` exactly as a
   reader would. A tutorial page with no marker, a name pydicom does not
   bundle, or a name only `get_testdata_file()` could download is a
   failure: a tutorial that needs the network is one that cannot be run
   offline by its reader or by the release gate.

3. **Fences run in page order, in one shared namespace.** A ```` ```python ````
   fence containing `>>>` runs as a doctest against that namespace, so
   its shown output is checked; any other ```` ```python ```` fence is
   `exec`'d. Keep actions (`ingest`, `export`, anything that prints
   progress) in plain fences and claims in `>>>` fences, so a claim's
   expected output is the value and not the console chatter around it.
   The first fence that fails stops the page: later fences read state it
   was meant to build, and their failures would be noise.

4. **A file the reader saves** (a configuration) is a non-python fence
   with `<!-- tutorial: file=NAME -->` on the line directly above it; its
   body is written to `./NAME` at that point in page order, so the page's
   later code loads what the reader sees. Any other non-python fence on a
   run page is a failure, because it would be a block nobody checks. The
   marker is held to four rules, checked before any fence runs (arch-L14):
   it is the *last line* before its fence (a blank line between, a marker
   with no fence below, or one above a python fence is red); its name is
   a *bare filename*, never a path, so the write stays in the working
   directory; a *later python fence names the file*, so what the reader
   saves is something the page reads (`load_config`'s strict validation
   then checks the YAML itself); and the vocabulary is *two keys*,
   `inputs=` (the header, once) and `file=` (a fence marker) -- any other
   `<!-- tutorial: ... -->` is red. Writing one file twice is allowed;
   the writes land in page order.

5. **Outcome claims are visible `>>>` output, never hidden assertions.**
   A hidden check is a second copy of the claim that the reader never
   sees. The grade is read from the **report file's** `Validation Status`
   line, not from `ComplianceReport` (its fields are tier 2, owner ruling
   Q3 on L14); Grade Basis wording is tier 2 as well, so a doctest shows
   it with `...` where the wording, not the grade, is incidental.

6. **The page cleans up after itself** by calling `session.close()`, as a
   reader must. The runner also closes every `Session` left in the
   namespace when a page stops early, so a red page does not leak worker
   processes into the rest of the run.

7. **Cost: about 30 s per page, measured on 3.14t.** One parametrised
   test per page, so `pytest --changed` or a shard can take one page.

The info string stays exactly ```` ```python ````, #304's reason for the
HTML-comment markers: `test_documented_api_exists.py` matches that
string, so every name a tutorial calls is also name-checked there.

**`TARGETS` rows: this file is listed under `session.py`,
`io_handlers.py`, `reporting.py`, `remediation.py` and `privacy.py`**,
the modules T1 exercises end to end (review of #785). It imports none of
them, so no import scan demands the rows; they are there for
`pytest --changed`. A page is new to the coverage map until the release's
3.14t `test_map build` records it, and until then a change to one of
those modules that breaks a tutorial is selected only through its
`TARGETS` row. The cost is one page run (T1: about 8 s) per mutant, or per
`--changed` selection, of those modules. A module a tutorial reaches but
that has no row here is covered once the map is rebuilt.
"""
import doctest
import pathlib
import re
import shutil
import traceback

import pydicom
import pytest

from test_documented_output_matches import _OPTIONFLAGS

REPO = pathlib.Path(__file__).resolve().parent.parent
TUTORIALS = "docs/tutorials"

# Dated design records, not documentation (same carve-out as #234/#304).
_EXCLUDED_DOCS = "docs/superpowers/"

#: The one directory an input may come from: the files pydicom ships in
#: its own package. `get_testdata_file()` also searches pydicom-data and
#: then downloads, and neither is something a reader is guaranteed.
BUNDLED = pathlib.Path(pydicom.__file__).resolve().parent / "data" / "test_files"

# Every `<!-- tutorial: KEY=VALUE -->` comment, whatever its key: the
# vocabulary is checked on what was written, not on what was expected.
_MARKER = re.compile(r"<!--[ \t]*tutorial:(.*?)-->", re.DOTALL)
_HEADER = re.compile(r"<!--[ \t]*tutorial:[ \t]*inputs=([^\s>]*)[ \t]*-->")
_FILE_MARKER = re.compile(
    r"<!--[ \t]*tutorial:[ \t]*file=([^\s>]+)[ \t]*-->[ \t]*\n\Z")
_FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
# Any fence, with its info string. Fences are not nested in Markdown, so
# a lazy body up to the next line-leading ``` is the whole fence.
_FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
# Anything that looks like a fence line, however indented or spelled.
# Every such line must be the opener or closer of a fence `_FENCE`
# consumed, or be inside one; otherwise it is code the runner would not
# see (an indented, tilde, four-backtick or unclosed fence).
_ANY_FENCE_LINE = re.compile(r"^[ \t]*(```|~~~)", re.MULTILINE)
# Raw HTML code, which Markdown renders as code and `_FENCE` never sees.
_PRE = re.compile(r"<pre\b", re.IGNORECASE)


def tutorial_pages(root=None):
    """Every page this runner runs: the tutorials, plus marked pages."""
    root = root or REPO
    pages = sorted((root / TUTORIALS).glob("*.md"))
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            if (path not in pages and _EXCLUDED_DOCS not in rel
                    and _HEADER.search(path.read_text(encoding="utf-8"))):
                pages.append(path)
    return pages


def _inputs(text, where):
    """The header's input names, or raise with why the page cannot run."""
    headers = _HEADER.findall(text)
    if len(headers) != 1:
        raise AssertionError(
            f"{where}: needs exactly one `<!-- tutorial: inputs=... -->` "
            f"header, found {len(headers)} -- a tutorial with no declared "
            "inputs cannot be run as its reader would run it")
    names = [n for n in headers[0].split(",") if n]
    for name in names:
        if pathlib.PurePath(name).name != name:
            raise AssertionError(
                f"{where}: input {name!r} is a path; name a file pydicom "
                "bundles")
        if not (BUNDLED / name).is_file():
            raise AssertionError(
                f"{where}: input {name!r} is not bundled with pydicom "
                f"(looked in {BUNDLED}); a file get_testdata_file() would "
                "download is not an offline input, and a tutorial must run "
                "offline")
    return names


def _marker_failures(text, where):
    """Every tutorial marker's shape, checked before any fence runs.

    Four rules (arch-L14, on C2a): `inputs=` is the header and `file=`
    only a fence marker, and no other key exists; a `file=` marker is the
    last line before a non-python fence; its name is a bare filename, so
    the write stays in the test's working directory; and a later python
    fence names the file, so what the reader saves is something the page
    reads. None of these is a skip.
    """
    failures = []
    fences = [(m.start(), m.group(1).strip(), m.group(2))
              for m in _FENCE.finditer(text)]
    for match in _MARKER.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        key, sep, value = match.group(1).strip().partition("=")
        if not sep or key not in ("inputs", "file"):
            failures.append(
                f"{where}:{line}: unknown tutorial marker "
                f"{match.group(0)!r}; the keys are `inputs=` (the page "
                "header) and `file=` (a fence marker)")
            continue
        if key == "inputs":
            continue  # counted and resolved by _inputs()
        following = text[match.end():]
        fence = re.match(r"[ \t]*\n```([^\n`]*)\n", following)
        if fence is None:
            failures.append(
                f"{where}:{line}: `file=` marker is not the line directly "
                "above a fence, so it claims nothing")
            continue
        if fence.group(1).strip() == "python":
            failures.append(
                f"{where}:{line}: `file=` marker sits above a python fence; "
                "it names a file the reader saves, and python fences run")
            continue
        name = value.strip()
        if not _FILE_NAME.fullmatch(name) or ".." in name:
            failures.append(
                f"{where}:{line}: `file=` names {name!r}; it must be a bare "
                "filename, written only into the working directory")
            continue
        fence_at = match.end() + fence.start()
        if not any(start > fence_at and info == "python" and name in body
                   for start, info, body in fences):
            failures.append(
                f"{where}:{line}: `file={name}` is never named by a later "
                "python fence, so nothing the page runs reads it")
    return failures


def _shape_failures(text, where):
    """Code on the page that the runner would not run, or not check.

    Found in review of #785: each of these passed a page whose failing
    code never ran. They are refused before any fence runs, never
    skipped.

    - A fence line `_FENCE` did not consume: indented (in a list item,
      an admonition or a tab), `~~~`, four backticks, or never closed.
      Mkdocs renders all of them as code; the runner saw prose.
    - A `<pre>` block: raw HTML code, which a reader copies as code.
      Refused rather than run, because a tutorial's code belongs in a
      fence where `test_documented_api_exists.py` can also read it. A
      four-space indented code block is *not* detected: telling it from
      list-item prose needs a full Markdown parser.
    - In a `>>>` fence, anything that is not an example: `doctest` reads
      a line before the first `>>>`, or after the blank line that ends
      an example's output, as prose and drops it.
    - A `doctest:` directive: `+SKIP` is a skip dressed as a pass, and
      the others change what "matches" means page by page.
    An expected output of only `...` needs no rule: doctest reads a `...`
    line straight after `>>>` as a continuation of the source, so the
    example expects nothing and fails on any output.
    """
    failures = []
    consumed = [(m.start(), m.end()) for m in _FENCE.finditer(text)]

    def outside(pos):
        return not any(start <= pos < end for start, end in consumed)

    def line_of(pos):
        return text.count("\n", 0, pos) + 1

    for match in _ANY_FENCE_LINE.finditer(text):
        if outside(match.start()):
            failures.append(
                f"{where}:{line_of(match.start())}: a fence the runner does "
                "not run (indented, `~~~`, four backticks, or unclosed); a "
                "run page's fences start at column 0 with exactly ```")
    for match in _PRE.finditer(text):
        if outside(match.start()):
            failures.append(
                f"{where}:{line_of(match.start())}: a <pre> block is code the "
                "runner does not run; put it in a ```python fence")

    parser = doctest.DocTestParser()
    for match in _FENCE.finditer(text):
        if match.group(1).strip() != "python":
            continue
        body = match.group(2)
        first = line_of(match.start(2))
        pieces = parser.parse(body)
        if not any(isinstance(p, doctest.Example) for p in pieces):
            continue
        offset = 0
        for piece in pieces:
            if isinstance(piece, str):
                if piece.strip():
                    failures.append(
                        f"{where}:{first + offset}: a `>>>` fence holds a "
                        f"line that is not an example ({piece.strip()[:60]!r}); "
                        "doctest would drop it unrun -- put actions in a "
                        "fence of their own, or prefix them with >>>")
                offset += piece.count("\n")
                continue
            if piece.options:
                failures.append(
                    f"{where}:{first + piece.lineno}: a `doctest:` directive; "
                    "a tutorial's examples all run under the one comparison")
            offset = piece.lineno + piece.source.count("\n") \
                + piece.want.count("\n")
    return failures


def run_page(page, workdir, root=None):
    """Run one page in `workdir`. Returns a list of failure strings.

    Empty means every fence ran and every shown output matched. Stops at
    the first failing fence (convention 3).
    """
    root = root or REPO
    workdir = pathlib.Path(workdir)
    # The runner writes into `workdir`; the page's code reads relative
    # paths, so from the cwd. They are one directory only because
    # conftest's autouse chdir makes the cwd the test's tmp_path (#707).
    if pathlib.Path.cwd().resolve() != workdir.resolve():
        return [f"run_page: cwd {pathlib.Path.cwd()} is not the working "
                f"directory {workdir}; the page would read files the "
                "runner did not write"]
    text = page.read_text(encoding="utf-8")
    where = page.relative_to(root).as_posix()
    try:
        names = _inputs(text, where)
    except AssertionError as exc:
        return [str(exc)]
    failures = _marker_failures(text, where) + _shape_failures(text, where)
    if failures:
        return failures

    (workdir / "input").mkdir(exist_ok=True)
    for name in names:
        shutil.copyfile(BUNDLED / name, workdir / "input" / name)

    namespace = {"__name__": "__tutorial__"}
    fences = examples = 0
    try:
        for match in _FENCE.finditer(text):
            info, body = match.group(1).strip(), match.group(2)
            line = text.count("\n", 0, match.start(2)) + 1
            label = f"{where}:{line}"
            # `_marker_failures` has already refused a misplaced marker;
            # this finds which marker, if any, owns this fence.
            marker = _FILE_MARKER.search(text, 0, match.start())

            if info != "python":
                if marker is None:
                    return [f"{label}: a ```{info} fence on a run page is "
                            "neither python nor a `<!-- tutorial: file=... "
                            "-->` file, so nothing checks it"]
                (workdir / marker.group(1)).write_text(body, encoding="utf-8")
                continue

            fences += 1
            parser = doctest.DocTestParser()
            found = parser.get_examples(body)
            if not found:
                try:
                    exec(compile(body, label, "exec"), namespace)
                except Exception:  # pylint: disable=broad-except
                    return [f"{label}: the fence raised:\n"
                            + traceback.format_exc()]
                continue

            examples += len(found)
            test = parser.get_doctest(body, namespace, label, str(page),
                                      line - 1)
            captured = []
            runner = doctest.DocTestRunner(optionflags=_OPTIONFLAGS)
            runner.run(test, out=captured.append, clear_globs=False)
            # `DocTest` runs on a *copy* of the globals it is given, so a
            # name a `>>>` line binds would vanish from the next fence.
            namespace.update(test.globs)
            if runner.failures:
                return [f"{label}: {runner.failures} of {runner.tries} "
                        "example(s) printed something other than what the "
                        "page shows:\n" + "".join(captured)]
    finally:
        _close_sessions(namespace)

    if not fences:
        return [f"{where}: holds no ```python fence, so it runs nothing"]
    if not examples:
        return [f"{where}: holds no `>>>` example, so it shows no outcome "
                "that is checked -- a tutorial's claims are its shown output"]
    return []


def _close_sessions(namespace):
    """Close any session a stopped page left open (convention 6)."""
    try:
        from isocenter import Session
    except ImportError:  # a synthetic page that never imported it
        return
    for value in list(namespace.values()):
        if isinstance(value, Session):
            try:
                value.close()
            except Exception:  # pylint: disable=broad-except
                pass  # already closed by the page, or broken by its failure


_PAGES = tutorial_pages()


def test_there_is_a_tutorial_to_run():
    """An empty glob is a broken check, not a clean one (#299, #304)."""
    assert list((REPO / TUTORIALS).glob("*.md")), (
        f"no page under {TUTORIALS}/, so the runner graded nothing")


@pytest.mark.parametrize(
    "page", _PAGES, ids=[p.relative_to(REPO).as_posix() for p in _PAGES])
def test_the_tutorial_runs_and_shows_what_it_prints(page, tmp_path):
    failures = run_page(page, tmp_path)
    assert not failures, "\n".join(failures)


# -- The runner's own negative cases. Synthetic pages, no Session, so ----
# -- these cost milliseconds and prove the check can go red. -------------

def _page(root, body, name="t.md"):
    path = root / TUTORIALS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


_OK_HEADER = "<!-- tutorial: inputs=CT_small.dcm -->\n"


def test_a_wrong_shown_output_is_red_and_names_its_fence(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "```python\nx = 2\n```\n\n"
        "```python\n>>> x + 2\n5\n```\n"))
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1, failures
    assert failures[0].startswith(f"{TUTORIALS}/t.md:7:"), failures[0]
    assert "printed something other" in failures[0]


def test_fences_share_one_namespace_in_page_order(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "```python\nx = 2\n```\n\n"
        "```python\n>>> x + 2\n4\n```\n"))
    assert run_page(page, tmp_path, root=tmp_path) == []


def test_a_name_bound_in_a_doctest_reaches_the_next_fence(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "```python\n>>> y = 3\n```\n\n"
        "```python\nz = y + 1\n```\n\n"
        "```python\n>>> z\n4\n```\n"))
    assert run_page(page, tmp_path, root=tmp_path) == []


def test_the_inputs_are_copied_into_input(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "```python\n>>> import os; sorted(os.listdir('input'))\n"
        "['CT_small.dcm']\n```\n"))
    assert run_page(page, tmp_path, root=tmp_path) == []


def test_a_page_with_no_header_is_red(tmp_path):
    page = _page(tmp_path, "```python\n>>> 1\n1\n```\n")
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "found 0" in failures[0], failures


def test_an_input_pydicom_does_not_bundle_is_red(tmp_path):
    page = _page(tmp_path, "<!-- tutorial: inputs=CT_small.dcm,"
                 "no_such_file.dcm -->\n```python\n>>> 1\n1\n```\n")
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1, failures
    assert "'no_such_file.dcm' is not bundled" in failures[0]


def test_an_input_only_the_network_has_is_red(tmp_path):
    # In pydicom's download index, never in its package: the runner must
    # not reach for get_testdata_file(), which would fetch it.
    from pydicom.data.download import get_url_map
    remote = sorted(set(get_url_map()) - {p.name for p in BUNDLED.iterdir()})
    page = _page(tmp_path, f"<!-- tutorial: inputs={remote[0]} -->\n"
                 "```python\n>>> 1\n1\n```\n")
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "is not bundled" in failures[0], failures
    assert not (tmp_path / "input" / remote[0]).exists()


def test_a_fence_that_raises_is_red(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "```python\nraise ValueError('boom')\n```\n\n"
        "```python\n>>> 1\n1\n```\n"))
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "ValueError: boom" in failures[0], failures


def test_a_page_with_no_shown_output_is_red(tmp_path):
    page = _page(tmp_path, _OK_HEADER + "```python\nx = 1\n```\n")
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "no `>>>` example" in failures[0], failures


def test_an_unmarked_non_python_fence_is_red(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "```yaml\na: 1\n```\n\n```python\n>>> 1\n1\n```\n"))
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "nothing checks it" in failures[0], failures


def test_a_file_fence_is_written_where_the_page_reads_it(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "<!-- tutorial: file=config.yaml -->\n```yaml\na: 1\n```\n\n"
        + _READS_IT))
    assert run_page(page, tmp_path, root=tmp_path) == []


_READS_IT = "```python\n>>> open('config.yaml').read()\n'a: 1\\n'\n```\n"


@pytest.mark.parametrize("between", ["\n", "Save this:\n\n"])
def test_a_file_marker_claims_only_the_fence_directly_below(
        tmp_path, between):
    page = _page(tmp_path, _OK_HEADER + (
        "<!-- tutorial: file=config.yaml -->\n" + between
        + "```yaml\na: 1\n```\n\n" + _READS_IT))
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1, failures
    assert "not the line directly above a fence" in failures[0]
    assert not (tmp_path / "config.yaml").exists()


def test_a_file_marker_with_no_fence_below_is_red(tmp_path):
    page = _page(tmp_path, _OK_HEADER + "```python\n>>> 1\n1\n```\n\n"
                 "<!-- tutorial: file=config.yaml -->\n")
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1, failures
    assert "not the line directly above a fence" in failures[0]


def test_a_file_marker_above_a_python_fence_is_red(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "<!-- tutorial: file=config.yaml -->\n```python\nx = 1\n```\n\n"
        "```python\n>>> 'config.yaml'\n'config.yaml'\n```\n"))
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "above a python fence" in failures[0], (
        failures)


@pytest.mark.parametrize("name", ["../config.yaml", "sub/config.yaml",
                                  "/tmp/config.yaml", "..", ".config"])
def test_a_file_marker_name_must_stay_in_the_working_directory(
        tmp_path, name):
    page = _page(tmp_path, _OK_HEADER + (
        f"<!-- tutorial: file={name} -->\n```yaml\na: 1\n```\n\n"
        f"```python\n>>> {name!r}\n{name!r}\n```\n"))
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "must be a bare filename" in failures[0], (
        failures)
    assert not (tmp_path / "input").exists()  # refused before any write


def test_a_file_no_later_fence_reads_is_red(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "```python\n>>> 'config.yaml'\n'config.yaml'\n```\n\n"
        "<!-- tutorial: file=config.yaml -->\n```yaml\na: 1\n```\n"))
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "never named by a later" in failures[0], (
        failures)


@pytest.mark.parametrize("marker", ["<!-- tutorial: input=CT_small.dcm -->",
                                    "<!-- tutorial: run -->",
                                    "<!-- tutorial: output=x.txt -->"])
def test_an_unknown_marker_key_is_red(tmp_path, marker):
    page = _page(tmp_path, _OK_HEADER + marker + "\n\n"
                 "```python\n>>> 1\n1\n```\n")
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "unknown tutorial marker" in failures[0], (
        failures)


def test_two_headers_are_red(tmp_path):
    page = _page(tmp_path, _OK_HEADER + _OK_HEADER
                 + "```python\n>>> 1\n1\n```\n")
    failures = run_page(page, tmp_path, root=tmp_path)
    assert len(failures) == 1 and "found 2" in failures[0], failures


def test_the_same_file_written_twice_is_read_in_page_order(tmp_path):
    page = _page(tmp_path, _OK_HEADER + (
        "<!-- tutorial: file=config.yaml -->\n```yaml\na: 1\n```\n\n"
        + _READS_IT +
        "\n<!-- tutorial: file=config.yaml -->\n```yaml\na: 2\n```\n\n"
        "```python\n>>> open('config.yaml').read()\n'a: 2\\n'\n```\n"))
    assert run_page(page, tmp_path, root=tmp_path) == []


def test_a_workdir_that_is_not_the_cwd_is_red(tmp_path):
    page = _page(tmp_path, _OK_HEADER + "```python\n>>> 1\n1\n```\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    failures = run_page(page, elsewhere, root=tmp_path)
    assert len(failures) == 1 and "is not the working" in failures[0], (
        failures)


def test_a_marked_page_outside_tutorials_is_run(tmp_path):
    guide = tmp_path / "docs" / "guide.md"
    guide.parent.mkdir(parents=True)
    guide.write_text(_OK_HEADER + "```python\n>>> 1\n1\n```\n",
                     encoding="utf-8")
    (tmp_path / "docs" / "plain.md").write_text("```python\nx\n```\n",
                                                encoding="utf-8")
    assert tutorial_pages(tmp_path) == [guide]


# -- Code the runner would not run (review of #785). Each page holds a ---
# -- fence that raises if it ran, and must be refused before any does. ---

_BOOM = "raise SystemError('THIS RAN')"
_CHECKED = "```python\n>>> 1\n1\n```\n"


@pytest.mark.parametrize("body, says", [
    ('!!! tip "x"\n\n    ```python\n    ' + _BOOM + "\n    ```\n\n",
     "a fence the runner does not run"),
    ("1. step\n\n    ```python\n    " + _BOOM + "\n    ```\n\n",
     "a fence the runner does not run"),
    ("~~~python\n" + _BOOM + "\n~~~\n\n", "a fence the runner does not run"),
    ("````python\n" + _BOOM + "\n````\n\n", "a fence the runner does not run"),
    (_CHECKED + "\n```python\n" + _BOOM + "\n", "a fence the runner does not run"),
    ("<pre>\n" + _BOOM + "\n</pre>\n\n", "a <pre> block"),
    ("```python\n" + _BOOM + "\n>>> 1\n1\n```\n\n", "not an example"),
    ("```python\n>>> 1\n1\n\n" + _BOOM + "\n```\n\n", "not an example"),
    ("```python\n>>> 1/0  # doctest: +SKIP\n```\n\n", "`doctest:` directive"),
    ("```python\n>>> 2  # doctest: +ELLIPSIS\n2\n```\n\n",
     "`doctest:` directive"),
    # Red because it fails, not by a rule: see `_shape_failures`.
    ("```python\n>>> 'anything at all'\n...\n```\n\n", "Expected nothing"),
], ids=["admonition", "list-item", "tilde", "four-backticks", "unclosed",
        "pre", "code-before-example", "code-after-output", "skip",
        "other-directive", "ellipsis-only"])
def test_code_the_runner_would_not_run_is_red(tmp_path, body, says):
    page = _page(tmp_path, _OK_HEADER + _CHECKED + "\n" + body)
    failures = run_page(page, tmp_path, root=tmp_path)
    assert failures, "passed a page holding code that never ran"
    assert not any("SystemError: THIS RAN" in f for f in failures), failures
    assert any(says in f for f in failures), failures


def test_a_fence_nested_in_a_consumed_fence_is_content(tmp_path):
    # A fence line *inside* a fence the runner runs is that fence's text.
    page = _page(tmp_path, _OK_HEADER + (
        "```python\n>>> print('    ```')\n    ```\n```\n"))
    assert run_page(page, tmp_path, root=tmp_path) == []

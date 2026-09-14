"""Method names this project puts in front of a user must exist (#234).

Isocenter tells users what to call in two places that no test has ever
read: strings the package prints or logs at runtime ("Tip: Run
`.audit()`..."), and the Python fences in `README.md` and `docs/`. Both
are claims about the API, both were written by hand, and both go stale
in exactly the way an ordinary docstring cannot -- nothing imports them,
so nothing notices. Four such claims were false when this file was
added; two of them had been false since before the package was renamed.

**Why this is a new file rather than a section of
`tests/test_api_coherence.py`**, which #234 names as the natural home:
that file is listed in `scripts/mutation_probe.py`'s `TARGETS` under
`io_handlers.py`, so every test in it is re-run against every mutant of
that module. Sixty lines that never touch the exporter would buy zero
kill signal and cost on every mutant. This file imports no target module
and needs no `TARGETS` entry of its own.

The namespace is built with `ast` over the source tree rather than by
importing the package. Three reasons, all measured: it gives the
identical answer to the import-based version; it avoids importing
thirty-five modules inside a test for a question that is answerable from
the text; and it means this file never has to spell a dotted module
name, which `tests/test_mutation_probe_targets.py` reads file *text*
for.
"""
import ast
import doctest
import json
import pathlib
import re
import textwrap

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "isocenter"

# `.name(` inside a string literal or a doc fence, with the word touching
# the dot on its left when there is one. The receiver decides whether
# the mention is a claim (#533): `str.strip()` in a docstring is a
# sentence about the standard library, `session.strip()` or a bare
# `.strip()` is a promise about ours. No whitespace is admitted between
# the receiver and the dot, so `call .date() on it` is the bare form.
_CALL_IN_TEXT = re.compile(
    r"(?:(?P<recv>[A-Za-z_]\w*))?\.(?P<name>[A-Za-z_]\w*)\(")

# ```python fences, captured with their offset so a failure can name a
# line rather than a fence ordinal.
_PYTHON_FENCE = re.compile(r"```python\n(.*?)```", re.DOTALL)

# Receivers, beyond the class names the package defines and `ROOTS`
# below, whose `.name(` in a package string is a claim about our API:
# the spellings the package's own strings use for a session, an
# instance, a store or an exporter. A receiver outside this set and the
# class names is another library's object, and its method is not looked
# for here.
_RECEIVERS = frozenset({"self", "inst", "instance", "store_backend",
                        "persistence_manager", "configuration", "exporter"})

# Methods named correctly in prose that belong to the standard library
# or a third-party package, not to Isocenter, and that a string spells
# with no receiver at all. Each entry is a CLAIM that the name is not an
# Isocenter method and never should be looked for as one -- adding to
# this set to silence a failure is how the defect class this file
# exists for gets back in. Since #533 a receiver'd mention (`str.lower()`,
# `Record.wrheader()`, `pixel_array.tobytes()`) is not read as a claim,
# so the set shrank from six to one: `date` is the `.date()` a user is
# told to call in the `TypeError` `set_attr` raises for a `datetime`
# study date, a runtime message that keeps its wording. It is also
# redundant -- `_defined_names()` picks up every name the package imports
# at module scope, and `from datetime import date` appears in two
# modules -- and stays as the record of why it is bare.
NOT_OURS = frozenset({
    "date",       # datetime.datetime.date(), bare in a runtime TypeError
})


# Receiver roots whose attribute calls are this project's claims. A
# documented `df.head()` or `plt.show()` is pandas' or matplotlib's
# promise, not ours, and checking those needs a thirteen-entry allowlist
# of third-party method names -- an allowlist that would then have to be
# maintained, which is the same defect in a new place. Restricting to
# these roots needs no allowlist at all.
#
# This is a NAME list, not a type check. A session bound to any other
# name escapes it -- `docs/migration.md` writes
# `with Session("store.db") as s:`, which is why `s` is here. A new
# short alias in a future fence goes unchecked until someone adds it.
#
# The deliberate blind spot, stated in full because understating it
# would be the same defect this file exists for: thirteen documented calls
# sit on receivers outside this set and are values of ours --
# `result.*` (twelve: six in `docs/ocr.md`, five in README's zone
# discovery section, one in `docs/configuration.md`) and
# `filtered.to_zones()`. All thirteen were hand-resolved against
# `isocenter/discovery.py` and `isocenter/privacy.py` and all exist;
# README's five are executed for their *output* by
# `tests/test_documented_output_matches.py` (#304), and configuration.md's
# `result.to_zones()` is executed by
# `tests/test_documented_zones_are_zone_space.py` (#424), both of which
# resolve them for real. The count moved from seven to twelve when #303
# restored the README section, and to thirteen with #424; it is stated
# here rather than left vague because an understated blind spot is the
# same defect this file exists for. The rest (`plt`, `df`, `re`,
# and calls on unnamed receivers) are third-party or chained
# expressions and are none of our business. Do not widen `ROOTS` to a
# bare "every attribute call" without reading the allowlist cost above.
ROOTS = frozenset({"session", "sess", "s", "config", "isocenter",
                   "store", "exporter"})

# Fences under this prefix are dated design records, not documentation.
# They describe the API as it stood on their date and are deliberately
# not rewritten when it moves -- see CLAUDE.md's Conventions section.
_EXCLUDED_DOCS = "docs/superpowers/"


def _package_sources():
    return sorted(
        path for path in PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts)


def _defined_names():
    """Every name the package defines that a `.name()` could refer to.

    Functions, async functions and classes at any nesting depth, plus
    module-scope `from ... import X as Y` bindings -- the asname matters
    because `isocenter/__init__.py` does
    `from .session import DicomSession as Session`, so `Session` exists
    only under its alias.
    """
    names = set()
    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
    return names


def _class_names():
    """Every class the package defines, at any nesting depth."""
    names = set()
    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        names.update(node.name for node in ast.walk(tree)
                     if isinstance(node, ast.ClassDef))
    return names


def _offenders_in_text(text, defined, ours):
    """The method names `text` claims for our API and `defined` lacks.

    A `.name(` is a claim when it has no receiver, or when its receiver
    is one of `ours` (a class name, a `ROOTS` entry or a `_RECEIVERS`
    spelling). Any other receiver is another library's object -- a
    docstring saying `str.strip()` is describing `str` -- and the name
    is not looked for (#533). A claim is an offender when neither
    `defined` nor `NOT_OURS` carries it.
    """
    offenders = []
    for match in _CALL_IN_TEXT.finditer(text):
        receiver, name = match.group("recv"), match.group("name")
        if receiver is not None and receiver not in ours:
            continue
        if name in defined or name in NOT_OURS:
            continue
        offenders.append(name)
    return offenders


def test_every_method_named_in_a_package_string_exists():
    """A method named in a string the package emits must be real.

    These are the tips, log lines and error messages a user is told to
    act on. A wrong one costs them an `AttributeError` at best; the two
    this test found first were a `.redact_pixels()` that was renamed
    (#227) and a `.save_config()` that has never existed under any name
    this package has had.

    Resolution is by NAME, with the receiver deciding only whether the
    name is *ours to check* (#533). A string saying
    `wrong_receiver.save()` passes because `wrong_receiver` is not a
    spelling this file knows for our objects; `session.save()` and a
    bare `.save()` are checked and pass because `save` is defined
    somewhere in the package. Full receiver-aware resolution would need
    the thirteen-entry third-party allowlist that the doc-fence half of
    this file avoids by restricting itself to known receivers, and a
    maintained allowlist is a fresh instance of the defect this file is
    about. Before #533 every `.name(` was a claim whatever preceded it,
    and a docstring naming `str`'s own method failed the suite twice;
    `NOT_OURS` carried the exceptions by hand, which is that allowlist
    under another name. The looser check still caught every real
    defect at the time it was written; do not "strengthen" it into the
    wider variant without pricing that in.
    """
    defined = _defined_names()
    ours = _class_names() | ROOTS | _RECEIVERS
    sources = _package_sources()
    # The walk must find something, or this passes while checking
    # nothing -- a package rename or a move under `src/` would empty it
    # silently. Same precedent as #299's `len(subclasses) >= 5`.
    assert len(sources) > 30, (
        f"only {len(sources)} source files found under {PACKAGE}; the "
        "walk is broken and this test would otherwise pass vacuously")
    offenders = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            for name in _offenders_in_text(node.value, defined, ours):
                offenders.append((
                    path.relative_to(REPO).as_posix(), node.lineno, name,
                    node.value.strip()[:120]))

    assert not offenders, (
        "these strings name a method the package does not define, so a "
        "user who follows them gets an AttributeError (#234):\n"
        + "\n".join(
            f"    {where}:{line}: .{name}() in {text!r}"
            for where, line, name, text in offenders))


def _documentation_files():
    files = [REPO / "README.md"]
    for path in sorted((REPO / "docs").rglob("*.md")):
        if _EXCLUDED_DOCS in path.relative_to(REPO).as_posix():
            continue
        files.append(path)
    return [path for path in files if path.is_file()]


def _receiver_root(node):
    """The `ast.Name` at the base of an attribute chain, or None."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _fence_source(body):
    """The Python a fence contains, or None if it is not Python.

    A doctest fence (`>>> ...`) is ordinary ```` ```python ```` on the
    page but is not valid Python, so `ast.parse` raises `SyntaxError` on
    it and the caller's escape hatch below swallows the *whole* fence --
    every name in it, silently. That matters now that a fence can opt in
    to output checking by being written as a doctest
    (`tests/test_documented_output_matches.py`, #304): opting in there
    would have opted the fence out here, and the guard would still have
    reported a clean pass.

    So: parse the fence's doctest examples first and, if it has any,
    hand back the concatenation of their sources. `Example.source`
    already carries the prompts stripped and the continuation lines
    joined, which is why this is a two-line fix rather than a prompt
    stripper of our own.
    """
    examples = doctest.DocTestParser().get_examples(body)
    if examples:
        return "".join(example.source for example in examples)
    # `dedent` first: a fence nested inside a list item is indented, and
    # `ast.parse` raises IndentationError on it. `docs/quickstart.md`'s
    # repair snippet is exactly that, and without this it was silently
    # skipped -- the escape hatch firing on a real fence with real calls
    # in it rather than on the hypothetical pseudo-code one it was
    # written for (#234).
    return textwrap.dedent(body)


def _unresolved_calls_in(path, defined):
    """Documented calls in one file that name no method we define.

    Returns `(where, line, name)` triples. Fences that do not parse are
    skipped rather than failed -- every fence parses today, but a future
    pseudo-code fence should not turn the guard red for being prose.
    """
    try:
        where = path.relative_to(REPO).as_posix()
    except ValueError:      # a tmp_path fixture, not a tree file
        where = path.name
    text = path.read_text(encoding="utf-8")
    offenders = []
    for match in _PYTHON_FENCE.finditer(text):
        fence_line = text.count("\n", 0, match.start()) + 1
        try:
            tree = ast.parse(_fence_source(match.group(1)))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if _receiver_root(node.func) not in ROOTS:
                continue
            if node.func.attr in defined:
                continue
            offenders.append((where, fence_line + node.lineno,
                              node.func.attr))
    return offenders


def test_every_isocenter_call_in_the_docs_resolves():
    """Every documented call on one of our own objects must exist.

    `README.md` and `docs/` are the first thing a user runs. A fence
    that raises `AttributeError` on line three is worse than no fence,
    and there is nothing in the build that reads them.
    """
    defined = _defined_names()
    files = _documentation_files()
    # As above (#299's precedent): an empty file list is a broken walk,
    # not a clean bill of health.
    assert len(files) > 10, (
        f"only {len(files)} documentation files found; the walk is "
        "broken and this test would otherwise pass vacuously")
    offenders = []
    for path in files:
        offenders.extend(_unresolved_calls_in(path, defined))

    assert not offenders, (
        "these documented calls name a method the package does not "
        "define, so the example raises AttributeError for anyone who "
        "runs it (#234):\n"
        + "\n".join(f"    {where}:~{line}: .{name}()"
                    for where, line, name in offenders))


# --- README's zone-discovery section (#303) -------------------------------
#
# `discover_redaction_zones()` and the whole `DiscoveryResult` surface are
# public API that README described in a "5b" section until that section was
# deleted, leaving §5a followed by §6 and no documented way in. The two
# claims the old section got wrong -- what `to_dataframe()`'s columns are,
# and what `get_density_matrix()` is a matrix *of* -- are why it is worth a
# guard rather than a one-line fix: the second is the trap, because
# `get_density_matrix()` normalises by the largest candidate *origin*
# (`isocenter/discovery.py`, `max(xs)`/`max(ys)`), not by the image's Rows
# and Columns, so a section that calls it an image heatmap is telling
# readers to plot coordinates that do not mean what they read as.
#
# This lives here rather than in a file of its own because this module is
# already the home of "what README and docs/ claim about our API", and it
# needs no `TARGETS` entry for the reason the module docstring gives.
#
# The *output* of the section's runnable fence is checked by
# `tests/test_documented_output_matches.py`; this checks only that the
# section exists and does not reintroduce the image-space claim.
_DISCOVERY_HEADING = "### 5b. Zone Discovery"


def test_the_readme_documents_zone_discovery_and_its_density_matrix_limit():
    """README must route users to `discover_redaction_zones()` (#303).

    Checked, in order of how badly getting each one wrong costs the
    reader: the section exists at all; it names the entry point and the
    result type; it names the `ocr` extra, without which the scan cannot read
    the pixels and refuses with `OcrUnavailableError` (#422); and it says
    somewhere in the section that the density matrix is not in image
    coordinates.
    """
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert _DISCOVERY_HEADING in text, (
        f"README.md has no {_DISCOVERY_HEADING!r} section, so "
        "discover_redaction_zones() and DiscoveryResult are public API "
        "with no documented entry point (#303)")

    start = text.index(_DISCOVERY_HEADING)
    section = text[start:text.index("\n### ", start + 1)]

    for required in ("discover_redaction_zones", "DiscoveryResult",
                     "to_zones", "get_density_matrix", "ocr"):
        assert required in section, (
            f"README's zone-discovery section never mentions {required!r} "
            "(#303)")

    # The claim the deleted section got wrong. Phrasing is free; the
    # section must state, in some form, that the grid is not the image.
    assert "not an image-space heatmap" in section, (
        "README's zone-discovery section must state that "
        "get_density_matrix() is not in image coordinates -- it "
        "normalises by the largest candidate box origin, not by Rows and "
        "Columns, so plotting it as an overlay is wrong (#303)")


def test_a_doctest_fence_is_still_read_for_method_names(tmp_path):
    """A `>>>` fence must not fall out of the name check (#304).

    `_PYTHON_FENCE` matches a doctest fence -- its info string is an
    ordinary ```` ```python ```` -- but `ast.parse` raises `SyntaxError`
    on the `>>> ` prompts, and the escape hatch above swallows the whole
    fence. So the moment a fence is rewritten as a doctest so that
    `tests/test_documented_output_matches.py` can execute it, it stops
    being read here: opting a fence *in* to output checking silently
    opted it *out* of name checking, and the file it happened to would
    still report a clean pass.

    This is the same "a silent skip reads as a pass" failure the escape
    hatch itself is a controlled instance of (#162), and it is why the
    fix belongs with the marker rather than after it.
    """
    doc = tmp_path / "page.md"
    doc.write_text(
        "# Page\n\n"
        "```python\n"
        ">>> session = Session('store.db')\n"
        ">>> session.no_such_method()\n"
        "```\n",
        encoding="utf-8")

    offenders = _unresolved_calls_in(doc, _defined_names())

    assert [name for _, _, name in offenders] == ["no_such_method"], (
        "a doctest fence is not being read for method names, so any "
        "fence converted to `>>>` drops out of this guard silently "
        f"(#304); offenders={offenders}")


# --- The package-string predicate on its own (#533) ------------------------
#
# Fixture-driven, so each rule is pinned without depending on which
# strings the package happens to carry today. `defined` and `ours` are
# passed in rather than read from the tree, for the same reason.

_FIXTURE_DEFINED = frozenset({"audit", "save"})
_FIXTURE_OURS = frozenset({"session", "Session"})


def test_a_bare_call_that_does_not_exist_is_an_offender():
    """`.no_such()` with no receiver is a claim about our API."""
    assert _offenders_in_text("Tip: run `.no_such()` next.",
                              _FIXTURE_DEFINED, _FIXTURE_OURS) == ["no_such"]
    assert _offenders_in_text("Tip: run `.audit()` next.",
                              _FIXTURE_DEFINED, _FIXTURE_OURS) == []


def test_a_stdlib_receiver_is_not_a_claim():
    """`str.strip()` is a sentence about another library (#533).

    The mutant that treats every receiver as ours reports `strip` here.
    """
    assert _offenders_in_text("normalised as str.strip() does, then "
                              "queue.get() and Record.wrheader().",
                              _FIXTURE_DEFINED, _FIXTURE_OURS) == []


def test_an_ours_receiver_is_a_claim():
    """`session.no_such()` names one of our objects, so it is checked."""
    assert _offenders_in_text("then session.no_such() and Session.audit()",
                              _FIXTURE_DEFINED, _FIXTURE_OURS) == ["no_such"]


def test_a_runtime_message_is_read_too():
    """A constant outside a docstring is a string the package emits."""
    source = ('MSG = "call session.no_such() first"\n'
              'def f():\n    """Docs mention .audit()."""\n')
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend(_offenders_in_text(node.value, _FIXTURE_DEFINED,
                                            _FIXTURE_OURS))
    assert found == ["no_such"]


# --- GettingStarted.ipynb (#634) -------------------------------------------
#
# The notebook is the third place this project puts calls in front of a
# user, and until #634 nothing read it: its export cell passed
# `safe=True, compression="j2k"` to a `session.export()` that has never
# accepted either, so the last runnable cell of the getting-started walk
# raised `TypeError`. The name check above would not have seen it -- the
# method exists -- so this checks the *keywords* too, against the
# signature read by AST from `session.py`, never by importing it (the
# module docstring's reason).
#
# `export(folder, format, **options)` forwards its options to the
# format's door, so its accepted keywords are the union of its own and
# the DICOM door's (`_export_dicom`); the WFDB door is not walked because
# the notebook exports DICOM. A method that takes `**kwargs` and is not
# in that table accepts anything, which is the honest reading of a
# signature this test cannot see through.
NOTEBOOK = REPO / "GettingStarted.ipynb"
SESSION_CLASS = "DicomSession"
_FORWARDS_OPTIONS_TO = {"export": "_export_dicom"}


def _session_signatures(source=None):
    """`{method: (accepted keywords, takes **kwargs)}` for the session class."""
    source = source or (PACKAGE / "session.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == SESSION_CLASS)
    signatures = {}
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        names.discard("self")
        signatures[node.name] = (names, args.kwarg is not None)
    for method, door in _FORWARDS_OPTIONS_TO.items():
        if method in signatures and door in signatures:
            own, _ = signatures[method]
            forwarded, variadic = signatures[door]
            signatures[method] = (own | forwarded, variadic)
    return signatures


def _notebook_code(path):
    """The notebook's code cells as one module, magics and shell lines dropped."""
    cells = json.loads(path.read_text(encoding="utf-8"))["cells"]
    lines = []
    for cell in cells:
        if cell["cell_type"] != "code":
            continue
        for line in "".join(cell["source"]).splitlines():
            lines.append("" if line.lstrip().startswith(("%", "!")) else line)
        lines.append("")
    return "\n".join(lines)


def _notebook_offenders(path, signatures, receiver="session"):
    """`(line, method, keyword or None)` per call the session cannot take."""
    tree = ast.parse(_notebook_code(path))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _receiver_root(node.func) == receiver
                and isinstance(node.func.value, ast.Name)):
            continue
        method = node.func.attr
        if method not in signatures:
            offenders.append((node.lineno, method, None))
            continue
        accepted, variadic = signatures[method]
        for keyword in node.keywords:
            if keyword.arg is None or variadic and method not in _FORWARDS_OPTIONS_TO:
                continue
            if keyword.arg not in accepted:
                offenders.append((node.lineno, method, keyword.arg))
    return offenders


def test_the_getting_started_notebook_calls_methods_with_keywords_they_accept():
    """Every `session.<m>(k=...)` in the notebook must be a call `m` takes (#634).

    Red on the notebook as it stood: `export(..., safe=True,
    compression="j2k")`, two keywords the DICOM door never had.
    """
    signatures = _session_signatures()
    assert "export" in signatures and "ingest" in signatures, signatures.keys()
    offenders = _notebook_offenders(NOTEBOOK, signatures)
    assert not offenders, (
        "GettingStarted.ipynb calls the session with a method or keyword "
        "it does not accept, so the notebook raises for anyone who runs "
        "it (#634):\n" + "\n".join(
            f"    line {line}: session.{method}("
            + (f"{keyword}=...)" if keyword else ") does not exist")
            for line, method, keyword in offenders))


def _notebook(tmp_path, *code_cells):
    path = tmp_path / "nb.ipynb"
    path.write_text(json.dumps({"cells": [
        {"cell_type": "markdown", "source": ["# Not code\n"]},
        *({"cell_type": "code", "source": [code]} for code in code_cells)]}),
        encoding="utf-8")
    return path


_FIXTURE_SESSION = (
    f"class {SESSION_CLASS}:\n"
    "    def ingest(self, directory): pass\n"
    "    def export(self, folder, format='dicom', **options): pass\n"
    "    def _export_dicom(self, folder, subset=None, verify_readback=False): pass\n"
    "    def anything(self, **kwargs): pass\n")


def test_the_notebook_check_resolves_export_through_its_dicom_door(tmp_path):
    """`export(subset=...)` is accepted because `_export_dicom` takes it.

    The mutant that drops the forwarding table reports `subset` unknown
    on a correct notebook -- a wrong-reason red that this fixture is
    green against.
    """
    signatures = _session_signatures(_FIXTURE_SESSION)
    path = _notebook(tmp_path,
                     "%pip install something\n"
                     "session.ingest('in')\n"
                     "session.export('out', subset=df, verify_readback=True)\n",
                     "!ls\nsession.anything(whatever=1)\n")

    assert _notebook_offenders(path, signatures) == []


def test_a_keyword_the_dicom_door_does_not_take_is_flagged(tmp_path):
    """`safe=` and a misspelt method are the two shapes #634 found."""
    signatures = _session_signatures(_FIXTURE_SESSION)
    path = _notebook(tmp_path,
                     "session.export('out', safe=True, compression='j2k')\n"
                     "session.ingets('in')\n"
                     "other.export('out', safe=True)\n")

    assert _notebook_offenders(path, signatures) == [
        (1, "export", "safe"), (1, "export", "compression"),
        (2, "ingets", None)]

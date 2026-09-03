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
import pathlib
import re
import textwrap

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "isocenter"

# `.name(` inside a string literal or a doc fence.
_CALL_IN_TEXT = re.compile(r"\.([A-Za-z_]\w*)\(")

# ```python fences, captured with their offset so a failure can name a
# line rather than a fence ordinal.
_PYTHON_FENCE = re.compile(r"```python\n(.*?)```", re.DOTALL)

# Methods named correctly in prose that belong to the standard library
# or a third-party package, not to Isocenter. Each entry is a CLAIM that
# the name is not an Isocenter method and never should be looked for as
# one -- adding to this set to silence a failure is how the defect class
# this file exists for gets back in.
# Note that `_defined_names()` also picks up every name the package
# imports at module scope, so a third-party name imported anywhere is
# already resolvable and never reaches this set -- `date` below is in
# fact redundant for that reason (`from datetime import date` appears in
# two modules). Test A is therefore looser than "the package defines
# it"; that looseness is priced in on the test itself.
NOT_OURS = frozenset({
    "date",       # datetime.datetime.date() -- redundant, see above
    "wrheader",   # wfdb.Record.wrheader()
    "lower",      # str.lower()
    "upper",      # str.upper()
    "get",        # queue.Queue.get()
    "tobytes",    # numpy.ndarray.tobytes()
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
# would be the same defect this file exists for: seven documented calls
# sit on receivers outside this set and are values of ours --
# `result.*` (six, in `docs/ocr.md`) and `filtered.to_zones()`. All
# seven were hand-resolved against `isocenter/discovery.py` and
# `isocenter/privacy.py` and all exist. The rest (`plt`, `df`, `re`,
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


def test_every_method_named_in_a_package_string_exists():
    """A method named in a string the package emits must be real.

    These are the tips, log lines and error messages a user is told to
    act on. A wrong one costs them an `AttributeError` at best; the two
    this test found first were a `.redact_pixels()` that was renamed
    (#227) and a `.save_config()` that has never existed under any name
    this package has had.

    Resolution is by NAME ONLY, deliberately. A string saying
    `.wrong_receiver.save()` passes because `save` is defined somewhere
    in the package. Receiver-aware resolution is possible but needs the
    thirteen-entry third-party allowlist that the doc-fence half of
    this file avoids by restricting itself to known receivers, and a
    maintained allowlist is a fresh instance of the defect this file
    is about. The looser check still caught every real defect at the
    time it was written; do not "strengthen" it into the wider variant
    without pricing that in.
    """
    defined = _defined_names()
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
            for name in _CALL_IN_TEXT.findall(node.value):
                if name in defined or name in NOT_OURS:
                    continue
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


def test_every_isocenter_call_in_the_docs_resolves():
    """Every documented call on one of our own objects must exist.

    `README.md` and `docs/` are the first thing a user runs. A fence
    that raises `AttributeError` on line three is worse than no fence,
    and there is nothing in the build that reads them.

    Fences that do not parse are skipped rather than failed -- every
    fence parses today, but a future pseudo-code fence should not turn
    this guard red for being prose.
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
        text = path.read_text(encoding="utf-8")
        for match in _PYTHON_FENCE.finditer(text):
            fence_line = text.count("\n", 0, match.start()) + 1
            try:
                # `dedent` first: a fence nested inside a list item is
                # indented, and `ast.parse` raises IndentationError on it.
                # `docs/quickstart.md`'s repair snippet is exactly that,
                # and without this it was silently skipped -- the escape
                # hatch below firing on a real fence with real calls in
                # it rather than on the hypothetical pseudo-code one it
                # was written for (#234).
                tree = ast.parse(textwrap.dedent(match.group(1)))
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
                offenders.append((
                    path.relative_to(REPO).as_posix(),
                    fence_line + node.lineno,
                    node.func.attr))

    assert not offenders, (
        "these documented calls name a method the package does not "
        "define, so the example raises AttributeError for anyone who "
        "runs it (#234):\n"
        + "\n".join(f"    {where}:~{line}: .{name}()"
                    for where, line, name in offenders))

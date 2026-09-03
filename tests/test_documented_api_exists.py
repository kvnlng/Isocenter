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

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "isocenter"

# `.name(` inside a string literal or a doc fence.
_CALL_IN_TEXT = re.compile(r"\.([A-Za-z_]\w*)\(")

# Methods named correctly in prose that belong to the standard library
# or a third-party package, not to Isocenter. Each entry is a CLAIM that
# the name is not an Isocenter method and never should be looked for as
# one -- adding to this set to silence a failure is how the defect class
# this file exists for gets back in.
NOT_OURS = frozenset({
    "date",       # datetime.datetime.date()
    "wrheader",   # wfdb.Record.wrheader()
    "lower",      # str.lower()
    "upper",      # str.upper()
    "get",        # queue.Queue.get()
    "tobytes",    # numpy.ndarray.tobytes()
})


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
    offenders = []
    for path in _package_sources():
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

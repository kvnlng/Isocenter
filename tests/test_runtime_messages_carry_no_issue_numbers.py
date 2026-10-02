"""No runtime message names an issue number (#811, owner ruling Q3 A).

Audit rows, the report, printed notices, log lines and exception messages
carried references such as `(#555)`, `(#715)` and `(#527)`. A user reading
a report or a refusal cannot act on one; the CHANGELOG carries the
history, and a comment beside the literal is where the reasoning lives.

This walks every `isocenter/**/*.py` and reads every string constant that
is not a docstring, the parts of an f-string included. Comments are not
string constants, so they are not read. The allowlist is explicit, keyed
by file and by what matched, and every entry must still match something,
so it cannot outlive its reason.
"""
import ast
import glob
import os
import re

import isocenter

_PACKAGE = os.path.dirname(os.path.abspath(isocenter.__file__))
_ROOT = os.path.dirname(_PACKAGE)
_ISSUE = re.compile(r"#\d{2,4}\b")

#: CSS colours in the HTML manifest's stylesheet, not issue numbers.
_ALLOWED_TEXT = {
    ("isocenter/manifest.py", "#333"),
    ("isocenter/manifest.py", "#666"),
}
#: SQL whose embedded `--` comments cite issues. The schema and its insert
#: statements are never shown to a user.
_SQL_PREFIXES = ("CREATE TABLE", "INSERT INTO")


def _docstring_nodes(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                found.add(id(body[0].value))
    return found


def _hits(source, relpath):
    """Every `(relpath, match, line, sql)` in `source`'s non-docstring
    string constants."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            sql = node.value.strip().startswith(_SQL_PREFIXES)
            for match in _ISSUE.finditer(node.value):
                out.append((relpath, match.group(0), node.lineno, sql))
    return out


def _package_hits():
    hits = []
    for path in sorted(glob.glob(os.path.join(_PACKAGE, "**", "*.py"),
                                 recursive=True)):
        if not path.endswith(".py"):
            continue
        relpath = os.path.relpath(path, _ROOT).replace(os.sep, "/")
        with open(path, encoding="utf-8") as fh:
            hits.extend(_hits(fh.read(), relpath))
    return hits


def test_no_runtime_string_carries_an_issue_number():
    offenders = [f"{path}:{line}: {text}"
                 for path, text, line, sql in _package_hits()
                 if not sql and (path, text) not in _ALLOWED_TEXT]
    assert offenders == [], (
        "runtime strings carry issue numbers a user cannot act on; say what "
        "the number pointed at, and put the number in a comment beside the "
        "literal (#811):\n" + "\n".join(offenders))


def test_every_allowlist_entry_still_matches():
    hits = _package_hits()
    seen = {(path, text) for path, text, _, _ in hits}
    stale = sorted(_ALLOWED_TEXT - seen)
    assert stale == [], f"allowlist entries that match nothing: {stale}"
    assert any(sql for _, _, _, sql in hits), (
        "no SQL literal cites an issue any more; drop the SQL exemption")


def test_the_walk_sees_what_it_must_and_skips_what_it_may():
    """The detector's own arms, so an empty result above is not blind."""
    src = '''
"""Module docstring (#1)."""
def f():
    """Function docstring (#22)."""
    x = 1
    raise ValueError(f"bad {x} (#333)")
LOG = "plain (#4444)"  # a comment (#55)
SQL = """CREATE TABLE t (a -- see #66
)"""
'''
    found = {(text, sql) for _, text, _, sql in _hits(src, "m.py")}
    assert found == {("#333", False), ("#4444", False), ("#66", True)}

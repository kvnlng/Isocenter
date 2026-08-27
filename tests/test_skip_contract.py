"""A test that can never run is not a test (#107).

`tests/test_discovery_integration.py` skipped unconditionally for months
because `faker` was undeclared. It was the single skip in an otherwise
green suite -- dead coverage reporting as a skip rather than as a gap,
over redaction-zone logic.

The rule this pins is the issue's own criterion, applied in both
directions: **a skip's condition must be capable of being either true or
false in a documented environment.** For a skip gated on importing a
module, that means the module has to sit in exactly the optional extras:

- in `install_requires` -> its absence is a broken installation, not a
  reason to pass quietly. `import isocenter` would already have failed.
- in the `tests` extra -> `pytest` itself ships there, so any
  environment that can run the suite at all already has it.
- in no declared extra at all -> it can never be installed, so the skip
  can never be false. This is the `faker` case exactly.

What is left -- `ocr`, `nlp`, `docs` -- is the set a user may
legitimately not have, and `ocr` additionally needs a `tesseract` binary
pip cannot supply. No hand-maintained allowlist: the rule reads the
extras, so adding one is enough.

## What this deliberately cannot see

A skip gated on a boolean rather than an import (`if not HAS_OCR:
self.skipTest(...)`) names no module, so the AST walk is structurally
blind to it. That is the legitimate shape anyway. Do not read a green
run here as "every skip in the suite was checked" -- read it as "every
skip that names a module was checked, and no skip form appeared that
this file does not recognise". The second half is
`test_no_unrecognised_skip_form_has_appeared`, and it is what stops this
guard from going quietly vacuous.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# Extras a documented environment may legitimately lack. Everything else
# declared -- `install_requires`, `tests` -- is present wherever the
# suite can run, so a skip gated on it masks a broken environment.
OPTIONAL_EXTRAS = {"ocr", "nlp", "docs"}

# Import name != distribution name for a handful of packages. Only the
# ones that could plausibly gate a skip need listing; an unknown name
# simply fails the "declared anywhere" half, which is the safe direction.
DISTRIBUTION_TO_MODULE = {
    "pyyaml": "yaml",
    "python-dotenv": "dotenv",
    "pillow": "PIL",
}

SKIP_TOKENS = re.compile(
    r"importorskip|pytest\.skip|pytest\.mark\.skip|skipTest|unittest\.skip")


def _module_name(requirement: str) -> str:
    """`"wfdb>=4.1.0"` -> `"wfdb"`, as the name you would `import`."""
    dist = re.split(r"[<>=!\[;]", requirement, maxsplit=1)[0].strip().lower()
    return DISTRIBUTION_TO_MODULE.get(dist, dist.replace("-", "_"))


def _declared_modules():
    """Every importable name setup.py declares, grouped by where."""
    tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
    required, extras = set(), {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "install_requires":
                required = {_module_name(v.value) for v in kw.value.elts}
            elif kw.arg == "extras_require":
                for key, value in zip(kw.value.keys, kw.value.values):
                    extras[key.value] = {
                        _module_name(v.value) for v in value.elts}

    assert required, "could not parse install_requires out of setup.py"
    assert extras, "could not parse extras_require out of setup.py"
    return required, extras


class _SkipVisitor(ast.NodeVisitor):
    """Collects every skip site, and the module gating it if there is one.

    `visit_Try`/`visit_Call` are not snake_case because they cannot be:
    `ast.NodeVisitor` dispatches on `visit_<ClassName>`, so renaming
    them silently stops the walk rather than raising.
    """
    # pylint: disable=invalid-name

    def __init__(self):
        self.sites = []          # (lineno, module or None)
        self._import_stack = []  # modules imported by an enclosing `try`

    def visit_Try(self, node):
        # `try: import x / except ImportError: pytest.skip(...)` -- the
        # handler names no module, so the gating module has to come from
        # the body it is guarding.
        imported = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                imported |= {a.name.split(".")[0] for a in child.names}
            elif isinstance(child, ast.ImportFrom) and child.module:
                imported.add(child.module.split(".")[0])

        for stmt in node.body:
            self.visit(stmt)
        for handler in node.handlers:
            self._import_stack.append(imported)
            for stmt in handler.body:
                self.visit(stmt)
            self._import_stack.pop()
        for stmt in node.orelse + node.finalbody:
            self.visit(stmt)

    def visit_Call(self, node):
        name = _called_name(node.func)
        if name in {"importorskip", "skip", "skipTest", "skipIf",
                    "skipUnless"}:
            module = None
            if name == "importorskip" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    module = first.value
            elif self._import_stack:
                candidates = self._import_stack[-1]
                # One import in the guarded block is unambiguous. Several
                # means the skip covers all of them, and any one being
                # non-optional is enough to condemn it, so take them all.
                module = sorted(candidates)[0] if candidates else None
            self.sites.append((node.lineno, module))
        self.generic_visit(node)


def _called_name(func):
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _skip_sites():
    """[(path, lineno, module-or-None)] for every skip in tests/."""
    found = []
    for path in sorted(TESTS.glob("*.py")):
        visitor = _SkipVisitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.extend((path, lineno, module) for lineno, module in visitor.sites)
    return found


def test_every_module_gated_skip_names_an_optional_extra():
    """The #107 rule, in both directions."""
    required, extras = _declared_modules()
    optional = set().union(*(extras[e] for e in OPTIONAL_EXTRAS if e in extras))
    non_optional = required | set().union(
        *(v for k, v in extras.items() if k not in OPTIONAL_EXTRAS))

    offenders = []
    for path, lineno, module in _skip_sites():
        if module is None:
            continue
        where = f"{path.relative_to(ROOT)}:{lineno}"
        if module in optional:
            continue
        if module in non_optional:
            offenders.append(
                f"{where}: skips on `{module}`, which is a required "
                "dependency. Its absence is a broken environment, not a "
                "reason to pass quietly -- import it directly so a missing "
                "one is an error.")
        else:
            offenders.append(
                f"{where}: skips on `{module}`, which setup.py declares "
                "nowhere. Nothing can install it, so this skip can never "
                "be false and the test can never run. This is the `faker` "
                "case (#107). Declare it or drop the skip.")

    assert not offenders, "\n".join([""] + offenders)


def test_no_unrecognised_skip_form_has_appeared():
    """Stops this guard from going vacuous the day someone writes
    `@unittest.skipIf` instead of `pytest.importorskip`.

    The AST walk understands a fixed set of call names. A dumb text scan
    understands none of them and cannot be fooled by structure. If the
    text scan finds a skip on a line the AST walk never accounted for,
    the walk has a blind spot -- and a guard with a blind spot reports
    green for the thing it stopped looking at.
    """
    seen = {(path, lineno) for path, lineno, _ in _skip_sites()}

    missed = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue  # this file names the tokens in prose and in a regex
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if SKIP_TOKENS.search(line) and (path, number) not in seen:
                missed.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")

    assert not missed, (
        "a skip form appeared that _SkipVisitor does not recognise, so it "
        "was never checked against the extras. Teach the visitor this "
        "form rather than deleting this assertion:\n" + "\n".join(missed))


def _decorator_skips():
    """[(path, lineno, label)] for every skip expressed as a decorator."""
    found = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            decorators = getattr(node, "decorator_list", [])
            for dec in decorators:
                call = dec if isinstance(dec, ast.Call) else None
                target = call.func if call else dec
                name = _called_name(target)
                if name not in {"skip", "skipIf", "skipUnless"}:
                    continue
                found.append((path, dec.lineno, name, call))
    return found


def test_no_test_is_skipped_in_every_environment():
    """"A test that is skipped in **every** environment is not a test" --
    #107's opening line, and the one shape the extras check cannot see.

    `@pytest.mark.skip`, `@unittest.skip`, `skipIf(True, ...)` and
    `skipUnless(False, ...)` do not depend on the environment at all.
    They report as a skip, which reads as "not applicable here" rather
    than "nobody has run this since it was written".

    A test worth keeping but not worth running is a test worth deleting;
    git remembers it either way. If it is temporarily broken, xfail says
    so honestly -- it still executes, and it tells you when it starts
    passing again.
    """
    offenders = []
    for path, lineno, name, call in _decorator_skips():
        where = f"{path.relative_to(ROOT)}:{lineno}"
        if name == "skip":
            offenders.append(f"{where}: unconditional skip")
            continue
        if call is None or not call.args:
            continue
        condition = call.args[0]
        if not isinstance(condition, ast.Constant):
            continue  # a real runtime condition
        always = bool(condition.value) if name == "skipIf" \
            else not bool(condition.value)
        if always:
            offenders.append(
                f"{where}: {name}({condition.value!r}, ...) never runs")

    assert not offenders, (
        "these skip in every environment, so they are not tests:\n"
        + "\n".join(offenders)
        + "\nDelete them, or use xfail so they still execute.")

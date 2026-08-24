"""The packaging metadata must match what the code actually imports.

Gantry is distributed on PyPI, so `setup.py` is the contract a user gets
when they `pip install gantry` -- `requirements.txt` is not consulted.
Anything imported unguarded at module scope must therefore be declared in
`install_requires`, or the install succeeds and `import gantry` raises.

That is not hypothetical: `python-dotenv` sat in a `requirements.txt`
but not in `setup.py`, while `gantry/config_manager.py` imported it
unguarded, so CI passed (it installed both files) and a real install
would have failed at import. There is now one dependency list.
"""
import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "gantry"

# Import name -> distribution name, where they differ.
DISTRIBUTION_NAMES = {
    "dotenv": "python-dotenv",
    "yaml": "PyYAML",
    "PIL": "pillow",
    "dateutil": "python-dateutil",
}

# Modules in the standard library or provided by this package itself.
LOCAL_PREFIXES = ("gantry", "scripts", "tests")


def _declared_dependencies():
    """Distribution names in setup.py's install_requires, lowercased.

    Parsed with `ast` rather than executed: importing setup.py would run
    setuptools.
    """
    tree = ast.parse((REPO / "setup.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "install_requires":
                names = set()
                for element in keyword.value.elts:
                    spec = element.value
                    name = spec.split(">=")[0].split("==")[0].split("[")[0]
                    names.add(name.strip().lower())
                return names
    raise AssertionError("install_requires not found in setup.py")


def _catches_import_error(try_node):
    """Whether a `try` swallows ImportError.

    A bare `except:` and `except Exception:` both catch ImportError, so
    all three forms make the import optional. Only matching the literal
    name `ImportError` would misread the other two as hard imports.
    """
    for handler in try_node.handlers:
        if handler.type is None:  # bare except
            return True
        caught = ast.unparse(handler.type)
        if "ImportError" in caught or "Exception" in caught:
            return True
    return False


def _module_level_imports(tree):
    """Imports that execute at module import time, with their guard state.

    Only module-scope statements count. An import inside a function body
    runs when that function is called, so a missing package surfaces there
    rather than breaking `import gantry` -- that is a lazy import, not a
    packaging defect.
    """
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            yield statement, False
        elif isinstance(statement, ast.Try):
            guarded = _catches_import_error(statement)
            for inner in statement.body:
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    yield inner, guarded


def _unguarded_third_party_imports():
    """Every third-party module imported unguarded at module scope."""
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())

        for node, guarded in _module_level_imports(tree):
            if guarded:
                continue

            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                # `from . import x` / `from .mod import x` are local.
                if node.level:
                    continue
                names = [node.module or ""]

            for name in names:
                root = name.split(".")[0]
                if not root or root.startswith(LOCAL_PREFIXES):
                    continue
                found.setdefault(root, set()).add(
                    f"{path.relative_to(REPO)}:{node.lineno}")
    return found


def _is_stdlib(module_name):
    import sys
    return module_name in sys.stdlib_module_names


def test_every_unguarded_third_party_import_is_declared_in_setup_py():
    """A hard import missing from install_requires breaks `pip install`."""
    declared = _declared_dependencies()
    assert declared, "parsed no dependencies from setup.py -- parser is broken"

    missing = {}
    for module, sites in _unguarded_third_party_imports().items():
        if _is_stdlib(module):
            continue
        distribution = DISTRIBUTION_NAMES.get(module, module).lower()
        if distribution not in declared:
            missing[distribution] = sorted(sites)

    assert not missing, (
        "imported unguarded but not declared in setup.py install_requires, "
        "so `pip install gantry` would install successfully and then fail at "
        f"`import gantry`: {missing}")


def test_dependencies_have_exactly_one_source_of_truth():
    """No second dependency list may reappear alongside setup.py.

    A `requirements.txt` used to sit beside `install_requires` and the two
    drifted: python-dotenv was in one, pytesseract in the other. CI
    installed both and passed, hiding that either file alone produced a
    broken environment. Keeping one list makes that class of drift
    impossible rather than merely unlikely.
    """
    rival_lists = [
        path for path in (
            REPO / "requirements.txt",
            REPO / "requirements-dev.txt",
            REPO / "Pipfile",
        ) if path.exists()
    ]
    assert not rival_lists, (
        "a second dependency list has reappeared alongside setup.py's "
        f"install_requires: {[p.name for p in rival_lists]}. Declare "
        "dependencies once, in setup.py.")


def test_optional_dependencies_are_not_also_required():
    """A package cannot be both an extra and a hard requirement.

    Listing one in both places is the contradiction that makes `pip install
    gantry[ocr]` and `pip install gantry` disagree about what is optional.
    """
    tree = ast.parse((REPO / "setup.py").read_text())
    extras = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extras_require":
                continue
            for value in keyword.value.values:
                for element in value.elts:
                    spec = element.value
                    name = spec.split(">=")[0].split("==")[0].split("[")[0]
                    name = name.split("@")[0]
                    extras.add(name.strip().lower())

    assert extras, "parsed no extras from setup.py"
    overlap = extras & _declared_dependencies()
    assert not overlap, (
        f"declared as both an extra and a hard requirement: {sorted(overlap)}")


def test_python_requires_matches_the_floor_the_source_actually_needs():
    """`python_requires` is a promise; an unmet one fails after install.

    `@dataclass(slots=True)` is 3.10+, and the current dependency set
    (numpy, imagecodecs) resolves only on 3.12+. Declaring anything lower
    means pip happily installs onto an interpreter that cannot run us.
    """
    tree = ast.parse((REPO / "setup.py").read_text())
    declared = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "python_requires":
                    declared = keyword.value.value
    assert declared is not None, "setup.py declares no python_requires"

    floor = tuple(int(part) for part in declared.replace(">=", "").split("."))

    uses_slots = any(
        "slots=True" in path.read_text() for path in PACKAGE.rglob("*.py"))
    if uses_slots:
        assert floor >= (3, 10), (
            f"python_requires={declared!r} but dataclass(slots=True) needs "
            "3.10+; a 3.9 install would fail at import")

    assert floor >= (3, 12), (
        f"python_requires={declared!r} but the declared dependency set "
        "(numpy, imagecodecs) resolves only on 3.12+")


@pytest.mark.parametrize("module", ["pytesseract", "imagecodecs"])
def test_optional_dependencies_are_imported_defensively(module):
    """Optional features must degrade, not explode.

    If one of these becomes a hard import, it must move into
    install_requires -- otherwise `import gantry` breaks for anyone who
    did not install the extra.
    """
    hard_sites = _unguarded_third_party_imports().get(module)
    assert not hard_sites, (
        f"{module} is imported unguarded at {sorted(hard_sites or [])}, but is "
        "treated as optional. Either guard it with try/except ImportError or "
        "declare it in install_requires.")

"""The packaging metadata must match what the code actually imports.

Isocenter is distributed on PyPI, so `setup.py` is the contract a user gets
when they `pip install isocenter` -- `requirements.txt` is not consulted.
Anything imported unguarded at module scope must therefore be declared in
`install_requires`, or the install succeeds and `import isocenter` raises.

That is not hypothetical: `python-dotenv` sat in a `requirements.txt`
but not in `setup.py`, while `isocenter/config_manager.py` imported it
unguarded, so CI passed (it installed both files) and a real install
would have failed at import. There is now one dependency list.
"""
import ast
import fnmatch
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import tomllib
import warnings
import zipfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "isocenter"

# Import name -> distribution name, where they differ.
DISTRIBUTION_NAMES = {
    "yaml": "PyYAML",
    "PIL": "pillow",
    "dateutil": "python-dateutil",
}

# Modules in the standard library or provided by this package itself.
LOCAL_PREFIXES = ("isocenter", "scripts", "tests")


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
    rather than breaking `import isocenter` -- that is a lazy import, not a
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
        "so `pip install isocenter` would install successfully and then fail at "
        f"`import isocenter`: {missing}")


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
    isocenter[ocr]` and `pip install isocenter` disagree about what is optional.
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


@pytest.mark.parametrize("module", ["pytesseract"])
def test_optional_dependencies_are_imported_defensively(module):
    """Optional features must degrade, not explode.

    If one of these becomes a hard import, it must move into
    install_requires -- otherwise `import isocenter` breaks for anyone who
    did not install the extra.

    **`imagecodecs` left this list in #404, in the direction the docstring
    above describes.** It has been in `install_requires` all along -- it
    was never an extra -- and `io_handlers.py` now imports it unguarded to
    encode JPEG 2000, because Pillow's encoder accepted only `uint8` and
    `uint16` and so wrote nothing at all for CT and MR. A guarded import
    whose absence turns into `Compression failed` is the same shape as a
    loader returning `[]` for a missing shipped resource (#388), and this
    release removes that shape rather than adding one.
    `test_every_unguarded_third_party_import_is_declared_in_setup_py` is
    what covers it now, and it is the stronger check: it reads every
    unguarded import in the package rather than a hand-kept list.
    """
    hard_sites = _unguarded_third_party_imports().get(module)
    assert not hard_sites, (
        f"{module} is imported unguarded at {sorted(hard_sites or [])}, but is "
        "treated as optional. Either guard it with try/except ImportError or "
        "declare it in install_requires.")


def _setup_keyword(name):
    """The literal value passed to setup() for `name`, or None."""
    tree = ast.parse((REPO / "setup.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == name:
                    return ast.literal_eval(keyword.value)
    return None


def test_distribution_metadata_matches_the_shipped_licence():
    """PyPI needs the licence declared, and it must match the LICENSE file.

    Isocenter moved from MIT to AGPLv3, and then to Apache-2.0 (#348: a
    library is adopted by being imported, and the pydicom ecosystem it
    sits in is permissive). A distribution that ships one LICENSE while
    declaring another misstates the terms under which it is published --
    the one piece of packaging metadata with legal weight rather than
    merely operational. Three places name the licence and this test holds
    them together: the LICENSE text, the `license` field, and the
    classifier PyPI categorises by.
    """
    licence_text = (REPO / "LICENSE").read_text()
    assert "Apache License" in licence_text and "Version 2.0" in licence_text, (
        "LICENSE is no longer Apache-2.0; this test pins the three "
        "declarations together and needs updating alongside the licence "
        "change")
    assert "AFFERO" not in licence_text.upper(), (
        "LICENSE still carries AGPL text; a file naming two licences "
        "publishes neither")

    declared = _setup_keyword("license")
    assert declared, "setup.py declares no license; PyPI would show 'UNKNOWN'"
    assert declared == "Apache-2.0", (
        f"setup.py declares license={declared!r} but LICENSE is Apache-2.0; "
        "the field is an SPDX expression and this is its spelling")

    classifiers = _setup_keyword("classifiers") or []
    assert "License :: OSI Approved :: Apache Software License" in classifiers, (
        "no Apache licence classifier; PyPI categorises by classifier, not "
        "by the license field")
    assert not any("Affero" in item for item in classifiers), (
        "the AGPL classifier is still present; two licence classifiers "
        "advertise a dual licence that does not exist")

    citation = (REPO / "CITATION.cff").read_text()
    assert "license: Apache-2.0" in citation, (
        "CITATION.cff names a different licence from LICENSE; this file "
        "is what tooling copies into bibliographies")


def test_classifiers_do_not_advertise_unsupported_python_versions():
    """A `Programming Language :: Python` classifier is a support claim.

    Advertising a version below `python_requires`, or one CI does not
    run, is the same defect as the old `>=3.9`: a promise nothing tests.
    """
    classifiers = _setup_keyword("classifiers") or []
    declared = _setup_keyword("python_requires") or ""
    floor = tuple(int(p) for p in declared.replace(">=", "").split("."))

    advertised = []
    for item in classifiers:
        prefix = "Programming Language :: Python :: "
        if item.startswith(prefix):
            suffix = item[len(prefix):]
            if suffix[0].isdigit() and "." in suffix:
                advertised.append(
                    tuple(int(p) for p in suffix.split(".")))

    assert advertised, "no specific Python version classifiers declared"
    below_floor = [v for v in advertised if v < floor]
    assert not below_floor, (
        f"classifiers advertise {below_floor} but python_requires is "
        f"{declared!r}; pip would refuse to install there")


# --- What the built distributions actually contain -------------------
#
# Everything above reads setup.py. That is not the same contract: the
# defect these next tests exist for was invisible to a source-tree read.
# `isocenter/resources/*.json` shipped in neither the wheel nor the sdist,
# because nothing declared package_data -- and the loaders guarded on
# os.path.exists, so a pip-installed Isocenter did not crash. It audited
# against an empty PHI tag list (`ConfigLoader.load_phi_config` returned
# {}, `PhiInspector` took it) and reported clean. A de-identification tool
# that silently stops looking for PHI is the worst failure this project
# can ship, and every test in the suite passed while it was true, because
# tests import from the source tree where the files are present.
#
# #388 removed the silence at runtime: those loaders now raise
# `RuntimeError` rather than returning an empty collection, so an install
# missing a shipped resource refuses at the first call that needs it. That
# does not retire these tests -- it changes what a missing resource costs,
# from a clean-looking wrong answer to a broken install, and neither is
# something to discover after publishing. The wheel is still the artefact
# under test.
#
# So these build the real artefacts and read what is inside them.

def _tracked_paths_in_package():
    """Paths under isocenter/ that git considers source, or None.

    None means "git could not answer", not "nothing is tracked". An
    empty tracked set is never a valid answer for a package that must
    ship three JSON resources, so an empty result is treated the same as
    a failed call: the caller falls back to the bare walk rather than
    computing an intersection against nothing and passing while checking
    nothing. A guard that goes green because git is missing is the exact
    defect this file's newer tests exist to catch.

    `cwd` is pinned to the repository root rather than inherited,
    because pytest can be invoked from anywhere.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "isocenter"],
            cwd=REPO, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    tracked = {line for line in proc.stdout.splitlines() if line}
    return tracked or None


def _data_files_in_package():
    """Non-Python files under isocenter/ that the code reads at runtime.

    The walk asks git which of them are source. Without that, any
    untracked artefact sitting in the tree -- a macOS `.DS_Store`, an
    editor swapfile, a stray download -- reads as a data file the
    package needs, and both consumers of this set fail telling the
    reader to declare it in `setup.py`'s `package_data`. Following that
    advice ships a Finder artefact in the wheel forever, which is worse
    than the failure it silences (#234).

    The trade, written down because a future reader will otherwise
    "fix" it back to a bare `rglob`: **a brand-new resource file that
    has not been `git add`ed yet is invisible to this test.** That is
    correct semantics rather than a gap -- an untracked file is not in
    the sdist and cannot reach a user -- but it does mean adding a
    resource and running only this test proves nothing until the file
    is staged.

    `pathspec` would let us read `.gitignore` in-process and was
    rejected: a new test dependency for a dotfile filter, where one
    `git ls-files` subprocess answers the question exactly. Asking
    `git check-ignore` per file would be one subprocess per file.
    """
    walked = {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file()
        and path.suffix != ".py"
        and "__pycache__" not in path.parts
    }
    tracked = _tracked_paths_in_package()
    if tracked is None:
        # No git: an unpacked sdist, or git not installed. Fall back to
        # the walk minus dotfiles, which is the crude version of the
        # same filter, rather than erroring or -- worse -- passing.
        return {
            name for name in walked
            if not any(part.startswith(".") for part in name.split("/"))
        }
    tracked_relative = {
        name[len("isocenter/"):] for name in tracked
        if name.startswith("isocenter/")}
    return walked & tracked_relative


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The wheel and sdist setup.py actually produces.

    Built once per module: this shells out to a real build, which costs a
    couple of seconds. `--dist-dir` is repeated per command on purpose --
    distutils applies an option to the command it follows, so a single
    trailing `--dist-dir` would send the sdist to the repo's own dist/.
    """
    if importlib.util.find_spec("setuptools") is None:
        pytest.fail(
            "setuptools is not installed in this environment, so the "
            "distributions cannot be built and this module's guarantees "
            "cannot be checked. It is declared in the `tests` extra: "
            'install with `pip install -e ".[tests]"`.')

    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "setup.py", "-q",
         "sdist", "--dist-dir", str(out),
         "bdist_wheel", "--dist-dir", str(out)],
        cwd=REPO, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"building the distributions failed:\n{result.stderr}")

    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    assert wheels, f"no wheel was built: {result.stderr}"
    assert sdists, f"no sdist was built: {result.stderr}"

    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_names = archive.namelist()
        metadata = next(
            archive.read(name).decode("utf-8")
            for name in wheel_names if name.endswith("dist-info/METADATA"))
        top_level = next(
            (archive.read(name).decode("utf-8").split()
             for name in wheel_names if name.endswith("top_level.txt")), [])

    with tarfile.open(sdists[0]) as archive:
        # Strip the leading `isocenter-<version>/` component.
        sdist_names = [
            name.split("/", 1)[1]
            for name in archive.getnames() if "/" in name]

    return {
        "wheel": wheel_names,
        "sdist": sdist_names,
        "metadata": metadata,
        "top_level": top_level,
    }


def test_the_wheel_ships_every_resource_the_package_reads(built):
    """A data file left out of the wheel silently disables a feature.

    isocenter/resources/redaction_rules.json is the machine redaction
    knowledge base, and leaving it out of the wheel is still
    release-blocking. (This named `phi_tags.json`, the default PHI
    policy, until #495 moved that policy into Python as `FLOOR_POLICY`
    and deleted the file.)

    What changed in #388 is the failure mode, not the requirement. When a
    shipped resource is absent, its loader now raises `RuntimeError` at
    the first call that needs it -- naming the file, the path it looked
    in, and what continuing would have done -- where it used to return an
    empty collection and let the run report success. A wheel without it
    therefore refuses at first use
    instead of degrading, which is why this test still has to fail rather
    than leave the check to runtime: a build that ships without the file
    is broken for every user of it, and finding that out one `pip install`
    later is not the same as finding it out here.
    """
    shipped = {
        name[len("isocenter/"):]
        for name in built["wheel"] if name.startswith("isocenter/")}

    missing = sorted(_data_files_in_package() - shipped)
    assert not missing, (
        "read from isocenter/ at runtime but absent from the wheel, so a "
        "pip-installed Isocenter degrades silently instead of failing: "
        f"{missing}. Every name here is a git-tracked source file, so "
        "the fix is to declare it in setup.py's package_data -- not to "
        "delete it. (Before #234 this advice was given for untracked "
        "artefacts too, and following it shipped them.)")


def test_the_sdist_ships_every_resource_the_package_reads(built):
    """The sdist is what pip builds from when no wheel matches."""
    shipped = {
        name[len("isocenter/"):]
        for name in built["sdist"] if name.startswith("isocenter/")}

    missing = sorted(_data_files_in_package() - shipped)
    assert not missing, (
        f"absent from the sdist: {missing}. A wheel built from this sdist "
        "would inherit the omission. Every name here is a git-tracked "
        "source file, so declaring it in setup.py's package_data is the "
        "fix (#234).")


def _string_literals_in_package():
    """Every `str` constant in `isocenter/**/*.py`, by AST walk.

    An AST walk rather than a regex over source text, so a commented-out
    reference does not count as a reader. An f-string's literal parts
    are `ast.Constant` nodes too, but each holds its *segment* -- for
    `f"{RESOURCES_DIR}/redaction_rules.json"` that is
    `"/redaction_rules.json"`, which is not the basename the caller
    checks for. Measured by review of #391: that rewrite of the
    `RESOURCES_DIR, "redaction_rules.json",` at session.py line 381 turns
    `test_every_shipped_resource_is_named_by_the_package` red. That is
    the safe direction (a resource the walk cannot
    see reads as unnamed, never as named), and it is the same rule the
    test's docstring states: a loader that built the name from parts
    would need this test taught the new spelling.
    """
    literals = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
    return literals


def test_every_shipped_resource_is_named_by_the_package():
    """A file under `isocenter/resources/` is a promise that code reads it.

    `research_tags.json` shipped from 0.7.0 to 0.9.3 and no Python file
    ever named it (#357): its content was `session.py`'s
    `_default_action_for_tag` written out a second time, and the
    scaffold is what runs. The 0.7.0 entry that introduced "four JSON
    resource files" made the promise; this test is what keeps a fifth
    from being made the same way.

    The direction is **resource -> code**, not the reverse:
    `ctp_rules.yaml` is named by `session.py` and deliberately does not
    ship, so "every literal names a file" would be red on purpose. And
    it is a *basename* match against a string literal, because that is
    how every loader here spells its path (`require_package_resource(
    RESOURCES_DIR, "redaction_rules.json", ...)`); a loader that built the name from parts would
    need this test taught the new spelling, which is the right cost.

    Mutations that kill it: restore the file (red: no literal names it);
    misspell the `"redaction_rules.json"` literal in `session.py` (red --
    and that mutant is also a silent-degrade class of its own, since
    `_load_redaction_knowledge_base` returns `[]` when the path is
    missing rather than raising).
    """
    tracked = _tracked_paths_in_package()
    if tracked is None:
        pytest.skip("git could not list the package; see _tracked_paths_in_package")
    resources = sorted(
        name[len("isocenter/resources/"):]
        for name in tracked if name.startswith("isocenter/resources/"))
    assert resources, "no tracked resources found under isocenter/resources/"

    literals = _string_literals_in_package()
    unnamed = [name for name in resources if name not in literals]
    assert not unnamed, (
        f"shipped under isocenter/resources/ and named by no string literal "
        f"in the package: {unnamed}. A resource nothing reads is a promise "
        "nothing keeps (#357); either add the reader or delete the file.")


def test_the_wheel_installs_nothing_but_the_library(built):
    """Installing must not claim a top-level name we do not own.

    `scripts/` carries an __init__.py for the benchmark imports, so a
    bare find_packages() swept it into the distribution and installing
    Isocenter dropped a module called `scripts` into site-packages --
    a name any number of other projects also use.
    """
    assert built["top_level"] == ["isocenter"], (
        f"the wheel installs top-level packages {built['top_level']}; only "
        "'isocenter' belongs to us. Exclude the rest in find_packages().")


def test_no_requirement_is_a_direct_url(built):
    """PyPI rejects any distribution whose metadata carries a URL.

    The nlp extra pinned spaCy's en_core_web_sm to a GitHub release URL.
    That is legal for `pip install -e .` and fatal for `twine upload`,
    which fails with "Can't have direct dependency" -- the upload is
    refused outright, so this is not a defect a user ever sees. We do.
    """
    direct = [
        line for line in built["metadata"].splitlines()
        if line.startswith("Requires-Dist:") and " @ " in line]
    assert not direct, (
        "PyPI refuses metadata containing direct URL requirements, so "
        f"`twine upload` would reject this build: {direct}")


def test_the_sdist_ships_a_test_suite_that_can_run(built):
    """Half a test suite is worse than none.

    The sdist shipped 105 test modules without conftest.py, without
    tests/fixtures/, and without pytest.ini, so `pytest` inside an
    unpacked sdist failed at collection. Ship the suite whole or not at
    all; this test only demands consistency.
    """
    test_modules = [
        name for name in built["sdist"]
        if name.startswith("tests/") and name.endswith(".py")]
    if not test_modules:
        pytest.skip("the sdist deliberately ships no tests")

    required = ["tests/conftest.py", "tests/fixtures/annotations.schema.json",
                "pytest.ini"]
    missing = [name for name in required if name not in built["sdist"]]
    assert not missing, (
        f"the sdist ships {len(test_modules)} test modules but omits "
        f"{missing}, so pytest cannot collect them. Add them to MANIFEST.in "
        "or stop shipping tests.")


# pytest's own defaults for `python_files`. Spelled here rather than read
# from pytest because the test below asserts that `pytest.ini` does not
# override them -- these two patterns are the premise of "collected".
_DEFAULT_TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")


def _tracked_top_level_test_modules():
    """Top-level `tests/*.py` that git tracks, or None (#324's shape).

    Same contract as `_tracked_paths_in_package()` above: `None` means
    "git could not answer", never "nothing is tracked", and the caller
    falls back to the bare glob rather than intersecting against
    nothing and passing while checking nothing.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "tests"],
            cwd=REPO, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    tracked = {
        line for line in proc.stdout.splitlines()
        if line.endswith(".py") and line.count("/") == 1}
    return tracked or None


def test_every_top_level_tests_module_is_one_pytest_collects():
    """A module under tests/ that pytest never collects reads as coverage
    and is not (#347).

    `tests/profile_memory.py` asserted `maxtasksperchild == 10` against a
    `25` that had shipped for releases. It could not go red: `pytest.ini`
    names `testpaths = tests` and no `python_files`, so pytest's default
    `test_*.py` / `*_test.py` patterns applied and the name matched
    neither -- and shipped prose cited it as one of the files defending
    the export subprocess boundary. Two siblings (`detect_memory_leak.py`,
    `profile_export_memory.py`) had the same shape. All three were
    deleted, and this is what stops a fourth. Red on those three when
    written; green on the tree it landed in.

    The same argument as the sdist test above: half a test suite is
    worse than none, and a file that looks like a test and is never run
    is the half that is missing.

    **What is swept, and why.** The top-level `tests/*.py` glob,
    intersected with what git tracks when git can answer (the shape
    `_data_files_in_package()` uses, for #324's reason: a contributor's
    untracked scratch module must not turn CI red, and a file added but
    not yet tracked is invisible, which is correct because CI starts
    from a clean tree). Top level only: `tests/benchmarks/` holds
    `python -m` entry points that are not tests by design, and
    `tests/fixtures/` holds data. That is a statement about *this
    directory's* contents, not about location -- a
    `tests/benchmarks/test_foo.py` *would* be collected, because
    `testpaths` recurses -- so the sweep does not claim benchmarks are
    uncollectable; it claims that at the top level, where the real
    suite lives, every module is one the suite runs.

    **The `pytest.ini` assertion is the premise, not decoration.** The
    default patterns are what makes "collected" mean what this test
    says it means. A `python_files` line would silently redefine it --
    widening it to `*.py` makes every offender collectable and turns
    this test green while changing what the suite runs. Matched on a
    non-comment line (`^\\s*python_files\\s*=`), so a commented-out
    `# python_files = ...` neither satisfies nor trips it; the naive
    `"python_files" not in text` would go red on the comment that
    explains the absence.
    """
    ini = (REPO / "pytest.ini").read_text(encoding="utf-8")
    assert not re.search(r"^\s*python_files\s*=", ini, re.M), (
        "pytest.ini now sets python_files, so pytest's default test-file "
        "patterns no longer decide what is collected and this test's "
        "premise is gone; if that is deliberate, teach this test the new "
        "patterns rather than deleting it (#347)")

    walked = {f"tests/{path.name}" for path in (REPO / "tests").glob("*.py")}
    tracked = _tracked_top_level_test_modules()
    modules = walked if tracked is None else walked & tracked

    collected = {
        name for name in modules
        if any(fnmatch.fnmatch(pathlib.PurePosixPath(name).name, pattern)
               for pattern in _DEFAULT_TEST_FILE_PATTERNS)}
    # A green result is only meaningful if the sweep actually saw the
    # suite. 195 top-level test modules match a default pattern on the
    # tree this landed in -- a measurement, not a pin: the floor below
    # is deliberately far under it, because every branch that adds a
    # test file moves the count and a pin would make that a conflict.
    assert len(collected) >= 150, (
        f"only {len(collected)} collectable modules found under tests/; "
        "the sweep is broken and this test would otherwise pass "
        "vacuously (#347)")

    offenders = sorted(modules - collected - {"tests/conftest.py"})
    assert not offenders, (
        "a module under tests/ that pytest never collects reads as "
        "coverage and is not (#347). These match neither of pytest's "
        f"default patterns {_DEFAULT_TEST_FILE_PATTERNS} and are not "
        "conftest.py, so nothing runs them: rename them to test_*.py so "
        "they run, move them to tests/benchmarks/ if they are entry "
        "points, or delete them:\n    " + "\n    ".join(offenders))


# --- A session a test opens is a session the test must close ---
#
# `Session.close()` releases a `ProcessPoolExecutor` and two threads
# holding sqlite handles. A test that constructs one and walks away
# leaks all of it for the remainder of the pytest process -- measured at
# five threads and five child subprocesses for a single ingest-and-export
# session -- and this suite's history is load-dependent races (#250,
# #343, #274, #297). #371 found 36 top-level modules doing it across 58
# construction sites; one of the 36 was fixed in the commit before this
# test landed, so it went red on the remaining 35 / 57.

_SESSION_CONSTRUCTORS = frozenset({"Session", "DicomSession"})
_SESSION_MODULES = frozenset({"isocenter", "isocenter.session"})


def _session_constructor_names(tree):
    """Local names bound to isocenter's Session class in one module.

    Resolved from the imports rather than hard-coded, because the tree
    spells this four ways: `from isocenter import Session`, `from
    isocenter.session import DicomSession`, and both `as` renames of
    those. A sweep keyed on the literal string `DicomSession(` misses
    `tests/test_unified_config.py`, which is how #371's first pass
    counted 28 offenders where there were 29.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _SESSION_MODULES:
            for alias in node.names:
                if alias.name in _SESSION_CONSTRUCTORS:
                    names.add(alias.asname or alias.name)
    return names


def _enclosing_scopes(tree):
    """Map every node to its nearest enclosing function, and to its class."""
    function_of = {}
    class_of = {}

    def walk(node, function, klass):
        for child in ast.iter_child_nodes(node):
            function_of[child] = function
            class_of[child] = klass
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child, klass)
            elif isinstance(child, ast.ClassDef):
                walk(child, function, child)
            else:
                walk(child, function, klass)

    walk(tree, None, None)
    return function_of, class_of


def _dotted(node):
    """`self.session` -> "self.session"; anything else -> None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_pytest_fixture(node):
    """Is this function decorated as a pytest fixture?"""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any("fixture" in ast.unparse(dec) for dec in node.decorator_list)


def _escapes(scope, target, after):
    """Is `target` handed to someone else, so this scope stops owning it?

    Two spellings, both idioms in this tree:

    * `return session` (also inside a tuple) -- a factory whose caller
      closes what it is given.
    * `opened.append(session)` -- registration with a list a fixture's
      teardown closes in a loop. The loop variable is a different name
      from the one assigned here, so no name match can see that close;
      what is checkable is that the session left this scope.

    The first does **not** apply to a `@pytest.fixture` itself. A fixture
    that `return`s a session hands it to a test function, and a test has
    no idiom for closing what a fixture gave it -- `populated_session` in
    `tests/test_reporting_features.py` was exactly that, and leaked into
    every test that requested it. A fixture owning a session must
    `yield` it and close after, or register it with a list its teardown
    drains; both of those this function and `_closes` already accept.
    """
    if _is_pytest_fixture(scope):
        returns_escape = False
    else:
        returns_escape = True
    for node in ast.walk(scope):
        if (returns_escape and isinstance(node, ast.Return)
                and node.value is not None):
            if node.lineno > after:
                values = (node.value.elts
                          if isinstance(node.value, ast.Tuple)
                          else [node.value])
                if any(isinstance(v, ast.Name) and v.id == target
                       for v in values):
                    return True
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "add")
                and node.lineno > after):
            if any(isinstance(a, ast.Name) and a.id == target
                   for a in node.args):
                return True
    return False


def _closes(scope, target, after=None):
    """Is `<target>.close` mentioned inside `scope` (optionally later)?

    The attribute reference, not only a call of it: `self.addCleanup(
    session.close)` and `stack.callback(session.close)` are how two
    modules in this tree hand the close to someone else, and demanding
    `session.close()` would flag both.
    """
    for node in ast.walk(scope):
        if isinstance(node, ast.Attribute) and node.attr == "close":
            if _dotted(node.value) == target:
                if after is None or node.lineno > after:
                    return True
        # `with session:` re-enters the same object and exits it.
        if isinstance(node, ast.withitem):
            if _dotted(node.context_expr) == target:
                if after is None or node.context_expr.lineno > after:
                    return True
    return False


def _unclosed_session_sites(path, source):
    """Construction sites in one module that nothing visibly closes.

    Yields `(lineno, shape)` for each. See the test below for exactly
    which shapes count as closed and which do not.
    """
    tree = ast.parse(source, filename=str(path))
    constructors = _session_constructor_names(tree)
    if not constructors:
        return [], 0

    parent_of = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_of[child] = node
    function_of, class_of = _enclosing_scopes(tree)

    offenders = []
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            if node.func.id not in constructors:
                continue
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr not in _SESSION_CONSTRUCTORS:
                continue
            if not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id != "isocenter":
                continue
        else:
            continue

        total += 1
        parent = parent_of.get(node)

        # (a) `with Session(...) as s:` -- the shape to prefer.
        if isinstance(parent, ast.withitem):
            continue
        # (d) `return Session(...)` -- a factory hands ownership on.
        if isinstance(parent, ast.Return):
            continue

        if isinstance(parent, ast.Assign) and len(parent.targets) == 1:
            target = parent.targets[0]
            # (b) `s = Session(...)` ... `s.close()` later in the
            # function. Bounded below by this line so an earlier close of
            # a name since rebound does not vouch for the new session,
            # and bounded above by the next rebinding of the same name.
            if isinstance(target, ast.Name):
                scope = function_of.get(node) or tree
                if _closes(scope, target.id, after=node.lineno):
                    continue
                # (e)/(f) ownership left this scope; see `_escapes`.
                if _escapes(scope, target.id, after=node.lineno):
                    continue
                offenders.append((node.lineno, f"{target.id} = ..."))
                continue
            # (c) `self.session = Session(...)` in setUp, closed from
            # tearDown or addCleanup. Different method, so no ordering
            # can be asked for; the class is the scope.
            dotted = _dotted(target)
            if dotted is not None:
                scope = class_of.get(node) or function_of.get(node) or tree
                if _closes(scope, dotted):
                    continue
                offenders.append((node.lineno, f"{dotted} = ..."))
                continue

        offenders.append(
            (node.lineno, f"unrecognised shape ({type(parent).__name__})"))
    return offenders, total


def test_a_session_a_test_opens_is_a_session_the_test_closes():
    """A `Session` built by a test and never closed leaks threads and
    subprocesses into every test that runs after it (#371).

    `close()` shuts down a `ProcessPoolExecutor` and joins two threads
    holding sqlite handles. `tests/test_naming_structure.py` was measured
    at **five threads and five child processes** still live after its one
    test returned; they stay for the life of the pytest process, and this
    suite's recurring failures are load-dependent races (#250, #343,
    #274, #297). 36 modules leaked this way across 58 sites, one of
    which was already fixed when this test was written -- so its own
    red-first run listed the remaining 35 modules and 57 sites.

    **What this checks.** Every call to a name the module imported from
    `isocenter`/`isocenter.session` as `Session` or `DicomSession` --
    resolved from the imports, so an `as` rename is still seen -- must be
    written in one of four shapes:

    * `with Session(...) as s:`, the preferred one;
    * `s = Session(...)` with `s.close` named later in the same function
      (a call, or a bare reference handed to `addCleanup`/`callback`), or
      a later `with s:`;
    * `self.session = Session(...)` with `self.session.close` named
      anywhere in the enclosing class -- `setUp` and `tearDown` are
      different methods, so no ordering can be required here;
    * `return Session(...)`, or `s = Session(...)` later returned, a
      factory handing ownership to its caller -- but not from a
      `@pytest.fixture`, which has no caller that would close it;
    * `opened.append(s)`, registration with a list a fixture teardown
      drains in a loop, where the loop variable is a different name and
      no name match could see the close.

    Anything else is flagged, including shapes that may well be correct
    (`stack.enter_context(Session(...))`, a tuple target, a bare
    expression statement). None exist in the tree today. If one is
    wanted, teach this test the shape rather than deleting the test --
    but prefer writing the site as a plain `with`, which is what the
    flag is asking for.

    **What this does not catch, and it is a real list.**

    * *Reachability, only spelling.* A `close()` in a branch that does
      not run, or after the line that raises, satisfies this test and
      leaks at runtime. That is why `with` is the preferred shape and
      `try/finally` the fallback: both are structural. This test cannot
      tell a `finally` from an `else`.
    * *Factories and registration.* `return Session(...)` is accepted
      without following the caller, and `opened.append(s)` without
      following the list, so a helper whose callers leak passes here.
      Every such helper in the tree today has callers that close.
    * *Sessions built elsewhere.* A module that gets its session from
      `conftest.py`, a fixture in another file, or a plain
      `isocenter.Session` attribute access that is not spelled
      `isocenter.Session(...)`, is invisible to the import scan.
    * *`tests/benchmarks/`.* Top level only, for the same reason as
      `test_every_top_level_tests_module_is_one_pytest_collects` above.
    * *Whether close is the right close.* Two sessions assigned to one
      name with one `close()` between them pass for both; the rebinding
      bound only stops a close *above* the construction from counting.

    It is per-site, not per-file, which is the whole point. The grep
    that found #371 (`contains DicomSession(` and `contains neither
    "with DicomSession" nor ".close()"`) reported 28 modules; this test
    reported 35 on the same tree. The seven it missed are three
    different ways a file-level grep is wrong, all worth knowing:

    * `tests/test_unified_config.py` spells the constructor `Session`,
      the alias the grep did not look for;
    * `test_analysis`, `test_analysis_persistence` and
      `test_optimization` contain `conn.close()` -- a sqlite
      connection, not a session;
    * `test_api_coherence` and `test_wfdb_conformance` contain a real
      `session.close()` belonging to a *different* session several
      tests away from the leaking one;
    * `test_sidecar` matched on **prose**: its docstring reads "Test
      full integration with DicomSession", which the `with
      DicomSession` exclusion counted as a context manager.
    """
    walked = {path for path in (REPO / "tests").glob("*.py")}
    tracked = _tracked_top_level_test_modules()
    if tracked is not None:
        walked = {p for p in walked if f"tests/{p.name}" in tracked}

    offenders = {}
    sites = 0
    for path in sorted(walked):
        found, total = _unclosed_session_sites(
            path, path.read_text(encoding="utf-8"))
        sites += total
        if found:
            offenders[f"tests/{path.name}"] = found

    # A green result is only meaningful if the sweep found the sessions.
    # 290 recognised construction sites on the tree this landed in -- a
    # measurement, not a pin: the floor is deliberately far under it so
    # that adding or removing tests is not a conflict here.
    assert sites >= 200, (
        f"only {sites} Session construction sites found under tests/; the "
        "sweep is broken and this test would otherwise pass vacuously "
        "(#371)")

    report = "\n".join(
        f"    {name}:{lineno}  {shape}"
        for name in sorted(offenders)
        for lineno, shape in offenders[name])
    assert not offenders, (
        f"{sum(len(v) for v in offenders.values())} Session construction "
        f"site(s) in {len(offenders)} module(s) leak a "
        "ProcessPoolExecutor and two sqlite threads into every test that "
        "runs afterwards (#371). Write each as `with Session(...) as s:`, "
        "or close it in a `finally`/`tearDown`/`addCleanup`:\n" + report)


def test_the_build_backend_is_declared_exactly_once():
    """pip needs a PEP 517 backend, and setup.py stays the metadata.

    With no pyproject.toml at all, pip falls back to setuptools'
    `__legacy__` backend -- it works today and is not promised to keep
    working. Declaring the backend fixes that.

    The second half matters more: every test above parses setup.py with
    `ast` to learn what Isocenter depends on. A `[project]` table in
    pyproject.toml would be a second, higher-precedence dependency list
    that those tests cannot see, recreating the exact requirements.txt
    drift that this module exists to prevent.
    """
    pyproject = REPO / "pyproject.toml"
    assert pyproject.exists(), (
        "no pyproject.toml, so builds depend on setuptools' legacy "
        "fallback backend")

    config = tomllib.loads(pyproject.read_text())
    backend = config.get("build-system", {})
    assert backend.get("build-backend"), (
        "pyproject.toml declares no build-backend")
    assert backend.get("requires"), (
        "pyproject.toml declares no build requirements")

    assert "project" not in config, (
        "pyproject.toml declares a [project] table, which overrides "
        "setup.py and silently becomes a second source of dependency "
        "truth. Keep the metadata in setup.py or move all of it here and "
        "rewrite this module's parsers.")


# --- Support claims must be backed by the release matrix ---------------
#
# The version classifiers above are checked against `python_requires`,
# which catches advertising *below* the floor. Neither catches the other
# direction: a claim nothing runs. Since #704 nothing runs the suite on a
# push or a pull request. GitHub runs it only when `publish.yml` calls
# `tests.yml` at release, with two explicit version lists: `test-floor`,
# which blocks the upload, and `test-supported`, which only reports.
# Those lists are what back a classifier, so they are what these tests
# read -- parsed from the YAML, never copied. `tests.yml`'s own default
# list is deliberately not read: it runs only on a hand dispatch, and
# pinning it stayed green while the release matrix could be narrowed
# without a test noticing (#705).

GATE_WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
# `PUBLISH_WORKFLOW`, which the helpers below read, is defined with the
# publish.yml trigger tests further down; it resolves at call time.


def _release_versions(job):
    """The Python versions `publish.yml`'s `job` passes to tests.yml."""
    import yaml

    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    called = workflow["jobs"][job]
    assert called.get("uses") == "./.github/workflows/tests.yml", (
        f"publish.yml's {job} calls {called.get('uses')!r}, not tests.yml; "
        "the version list it passes is no longer the suite's matrix")
    versions = json.loads(called["with"]["python-versions"])
    assert versions, f"publish.yml's {job} passes an empty version list"
    return versions


def _blocking_release_versions():
    """`test-floor`'s versions, after checking it still blocks the upload."""
    import yaml

    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    assert "test-floor" in workflow["jobs"]["publish"]["needs"], (
        "publish no longer needs test-floor, so the floor list runs "
        "without blocking anything")
    return _release_versions("test-floor")


def test_the_release_floor_runs_the_floor_python_requires_declares():
    """A floor is the one claim a single-version job can prove.

    `python_requires=">=3.12"` says a 3.12 user can install and run this.
    Only 3.12 can show that: 3.13 passing says nothing about syntax or a
    stdlib API that does not exist a version earlier. It must be in the
    list that blocks the upload, not the one that only reports.
    """
    declared = _setup_keyword("python_requires") or ""
    floor = declared.replace(">=", "").strip()

    versions = _blocking_release_versions()
    assert floor in versions, (
        f"python_requires={declared!r} but publish.yml's test-floor runs "
        f"{versions}; the floor is advertised and nothing that blocks a "
        "release tests it")


def test_a_free_threading_claim_is_backed_by_a_free_threaded_job():
    """`Free Threading :: 3 - Stable` is a promise `python_requires` cannot make.

    3.14t *is* 3.14 -- the `t` is the build variant, not the version --
    and the wheel is py3-none-any, so neither the version specifier nor
    the ABI tag carries this claim. The classifier is the whole promise.

    It is not a formality: `run_parallel()` chooses threads over
    processes when there is no GIL to escape (`isocenter/parallel.py`),
    and everything heavy funnels through it. A GIL-enabled interpreter
    never executes that path, so a matrix without a `t` build tests none
    of what is being claimed. The build must be in `test-floor`, the list
    that blocks the upload.
    """
    classifiers = _setup_keyword("classifiers") or []
    claims_free_threading = any(
        item.startswith("Programming Language :: Python :: Free Threading")
        for item in classifiers)

    if not claims_free_threading:
        pytest.skip("no free-threading claim to back")

    versions = _blocking_release_versions()
    assert any(v.endswith("t") for v in versions), (
        "setup.py advertises free-threading support but publish.yml's "
        f"test-floor runs {versions} -- no free-threaded build, so "
        "run_parallel()'s no-GIL path is never executed before an upload")


def test_every_version_classifier_is_run_by_the_release_matrix():
    """Each `Programming Language :: Python :: 3.N` classifier has a job.

    The union of `test-floor` and `test-supported` is every version a
    release runs. A classifier outside it advertises a version nothing
    tests -- README's "a test fails if the matrix is narrowed without
    removing the classifier".

    **A `t` build does not stand in for its GIL version.** 3.14t and 3.14
    take different `run_parallel()` paths (threads without a GIL,
    processes with one), so a 3.14t job never runs the path a 3.14 user
    gets. Stripping the `t` here let `test-supported` drop 3.14 with the
    classifier kept and nothing red (#705 review). The versions are
    compared literally; the free-threading classifier is backed by the
    test above.
    """
    prefix = "Programming Language :: Python :: "
    advertised = sorted(
        item[len(prefix):] for item in _setup_keyword("classifiers") or []
        if item.startswith(prefix) and item[len(prefix):][:1].isdigit()
        and "." in item[len(prefix):])
    assert advertised, "no specific Python version classifiers declared"

    run = {version
           for job in ("test-floor", "test-supported")
           for version in _release_versions(job)}
    untested = [version for version in advertised if version not in run]
    assert not untested, (
        f"setup.py advertises Python {untested} but publish.yml's release "
        f"matrix runs only {sorted(run)}; run it or remove the classifier")


def test_the_gate_workflow_cannot_cancel_its_own_release_matrix():
    """publish.yml calls tests.yml twice; both calls must survive.

    A reusable workflow's `github.workflow` is the *caller's* name, so
    `group: ${{ github.workflow }}-${{ github.ref }}` evaluates to the
    same string -- `Publish-refs/heads/main` -- for both invocations.
    With `cancel-in-progress: true`, whichever starts second cancels the
    first.

    Observed on run 33032212241: `test-floor` was cancelled, `publish`
    was correctly skipped, and nothing shipped. The failure that matters
    is the other side of the coin flip -- `test-supported` loses instead,
    `test-floor` passes, and the release goes out having run half the
    matrix behind a green check.

    So the group must vary with what was asked for, and a release's tests
    must not be cancellable at all.
    """
    text = GATE_WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"^concurrency:\n(?:[ \t]+.*\n)+", text, re.M)
    assert block, f"{GATE_WORKFLOW.name} declares no concurrency block"
    block = block.group(0)

    assert "inputs.python-versions" in block, (
        "the concurrency group does not vary with the requested versions, "
        "so publish.yml's two calls to this workflow share a group and "
        "cancel each other")
    assert re.search(r"cancel-in-progress:\s*\$\{\{", block), (
        "cancel-in-progress is unconditional; a release's matrix must not "
        "be cancellable, whatever the PR gate wants")


def test_the_job_cap_cannot_fire_before_a_steps_own_timeout():
    """A job-level timeout that undercuts a step's strips the diagnostics.

    When the job cap fires first, the run dies as 'cancelled' with no
    failing step: #102 raised the Run Tests step to 20 minutes precisely
    so a slow-but-healthy run stops dying as an unexplained failure, and
    the pre-existing job cap of 15 made that allowance unreachable from
    the day it landed (#243; three hangs in #250 each killed at 15m16s
    with the log naming nothing).

    The invariant that keeps this fixed: every step carries its own
    timeout, and the job cap exceeds their sum -- so whatever hangs, the
    timeout that fires belongs to the step that hung, and the job cap is
    only a backstop against what no step timeout covers.
    """
    import yaml

    workflow = yaml.safe_load(GATE_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["test"]
    steps = job["steps"]

    uncapped = [step.get("name") or step.get("uses") or "<unnamed>"
                for step in steps if "timeout-minutes" not in step]
    assert not uncapped, (
        f"steps without their own timeout-minutes: {uncapped}; an "
        "uncapped step makes the job cap the only thing that can stop a "
        "hang there, and a job cap reports no failing step")

    step_total = sum(step["timeout-minutes"] for step in steps)
    job_cap = job.get("timeout-minutes")
    assert job_cap is not None, (
        "the test job has no timeout-minutes; the default is 360, which "
        "lets a hang burn a runner for six hours")
    assert job_cap > step_total, (
        f"jobs.test.timeout-minutes ({job_cap}) does not exceed the sum "
        f"of the step allowances ({step_total}); some step's timeout is "
        "unreachable and a hang there dies as 'cancelled' with no "
        "failing step in the log -- the exact shape of #243/#250")


def test_a_hang_dumps_tracebacks_before_any_timeout_kills_it():
    """A hang must diagnose itself; a timeout only bounds the damage.

    Each of #250's three CI hangs left ~12 minutes of silence and a log
    whose last line was the previous test passing -- a data point, not a
    diagnosis. pytest's built-in faulthandler can dump every thread's
    traceback after a test exceeds a threshold, turning the next
    occurrence into a stack trace of where it stuck.

    The dump only happens if the threshold elapses while the process is
    still alive, so it must sit well under the Run Tests step timeout
    that kills the run.
    """
    import configparser
    import yaml

    ini = configparser.ConfigParser()
    ini.read(REPO / "pytest.ini")
    assert ini.has_option("pytest", "faulthandler_timeout"), (
        "pytest.ini sets no faulthandler_timeout; the next CI hang will "
        "be another silent gap in the log instead of a traceback (#250)")
    threshold = float(ini.get("pytest", "faulthandler_timeout"))

    workflow = yaml.safe_load(GATE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    run_tests = next(s for s in steps if s.get("id") == "suite")
    step_seconds = run_tests["timeout-minutes"] * 60

    assert threshold < step_seconds / 2, (
        f"faulthandler_timeout={threshold:g}s leaves no room to fire "
        f"before the Run Tests step timeout ({step_seconds}s) kills the "
        "process; the dump has to land while the run is still alive")


#: The least the Run Tests step may be allowed, in minutes. Set from
#: the measured step durations of the 2026-09-11 gate runs (#475):
#: 748-993 s on 3.12 and 574-899 s on 3.14t over eight runs each, so
#: the previous cap of 20 minutes (1200 s) was 83% used at the peak,
#: with the milestone still adding tests. 30 minutes puts that peak at
#: 55%. A cap the suite grows into dies as `Terminate orphan process:
#: pytest` with no failing test in the log -- #243's shape -- which is
#: why this is pinned rather than left to be noticed at the next kill.
#: A floor, not an exact value: raising the cap is a decision that
#: needs no red test, dropping it below what the suite needs is the
#: defect. The job cap above it must still exceed the sum of the steps
#: (`test_the_job_cap_cannot_fire_before_a_steps_own_timeout`), and the
#: faulthandler threshold must still sit inside half of it
#: (`test_a_hang_dumps_tracebacks_before_any_timeout_kills_it`), so a
#: raise here is a raise of the whole family.
#:
#: Raised to 45 in v0.9.8 (2026-09-14), when the suite had grown to
#: about 3,800 tests. The last ten gate runs took 1314-1643 s on 3.12 and
#: 1213-1559 s on 3.14t, which put the peak at 91% of 30 minutes. One 3.12
#: run for #614 was killed at 98% of the suite with no failing test.
#: 45 minutes puts the measured peak at 61%.
#:
#: Raised to 75 in v0.9.8 (2026-09-16), before the tag. On PR #690 at
#: 30d9e6b, run 35127628235, the 3.12 Run Tests step was killed at the
#: 45-minute cap with 98% of the suite run and every listed test passing,
#: and 3.14t finished at 44m37s wall. 75 minutes puts that peak near 60%.
#:
#: Lowered to 25 when the suite became four shards per version (#707,
#: 2026-09-21). A step now runs a quarter of the suite. Dispatched run
#: 35636320365 at 83009f1e: 435-751 s on 3.12 and 483-715 s on 3.14t
#: across the four shards, peak 751 s. The rule was the measured peak at
#: no more than 60% of the cap, rounded up to 5 minutes, and never under
#: 20 (faulthandler_timeout = 300 s must sit well inside half of it).
#: That gives 25, which puts the peak at 50%. A floor still: the job cap
#: and faulthandler tests below move with it.
_RUN_TESTS_STEP_MINUTES_FLOOR = 25


def test_the_run_tests_step_keeps_the_headroom_the_suite_needs():
    """The Run Tests step cap cannot quietly drop below what was measured (#475).

    `_RUN_TESTS_STEP_MINUTES_FLOOR` carries the figures. The step is
    found by its `id`, as the faulthandler test finds it, so a renamed
    step is a failed lookup rather than a silent pass over the wrong
    step. publish.yml's `test-floor` and `test-supported` call this
    workflow, and a `uses:` job carries no `timeout-minutes` of its
    own, so the release path inherits exactly this cap.
    """
    _threshold, step_seconds, run_tests = (
        _faulthandler_threshold_and_step_seconds())
    assert run_tests["timeout-minutes"] >= _RUN_TESTS_STEP_MINUTES_FLOOR, (
        f"the Run Tests step allows {run_tests['timeout-minutes']} minutes "
        f"({step_seconds}s); the measured peak shard of 751s needs at "
        f"least {_RUN_TESTS_STEP_MINUTES_FLOOR} (#475, #707), or a healthy but "
        "slow run is killed with no failing test in the log (#243)")


def _faulthandler_threshold_and_step_seconds():
    """The two outer bounds of the timeout-inequality family.

    pytest's faulthandler threshold (the diagnosis window) and the Run
    Tests step cap (the kill). Everything that can stall must resolve
    inside the first, which must sit inside the second.
    """
    import configparser
    import yaml

    ini = configparser.ConfigParser()
    ini.read(REPO / "pytest.ini")
    threshold = float(ini.get("pytest", "faulthandler_timeout"))

    workflow = yaml.safe_load(GATE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    run_tests = next(s for s in steps if s.get("id") == "suite")
    return threshold, run_tests["timeout-minutes"] * 60, run_tests


def test_a_locked_database_errors_inside_one_faulthandler_window():
    """A lock that will not clear must surface as an error, not a stall.

    Every file connection used to be opened with `timeout=900.0` -- a
    bare literal. #250's occurrence four proved what that buys: a
    forked child that could not get the write lock was not going to get
    it at second 890 either, and the 900s busy timeout turned a
    diagnosable `sqlite3.OperationalError: database is locked` into a
    15-minute stall that three CI runs died inside without naming a
    test. The invariant: the busy timeout resolves inside one
    faulthandler window (so the dump shows a thread still *waiting*,
    with a stack), and the error -- not the job cap -- is what ends the
    test.
    """
    import inspect

    from isocenter import persistence

    threshold, _step_seconds, _ = _faulthandler_threshold_and_step_seconds()

    assert persistence._SQLITE_BUSY_TIMEOUT_S < threshold, (
        f"_SQLITE_BUSY_TIMEOUT_S={persistence._SQLITE_BUSY_TIMEOUT_S:g}s "
        f"outlasts the faulthandler window ({threshold:g}s): a stuck "
        "writer stalls past the diagnosis instead of erroring inside it "
        "(#250)")

    source = inspect.getsource(persistence.SqliteStore._get_connection)
    assert "_SQLITE_BUSY_TIMEOUT_S" in source, (
        "_get_connection no longer reads _SQLITE_BUSY_TIMEOUT_S; a "
        "re-inlined literal is exactly how 900.0 went unquestioned for "
        "so long (#250)")


def test_the_worker_watchdog_fires_inside_the_parents_window():
    """A stalled child must dump its own stack before anything kills it.

    pytest's `faulthandler_timeout` sees only the parent's threads:
    #250's occurrence-four dump showed every parent thread idle and
    nothing inside a sqlite write, because the 900-second lock holder
    was a pool child faulthandler cannot see into. The child-side
    watchdog (`parallel._worker_init`) is the other half of that
    picture, and it must fire *before* the parent's window closes so
    the two dumps land in the same log -- and before anything kills the
    run. It is armed by `ISOCENTER_WORKER_FAULTHANDLER`, which only
    tests.yml sets: production users never pay for instrumentation.
    """
    from isocenter import parallel

    threshold, step_seconds, run_tests = (
        _faulthandler_threshold_and_step_seconds())

    assert parallel._WORKER_FAULTHANDLER_TIMEOUT_S < threshold, (
        f"_WORKER_FAULTHANDLER_TIMEOUT_S="
        f"{parallel._WORKER_FAULTHANDLER_TIMEOUT_S:g}s does not fire "
        f"inside the parent's faulthandler window ({threshold:g}s); the "
        "child's dump must land while the parent's diagnosis is still "
        "assembling the same picture (#250)")
    assert threshold < step_seconds, (
        "the parent window itself no longer fits the Run Tests step; "
        "see test_a_hang_dumps_tracebacks_before_any_timeout_kills_it")

    env = run_tests.get("env") or {}
    assert str(env.get("ISOCENTER_WORKER_FAULTHANDLER")) == "1", (
        "tests.yml's Run Tests step does not set "
        "ISOCENTER_WORKER_FAULTHANDLER=1, so the next child-side stall "
        "in CI is again a dump with the child's half missing (#250)")


def test_the_stall_watchdog_fires_inside_the_run_tests_step():
    """A stall outside any test item must still dump before the kill (#250).

    pytest's `faulthandler_timeout` is armed inside
    `pytest_runtest_protocol` and cancelled in its `finally`, so it covers
    setup, call and teardown of one item and nothing else -- not
    collection, not the gap between items, not `pytest_sessionfinish`, and
    not the `atexit` phase that runs after the summary line. A hang in any
    of those is #250's exact signature: killed by an outer cap with no
    failing test named. `tests/conftest.py`'s watchdog covers those gaps,
    and like every other member of this timeout family it is only worth
    anything if it fires while the process is still alive.

    Its threshold must also exceed the longest legitimate gap in a healthy
    run, or it cries wolf; that end is set by measurement, not by an
    assertion here, and recorded beside `_STALL_S`.

    The value is read out of the conftest source rather than by importing
    it: `tests/` is not a package and nothing else in the suite imports
    across test modules.
    """
    _threshold, step_seconds, _ = _faulthandler_threshold_and_step_seconds()

    source = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    match = re.search(r"^_STALL_S = ([0-9.]+)$", source, re.MULTILINE)
    assert match, (
        "tests/conftest.py no longer defines _STALL_S at module scope; the "
        "stall watchdog is how a hang outside a test item names itself "
        "(#250)")
    stall_s = float(match.group(1))

    assert stall_s < step_seconds, (
        f"_STALL_S={stall_s:g}s does not fire before the Run Tests step "
        f"timeout ({step_seconds}s) kills the process; the dump has to "
        "land while the run is still alive (#250)")

    assert "faulthandler.dump_traceback" in source, (
        "the watchdog no longer dumps thread tracebacks, so a stall "
        "outside a test item is again a silent gap in the log (#250)")
    assert "os.dup(2)" in source and "def pytest_configure" in source, (
        "the watchdog no longer owns an fd duplicated inside "
        "pytest_configure, where global capture is suspended; measured, a "
        "dup taken at conftest import time lands on the capture temp file "
        "and the dump reaches nobody (#250)")


# ---------------------------------------------------------------------------
# Invalid escape sequences (#292)
# ---------------------------------------------------------------------------

# Roots swept for invalid escape sequences. `isocenter/` is the one that
# matters for a user -- an invalid escape there becomes an `import
# isocenter` failure the day CPython escalates -- but `scripts/` and
# `tests/` are swept too, because #292's goal is that a new one cannot
# land anywhere, and compiling all 224 files costs about 0.2s.
_ESCAPE_SWEEP_ROOTS = ("isocenter", "scripts", "tests")


def _python_files_under(root: pathlib.Path):
    """Every `.py` file under `root`, skipping bytecode caches."""
    return sorted(
        path for path in root.rglob("*.py")
        if "__pycache__" not in path.parts)


def test_no_shipped_module_carries_an_invalid_escape_sequence():
    """An invalid escape sequence is a warning today and a failure later.

    CPython's own account of it: 3.6 made an unrecognised `\\x` escape in
    a non-raw string a `DeprecationWarning`, 3.12 promoted it to a
    `SyntaxWarning`, and the documentation says "In a future Python
    version they will raise a SyntaxError". No release is named, so the
    only safe reading is that it happens. This project's floor is 3.12 --
    exactly where the `SyntaxWarning` begins -- so this guard behaves
    identically across the whole support matrix.

    The shape of the check is load-bearing, and the obvious alternative
    is worse in a way that hides defects:

    - `simplefilter("error", SyntaxWarning)` and catching `SyntaxError`
      also detects the problem, but the escalated warning aborts the
      compile at the *first* site in a file. A second invalid escape in
      the same module is invisible until the first is fixed. Recording
      instead of raising reports every site in every file in one run.
    - `simplefilter("always")` is not decoration. Warnings are deduped
      per location by default, and the default filters can drop a repeat
      entirely; without `always` a second occurrence can go unrecorded.

    Both traps produce a guard that passes while the defect is present,
    which is the failure mode this milestone is named for. Note also
    that under the escalating filter the problem surfaces as
    `SyntaxError`, not `SyntaxWarning` -- so a guard written as
    `pytest.warns(SyntaxWarning)` around an escalated compile passes
    vacuously. This one records.
    """
    offenders = []
    compiled = 0
    for root_name in _ESCAPE_SWEEP_ROOTS:
        root = REPO / root_name
        assert root.is_dir(), (
            f"{root_name}/ does not exist, so this guard would sweep "
            "nothing and pass vacuously; update _ESCAPE_SWEEP_ROOTS if "
            "the layout moved")
        for path in _python_files_under(root):
            compiled += 1
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                compile(path.read_bytes(), str(path), "exec")
            for entry in recorded:
                if issubclass(entry.category, SyntaxWarning):
                    offenders.append((
                        path.relative_to(REPO).as_posix(),
                        entry.lineno,
                        str(entry.message)))

    # A green result is only meaningful if the sweep actually ran.
    # Same precedent as #299's `len(subclasses) >= 5` guard.
    assert compiled > 200, (
        f"only {compiled} files compiled; the sweep is broken and this "
        "test would otherwise pass vacuously")

    shipped = [o for o in offenders if o[0].startswith("isocenter/")]
    unshipped = [o for o in offenders if not o[0].startswith("isocenter/")]

    def _render(rows):
        return "\n".join(f"    {name}:{line}: {message}"
                         for name, line, message in rows)

    detail = []
    if shipped:
        detail.append(
            "in the shipped package -- these become `import isocenter` "
            "failures when CPython escalates:\n" + _render(shipped))
    if unshipped:
        detail.append(
            "outside the shipped package -- these break the tooling "
            "rather than the install, and are still defects:\n"
            + _render(unshipped))

    assert not offenders, (
        "invalid escape sequences found; CPython warns about them today "
        "and the documentation says a future version will raise "
        "`SyntaxError` (#292).\n" + "\n".join(detail))


# ---------------------------------------------------------------------------
# #376: the platform claim must match the platform the code needs
# ---------------------------------------------------------------------------

def _module_scope_imports_of(module_name):
    """Every unguarded module-scope `import <module_name>` under isocenter/.

    Walks `tree.body` only -- the statements that *are* module scope --
    so an import inside `try:`, `if:`, a function or a class is not
    counted. That narrowness is the point: a `try: import fcntl` would
    make the POSIX classifier true and this walk's answer false, and the
    walk must say "unbacked" in that case rather than "backed".
    """
    hits = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                if any(alias.name == module_name for alias in node.names):
                    hits.add((path.relative_to(REPO).as_posix(), node.lineno))
            elif isinstance(node, ast.ImportFrom) and node.module == module_name:
                hits.add((path.relative_to(REPO).as_posix(), node.lineno))
    return hits


def test_the_platform_classifier_matches_the_fcntl_import():
    """`Operating System :: POSIX`, because the code is (#376).

    Every sidecar write takes `fcntl.flock`, and the gate and pass-lock
    beside the sidecar are `flock` files too (#368). `fcntl` does not
    exist on Windows, so `Operating System :: OS Independent` promised a
    platform on which the first sidecar write raised
    `ModuleNotFoundError: No module named 'fcntl'` -- after a
    successful install. The claim and the import are checked against
    each other in both directions: the classifier must be POSIX, and
    `import fcntl` must be at module scope in the package, so that on
    Windows the failure is at `import isocenter`, where the packaging
    claim is checked, rather than at the first write. If `fcntl` ever
    leaves the tree, this test says the POSIX claim is now the unbacked
    one -- which is the direction a Windows port would take.
    """
    classifiers = _setup_keyword("classifiers") or []

    assert not any("OS Independent" in item for item in classifiers), (
        "setup.py claims `Operating System :: OS Independent`, but every "
        "sidecar write takes fcntl.flock and fcntl does not exist on "
        "Windows: a Windows install succeeds and fails at the first "
        "sidecar write (#376)")
    assert any(item.startswith("Operating System :: POSIX")
               for item in classifiers), (
        "setup.py carries no `Operating System :: POSIX` classifier; the "
        "flock-based sidecar, gate and pass-lock are POSIX-only and the "
        "metadata must say so (#376)")

    fcntl_sites = _module_scope_imports_of("fcntl")
    assert fcntl_sites, (
        "no module-scope `import fcntl` under isocenter/. Either the "
        "import is guarded or function-local (then a Windows install "
        "fails at the first sidecar write instead of at `import "
        "isocenter`), or fcntl is gone -- in which case the POSIX "
        "classifier is the unbacked claim now (#376)")


def test_sidecar_lock_files_are_ignored_before_any_exist():
    """`*.lock` is in `.gitignore` (#376).

    Tests write beside the sidecar in the repo root and CLAUDE.md says
    to leave those artefacts alone rather than add cleanup, so the gate
    and pass-lock files (`<name>_pixels.bin.lock`,
    `<name>_pixels.bin.pass.lock`) would otherwise sit untracked in the
    root after the first run. Low value on its own; its job is to fail
    *before* a lock file is committed by accident.
    """
    patterns = [line.strip()
                for line in (REPO / ".gitignore").read_text(
                    encoding="utf-8").splitlines()]
    assert "*.lock" in patterns, (
        ".gitignore has no `*.lock` pattern; the sidecar gate and "
        "pass-lock files would be left untracked in the repo root by the "
        "first test run (#376)")


def test_the_sidecar_gate_deadline_sits_inside_the_timeout_family():
    """`120 < _SIDECAR_GATE_TIMEOUT_S < 240 < 300`, and the helper reads it.

    The sidecar gate (#368) is the one lock deliberately held across a
    sqlite write, so a waiter behind a holder that is itself waiting out
    `_SQLITE_BUSY_TIMEOUT_S` must not give up first: it would raise a
    gate error that misnames the fault (the database is what is stuck)
    and, on the save path, leave the instances dirty when the holder was
    seconds from succeeding. Above that, it must expire inside both
    faulthandler windows (`_WORKER_FAULTHANDLER_TIMEOUT_S` in a worker,
    pytest's threshold in the parent) so a stuck gate dumps a thread
    *waiting at the gate* with a stack and the error, not the job cap,
    ends the test (#280, #250). And the helper must read the constant by
    name: a re-inlined literal is exactly how `timeout=900.0` went
    unquestioned for so long (2026-09-08 spec §2.3).
    """
    import inspect

    from isocenter import parallel, persistence

    threshold, _step_seconds, _ = _faulthandler_threshold_and_step_seconds()
    gate = persistence._SIDECAR_GATE_TIMEOUT_S

    assert persistence._SQLITE_BUSY_TIMEOUT_S < gate, (
        f"_SIDECAR_GATE_TIMEOUT_S={gate:g}s is not above "
        f"_SQLITE_BUSY_TIMEOUT_S={persistence._SQLITE_BUSY_TIMEOUT_S:g}s: a "
        "writer queued behind one legitimately waiting out sqlite would "
        "expire first and misname the fault (#368)")
    assert gate < parallel._WORKER_FAULTHANDLER_TIMEOUT_S, (
        f"_SIDECAR_GATE_TIMEOUT_S={gate:g}s outlasts the worker "
        f"faulthandler ({parallel._WORKER_FAULTHANDLER_TIMEOUT_S:g}s): a "
        "stuck gate in a worker dumps nothing before the parent gives up "
        "(#368)")
    assert gate < threshold, (
        f"_SIDECAR_GATE_TIMEOUT_S={gate:g}s outlasts the faulthandler "
        f"window ({threshold:g}s): a stuck gate stalls past the diagnosis "
        "instead of erroring inside it (#368)")

    # By AST, not by substring: the helper's docstring names the constant
    # in prose, and a substring check would be satisfied by that alone
    # while the body carried a literal.
    import textwrap

    source = inspect.getsource(persistence.SqliteStore._hold_sidecar_gate)
    names = {node.id for node in ast.walk(ast.parse(textwrap.dedent(source)))
             if isinstance(node, ast.Name)}
    assert "_SIDECAR_GATE_TIMEOUT_S" in names, (
        "_hold_sidecar_gate no longer reads _SIDECAR_GATE_TIMEOUT_S; a "
        "re-inlined literal is exactly how 900.0 went unquestioned (#368)")


# ---------------------------------------------------------------------------
# The documentation deploy (#635) -- the one workflow that publishes to a
# live site, and until this test nothing in the suite read it at all
# ---------------------------------------------------------------------------

DOCS_WORKFLOW = REPO / ".github" / "workflows" / "docs.yml"


def test_the_docs_deploy_builds_strict_from_the_docs_extra():
    """`docs.yml` builds `--strict`, deploys only from release tags, and caps every step.

    Five assertions about the only workflow in the repository that
    publishes somewhere a reader can see: `mkdocs gh-deploy` pushes
    straight to the live documentation site.

    **Strict.** Without `--strict` a mkdocs or griffe warning goes into
    a log nobody reads and the site deploys anyway (#635). Measured by
    mutation on 0.9.8: a broken relative link under `docs/` is red under
    `--strict` and **green on all 38 documentation tests the PR gate
    runs**, so the flag closes a class nothing else covers -- the gap
    `tests/test_doc_anchors.py`'s module docstring already writes down.
    It is not a superset of those tests and does not retire them: a
    second line at the base indent under a `Returns:`, which griffe
    renders as several untyped "returned value N" rows, draws no warning
    at all and is caught only by
    `tests/test_api_docstrings_render_cleanly.py` (#565).

    Safe on a live site because `gh-deploy` builds before it pushes:
    `gh_deploy_command` calls `build.build(cfg)` inside a `try/finally`
    and reaches `gh_deploy.gh_deploy(...)` only afterwards, and strict
    makes `build.build` raise `Abort` (mkdocs 1.6.1). A warning costs a
    red deploy run and a site still serving its previous build; it
    cannot publish a broken one.

    **The ref filter** was pinned by nothing before this test, and its
    comment records the two unreviewed deploys from a feature branch it
    exists to prevent. Since the release-branch procedure (`RELEASING.md`)
    it is release tags rather than `main`: `main` is the development
    branch, and the documentation follows the latest published release.
    The trigger set is asserted as an equality because a `pull_request`
    trigger on a workflow that deploys
    to the live site is not a thing to notice in review. Which tag may
    deploy is the next test's.

    **The package list has one home.** The workflow hand-copied five
    distributions, naming `pymdown-extensions` (absent from `setup.py`'s
    `docs` extra) and omitting `mkdocs` (present there). The two lists
    resolved to the identical eleven packages at the identical versions
    when measured, which is what a second source of truth looks like
    right up to the edit that moves one of them.

    **The caps** are the inequality `tests.yml` carries -- job cap above the sum of the step caps, every step
    capped -- so whatever hangs, the timeout that fires belongs to a
    step and names it. `docs.yml` had it backwards: a job cap of 10 over
    step caps of 3 + 3 + 5 = 11, with three steps uncapped entirely, so
    a long pip step was killed by the job cap, which says only "the docs
    job hung".

    **PyYAML parses the bare `on` key as the boolean `True`** (YAML 1.1
    treats `on`/`off`/`yes`/`no` as booleans), so the triggers live at
    `workflow[True]`, not `workflow["on"]` -- a `KeyError` on the
    string, and `assert "on" in workflow` would pass vacuously. Do not
    "fix" the lookup.

    Deliberately *not* here: any invocation of mkdocs. The build needs
    the `docs` extra, which the `tests` environment does not have, and a
    test that skips when mkdocs is absent reads as a pass --
    `tests/test_doc_anchors.py`'s fourth deferral makes that argument.
    """
    import yaml

    assert DOCS_WORKFLOW.exists(), (
        f"{DOCS_WORKFLOW.relative_to(REPO)} does not exist; the API "
        "reference is generated from docstrings, so nothing else "
        "publishes it")
    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["deploy"]
    steps = job["steps"]

    # 1. Strict. Found by walking the steps for the deploy command, so a
    #    flag moved to another step or another job is still found.
    deploying = [step for step in steps
                 if "gh-deploy" in (step.get("run") or "")]
    assert len(deploying) == 1, (
        f"{len(deploying)} steps run `mkdocs gh-deploy`; exactly one "
        "must, or which one publishes the site is ambiguous")
    assert "--strict" in deploying[0]["run"], (
        f"the deploy step runs {deploying[0]['run']!r} without "
        "`--strict`, so a mkdocs or griffe warning publishes the site "
        "anyway and the warning lands in a log nobody reads (#635). A "
        "strict failure cannot publish a broken site: gh-deploy builds "
        "before it pushes and aborts inside the build")

    # 2. The ref filter: release tags only, never a branch. `main` is the
    #    development branch, and the site follows the latest published
    #    release, not what has merged since.
    triggers = workflow[True]
    assert triggers["push"] == {"tags": ["v*"]}, (
        f"docs.yml deploys on pushes matching {triggers['push']!r}; it "
        "must deploy on release tags (`v*`) and nothing else. A branch "
        "here publishes unreleased documentation -- `main` is the "
        "development branch -- and a feature branch here is the "
        "unreviewed deploy that happened twice during the isocenter "
        "rename. A `paths` filter here would skip the deploy of a release "
        "whose docs did not change since the last tag, leaving the "
        "previous release's API reference live")

    # 3. The trigger set.
    assert set(triggers) == {"push", "workflow_dispatch"}, (
        f"docs.yml triggers on {sorted(str(key) for key in triggers)}; it "
        "must be push (release tags) and workflow_dispatch and nothing "
        "else -- a `pull_request` trigger on a workflow that deploys to "
        "the live site publishes every pull request")

    # 4. One home for the package list.
    installing = [step for step in steps
                  if "pip install" in (step.get("run") or "")]
    assert len(installing) == 1, (
        f"{len(installing)} steps run `pip install`; the packages the "
        "docs build needs come from one place or from two")
    install = installing[0]["run"]
    assert ".[docs]" in install, (
        f"the install step runs {install!r} rather than installing the "
        "`docs` extra; a hand-copied package list is a second source of "
        "truth for what the docs build needs, and it is what #635 filed "
        "-- the old list named pymdown-extensions, which setup.py does "
        "not, and omitted mkdocs, which it does")
    declared = _setup_keyword("extras_require")["docs"]
    restated = sorted(
        name for name in (spec.split(">=")[0].split("==")[0].split("[")[0]
                          for spec in declared)
        if name in install)
    assert not restated, (
        f"the install step names {restated} itself as well as the `docs` "
        "extra; the extra in setup.py is the one home for that list "
        "(#635)")

    # 5. The cap inequality, both halves.
    uncapped = [step.get("name") or step.get("uses") or step.get("run")
                for step in steps if "timeout-minutes" not in step]
    assert not uncapped, (
        f"docs.yml steps without their own timeout-minutes: {uncapped}; "
        "an uncapped step makes the job cap the only thing that can stop "
        "a hang there, and a job cap reports no failing step")
    step_total = sum(step["timeout-minutes"] for step in steps)
    job_cap = job.get("timeout-minutes")
    assert job_cap is not None, (
        "the deploy job has no timeout-minutes; the default is 360, "
        "which lets a hung deploy burn a runner for six hours")
    assert job_cap > step_total, (
        f"jobs.deploy.timeout-minutes ({job_cap}) does not exceed the sum "
        f"of the step allowances ({step_total}); some step's timeout is "
        "unreachable and a hang there dies as 'cancelled' with no failing "
        "step in the log -- the shape of #243/#250, and the state docs.yml "
        "was in when #635 was written")


def _step_named(workflow_path, job, name):
    """One step of a workflow job, by its `name`, or fail saying which."""
    import yaml

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = [step for step in workflow["jobs"][job]["steps"]
             if step.get("name") == name]
    assert len(steps) == 1, (
        f"{workflow_path.name} job {job!r} has {len(steps)} steps named "
        f"{name!r}; this test runs that step's script, so it must exist "
        "exactly once under that name")
    return steps[0]


def _run_step_script(step, cwd, env):
    """Run a workflow step's `run:` script under bash, as the runner does.

    Refuses a script containing a `${{ }}` expression: GitHub substitutes
    those before bash sees the text, so a script that uses one cannot be
    executed faithfully here -- and interpolating an input into a script
    is the injection shape Actions' own hardening guide warns about.
    Values reach the script through the step's `env:` instead. The runner
    starts `run:` steps as `bash -e`, so this does too.
    """
    import os
    import shutil

    script = step["run"]
    assert "${{" not in script, (
        f"step {step.get('name')!r} interpolates an expression into its "
        "script; pass it through `env:` so the script is the script that "
        "runs, and so this test can run it")
    bash = shutil.which("bash")
    assert bash, "bash is required to run a workflow step's script"
    return subprocess.run(
        [bash, "-e", "-c", script], cwd=str(cwd), capture_output=True,
        text=True, env={"PATH": os.environ["PATH"], **env}, check=False)


DOCS_LATEST_TAG_STEP = "Deploy only the latest release tag"


def test_the_docs_deploy_refuses_any_ref_but_the_latest_release_tag(tmp_path):
    """The site follows the latest release tag, and only that.

    A `v*` tag trigger alone would redeploy the site from whichever tag
    was pushed last. Under the release-branch procedure that is not
    always the newest release: a patch to an older line (a `v0.9.9` on
    `release/0.9` after `v0.10.0` shipped) would replace the newer
    documentation with the older line's. And `workflow_dispatch` can be
    started from any branch, including `main`, the development branch.

    So a job the deploy needs refuses unless the ref is a `v*` tag and
    that tag is the highest `v*` tag by version order, with `a`, `b` and
    `rc` sorted as pre-releases: under git's default version sort
    `v1.0.0rc1` outranks `v1.0.0`, and the 1.0.0 release would be refused
    for as long as its release candidate's tag existed. The script is
    executed here, against a scratch repository with real tags, rather
    than grepped: a guard that names the right strings and compares them
    wrongly passes a text search.
    """
    import os

    import yaml

    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    guard = _step_named(DOCS_WORKFLOW, "guard", DOCS_LATEST_TAG_STEP)
    assert "if" not in guard, (
        "the latest-tag guard is conditional; a condition is a way for "
        "some trigger to skip it")
    checkout = next(step for step in workflow["jobs"]["guard"]["steps"]
                    if str(step.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0, (
        "the guard's checkout does not fetch full history, so the runner "
        "has no tags to compare against and the guard cannot know the "
        "latest")

    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    def run_git(*args, when=None):
        dated = ({"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
                 if when else {})
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True,
                       env={"PATH": os.environ["PATH"], **git_env, **dated})

    days = iter(range(1, 29))

    def tag(name):
        when = f"2026-01-{next(days):02d}T12:00:00+00:00"
        run_git("commit", "-q", "--allow-empty", "-m", name, when=when)
        run_git("tag", name, when=when)

    def guarded(ref):
        return _run_step_script(guard, repo, {
            **git_env, "GITHUB_REF": ref,
            "GITHUB_REF_NAME": ref.split("/", 2)[-1]})

    def deploys(latest, refused):
        result = guarded(latest)
        assert result.returncode == 0, (
            f"the guard refused {latest}, the latest release tag:\n"
            f"{result.stdout}{result.stderr}")
        for ref in refused:
            result = guarded(ref)
            assert result.returncode != 0, (
                f"the guard let {ref} deploy the site; only the latest `v*` "
                f"tag ({latest}) may, or an older release, a pre-release or "
                "a branch replaces the current release's documentation")

    run_git("init", "-q")
    # Tagged out of version order on purpose: v0.9.9 is created last, a
    # day after v0.10.0, and sorts after v0.10.0 as text -- and v0.10.0 is
    # still the latest. The dates are explicit so creation order is
    # unambiguous; created in one second they tie, and a guard sorting by
    # creation date would pass by accident.
    for name in ("v0.9.7", "v0.9.8", "v0.10.0", "v0.9.9"):
        tag(name)
    # A tag outside the release pattern that sorts above every `v*` tag,
    # so a guard that drops the `v*` pattern compares against it.
    tag("zz-not-a-release")
    # A branch spelled like the latest tag: its short name is the latest
    # tag's, so only the ref filter refuses it.
    run_git("branch", "v0.10.0")
    deploys("refs/tags/v0.10.0",
            ("refs/tags/v0.9.9", "refs/tags/v0.9.8",
             "refs/tags/zz-not-a-release", "refs/heads/main",
             "refs/heads/release/0.10", "refs/heads/v0.10.0"))

    # A release candidate, then its release. Under git's default version
    # sort `v1.0.0rc1` outranks `v1.0.0`, and the release is refused.
    tag("v1.0.0rc1")
    tag("v1.0.0")
    deploys("refs/tags/v1.0.0", ("refs/tags/v1.0.0rc1", "refs/tags/v0.10.0"))


def test_a_refused_docs_run_cannot_cancel_a_deploy():
    """The guard is its own job, and only the deploy job is in the group.

    The deploy uses `concurrency` with `cancel-in-progress`, so the newest
    deploy wins -- which is right only among runs that will deploy. When
    the group sat at workflow level, a run the guard was about to refuse
    (a patch tag on an older line, a dispatch from a branch, the second
    of two tags pushed together) joined it first, cancelled the latest
    release's deploy in progress, then refused itself: the site stayed on
    whatever was live before that release. With the guard in a job with
    no group, and the deploy needing it, only runs that passed the guard
    compete.
    """
    import yaml

    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert set(jobs) == {"guard", "deploy"}, sorted(jobs)
    assert "concurrency" not in workflow, (
        "docs.yml has a workflow-level concurrency group; a run its guard "
        "refuses joins it and cancels the latest release's deploy in "
        "progress")
    assert "concurrency" not in jobs["guard"], (
        "the guard job is in a concurrency group; a refused run must not "
        "be able to cancel anything")
    assert jobs["deploy"].get("needs") in ("guard", ["guard"]), (
        "the deploy job does not need the guard, so it runs whether or not "
        "the ref is the latest release tag")
    assert "if" not in jobs["deploy"], (
        "the deploy job is conditional; `if: always()` or similar runs it "
        "after a refused guard")
    concurrency = jobs["deploy"].get("concurrency") or {}
    assert concurrency.get("cancel-in-progress") is True \
        and concurrency.get("group"), (
            "the deploy job has no concurrency group with "
            "cancel-in-progress; two deploys of the latest tag could "
            "interleave their pushes to gh-pages")
    assert not any(step.get("name") == DOCS_LATEST_TAG_STEP
                   for step in jobs["deploy"]["steps"]), (
        "the latest-tag guard runs in the deploy job, inside its "
        "concurrency group")

    steps = jobs["guard"]["steps"]
    uncapped = [step.get("name") or step.get("uses") for step in steps
                if "timeout-minutes" not in step]
    assert not uncapped, f"guard steps without timeout-minutes: {uncapped}"
    step_total = sum(step["timeout-minutes"] for step in steps)
    assert jobs["guard"].get("timeout-minutes", 0) > step_total, (
        f"jobs.guard.timeout-minutes does not exceed the sum of its step "
        f"allowances ({step_total}); a hang there dies as 'cancelled' with "
        "no failing step in the log")


PUBLISH_WORKFLOW = REPO / ".github" / "workflows" / "publish.yml"
PUBLISH_TAG_STEP = "The ref must be a release tag matching the version"


def test_publishing_is_a_manual_run_with_no_event_that_uploads_by_itself():
    """`publish.yml` runs only when someone dispatches it.

    Under the release-branch procedure (`RELEASING.md`) nothing publishes
    as a side effect: not a pushed tag, and not a published GitHub
    Release -- which Zenodo still archives, so creating one must upload
    nothing. The dispatch input decides the index, and anything that is
    not exactly `pypi` goes to TestPyPI. Every expression that read
    `github.event_name` is gone: with one trigger it could only ever
    evaluate one way, and a dead branch in an upload expression is where
    the next edit hides a real upload.
    """
    import yaml

    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow[True]
    assert set(triggers) == {"workflow_dispatch"}, (
        f"publish.yml triggers on {sorted(str(key) for key in triggers)}; "
        "publishing must be a deliberate manual run and nothing else")
    target = triggers["workflow_dispatch"]["inputs"]["target"]
    assert target["options"] == ["testpypi", "pypi"], target
    assert target["default"] == "testpypi", (
        "the dispatch default is not TestPyPI; a run started without "
        "choosing would spend a real version number")
    # Over the parsed values, not the text: the comments are allowed to
    # say what the check used to be gated on.
    assert "event_name" not in json.dumps(workflow, default=str), (
        "publish.yml still reads github.event_name; with a single trigger "
        "that expression can only take one value")

    publish = workflow["jobs"]["publish"]
    assert publish["environment"]["name"] == "${{ inputs.target }}", (
        "the publish environment is not the dispatched target; PyPI's "
        "trusted publisher matches on the environment name")
    assert set(publish["needs"]) == {"build", "test-floor"}, publish["needs"]
    upload = next(step for step in publish["steps"]
                  if "gh-action-pypi-publish" in str(step.get("uses", "")))
    assert upload["with"]["repository-url"] == (
        "${{ inputs.target == 'pypi' && 'https://upload.pypi.org/legacy/' "
        "|| 'https://test.pypi.org/legacy/' }}"), (
        "the upload URL is not keyed so that only an exact `pypi` reaches "
        "the real index")
    assert publish["environment"]["url"] == (
        "${{ inputs.target == 'pypi' && 'https://pypi.org/p/isocenter' "
        "|| 'https://test.pypi.org/p/isocenter' }}"), publish["environment"]


@pytest.mark.parametrize("target, ref, packaged, declared, allowed", [
    ("pypi", "refs/tags/v1.2.3", "1.2.3", "1.2.3", True),
    ("pypi", "refs/heads/release/1.2", "1.2.3", "1.2.3", False),
    ("pypi", "refs/heads/main", "1.2.3", "1.2.3", False),
    ("pypi", "refs/tags/1.2.3", "1.2.3", "1.2.3", False),
    ("pypi", "refs/tags/v1.2.4", "1.2.3", "1.2.3", False),
    ("pypi", "refs/tags/v1.2.3", "1.2.3", "1.2.4", False),
    ("pypi", "refs/tags/v1.2.3", "1.2.4", "1.2.3", False),
    ("testpypi", "refs/heads/release/1.2", "1.2.3", "1.2.3", True),
    ("testpypi", "refs/heads/release/1.2", "1.2.3", "1.2.4", False),
    ("testpypi", "refs/tags/v1.2.4", "1.2.3", "1.2.3", False),
    ("PyPI", "refs/heads/main", "1.2.3", "1.2.3", False),
    ("PYPI", "refs/tags/v1.2.3", "1.2.3", "1.2.3", False),
    ("pypi ", "refs/heads/main", "1.2.3", "1.2.3", False),
    ("TestPyPI", "refs/heads/release/1.2", "1.2.3", "1.2.3", False),
    ("", "refs/heads/release/1.2", "1.2.3", "1.2.3", False),
], ids=["pypi-from-matching-tag", "pypi-from-release-branch",
        "pypi-from-main", "pypi-from-tag-without-v",
        "pypi-tag-disagrees-with-both", "pypi-source-disagrees",
        "pypi-wheel-disagrees", "testpypi-rehearsal-from-branch",
        "testpypi-source-disagrees", "testpypi-from-mismatched-tag",
        "PyPI-from-main", "PYPI-even-from-a-matching-tag",
        "pypi-with-a-trailing-space", "TestPyPI-from-branch",
        "empty-target"])
def test_a_publish_run_refuses_a_ref_or_version_that_does_not_match(
        tmp_path, target, ref, packaged, declared, allowed):
    """The version check runs on every dispatch, not only on a release event.

    It was gated `if: github.event_name == 'release'`, so a manual run to
    `pypi` uploaded with no tag or version check at all. Now, on every run:

    * the wheel's version must equal `isocenter/_version.py`'s, the one
      place the number is declared;
    * a run to `pypi` must be dispatched from a `v*` tag, and that tag
      must equal both -- publishing `v0.7.1` from a tree that says
      `0.7.0` spends a version nobody can install by the name they were
      given, and PyPI never gives it back;
    * a TestPyPI rehearsal may run from a branch, but if it runs from a
      tag, the tag must match too;
    * the target must be exactly `pypi` or `testpypi`, checked first.
      GitHub compares expression strings case-insensitively and matches
      environment names the same way, so `PyPI` selects the `pypi`
      environment and the real upload URL; a script comparing
      case-sensitively would call the same run a rehearsal and let it
      upload from a branch. Refusing every other spelling is what keeps
      the script and the expressions agreeing on which runs are real,
      whatever the dispatch API does or does not validate.

    The step's script is executed with each combination rather than read,
    and `_version.py` is a real file in a scratch tree.
    """
    step = _step_named(PUBLISH_WORKFLOW, "build", PUBLISH_TAG_STEP)
    assert "if" not in step, (
        "the ref and version check is conditional; it must run on every "
        "dispatch, which is the gap it replaces")
    assert step["env"] == {
        "TARGET": "${{ inputs.target }}",
        "PACKAGED": "${{ steps.packaged.outputs.version }}"}, step["env"]

    (tmp_path / "isocenter").mkdir()
    (tmp_path / "isocenter" / "_version.py").write_text(
        f'"""Docstring."""\n\n__version__ = "{declared}"\n', encoding="utf-8")
    result = _run_step_script(step, tmp_path, {
        "TARGET": target, "PACKAGED": packaged, "GITHUB_REF": ref,
        "GITHUB_REF_NAME": ref.split("/", 2)[-1]})
    assert (result.returncode == 0) is allowed, (
        f"target={target} ref={ref} wheel={packaged} source={declared}: "
        f"exit {result.returncode}, expected "
        f"{'success' if allowed else 'refusal'}\n{result.stdout}"
        f"{result.stderr}")


def test_the_publish_check_parses_the_real_version_file(tmp_path):
    """The check reads `isocenter/_version.py` as it actually is.

    The script reads `__version__` with a pattern rather than by importing
    the package, which the build runner cannot do. If `_version.py`
    changes shape -- an annotation, single quotes -- the pattern finds
    nothing and every dispatch is refused. That refusal is safe, but it
    would first be seen on release day, so it is checked here against the
    real file's text.
    """
    import isocenter

    step = _step_named(PUBLISH_WORKFLOW, "build", PUBLISH_TAG_STEP)
    (tmp_path / "isocenter").mkdir()
    (tmp_path / "isocenter" / "_version.py").write_text(
        (REPO / "isocenter" / "_version.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    version = isocenter.__version__
    result = _run_step_script(step, tmp_path, {
        "TARGET": "pypi", "PACKAGED": version,
        "GITHUB_REF": f"refs/tags/v{version}",
        "GITHUB_REF_NAME": f"v{version}"})
    assert result.returncode == 0, (
        f"the check refused the real _version.py ({version}) against a "
        f"matching tag and wheel:\n{result.stdout}{result.stderr}")

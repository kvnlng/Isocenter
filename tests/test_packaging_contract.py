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
import pathlib
import subprocess
import sys
import tarfile
import tomllib
import zipfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "isocenter"

# Import name -> distribution name, where they differ.
DISTRIBUTION_NAMES = {
    "dotenv": "python-dotenv",
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


@pytest.mark.parametrize("module", ["pytesseract", "imagecodecs"])
def test_optional_dependencies_are_imported_defensively(module):
    """Optional features must degrade, not explode.

    If one of these becomes a hard import, it must move into
    install_requires -- otherwise `import isocenter` breaks for anyone who
    did not install the extra.
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

    Isocenter moved from MIT to AGPLv3, and a distribution that ships an
    AGPL LICENSE while declaring nothing (or MIT) misstates the terms
    under which it is published -- the one piece of packaging metadata
    with legal weight rather than merely operational.
    """
    licence_text = (REPO / "LICENSE").read_text()
    assert "AFFERO GENERAL PUBLIC LICENSE" in licence_text, (
        "LICENSE is no longer AGPL; this test pins the two together and "
        "needs updating alongside the licence change")

    declared = _setup_keyword("license")
    assert declared, "setup.py declares no license; PyPI would show 'UNKNOWN'"
    assert "AGPL" in declared.upper() or "AFFERO" in declared.upper(), (
        f"setup.py declares license={declared!r} but LICENSE is AGPLv3")

    classifiers = _setup_keyword("classifiers") or []
    assert any("Affero" in item for item in classifiers), (
        "no AGPL licence classifier; PyPI categorises by classifier, not by "
        "the license field")


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
# because nothing declared package_data -- and the loaders guard on
# os.path.exists, so a pip-installed Isocenter did not crash. It audited
# against an empty PHI tag list (`ConfigLoader.load_phi_config` returns
# {}, `PhiInspector` at isocenter/privacy.py:140 takes it) and reported
# clean. A de-identification tool that silently stops looking for PHI is
# the worst failure this project can ship, and every test in the suite
# passed while it was true, because tests import from the source tree
# where the files are present.
#
# So these build the real artefacts and read what is inside them.

def _data_files_in_package():
    """Non-Python files under isocenter/ that the code reads at runtime."""
    return {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file()
        and path.suffix != ".py"
        and "__pycache__" not in path.parts
    }


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The wheel and sdist setup.py actually produces.

    Built once per module: this shells out to a real build, which costs a
    couple of seconds. `--dist-dir` is repeated per command on purpose --
    distutils applies an option to the command it follows, so a single
    trailing `--dist-dir` would send the sdist to the repo's own dist/.
    """
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

    isocenter/resources/phi_tags.json is the whole default PHI policy. When
    it is absent, load_phi_config() returns {} rather than raising, so
    audit() finds nothing and reports success on data full of PHI.
    """
    shipped = {
        name[len("isocenter/"):]
        for name in built["wheel"] if name.startswith("isocenter/")}

    missing = sorted(_data_files_in_package() - shipped)
    assert not missing, (
        "read from isocenter/ at runtime but absent from the wheel, so a "
        "pip-installed Isocenter degrades silently instead of failing: "
        f"{missing}. Declare them in setup.py's package_data.")


def test_the_sdist_ships_every_resource_the_package_reads(built):
    """The sdist is what pip builds from when no wheel matches."""
    shipped = {
        name[len("isocenter/"):]
        for name in built["sdist"] if name.startswith("isocenter/")}

    missing = sorted(_data_files_in_package() - shipped)
    assert not missing, (
        f"absent from the sdist: {missing}. A wheel built from this sdist "
        "would inherit the omission.")


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

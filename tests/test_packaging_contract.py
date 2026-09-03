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

def _tracked_paths_in_package():
    """Paths under isocenter/ that git considers source, or None.

    None means "git could not answer", not "nothing is tracked". An
    empty tracked set is never a valid answer for a package that must
    ship four JSON resources, so an empty result is treated the same as
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


# --- Support claims must be backed by the gate that runs on every PR ---
#
# The version classifiers above are checked against `python_requires`,
# which catches advertising *below* the floor. Neither catches the other
# direction: a claim nothing runs. The PR gate is deliberately narrow
# (two versions, not four), so which two it runs is now load-bearing --
# drop one and a promise silently stops being tested, which is the exact
# defect `test_classifiers_do_not_advertise_unsupported_python_versions`
# was written for, one level up.

GATE_WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


def _gate_versions():
    """Python versions the PR gate runs by default.

    The matrix is `fromJSON(inputs.python-versions || '[...]')` -- a
    template string, because publish.yml overrides it to run the wider
    release matrix. The default inside that expression is what runs on a
    pull request, so that is what these assertions are about.
    """
    text = GATE_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"inputs\.python-versions\s*\|\|\s*'(\[[^']*\])'", text)
    assert match, (
        f"no default version list found in {GATE_WORKFLOW.name}; if the "
        "matrix was rewritten, this helper needs to be too -- it is the "
        "only thing checking the gate still covers what setup.py claims")
    return json.loads(match.group(1))


def test_the_gate_runs_the_floor_python_requires_declares():
    """A floor is the one claim a single-version gate can prove.

    `python_requires=">=3.12"` says a 3.12 user can install and run this.
    Only 3.12 can show that: 3.13 passing says nothing about syntax or a
    stdlib API that does not exist a version earlier.
    """
    declared = _setup_keyword("python_requires") or ""
    floor = declared.replace(">=", "").strip()

    assert floor in _gate_versions(), (
        f"python_requires={declared!r} but the PR gate does not run "
        f"{floor}; the floor is advertised and untested")


def test_a_free_threading_claim_is_backed_by_a_free_threaded_job():
    """`Free Threading :: 3 - Stable` is a promise `python_requires` cannot make.

    3.14t *is* 3.14 -- the `t` is the build variant, not the version --
    and the wheel is py3-none-any, so neither the version specifier nor
    the ABI tag carries this claim. The classifier is the whole promise.

    It is not a formality: `run_parallel()` chooses threads over
    processes when there is no GIL to escape (`isocenter/parallel.py`),
    and everything heavy funnels through it. A GIL-enabled interpreter
    never executes that path, so a matrix without a `t` build tests none
    of what is being claimed.
    """
    classifiers = _setup_keyword("classifiers") or []
    claims_free_threading = any(
        item.startswith("Programming Language :: Python :: Free Threading")
        for item in classifiers)

    if not claims_free_threading:
        pytest.skip("no free-threading claim to back")

    versions = _gate_versions()
    assert any(v.endswith("t") for v in versions), (
        "setup.py advertises free-threading support but the PR gate runs "
        f"{versions} -- no free-threaded build, so run_parallel()'s "
        "no-GIL path is never executed")


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

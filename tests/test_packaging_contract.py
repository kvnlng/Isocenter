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
import os
import pathlib
import re
import shutil
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


#: What the fallback copy leaves out when git cannot say what is tracked:
#: the build residue, caches and test artefacts a checkout accumulates.
_NOT_SOURCE = (".git", ".venv*", "build", "dist", "*.egg-info", "__pycache__",
               ".pytest_cache", ".coverage*", "*.db*", "*_pixels.bin*", "site")


def _tree_to_build(dest, root=REPO):
    """Copy the source tree at `root` into `dest`, to build from (#859).

    Each path `git ls-files` lists, from the working tree, not the index,
    so an uncommitted edit is built as it was when the build ran in the
    root; a tracked path deleted on disk is skipped. When git cannot answer
    (an unpacked sdist, no git), the whole tree minus `_NOT_SOURCE`.

    Returns:
        pathlib.Path: `dest`.
    """
    # Why a copy, and not the root (#859): setuptools makes `build/`,
    # `isocenter.egg-info/` and, for the sdist, a release tree
    # `isocenter-<version>/` beside `setup.py`, and deletes that tree
    # again. Another run in the same checkout whose closing root-guard
    # snapshot fell inside that window exited 1 (#849). And the sdist
    # depended on the checkout's leftovers: `manifest_maker` reads an
    # existing `isocenter.egg-info/SOURCES.txt`, and `graft tests` sweeps
    # untracked files under `tests/`.
    #
    # The trade, as `_data_files_in_package` states for resources: a new
    # file not yet `git add`ed is not in the copy, so not in the sdist
    # under test. That is what a clean tag checkout (`publish.yml`)
    # builds from. The comparison holds only while no revision-control
    # plugin such as setuptools-scm is installed: with one, a build in a
    # git directory takes its file list from git, and the copy is not one.
    dest = pathlib.Path(dest)
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True,
            check=False)
    except (OSError, subprocess.SubprocessError):
        listed = None
    if listed is None or listed.returncode != 0 or not listed.stdout:
        shutil.copytree(root, dest, symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*_NOT_SOURCE))
        return dest
    for name in listed.stdout.decode("utf-8").split("\0"):
        if not name:
            continue
        source = pathlib.Path(root) / name
        if not os.path.lexists(source):
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    return dest


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The wheel and sdist setup.py actually produces.

    Built once per module: this shells out to a real build, which costs a
    couple of seconds. `--dist-dir` is repeated per command on purpose --
    distutils applies an option to the command it follows, so a single
    trailing `--dist-dir` would send the sdist to the repo's own dist/.

    Built in a copy of the git-tracked tree (`_tree_to_build`), never in
    the repository root (#859): the build sees each tracked file with its
    working-tree content, which is what a clean tag checkout builds from,
    and an untracked file is not in it. `"tree"` is where it ran.
    """
    if importlib.util.find_spec("setuptools") is None:
        pytest.fail(
            "setuptools is not installed in this environment, so the "
            "distributions cannot be built and this module's guarantees "
            "cannot be checked. It is declared in the `tests` extra: "
            'install with `pip install -e ".[tests]"`.')

    out = tmp_path_factory.mktemp("dist")
    tree = _tree_to_build(tmp_path_factory.mktemp("tree"))
    result = subprocess.run(
        [sys.executable, "setup.py", "-q",
         "sdist", "--dist-dir", str(out),
         "bdist_wheel", "--dist-dir", str(out)],
        cwd=tree, capture_output=True, text=True, check=False)
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
        "tree": tree,
    }


def test_the_distributions_are_built_outside_the_repository_root(built):
    """P1: the build ran in the copy, not in the root (#859).

    `isocenter.egg-info/SOURCES.txt` in the copy is the evidence that
    setuptools ran there, not merely that a directory was made somewhere.
    The root's own `isocenter.egg-info` (the editable install's) is not in
    the copy: `_tree_to_build` copies tracked files only.
    """
    tree = built["tree"]
    assert REPO not in tree.resolve().parents and tree.resolve() != REPO, tree
    assert (tree / "isocenter.egg-info" / "SOURCES.txt").is_file(), sorted(
        p.name for p in tree.iterdir())


def test_the_sdist_still_carries_its_metadata(built):
    """P2: `PKG-INFO` and `isocenter.egg-info/SOURCES.txt` are in the sdist,
    built in the copy as in the root (#859).

    A build that dropped `egg-info` from the sdist is the shape #707
    measured for a redirected `--egg-base` (397 entries where 403 were
    expected); the copy must not do the same.
    """
    assert "PKG-INFO" in built["sdist"], built["sdist"][:20]
    assert "isocenter.egg-info/SOURCES.txt" in built["sdist"]


def test_the_tree_to_build_is_the_tracked_files_as_they_are_on_disk(tmp_path):
    """P4: `_tree_to_build` copies a tracked file, its uncommitted edit, and
    not an untracked file (#859).

    Killing mutation: a bare `copytree` with no git filter (N21).
    """
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "tracked.py").write_text("committed\n")
    (repo / "gone.txt").write_text("tracked, then deleted\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "commit", "-q", "-m", "one"]):
        subprocess.run(command, cwd=repo, env=env, check=True,
                       capture_output=True)
    (repo / "pkg" / "tracked.py").write_text("edited, not committed\n")
    (repo / "pkg" / "untracked.py").write_text("never added\n")
    (repo / "gone.txt").unlink()

    copy = _tree_to_build(tmp_path / "copy", root=repo)

    assert (copy / "pkg" / "tracked.py").read_text() == (
        "edited, not committed\n")
    assert not (copy / "pkg" / "untracked.py").exists()
    assert not (copy / "gone.txt").exists()
    assert not (copy / ".git").exists()


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
    `RESOURCES_DIR, "redaction_rules.json",` at session.py line 438 turns
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


def test_the_gate_checkout_brings_the_tags_and_main():
    """A runner must hold the refs the release checks read (#966).

    `actions/checkout` fetches one commit by default: no tag, and no
    `origin/main` unless `main` is the ref being run. Every test that
    compares this tree with a released one then skips, and a skip is
    green. Read from the logs of four release and dispatch runs: 43 of
    the 61 tests the rc15 publish skipped on 3.12 (run 37681895889), and
    83 of the 101 its rehearsal skipped (run 37677947792), were
    `tests/test_released_changelog_sections_stay_as_released.py`'s
    comparisons with each tag and with `origin/main`, and
    `test_no_row_the_previous_final_release_shipped_has_moved` -- the
    check RELEASING.md says stops a release when it skips for want of
    tags. On a runner it had never run.

    `fetch-depth: 0` is the checkout action's "all history for all
    branches and tags". Owner ruling on #966, 2026-10-08.
    """
    import yaml

    workflow = yaml.safe_load(GATE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    checkouts = [step for step in steps
                 if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1, (
        f"tests.yml's test job has {len(checkouts)} checkout steps")
    depth = (checkouts[0].get("with") or {}).get("fetch-depth")
    assert depth == 0, (
        f"tests.yml checks out with fetch-depth {depth!r}: without 0 the "
        "runner has no tags and no origin/main, and the comparisons with "
        "released text skip in every release run instead of running (#966)")


# --- The tesseract install retries inside its step (#984) --------------
#
# On 2026-10-07 the Ubuntu mirror answered some runners' connections and
# left others to apt's 30-second timeout, package after package. One
# `apt-get install` under the step's 3-minute cap was still cycling
# through `Ign:` lines when the cap fired, in six of 32 jobs at the rc15
# rehearsal and three of 32 at the rc15 publish, before any test ran; a
# job that did finish took up to 173 s. Owner ruling, 2026-10-08: retry
# inside the step, a loop with a pause.
#
# The tests below RUN the step's script, with `sudo`, `timeout`,
# `apt-get`, `sleep` and `tesseract` replaced by recorders on PATH. A
# test that matched the text for `for attempt` would stay green with the
# per-attempt bound deleted, and without that bound there is no retry:
# the first attempt eats the whole cap, as it did.

_STUB = """#!/bin/sh
echo "{name} $*" >> "$STUB_LOG"
{body}
"""

_STUB_BODIES = {
    "sudo": 'exec "$@"',
    # Drop the options and the duration; run the rest.
    "timeout": ('while [ "${1#--}" != "$1" ]; do shift; done\n'
                'shift\nexec "$@"'),
    "sleep": "exit 0",
    # The binary exists only once an install put it there.
    "tesseract": '[ -e "$STUB_STATE/installed" ] || exit 127\n'
                 'echo "tesseract 5.3.4"',
    # `update` exits as told. A download from the network fails
    # $STUB_FAILING_DOWNLOADS times and then leaves the archives in the
    # cache; `--no-download` installs from the cache or fails; a plain
    # `install` is a download and an install in one, as the step ran it
    # before #984.
    "apt-get": """
case " $* " in
  *" update "*) exit "${STUB_UPDATE_STATUS:-0}" ;;
  *" --no-download "*)
    [ -e "$STUB_STATE/fetched" ] || exit 100
    : > "$STUB_STATE/installed"; exit 0 ;;
esac
tried=$(cat "$STUB_STATE/tried" 2>/dev/null || echo 0)
tried=$((tried + 1))
echo "$tried" > "$STUB_STATE/tried"
[ "$tried" -gt "${STUB_FAILING_DOWNLOADS:-0}" ] || exit 100
: > "$STUB_STATE/fetched"
case " $* " in
  *" --download-only "*) ;;
  *) : > "$STUB_STATE/installed" ;;
esac
exit 0
""",
}


def _install_tesseract_step():
    import yaml

    workflow = yaml.safe_load(GATE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    found = [step for step in steps if step.get("name") == "Install tesseract"]
    assert len(found) == 1, (
        "tests.yml no longer has exactly one step named 'Install "
        "tesseract'; RELEASING.md's rerun rule names it")
    return found[0]


def _run_the_install_step(tmp_path, failing_downloads, update_status=0):
    """Run the step's script against the recorders; (exit status, calls)."""
    bash = shutil.which("bash")
    assert bash, "bash is what a runner runs this step with"
    stubs, state = tmp_path / "stubs", tmp_path / "state"
    tmp_path.mkdir(parents=True, exist_ok=True)
    stubs.mkdir()
    state.mkdir()
    for name, body in _STUB_BODIES.items():
        stub = stubs / name
        stub.write_text(_STUB.format(name=name, body=body), encoding="utf-8")
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    script = tmp_path / "step.sh"
    script.write_text(_install_tesseract_step()["run"], encoding="utf-8")
    # `bash -e {0}` is how GitHub runs a `run:` with no `shell:`.
    done = subprocess.run(
        [bash, "-e", str(script)], cwd=tmp_path, capture_output=True,
        text=True, timeout=60, check=False,
        env={"PATH": f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin",
             "STUB_LOG": str(log), "STUB_STATE": str(state),
             "STUB_FAILING_DOWNLOADS": str(failing_downloads),
             "STUB_UPDATE_STATUS": str(update_status)})
    return done.returncode, log.read_text(encoding="utf-8").splitlines()


def _downloads(calls):
    """The apt-get calls that fetch a package from the network."""
    return [call for call in calls
            if call.startswith("apt-get ") and " install " in f"{call} "
            and "--no-download" not in call]


def _attempts_allowed(tmp_path):
    """How many downloads the step makes when every one of them fails."""
    status, calls = _run_the_install_step(tmp_path, failing_downloads=10 ** 6)
    assert status != 0, (
        "the Install tesseract step passed with every download failing: "
        "the suite would run with no tesseract and skip its OCR tests, "
        "which is #44")
    return len(_downloads(calls)), calls


def test_the_tesseract_install_passes_at_once_on_a_healthy_mirror(tmp_path):
    status, calls = _run_the_install_step(tmp_path, failing_downloads=0)
    assert status == 0, calls
    assert len(_downloads(calls)) == 1, calls
    assert not [call for call in calls if call.startswith("sleep ")], (
        f"a healthy install paused: {calls}")
    assert calls[-1] == "tesseract --version", (
        "the version check is no longer the step's last word; it is what "
        f"proves the binary is there (#44): {calls}")


def test_a_tesseract_download_that_fails_is_tried_again_in_the_step(tmp_path):
    """#984: a download that fails or runs out of time is not the step's end.

    One failure, and every number of failures up to one short of the
    attempts the step allows, still ends with tesseract installed, and
    with a pause between one attempt and the next.
    """
    allowed, _calls = _attempts_allowed(tmp_path / "worst")
    assert allowed >= 2, (
        f"the Install tesseract step makes {allowed} download attempt: "
        "there is no retry (#984)")

    for failing in range(1, allowed):
        status, calls = _run_the_install_step(
            tmp_path / f"failing-{failing}", failing_downloads=failing)
        assert status == 0, (
            f"{failing} failed download(s) of an allowed {allowed} failed "
            f"the step: {calls}")
        assert len(_downloads(calls)) == failing + 1, calls
        pauses = [call for call in calls if call.startswith("sleep ")]
        assert len(pauses) == failing, (
            f"{failing} failed download(s) and {len(pauses)} pause(s): a "
            f"retry with no pause asks the same mirror at once: {calls}")
        assert calls[-1] == "tesseract --version", calls


def test_a_failing_apt_update_still_does_not_stop_the_install(tmp_path):
    """The 2026-08-27 lesson the step's comment carries, kept by #984."""
    status, calls = _run_the_install_step(
        tmp_path, failing_downloads=0, update_status=100)
    assert status == 0, calls
    assert calls[-1] == "tesseract --version", calls


def test_what_reads_the_network_is_bounded_by_root_and_what_runs_dpkg_is_not(
        tmp_path):
    """The bound is what makes the loop a retry; where it sits matters.

    The 2026-10-07 installs did not fail, they stalled: without a bound
    on each attempt the first one runs until the step's cap and no second
    attempt is made. Four things are held, each by its own assertion:

    - every `apt-get` call that reads the network runs under `timeout`;
    - every `timeout` is run by `sudo` (`sudo timeout … apt-get`, never
      `timeout … sudo apt-get`): `--kill-after` sends SIGKILL, which
      sudo cannot pass on, so killing sudo would leave apt-get running
      with the dpkg lock held (review of #1005, finding 1);
    - an `install` under `timeout` only downloads (`--download-only`):
      one that also unpacked would put dpkg under the bound, and a bound
      that fired there would leave the package database half-configured
      for the next attempt (finding 2);
    - the one install that unpacks reads the cache only (`--no-download`)
      and is under no `timeout`.
    """
    _allowed, calls = _attempts_allowed(tmp_path / "worst")
    _status, good = _run_the_install_step(tmp_path / "good", 0)

    for run in (calls, good):
        bounded = [call for call in run if call.startswith("timeout ")]
        network = [call for call in run if call.startswith("apt-get ")
                   and "--no-download" not in call]
        assert network, run
        for call in network:
            assert any(entry.endswith(call) for entry in bounded), (
                f"`{call}` reads the network and does not run under "
                f"`timeout`: a stalled mirror holds it until the step's "
                f"cap, and nothing is retried (#984): {run}")
        for entry in bounded:
            assert " sudo " not in f"{entry} " and f"sudo {entry}" in run, (
                f"`{entry}` is not run by sudo, or runs sudo itself: the "
                f"order is `sudo timeout … apt-get`, so that the SIGKILL "
                f"of `--kill-after` reaches apt-get and not a sudo that "
                f"cannot pass it on: {run}")
            if " install " in f"{entry} ":
                assert "--download-only" in entry.split(), (
                    f"`{entry}` installs under `timeout` without "
                    f"`--download-only`: dpkg would run under the bound, "
                    f"and a bound that fired inside it leaves the package "
                    f"database half-configured: {run}")

    unpacking = [call for call in good
                 if call.startswith("apt-get ") and "--no-download" in call]
    assert len(unpacking) == 1, (
        f"the step does not install from the cache exactly once: {good}")
    assert not [entry for entry in good if entry.startswith("timeout ")
                and "--no-download" in entry], (
        f"the install that runs dpkg is under `timeout`: {good}")


#: Seconds left, in the worst case, for what the step does besides wait:
#: unpacking six packages from the cache and the version check. Measured
#: on four release and dispatch runs of 2026-10-03 to 2026-10-07 (80
#: jobs): a healthy download and unpack together took 3 to 5 s, and
#: the slowest job (173 s, run 37681895889) spent 7.6 s between its last
#: byte fetched and `tesseract 5.3.4`.
_INSTALL_UNPACK_ALLOWANCE_S = 60


def test_the_tesseract_retries_worst_case_fits_the_steps_cap(tmp_path):
    """Every attempt running to its bound must still end inside the cap.

    Otherwise the last attempts are never made, and the step dies at its
    `timeout-minutes` naming no attempt -- the 2026-10-07 shape, with a
    loop around it. Read from the calls the step makes when every
    download fails: each `timeout`'s duration and its `--kill-after`
    grace, and each pause. Raising an attempt's bound, the attempts or
    the pause without raising the cap is red here; raising the cap
    raises the job cap with it
    (`test_the_job_cap_cannot_fire_before_a_steps_own_timeout`).
    """
    _allowed, calls = _attempts_allowed(tmp_path)

    worst = 0
    for call in calls:
        words = call.split()
        if words[0] == "timeout":
            options = [word for word in words[1:] if word.startswith("--")]
            duration = next(word for word in words[1:]
                            if not word.startswith("--"))
            assert duration.isdigit(), (
                f"`{call}`: a bound this test cannot read as seconds")
            graces = [word.split("=", 1)[1] for word in options
                      if word.startswith("--kill-after=")]
            assert len(graces) == 1 and graces[0].isdigit(), (
                f"`{call}` has no `--kill-after=<seconds>`: an apt-get "
                "that ignores SIGTERM outlives its bound")
            worst += int(duration) + int(graces[0])
        elif words[0] == "sleep":
            assert words[1].isdigit(), f"`{call}`: not whole seconds"
            worst += int(words[1])

    cap = _install_tesseract_step()["timeout-minutes"] * 60
    assert worst + _INSTALL_UNPACK_ALLOWANCE_S <= cap, (
        f"the Install tesseract step can wait {worst} s in the worst "
        f"case and needs {_INSTALL_UNPACK_ALLOWANCE_S} s more to unpack, "
        f"under a cap of {cap} s: the cap fires before the last attempt "
        "ends (#984)")


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
#:
#: Unchanged at 25 when the suite became eight shards per version (#935,
#: 2026-10-07, owner ruling Q1 A). By then a step ran a quarter of a
#: suite that had grown from 6,118 to 7,392 tests in twelve days: the
#: `(3.14t, 4)` step was killed at 1,513 s with every listed test passing
#: at the rc12 publish (run 37089244834) and at the rc13 rehearsal (run
#: 37555211835), and one commit's shard took 1.75 times as long on the
#: slowest runner seen as on the fastest, which the 60% rule could no
#: longer absorb. The room was made by the shard count, not here: an
#: eighth, cut from runner timings, is predicted at 575 s (38% of this)
#: on an ordinary runner and 1,006 s (67%) on the slowest. So the
#: figure in the message below, 751 s, is still the peak this floor was
#: sized from; it is no longer the peak of a shard.
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


def test_the_broken_pool_grace_sits_below_the_stall_watchdog():
    """`5 <= _BROKEN_POOL_GRACE_S < _STALL_S`, below the faulthandler window
    too, and the helper reads it (#796).

    After a pool breaks, its workers get the grace to end before
    `_end_broken_pool_stragglers` SIGKILLs them. Below `_STALL_S`, so a
    teardown that ends in a kill is never reported as a stall, or dumped;
    and below the faulthandler window for the same reason. It holds no
    sqlite handle and no gate, so it has no place among 120 < 180 < 240,
    which order waits on the database and the sidecar. Not below 5 s, the
    "few seconds" of the constant's comment: a worker that saves its state
    on SIGTERM must get to finish, and coverage's handler was measured at
    up to 0.8 s under load. A grace cut tenfold is below that floor.

    What this cannot see is the grace grown tenfold, which stays below
    both ceilings. Read from the conftest source, as
    `test_the_stall_watchdog_fires_inside_the_run_tests_step` reads it.
    """
    import inspect
    import textwrap

    from isocenter import parallel

    threshold, _step_seconds, _ = _faulthandler_threshold_and_step_seconds()
    source = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    match = re.search(r"^_STALL_S = ([0-9.]+)$", source, re.MULTILINE)
    assert match, "tests/conftest.py no longer defines _STALL_S (#250)"
    stall_s = float(match.group(1))
    grace = parallel._BROKEN_POOL_GRACE_S

    assert grace < stall_s, (
        f"_BROKEN_POOL_GRACE_S={grace:g}s is not below the stall watchdog's "
        f"{stall_s:g}s: a pool teardown that ends in SIGKILL would be "
        "reported as a stall (#796)")
    assert grace < threshold, (
        f"_BROKEN_POOL_GRACE_S={grace:g}s outlasts the faulthandler window "
        f"({threshold:g}s): a teardown that ends in SIGKILL would be dumped "
        "as a hang (#796)")
    assert grace >= 5, (
        f"_BROKEN_POOL_GRACE_S={grace:g}s is below the few seconds its "
        "comment sets as the floor: a worker that saves its state on SIGTERM "
        "must get to finish: coverage's `sigterm = True` handler, which "
        ".coveragerc set until #886, took up to 0.8 s under load to write "
        "the worker's data file (#796)")

    # By AST, as the gate's deadline is read above: the docstring names
    # the constant in prose.
    helper = inspect.getsource(parallel._end_broken_pool_stragglers)
    names = {node.id for node in ast.walk(ast.parse(textwrap.dedent(helper)))
             if isinstance(node, ast.Name)}
    assert "_BROKEN_POOL_GRACE_S" in names, (
        "_end_broken_pool_stragglers no longer reads _BROKEN_POOL_GRACE_S; "
        "a re-inlined literal is what this family exists to stop (#368)")


def test_the_recycling_pools_bounded_exit_sits_below_the_stall_watchdog():
    """`_BROKEN_POOL_GRACE_S + _POOL_EXIT_AFTER_KILL_S` is below `_STALL_S`
    and the faulthandler window, and the exit reads both by name (#860).

    The recycling pool's exit waits the grace for its workers, SIGKILLs the
    rest, and waits the second constant more for the stdlib's exit before
    it lets the caller go. Their sum is the longest a caller can be held,
    so it must stay below the watchdog and the faulthandler window: a
    bounded exit must never be reported as a stall, or dumped as a hang.
    The after-kill wait is not zero: the stdlib's exit still has to join
    the killed workers and its own threads.
    """
    import inspect
    import textwrap

    from isocenter import parallel

    threshold, _step_seconds, _ = _faulthandler_threshold_and_step_seconds()
    source = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    match = re.search(r"^_STALL_S = ([0-9.]+)$", source, re.MULTILINE)
    assert match, "tests/conftest.py no longer defines _STALL_S (#250)"
    stall_s = float(match.group(1))
    after_kill = parallel._POOL_EXIT_AFTER_KILL_S
    bound = parallel._BROKEN_POOL_GRACE_S + after_kill

    assert after_kill > 0, (
        "_POOL_EXIT_AFTER_KILL_S is not positive: the stdlib's exit would "
        "be left behind on every kill, with its pipes (#860)")
    assert bound < stall_s, (
        f"the recycling pool's exit can hold its caller {bound:g}s, not "
        f"below the stall watchdog's {stall_s:g}s (#860)")
    assert bound < threshold, (
        f"the recycling pool's exit can hold its caller {bound:g}s, beyond "
        f"the faulthandler window ({threshold:g}s) (#860)")

    helper = inspect.getsource(parallel._end_recycling_pool)
    names = {node.id for node in ast.walk(ast.parse(textwrap.dedent(helper)))
             if isinstance(node, ast.Name)}
    for name in ("_BROKEN_POOL_GRACE_S", "_POOL_EXIT_AFTER_KILL_S"):
        assert name in names, (
            f"_end_recycling_pool no longer reads {name}; a re-inlined "
            "literal is what this family exists to stop (#368)")


def test_a_workers_exit_bound_waits_for_the_recycling_pools_own_exit():
    """`_BROKEN_POOL_GRACE_S + _POOL_EXIT_AFTER_KILL_S <= _WORKER_EXIT_GRACE_S
    < _STALL_S`, below the faulthandler window, and each is read by name
    (#888, #844).

    A worker of the recycling pool arms its own SIGALRM once it leaves its
    task loop, however it left, the pool's own close and terminate
    included. Below the pool's exit bound, the worker's alarm would race
    the parent's kill, and #860's WARNING lines would say whichever won.
    Below the stall watchdog and the faulthandler window, a bounded exit is
    never reported as a stall or dumped as a hang. The watch's period is
    read by name too: it is the latency of seeing a dead worker (#887).
    """
    import inspect
    import textwrap

    from isocenter import parallel

    threshold, _step_seconds, _ = _faulthandler_threshold_and_step_seconds()
    source = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    match = re.search(r"^_STALL_S = ([0-9.]+)$", source, re.MULTILINE)
    assert match, "tests/conftest.py no longer defines _STALL_S (#250)"
    stall_s = float(match.group(1))
    exit_bound = parallel._WORKER_EXIT_GRACE_S
    pool_exit = parallel._BROKEN_POOL_GRACE_S + parallel._POOL_EXIT_AFTER_KILL_S

    assert pool_exit <= exit_bound, (
        f"_WORKER_EXIT_GRACE_S={exit_bound:g}s is below the recycling pool's "
        f"own exit bound ({pool_exit:g}s): a worker's alarm would race the "
        "parent's kill (#888)")
    assert exit_bound < stall_s, (
        f"_WORKER_EXIT_GRACE_S={exit_bound:g}s is not below the stall "
        f"watchdog's {stall_s:g}s (#888)")
    assert exit_bound < threshold, (
        f"_WORKER_EXIT_GRACE_S={exit_bound:g}s outlasts the faulthandler "
        f"window ({threshold:g}s) (#888)")
    assert exit_bound < parallel._WORKER_FAULTHANDLER_TIMEOUT_S, (
        "the exit bound outlasts the worker's own traceback dump (#844)")

    def names_in(function):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        return {node.id for node in ast.walk(tree)
                if isinstance(node, ast.Name)}

    assert "_WORKER_EXIT_GRACE_S" in names_in(
        parallel._run_on_recycling_pool), (
        "_run_on_recycling_pool no longer reads _WORKER_EXIT_GRACE_S; a "
        "re-inlined literal is what this family exists to stop (#368)")
    assert "_WORKER_EXIT_GRACE_S" in names_in(
        parallel.resolve_worker_initializer), (
        "resolve_worker_initializer no longer reads _WORKER_EXIT_GRACE_S "
        "(#844)")
    assert "_POOL_WATCH_S" in names_in(parallel._watched), (
        "_watched no longer reads _POOL_WATCH_S (#887)")


# ---------------------------------------------------------------------------
# The documentation deploy (#635) -- the one workflow that publishes to a
# live site, and until this test nothing in the suite read it at all
# ---------------------------------------------------------------------------

DOCS_WORKFLOW = REPO / ".github" / "workflows" / "docs.yml"


def test_the_docs_deploy_builds_strict_from_the_docs_extra():
    """`docs.yml` builds `--strict` before mike deploys, from the docs extra, and caps every step.

    Assertions about the only workflow in the repository that publishes
    somewhere a reader can see: `mike deploy --push` writes a version
    folder of the live documentation site (#866; `mkdocs gh-deploy`
    before it).

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

    mike has no strict mode: `mike/mkdocs_utils.py::build` runs `mkdocs
    build --clean` and the `mike deploy` CLI passes no flag through
    (mike 2.2.0). So the strict build is its own step, **before** the
    mike step in the same job: a warning fails the run before anything
    is pushed, and mike's own build is the same tree built again. No
    step may still run `gh-deploy`: it publishes a root site, and its
    commit holds only that build, so it deletes every version folder,
    `versions.json` and the old-URL stubs (#866's hazard).

    **mike never overrides the remote.** `--ignore-remote-status` pushes
    a diverged local `gh-pages`, and `--force` is the other way to throw
    away what another deploy wrote. mike's own push is a plain `git push`
    without force (`git_utils.push_branch`), which is the backstop for a
    writer outside the workflow, such as a hand-run `mike alias`.

    **The ref filter** was pinned by nothing before this test, and its
    comment records the two unreviewed deploys from a feature branch it
    exists to prevent. Since the release-branch procedure (`RELEASING.md`)
    it is release tags rather than `main`: `main` is the development
    branch, and the documentation follows the published releases. `main`
    reaches the hidden `dev` folder by manual dispatch only (#866, Q2),
    so no branch is a push trigger. The trigger set is asserted as an
    equality because a `pull_request` trigger on a workflow that deploys
    to the live site is not a thing to notice in review. Which ref may
    write which folder is the guard's, and the next tests'.

    **The package list has one home.** The workflow hand-copied five
    distributions, naming `pymdown-extensions` (absent from `setup.py`'s
    `docs` extra) and omitting `mkdocs` (present there). The two lists
    resolved to the identical eleven packages at the identical versions
    when measured, which is what a second source of truth looks like
    right up to the edit that moves one of them. `mike`, whose console
    script the deploy step runs, is in that extra too.

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

    # 1. Strict, then mike. Found by walking every job's steps for the
    #    commands, so one moved to another step or job is still found.
    every_run = [step.get("run") or ""
                 for each in workflow["jobs"].values()
                 for step in each["steps"]]
    gh_deploy = [run for run in every_run if "gh-deploy" in run]
    assert not gh_deploy, (
        f"a step still runs `mkdocs gh-deploy` ({gh_deploy!r}); it "
        "publishes a root site whose commit holds only that build, which "
        "deletes every version folder, versions.json and the old-URL "
        "stubs (#866)")
    runs = [step.get("run") or "" for step in steps]
    deploying = [index for index, run in enumerate(runs)
                 if "mike deploy" in run]
    assert len(deploying) == 1, (
        f"{len(deploying)} steps of the deploy job run `mike deploy`; "
        "exactly one must, or which one publishes the site is ambiguous")
    elsewhere = sum("mike deploy" in run for run in every_run) - 1
    assert not elsewhere, (
        "`mike deploy` also runs outside the deploy job, outside its "
        "concurrency group and without its strict build first")
    strict = [index for index, run in enumerate(runs)
              if "mkdocs build" in run and "--strict" in run]
    assert len(strict) == 1, (
        f"{len(strict)} steps run `mkdocs build --strict`; mike has no "
        "strict mode, so without that step a mkdocs or griffe warning "
        "publishes the site anyway and lands in a log nobody reads (#635)")
    assert strict[0] < deploying[0], (
        "the strict build runs after the mike deploy, so a warning fails "
        "the run only once the site is already pushed")
    mike_run = runs[deploying[0]]
    for flag in ("--ignore-remote-status", "--force"):
        assert flag not in mike_run, (
            f"the mike deploy passes {flag}; a diverged or newer gh-pages "
            "must fail the run, never be pushed over (#866)")

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
        "rename, and `main` reaches `dev/` by dispatch only (#866, Q2). "
        "A `paths` filter here would skip the deploy of a release "
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
    assert "mike" in {spec.split(">=")[0].split("==")[0].split("[")[0]
                      for spec in declared}, (
        "the deploy step runs `mike`, and the `docs` extra does not "
        "declare it; the command would come from nowhere the one list "
        "names")

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


DOCS_GUARD_STEP = "Decide which docs folder this ref may write"
DOCS_DEPLOY_STEP = "Deploy with mike"
DOCS_RECHECK_STEP = "Check the guard's decision still holds"
DOCS_DECIDE_SCRIPT = pathlib.PurePosixPath(".github/scripts/docs_decide.sh")


def _with_decide_script(repo):
    """Put this checkout's decision script where docs.yml runs it from.

    Untracked in the scratch repository, so it is in no commit and no
    `git diff` the rule takes. The steps run it by that relative path, so
    a step that stopped running this file fails here.
    """
    target = repo / DOCS_DECIDE_SCRIPT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((REPO / DOCS_DECIDE_SCRIPT).read_text(encoding="utf-8"),
                      encoding="utf-8")
DOCS_OUTPUTS = ("folder", "title", "alias", "hidden")


def _scratch_git(repo):
    """A `run_git(*args, when=None)` for a scratch repository at `repo`.

    Global and system git configuration are cut off, so a developer's
    `tag.sort` or `versionsort.suffix` cannot decide a test's outcome.
    """
    import os

    git_env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    def run_git(*args, when=None):
        dated = ({"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
                 if when else {})
        return subprocess.run(
            ["git", *args], cwd=str(repo), check=True, capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], **git_env, **dated}).stdout.strip()

    return run_git, git_env


def _read_outputs(path):
    """The `name=value` lines a step appended to its `$GITHUB_OUTPUT` file."""
    if not path.exists():
        return {}
    outputs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        assert name not in outputs, (
            f"the guard wrote {name!r} twice to $GITHUB_OUTPUT; the job "
            "output would be whichever GitHub reads last")
        outputs[name] = value
    return outputs


def test_the_docs_guard_maps_each_ref_to_the_folder_it_may_write(tmp_path):
    """Each ref writes one folder of the versioned site, or nothing (#866).

    The site is versioned with mike: one folder per minor line (`1.0/`,
    `1.1/`), a `latest` alias copied from the highest final release, and
    a hidden `dev/` for `main`. The guard job decides, for the ref a run
    was started from, which folder it may write, under what title, and
    whether it moves `latest`; the deploy job only carries that out. A
    wrong answer here is a silently wrong live site, so the decision is
    executed against a scratch repository with real, explicitly dated
    tags created out of version order, never grepped.

    - A final tag `vX.Y.Z` (X >= 1) writes `X.Y/` only if it is its
      line's head, the highest final `vX.Y.*`; it moves `latest` only if
      it is also the highest final release. So a patch to an older line
      updates that line's folder and leaves `latest` on the newer line,
      and an older patch tag (a re-run of `v1.0.0` after `v1.0.1`)
      writes nothing, where it would put a superseded release back.
    - A pre-release tag writes `X.Y/` only while X.Y has no final:
      `latest` is a copy of a final's folder, and must never come to
      describe a candidate of a patch.
    - `release/X.Y` by dispatch rewrites `X.Y/` under its head's title
      only while nothing under `isocenter/` changed since that tag
      (owner ruling Q6): the API reference is rendered from those
      docstrings, so a changed `isocenter/` describes code no `pip
      install` delivers. That is how a docs-only page reaches a released
      line without a tag.
    - `main` writes the hidden `dev/` (rulings Q2, Q3).
    - Everything else, and every line below 1.0, writes nothing.

    Version order is git's `version:refname`, which orders `v1.0.10`
    above `v1.0.9` and `v0.10.0` above `v0.9.9` where text order and
    creation order do not. The guard never ranks a pre-release against
    a final -- finals are taken first, and pre-releases only on a line
    with none -- so `versionsort.suffix` would change no answer here,
    and the guard deliberately does not set it.
    """
    import yaml

    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    guard = _step_named(DOCS_WORKFLOW, "guard", DOCS_GUARD_STEP)
    assert "if" not in guard, (
        "the guard step is conditional; a condition is a way for some "
        "trigger to skip it")
    checkout = next(step for step in workflow["jobs"]["guard"]["steps"]
                    if str(step.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0, (
        "the guard's checkout does not fetch full history, so the runner "
        "has no tags to compare against and no history to diff")

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git, git_env = _scratch_git(repo)
    days = iter(range(1, 60))

    def commit(message, path=None):
        day = next(days)
        when = f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}T12:00:00+00:00"
        if path:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(message, encoding="utf-8")
            run_git("add", path)
            run_git("commit", "-q", "-m", message, when=when)
        else:
            run_git("commit", "-q", "--allow-empty", "-m", message, when=when)
        return when

    def tag(name):
        when = commit(name)
        run_git("tag", name, when=when)

    runs = iter(range(1000))

    def decide(ref, sha=None):
        output = tmp_path / f"output-{next(runs)}"
        result = _run_step_script(guard, repo, {
            **git_env, "GITHUB_REF": ref,
            "GITHUB_REF_NAME": ref.split("/", 2)[-1],
            "GITHUB_SHA": sha or run_git("rev-parse", "HEAD"),
            "GITHUB_OUTPUT": str(output)})
        return result, _read_outputs(output)

    def writes(ref, folder, title, alias="", hidden="false", sha=None):
        result, outputs = decide(ref, sha)
        assert result.returncode == 0, (
            f"the guard refused {ref}, which should write {folder}/:\n"
            f"{result.stdout}{result.stderr}")
        expected = {"folder": folder, "title": title, "alias": alias,
                    "hidden": hidden}
        assert outputs == expected, (
            f"{ref} decided {outputs}, not {expected}:\n{result.stdout}")

    def refused(ref, why, sha=None):
        result, outputs = decide(ref, sha)
        assert result.returncode != 0, (
            f"the guard let {ref} deploy {outputs}; it must write nothing, "
            f"because {why}")
        assert "folder" not in outputs, (
            f"the guard refused {ref} but wrote {outputs} first; a job "
            "output from a failed step is a decision half taken")
        return result

    run_git("init", "-q", "-b", "main")
    commit("start")
    _with_decide_script(repo)

    # Lines below 1.0 are not published (no pre-1.0 compatibility).
    # v0.10.0 sorts below v0.9.9 as text; neither may deploy.
    for name in ("v0.9.8", "v0.10.0", "v0.9.9"):
        tag(name)
    refused("refs/tags/v0.9.9", "0.x lines are not published")
    refused("refs/tags/v0.10.0", "0.x lines are not published")

    tag("v1.0.0rc5")
    tag("v1.0.0rc6")
    writes("refs/tags/v1.0.0rc6", "1.0", "1.0.0rc6")
    # The cutover's own first run (RELEASING, "The cutover", step 3): a
    # dispatch from release/1.0 while the line's newest tag is a candidate.
    rc6 = run_git("rev-parse", "refs/tags/v1.0.0rc6^{commit}")
    writes("refs/heads/release/1.0", "1.0", "1.0.0rc6", sha=rc6)
    refused("refs/tags/v1.0.0rc5",
            "rc5 is not the head of 1.0; rc6 is, and would be replaced")

    tag("v1.0.0")
    writes("refs/tags/v1.0.0", "1.0", "1.0.0", alias="latest")
    refused("refs/tags/v1.0.0rc6",
            "1.0 has a final, and a candidate would replace it")

    tag("v1.1.0rc1")
    writes("refs/tags/v1.1.0rc1", "1.1", "1.1.0rc1")
    # A candidate of a later line is not a final: `latest` stays on 1.0.
    writes("refs/tags/v1.0.0", "1.0", "1.0.0", alias="latest")

    tag("v1.0.1")  # created after v1.1.0rc1
    writes("refs/tags/v1.0.1", "1.0", "1.0.1", alias="latest")
    refused("refs/tags/v1.0.0",
            "v1.0.1 is 1.0's head; an older patch would put a superseded "
            "release back into 1.0/ and latest/")

    tag("v1.1.0")
    writes("refs/tags/v1.1.0", "1.1", "1.1.0", alias="latest")
    writes("refs/tags/v1.0.1", "1.0", "1.0.1")  # no alias: 1.1 is newer
    refused("refs/tags/v1.1.0rc1", "1.1 has a final")

    tag("v1.0.2rc1")
    refused("refs/tags/v1.0.2rc1",
            "1.0 has a final; latest must never describe a patch candidate")

    # Numeric, not textual: v1.0.10 is the head of 1.0, above v1.0.9.
    tag("v1.0.10")
    tag("v1.0.9")
    writes("refs/tags/v1.0.10", "1.0", "1.0.10")
    refused("refs/tags/v1.0.9", "v1.0.10 is 1.0's head")

    # Not release tags, and a branch spelled like one: its short name is a
    # real line head's, so only the ref's prefix refuses it.
    tag("zz-not-a-release")
    tag("v1.0")
    tag("v1.1.0.post1")
    run_git("branch", "v1.1.0")
    for ref in ("refs/tags/zz-not-a-release", "refs/tags/v1.0",
                "refs/tags/v1.1.0.post1", "refs/heads/v1.1.0",
                "refs/heads/feature/x", "refs/heads/mainline",
                "refs/heads/main-x", "refs/heads/release/0.9",
                "refs/heads/release/1", "refs/heads/release/1.0.1",
                "refs/pull/1/merge"):
        refused(ref, "it is not a release tag, release/X.Y (X >= 1) or main")

    writes("refs/heads/main", "dev", "dev", hidden="true")

    # release/X.Y by dispatch. Its head is v1.1.0 for 1.1.
    run_git("checkout", "-q", "-b", "release/1.1", "refs/tags/v1.1.0")
    writes("refs/heads/release/1.1", "1.1", "1.1.0",
           sha=run_git("rev-parse", "HEAD"))
    commit("a docs-only page", "docs/midi-b.md")
    commit("a setup change", "setup.py")
    docs_only = run_git("rev-parse", "HEAD")
    writes("refs/heads/release/1.1", "1.1", "1.1.0", sha=docs_only)
    commit("a fix", "isocenter/x.py")
    code = run_git("rev-parse", "HEAD")
    result = refused("refs/heads/release/1.1",
                     "isocenter/ changed since v1.1.0, so the API reference "
                     "would describe code no release installs", sha=code)
    assert "isocenter/x.py" in result.stdout + result.stderr, (
        "the refusal does not name the changed file, so whoever dispatched "
        f"it cannot tell why:\n{result.stdout}{result.stderr}")
    # The sha decides, not what is checked out: the run is of the ref's tip.
    run_git("checkout", "-q", docs_only)
    refused("refs/heads/release/1.1", "the dispatched tip changed isocenter/",
            sha=code)
    run_git("checkout", "-q", "release/1.1")
    writes("refs/heads/release/1.1", "1.1", "1.1.0", sha=docs_only)

    # A branch that does not contain its line's head.
    run_git("checkout", "-q", "-B", "release/1.0", "refs/tags/v1.0.1")
    commit("docs on an old base", "docs/y.md")
    refused("refs/heads/release/1.0",
            "the branch does not contain v1.0.10, 1.0's head",
            sha=run_git("rev-parse", "HEAD"))
    # A line with no tag at all.
    run_git("checkout", "-q", "-b", "release/1.2", "refs/tags/v1.1.0")
    refused("refs/heads/release/1.2", "1.2 has no release to describe",
            sha=run_git("rev-parse", "HEAD"))


def test_the_docs_deploy_passes_the_guards_decision_to_mike(tmp_path):
    """The deploy step turns the guard's four outputs into one `mike deploy`.

    Run here with a stub `mike` on PATH that records its arguments, and a
    real `origin` holding a `gh-pages` branch for the step's fetch. mike
    itself is not run (the `docs` extra is not in the tests environment;
    see the strict test's last paragraph). What this pins is the
    assembly, which is where a silently wrong deploy would come from:
    `latest` given without `--update-aliases` makes mike refuse to move
    an alias another folder holds, and `dev` deployed without its hidden
    property shows `main` in the selector (ruling Q3).
    """
    import os
    import stat

    import yaml

    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    deploy = _step_named(DOCS_WORKFLOW, "deploy", DOCS_DEPLOY_STEP)
    env = deploy.get("env") or {}
    for name in DOCS_OUTPUTS:
        assert env.get(name.upper()) == \
            f"${{{{ needs.guard.outputs.{name} }}}}", (
                f"the deploy step's {name.upper()} does not come from the "
                f"guard's `{name}` output: {env.get(name.upper())!r}")
    guard_job = workflow["jobs"]["guard"]
    guard = _step_named(DOCS_WORKFLOW, "guard", DOCS_GUARD_STEP)
    step_id = guard.get("id")
    assert step_id, "the guard step has no id, so its outputs cannot be read"
    for name in DOCS_OUTPUTS:
        assert (guard_job.get("outputs") or {}).get(name) == \
            f"${{{{ steps.{step_id}.outputs.{name} }}}}", (
                f"the guard job does not declare its `{name}` output, so "
                "the deploy job reads an empty string")

    origin = tmp_path / "origin"
    origin.mkdir()
    run_origin, git_env = _scratch_git(origin)
    run_origin("init", "-q", "-b", "gh-pages")
    run_origin("commit", "-q", "--allow-empty", "-m", "live site")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                   capture_output=True,
                   env={"PATH": os.environ["PATH"], **git_env})

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "mike-argv"
    stub = bin_dir / "mike"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done '
                    f'> "{record}"\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    def deploys(folder, title, alias, hidden):
        if record.exists():
            record.unlink()
        result = _run_step_script(deploy, work, {
            **git_env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FOLDER": folder, "TITLE": title, "ALIAS": alias,
            "HIDDEN": hidden})
        assert result.returncode == 0, result.stdout + result.stderr
        return record.read_text(encoding="utf-8").splitlines()

    assert deploys("1.0", "1.0.1", "latest", "false") == [
        "deploy", "--push", "--title", "1.0.1", "--update-aliases",
        "1.0", "latest"]
    assert deploys("1.0", "1.0.1", "", "false") == [
        "deploy", "--push", "--title", "1.0.1", "1.0"]
    assert deploys("1.1", "1.1.0rc1", "", "false") == [
        "deploy", "--push", "--title", "1.1.0rc1", "1.1"]
    assert deploys("dev", "dev", "", "true") == [
        "deploy", "--push", "--title", "dev", "--prop-set", "hidden=true",
        "dev"]


def test_the_docs_deploy_refuses_a_guard_decision_the_tags_have_since_overtaken(tmp_path):
    """The deploy job decides again, just before mike, and stops on a change.

    The guard's outputs can be old by the time the deploy uses them
    (review of #871, finding 1). "Re-run failed jobs" reuses the outputs
    of the guard that already succeeded: a `v1.0.1` deploy that failed and
    is re-run after `v1.1.0` shipped would still carry `alias=latest`, and
    `mike deploy --update-aliases 1.0 latest` would move `latest` back to
    the older line. Two tags pushed together queue their deploys in the
    order their guards *finished*, with the same result. And a re-run
    after `v1.0.2` would put the superseded 1.0.1 back into `1.0/`.

    So the deploy job runs the guard's own script again against the tags
    as they are now, immediately before the mike step, and fails on any
    difference from the guard's outputs. Executed here against real tags,
    with the outputs of an earlier decision.
    """
    import os

    import yaml

    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    assert DOCS_RECHECK_STEP in names, (
        "the deploy job does not check the guard's decision again, so a "
        "re-run of an old run, or a deploy queued behind a newer tag, "
        "deploys a decision the tags have overtaken and can move `latest` "
        "back onto an older line")
    recheck = _step_named(DOCS_WORKFLOW, "deploy", DOCS_RECHECK_STEP)
    assert names.index(DOCS_RECHECK_STEP) == names.index(DOCS_DEPLOY_STEP) - 1, (
        "the recheck is not the step immediately before the mike deploy; "
        "every minute between them is a minute a newer tag can land in")
    assert "if" not in recheck, "the recheck is conditional"
    assert str(DOCS_DECIDE_SCRIPT) in recheck["run"], (
        "the recheck does not run the guard's script; a second copy of the "
        "rule is a second answer to it")
    env = recheck.get("env") or {}
    for name in DOCS_OUTPUTS:
        assert env.get(name.upper()) == \
            f"${{{{ needs.guard.outputs.{name} }}}}", (
                f"the recheck's {name.upper()} does not come from the "
                f"guard's `{name}` output")

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git, git_env = _scratch_git(repo)
    run_git("init", "-q", "-b", "main")
    # The step fetches tags from `origin` first; here that is itself.
    run_git("remote", "add", "origin", str(repo))
    _with_decide_script(repo)
    days = iter(range(1, 28))

    def tag(name):
        when = f"2026-03-{next(days):02d}T12:00:00+00:00"
        run_git("commit", "-q", "--allow-empty", "-m", name, when=when)
        run_git("tag", name, when=when)

    def rechecked(ref, folder, title, alias="", hidden="false"):
        return _run_step_script(recheck, repo, {
            **git_env, "GITHUB_REF": ref,
            "GITHUB_REF_NAME": ref.split("/", 2)[-1],
            "GITHUB_SHA": run_git("rev-parse", "HEAD"),
            "FOLDER": folder, "TITLE": title, "ALIAS": alias,
            "HIDDEN": hidden})

    def holds(*args, **kwargs):
        result = rechecked(*args, **kwargs)
        assert result.returncode == 0, (
            f"the recheck refused a decision that still holds, {args} "
            f"{kwargs}:\n{result.stdout}{result.stderr}")

    def stale(*args, why, **kwargs):
        result = rechecked(*args, **kwargs)
        assert result.returncode != 0, (
            f"the recheck let a stale decision deploy, {args} {kwargs}: "
            f"{why}")

    tag("v1.0.0")
    tag("v1.0.1")
    holds("refs/tags/v1.0.1", "1.0", "1.0.1", alias="latest")

    tag("v1.1.0")
    stale("refs/tags/v1.0.1", "1.0", "1.0.1", alias="latest",
          why="v1.1.0 is now the highest final, and this would move "
              "`latest` back onto 1.0")
    holds("refs/tags/v1.0.1", "1.0", "1.0.1")

    tag("v1.0.2")
    stale("refs/tags/v1.0.1", "1.0", "1.0.1",
          why="v1.0.2 is 1.0's head now; this would put 1.0.1 back")
    holds("refs/tags/v1.0.2", "1.0", "1.0.2")
    stale("refs/tags/v1.0.2", "1.0", "1.0.2", hidden="true",
          why="every output is compared, not only the alias")
    holds("refs/heads/main", "dev", "dev", hidden="true")


def test_a_refused_docs_run_cannot_cancel_a_deploy():
    """The guard is its own job, and deploys queue rather than cancel.

    When the concurrency group sat at workflow level, a run the guard was
    about to refuse (a patch tag on an older line, a dispatch from a
    branch, the second of two tags pushed together) joined it first,
    cancelled the latest release's deploy in progress, then refused
    itself: the site stayed on whatever was live before that release.
    With the guard in a job with no group, and the deploy needing it,
    only runs that passed the guard compete.

    **`cancel-in-progress` is now `false` (#866).** Newest-wins was right
    while every deploy wrote the one root site: a newer deploy replaced
    all of it. With a versioned site, runs write different folders, and
    cancelling a `1.0/` deploy because a `dev/` deploy started would lose
    1.0's update silently. With `false` the deploys serialise, so two
    mike commits never race inside the workflow. GitHub still keeps at
    most one run pending per group and cancels the older pending one, so
    losing a deploy takes three in flight and shows as a cancelled run;
    it is re-dispatched from its ref (RELEASING.md, "Documentation site").
    """
    import yaml

    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert set(jobs) == {"guard", "deploy"}, sorted(jobs)
    assert "concurrency" not in workflow, (
        "docs.yml has a workflow-level concurrency group; a run its guard "
        "refuses joins it and can cancel a deploy")
    assert "concurrency" not in jobs["guard"], (
        "the guard job is in a concurrency group; a refused run must not "
        "be able to cancel anything")
    assert jobs["deploy"].get("needs") in ("guard", ["guard"]), (
        "the deploy job does not need the guard, so it runs whether or not "
        "the ref may write any folder")
    assert "if" not in jobs["deploy"], (
        "the deploy job is conditional; `if: always()` or similar runs it "
        "after a refused guard")
    concurrency = jobs["deploy"].get("concurrency") or {}
    assert concurrency.get("group"), (
        "the deploy job has no concurrency group; two deploys could "
        "interleave their pushes to gh-pages")
    assert concurrency.get("cancel-in-progress") is False, (
        "the deploy job's group cancels a deploy in progress; with one "
        "folder per line, a newer run for another folder would silently "
        "drop the older run's update (#866)")
    assert not any(step.get("name") == DOCS_GUARD_STEP
                   for step in jobs["deploy"]["steps"]), (
        "the guard runs in the deploy job, inside its concurrency group")

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

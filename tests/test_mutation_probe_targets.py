"""`mutation_probe.TARGETS` must not omit a test that covers its module.

The probe runs only the tests listed for a module. A mutant that some
test in this repo would kill, but whose test is not on that list, is
reported as `SURVIVED` -- which reads as "the suite cannot see this
change to the de-identification core" and sends a human to investigate a
gap that does not exist.

That happened. `tests/test_config_tags_shapes.py` was added for #111 and
never added to `TARGETS`, so two mutations of the warning it covers were
reported as survivors (#106). The list had drifted the same way the docs
it mirrors had.

Omissions fail here; extras do not. `test_remediation_actions.py`
exercises `remediation.py` through `PhiInspector` without importing it,
and no import scan can see that -- so the list has to be able to say
more than a scan would.

A module nobody accounted for fails too. Four modules -- `persistence.py`
(#383), `session.py` (#414), `entities.py` and `imagecodecs_handler.py`
(#419) -- spent releases with no row at all, each found by a person
noticing. Every module under `isocenter/` must now have a `TARGETS` row
or a `NOT_PROBED` entry saying why it has none, and the checks below
keep that ledger honest: a reason, a real path, no empty rows, and a
"0 sites" claim that is still true.
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mutation_probe  # noqa: E402
from mutation_probe import TARGETS  # noqa: E402


def _dotted(module_path: str) -> str:
    """`pkg/sub/mod.py` -> `pkg.sub.mod`; a package's `__init__.py` is the
    package itself."""
    parts = list(pathlib.Path(module_path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_names(tree: ast.AST) -> set:
    """Every dotted name an absolute import statement in `tree` binds.

    The load-bearing form is `from pkg import mod`, yielded as `pkg.mod`:
    the alias may be a module or a name inside the package, the scan
    cannot tell which from syntax, and the text half never sees that
    dotted name. `import pkg.mod` and the `from pkg.mod import f` module
    name are collected too, but the text half already matches both, so
    dropping either leaves the union unchanged -- an equivalent mutant,
    not a gap. Relative imports (`level > 0`) name nothing in `isocenter`
    from a test, whatever module they spell.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif (isinstance(node, ast.ImportFrom) and node.level == 0
              and node.module is not None):
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _importers(module_path: str, tests_dir: pathlib.Path = ROOT / "tests") -> set:
    """Test files that reach `module_path` by name: the union of two scans.

    **The text half** matches the module's dotted name followed by a word
    boundary anywhere in the file. That is what sees
    `patch("<dotted name>.<function>")`, which is not an import, and the
    `\\b` keeps a module from matching a longer name that starts with
    its own (`persistence` against `persistence_manager`).

    **The import half** reads the file's import statements. Its one
    load-bearing contribution is `from <package> import <module>` -- a
    form the text half cannot see, since the dotted name never appears in
    it. The other import forms it collects spell the dotted name in the
    text, so the text half has them already. Before #419
    this function was the text half alone, keyed on the file's stem, and
    so demanded one of the four test files that kill
    `imagecodecs_handler.py` mutants and read `exporters/wfdb.py` as a
    top-level module named by its stem alone.

    Nothing in this file spells a real module's dotted name, the fixtures
    included, so that the scan does not demand this file for any row.

    Neither half alone is enough: the import half loses the `patch()`
    strings, and the text half loses the `from ... import` form. An import
    written inside a string literal (a `pytester.makepyfile` source) is
    seen by neither -- deliberately, since
    it imports nothing in the process the probe runs.
    """
    dotted = _dotted(module_path)
    pattern = re.compile(re.escape(dotted) + r"\b")
    found = set()
    for path in sorted(tests_dir.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text) or dotted in _imported_names(
                ast.parse(text, filename=str(path))):
            found.add(f"tests/{path.name}")
    return found


def test_importers_sees_every_import_form(tmp_path):
    """The scan behind the guard above must see every way a test reaches a module.

    The stem-only rule it replaces matched the package name, a dot and the
    file's stem, which cannot see `from <package> import <module>` -- the
    form three of the four files that kill `imagecodecs_handler.py` mutants
    use -- and names a subpackage module by its stem alone (#419).
    Each fixture below is one form; the decoys pin the two boundaries: a
    longer name that merely starts with the module's, and an import that
    exists only inside a string a test hands to `pytester`, which runs no import in this process.
    """
    demanded = {
        "test_import_dotted.py": "import isocenter.codec\n",
        "test_from_package.py": "from isocenter import codec\n",
        "test_from_package_aliased.py": "from isocenter import (other,\n    codec as c)\n",
        "test_from_module.py": "from isocenter.codec import f\n",
        "test_patch_string.py": "from unittest.mock import patch\n"
                                "def test_x():\n"
                                "    with patch('isocenter.codec.f'):\n        pass\n",
    }
    not_demanded = {
        "test_decoy_longer_name.py": "from isocenter import codecs_extra\n"
                                     "import isocenter.codecs_extra\n",
        "test_decoy_in_a_string.py": 'SRC = """\nfrom isocenter import codec\n"""\n',
        # Relative: `level == 1`, `module == "isocenter"`, so only the
        # level guard stops this reading as the absolute import it spells.
        "test_decoy_relative.py": "from .isocenter import codec\n",
    }
    for name, text in {**demanded, **not_demanded}.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert _importers("isocenter/codec.py", tests_dir=tmp_path) == {
        f"tests/{name}" for name in demanded}

    # A subpackage module is named by its dotted path, not its stem (the
    # shape of `exporters/wfdb.py`, under names that exist nowhere).
    (tmp_path / "test_sub_from.py").write_text(
        "from isocenter.sub import leaf\n", encoding="utf-8")
    (tmp_path / "test_sub_decoy_stem.py").write_text(
        "from isocenter import leaf\n", encoding="utf-8")
    assert _importers("isocenter/sub/leaf.py", tests_dir=tmp_path) == {
        "tests/test_sub_from.py"}

    # A package's `__init__.py` is the package itself.
    assert "tests/test_from_package.py" in _importers(
        "isocenter/__init__.py", tests_dir=tmp_path)


def test_every_test_that_imports_a_target_module_is_listed():
    missing = {}
    for module, (listed, _budget) in TARGETS.items():
        gap = _importers(module) - set(listed)
        if gap:
            missing[module] = sorted(gap)

    assert not missing, (
        "these test files import a probe target but are not in TARGETS, so "
        "the probe will not run them and will over-report survivors:\n"
        + "\n".join(f"  {mod}: {', '.join(files)}"
                    for mod, files in missing.items()))


def _package_modules():
    """Every module under `isocenter/`, as the relative posix paths TARGETS uses."""
    return {path.relative_to(ROOT).as_posix()
            for path in (ROOT / "isocenter").glob("**/*.py")}


def test_every_package_module_is_probed_or_listed_as_not_probed():
    """A module with no row is invisible to the probe, and nothing said so.

    `persistence.py` (#383), `session.py` (#414), `entities.py` and
    `imagecodecs_handler.py` (#419) each reached that state by growing
    into behaviour after the rows were written, and each was found by a
    person noticing. So every module now has to be in exactly one of two
    places: `TARGETS`, or `NOT_PROBED` with the reason it is not probed.
    A module in neither is the silent case; a module in both is a row
    whose own ledger entry says it is not run.
    """
    modules = _package_modules()
    rowed = set(mutation_probe.TARGETS)
    declined = set(mutation_probe.NOT_PROBED)

    unaccounted = sorted(modules - rowed - declined)
    both = sorted(rowed & declined)
    assert not unaccounted and not both, (
        "every module under isocenter/ must have a TARGETS row or a "
        "NOT_PROBED entry saying why it has none, and not both:\n"
        + "".join(f"  UNACCOUNTED: {m}\n" for m in unaccounted)
        + "".join(f"  BOTH: {m}\n" for m in both)
        + f"({len(unaccounted)} unaccounted, {len(both)} in both)")


def test_every_listed_test_file_exists():
    """A renamed or deleted test file would silently shrink the run."""
    missing = sorted(
        f"{module} -> {test}"
        for module, (tests, _budget) in TARGETS.items()
        for test in tests
        if not (ROOT / test).exists())

    assert not missing, "TARGETS names test files that do not exist:\n" + \
        "\n".join(missing)


#: Five mutation sites -- three comparisons, one bool-op, one return --
#: so a budget of 1 samples one of them (stride 5) and a budget of 5
#: samples all five (stride 1). The gap between those two counts is what
#: proves the budget half of a TARGETS value is read, not decoration.
_FIVE_SITE_SRC = "def f(a, b):\n    return (a == 1) and (b == 2) and (a < b)\n"


def _sampled_runs(tmp_path, monkeypatch, argv):
    """Run `main()` over two fake targets and count `run()` calls per module."""
    (tmp_path / "mod_a.py").write_text(_FIVE_SITE_SRC, encoding="utf-8")
    (tmp_path / "mod_b.py").write_text(_FIVE_SITE_SRC, encoding="utf-8")
    counts = {"a.py": 0, "b.py": 0}

    monkeypatch.setattr(mutation_probe, "REPO", tmp_path)
    monkeypatch.setattr(mutation_probe, "TARGETS", {
        "mod_a.py": (["a.py"], 1),
        "mod_b.py": (["b.py"], 5),
    })
    monkeypatch.setattr(mutation_probe, "subprocess_cache_path",
                        lambda p: pathlib.Path("/sentinel/none.pyc"))
    monkeypatch.setattr(mutation_probe, "assert_fresh", lambda p, c: None)

    def fake_run(tests, timeout):
        counts[tests[0]] += 1
        return True

    monkeypatch.setattr(mutation_probe, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["mutation_probe"] + argv)
    mutation_probe.main()
    return counts


def test_each_target_samples_at_its_own_budget(tmp_path, monkeypatch):
    """A `(tests, budget)` tuple whose budget nobody reads is decoration.

    One global knob cannot raise the sampling density of the largest
    module without forcing the already-measured ones to re-pay (#140):
    `io_handlers.py` at 380 sites needs `budget >= 190` for a stride of
    2, which would simultaneously drive `privacy.py` and
    `remediation.py` to stride 1. So each TARGETS value carries its own
    budget, and this pins that `main()` actually samples by it.
    """
    counts = _sampled_runs(tmp_path, monkeypatch, argv=[])
    # control + 1 sample at budget 1; control + 5 samples at budget 5.
    assert counts == {"a.py": 2, "b.py": 6}, counts


def test_a_cli_budget_overrides_every_module(tmp_path, monkeypatch):
    """The positional budget keeps its meaning: one knob for the whole run.

    `python -m scripts.mutation_probe 10` has always meant "sample
    everything at 10", and it stays the cheap pass now that a default
    run costs a full io_handlers sweep -- so an explicit CLI budget wins
    over every per-module default.
    """
    counts = _sampled_runs(tmp_path, monkeypatch, argv=["1"])
    assert counts == {"a.py": 2, "b.py": 2}, counts


def test_every_not_probed_entry_has_a_reason_and_names_a_real_module():
    """An opt-out with no reason is the silence this ledger replaced.

    A blank reason says a module is not probed without saying why, which
    is the state every unrowed module was in before the ledger existed.
    A path that no longer exists is an entry for a module that was moved
    or deleted, and a TARGETS key that no longer exists is a row whose
    control would die on a missing file mid-run.
    """
    blank = sorted(mod for mod, why in mutation_probe.NOT_PROBED.items()
                   if not why.strip())
    stale = sorted(mod for mod in mutation_probe.NOT_PROBED
                   if not (ROOT / mod).is_file())
    stale_rows = sorted(mod for mod in mutation_probe.TARGETS
                        if not (ROOT / mod).is_file())
    assert not blank and not stale and not stale_rows, (
        "".join(f"  NO REASON: {m}\n" for m in blank)
        + "".join(f"  STALE OPT-OUT: {m}\n" for m in stale)
        + "".join(f"  STALE ROW: {m}\n" for m in stale_rows))


def test_every_row_lists_at_least_one_test():
    """An empty list does not run no tests; it runs all of them.

    `run()` hands the list to pytest as its paths, and pytest with no path
    collects `testpaths = tests` from pytest.ini -- the whole suite, for
    every sampled mutant. A row with no tests is either a module nobody
    has measured, which belongs in NOT_PROBED, or an hours-long run
    nobody asked for.
    """
    empty = sorted(mod for mod, (tests, _budget) in mutation_probe.TARGETS.items()
                   if not tests)
    assert not empty, "".join(f"  EMPTY LIST: {m}\n" for m in empty)


def test_a_zero_sites_reason_still_has_zero_sites():
    """The one kind of reason that can be checked is checked.

    "0 sites" says no operator in the probe can reach the module, so a
    row would print NOT MEASURED and nothing else. That stops being true
    the day the module gains a comparison, and the reason would then
    keep excusing it, green. Other numbers in the ledger are dated notes
    and deliberately not checked: they would go red on every edit.
    """
    zero_claims = {mod: why for mod, why in mutation_probe.NOT_PROBED.items()
                   if why.startswith("0 sites")}
    assert zero_claims, "no reason claims 0 sites; this test has gone vacuous"
    wrong = sorted(
        f"{mod} claims 0 sites and has "
        f"{mutation_probe.count_ops((ROOT / mod).read_text(encoding='utf-8'))}"
        for mod in zero_claims
        if mutation_probe.count_ops((ROOT / mod).read_text(encoding="utf-8")))
    assert not wrong, "\n".join(wrong)


def test_a_default_run_prints_what_it_does_not_probe(tmp_path, monkeypatch, capsys):
    """A multi-hour run's tail should say what it did not measure.

    Without it, a default run's output lists nine modules' verdicts and
    reads as the package's coverage; the ledger is what says the rest
    were not looked at.
    """
    monkeypatch.setattr(mutation_probe, "REPO", tmp_path)
    monkeypatch.setattr(mutation_probe, "TARGETS", {})
    monkeypatch.setattr(mutation_probe, "NOT_PROBED", {"x.py": "because"})
    monkeypatch.setattr(sys, "argv", ["mutation_probe"])
    mutation_probe.main()
    out = capsys.readouterr().out
    assert "NOT PROBED" in out
    assert "x.py -- because" in out


def test_a_single_module_run_does_not_print_the_ledger(tmp_path, monkeypatch, capsys):
    """The CLI form names one module and says what it measured; the
    ledger is about a default run's silence, not this one's."""
    (tmp_path / "victim.py").write_text("def f(x):\n    return x == 1\n",
                                        encoding="utf-8")
    monkeypatch.setattr(mutation_probe, "REPO", tmp_path)
    monkeypatch.setattr(mutation_probe, "NOT_PROBED", {"x.py": "because"})
    monkeypatch.setattr(mutation_probe, "subprocess_cache_path",
                        lambda p: pathlib.Path("/sentinel/none.pyc"))
    monkeypatch.setattr(mutation_probe, "assert_fresh", lambda p, c: None)
    monkeypatch.setattr(mutation_probe, "run", lambda tests, timeout: True)
    monkeypatch.setattr(sys, "argv",
                        ["mutation_probe", "1", "victim.py", "t.py"])
    mutation_probe.main()
    out = capsys.readouterr().out
    assert "victim.py" in out
    assert "NOT PROBED" not in out


def test_neither_ledger_names_a_module_twice():
    """A module appears at most once in each of `TARGETS` and `NOT_PROBED`.

    Both are dict literals, and a dict literal keeps the later of two
    equal keys without a word. That is the shape a merge leaves when the
    resolver keeps both sides of a ledger hunk: a module one branch
    refreshed comes back a second time with the other branch's text. A
    module in both ledgers is already reported as BOTH above, but two
    `NOT_PROBED` entries for one module collapse into one before any test
    can import them, so the refreshed reason reverts to the stale one and
    every check that reads the dict stays green (#489's review, against
    #480's side of the ledger). Only the source still holds both, so this
    reads the source.
    """
    tree = ast.parse((ROOT / "scripts" / "mutation_probe.py").read_text())
    keys = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("TARGETS", "NOT_PROBED")):
            keys[node.targets[0].id] = [
                k.value if isinstance(k, ast.Constant) else ast.dump(k)
                for k in node.value.keys]
    assert set(keys) == {"TARGETS", "NOT_PROBED"}, (
        "a ledger is no longer a single dict literal; this guard cannot see it")
    repeated = {name: sorted({k for k in ks if ks.count(k) > 1})
                for name, ks in keys.items()}
    assert repeated == {"TARGETS": [], "NOT_PROBED": []}

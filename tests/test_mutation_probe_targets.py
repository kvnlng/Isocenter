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
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mutation_probe  # noqa: E402
from mutation_probe import TARGETS  # noqa: E402


def _importers(module_path: str):
    """Test files that name `isocenter.<module>` in an import."""
    name = pathlib.Path(module_path).stem
    pattern = re.compile(rf"isocenter\.{re.escape(name)}\b")
    return {
        f"tests/{path.name}"
        for path in sorted((ROOT / "tests").glob("test_*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    }


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

    def fake_run(tests):
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


def test_the_claude_md_mapping_matches_targets():
    """CLAUDE.md calls TARGETS "the maintained version of this list".

    Two copies of one mapping means one of them is out of date, and the
    one people read is rarely the one people edit. This pins them
    together rather than trusting the next editor to update both.
    """
    table = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    for module, (tests, _budget) in TARGETS.items():
        name = pathlib.Path(module).name
        row = next((line for line in table.splitlines()
                    if line.startswith("|") and f"`{name}`" in line), None)
        assert row, f"CLAUDE.md has no mapping row for {name}"

        listed = {cell.strip().strip("`")
                  for cell in row.split("|")[2].split(",")}
        expected = {pathlib.Path(t).name for t in tests}
        assert listed == expected, (
            f"CLAUDE.md's row for {name} lists {sorted(listed)} but "
            f"TARGETS has {sorted(expected)}. Update both.")

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
    for module, listed in TARGETS.items():
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
        for module, tests in TARGETS.items()
        for test in tests
        if not (ROOT / test).exists())

    assert not missing, "TARGETS names test files that do not exist:\n" + \
        "\n".join(missing)


def test_the_claude_md_mapping_matches_targets():
    """CLAUDE.md calls TARGETS "the maintained version of this list".

    Two copies of one mapping means one of them is out of date, and the
    one people read is rarely the one people edit. This pins them
    together rather than trusting the next editor to update both.
    """
    table = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    for module, tests in TARGETS.items():
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

"""Every test module collects the way CI collects it: bare `pytest`, no repository root on `sys.path`.

The trap. `tests.yml` runs the `pytest` script from the checkout, so
nothing puts the repository root on `sys.path`: `tests/` has no
`__init__.py`, and pytest's default `prepend` import mode adds `tests/`
itself, not its parent. A test module that imports `scripts.` (or
`tests.`) at module level therefore collects only if some module
collected *before* it has already put the root there --
`test_changed_code_selects_its_tests.py` and a few others do. So it
depends on the file's name sorting after theirs. Every local gate sets
`PYTHONPATH=<worktree>` (CLAUDE.md, RELEASING step 3), which puts the
root first and hides all of this. The v1.0.0rc10 TestPyPI rehearsal was
the first place `test_a_wfdb_export_saves_the_session.py` met it, and
all 16 test jobs died at collection.

So this collects the whole suite once, in a child that sees what CI
sees:
* the repository root as its cwd, as CI's step has, and `-P`, so the
  cwd is not on `sys.path` (the `pytest` script puts only its own
  `bin/` there). The cwd matters even so: `test_discovery_integration.py`
  inserts `os.path.abspath('.')`, which is the root in CI. From a
  temporary cwd eight more modules fail, all of which CI collects. This
  test pins CI's condition, not a stricter one. The child writes
  nothing there (no cache, collection only);
* `PYTHONPATH` set to a temporary directory holding only a symlink to
  this tree's `isocenter/`. The package under test is importable, as
  CI's install makes it, but the root (`scripts/`, `tests/`) is not.
  With `PYTHONPATH` merely stripped, a local run would import the main
  checkout's editable install instead, a different tree;
* `COVERAGE_*` stripped (CLAUDE.md: conftest's `COVERAGE_FILE` reaches
  every subprocess);
* `-p no:cacheprovider`, so the child writes no `.pytest_cache` into the
  root and the root guard stays quiet.

Any collection error fails the test, naming each file. The fix for one
is a lazy import inside the test that needs it, never a `sys.path`
change in conftest, which would hide the next one.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def test_every_test_module_collects_with_only_the_package_importable(tmp_path):
    shim = tmp_path / "pythonpath"
    shim.mkdir()
    (shim / "isocenter").symlink_to(ROOT / "isocenter", target_is_directory=True)
    env = {k: v for k, v in os.environ.items()
           if k != "PYTHONPATH" and not k.startswith("COVERAGE_")}
    env["PYTHONPATH"] = str(shim)

    done = subprocess.run(
        [sys.executable, "-P", "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "tests"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)

    failed = sorted({line.split()[1] for line in done.stdout.splitlines()
                     if line.startswith("ERROR ")})
    assert done.returncode == 0 and not failed, (
        "these test modules do not collect with only the package on "
        f"sys.path, as CI collects them: {failed or '(no ERROR line)'}\n"
        f"exit {done.returncode}\n{done.stdout[-4000:]}\n{done.stderr[-2000:]}")

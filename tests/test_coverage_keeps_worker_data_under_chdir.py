"""Coverage still sees spawned workers after the chdir fixture (#707, #380).

A worker's coverage data file is written relative to the worker's cwd
unless `COVERAGE_FILE` is absolute. Since every test now runs in its
own `tmp_path`, a relative path puts worker data in a directory pytest
deletes, and `coverage combine` reports a suite in which no worker line
ever ran -- green, and wrong. Measured before the fix: one data file
(the parent's) and no line of `ingest_worker`, against 49 files and 37
lines before the chdir.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_a_worker_executed_line_survives_combine(tmp_path):
    try:
        import coverage
    except ImportError:
        # `coverage` ships in the `dev` extra; the release matrix installs
        # `.[tests,ocr]` and has none.
        pytest.skip("coverage is in the dev extra")

    # A scratch project, so the run neither reads nor writes the real
    # root's `.coverage*`. The package itself is imported from REPO.
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    for name in (".coveragerc", "pytest.ini"):
        shutil.copy(REPO / name, proj / name)
    shutil.copy(REPO / "tests" / "conftest.py", proj / "tests")
    shutil.copy(REPO / "tests" / "test_multiprocessing.py", proj / "tests")
    shutil.copytree(REPO / "tests" / "support", proj / "tests" / "support")

    # This session's own COVERAGE_FILE (conftest sets one) points at the
    # real root; the scratch run must resolve its own.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("COVERAGE_")}
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    run = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q",
         "-p", "no:cacheprovider", "tests/test_multiprocessing.py"],
        cwd=proj, env=env, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout + run.stderr
    subprocess.run([sys.executable, "-m", "coverage", "combine"],
                   cwd=proj, env=env, check=True, timeout=120,
                   capture_output=True)

    data = coverage.CoverageData(str(proj / ".coverage"))
    data.read()
    handlers = next(p for p in data.measured_files()
                    if p.endswith(os.path.join("isocenter", "io_handlers.py")))
    source = Path(handlers).read_text(encoding="utf-8").split("\n")
    start = next(n for n, line in enumerate(source, 1)
                 if line.startswith("def ingest_worker"))
    body = [ln for ln in data.lines(handlers) if start < ln < start + 80]
    assert body, (
        "no line of ingest_worker was measured: the spawned workers' "
        "coverage data did not reach `coverage combine` (#380)")

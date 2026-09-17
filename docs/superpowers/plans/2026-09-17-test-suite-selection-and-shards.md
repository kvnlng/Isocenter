# Test Selection by Change, and Balanced CI Shards: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Date:** 2026-09-17
**Issue:** #707
**Status:** plan only. **No development starts until the owner says go** (owner, 2026-09-17).
**Scope:** internal. Excluded from the docs site by `exclude_docs`. A dated record: deviations found at implementation are listed under a `**Deviations at implementation**` heading at the top, as `2026-09-06-bunch-e-hang-probe-and-export-sweep.md` does, and the spec's §10 Amendments log gets the same entries.

**Goal:** give a developer `pytest --changed` -- the tests that exercise the code they touched -- and run the suite on GitHub as four duration-balanced shards per Python version, with every test isolated in its own working directory.

**Architecture:** three PRs in order. (1) An autouse `chdir(tmp_path)` fixture plus a session guard that fails the run if the repo root gained a file. (2) A `--shard=I/N` option backed by a pure, deterministic assignment function over a checked-in timings file, and a second matrix axis in `tests.yml`. (3) A generated, gitignored map from source lines to the tests that ran them, built from per-test coverage contexts (switched by a conftest hook, so fixtures count) on 3.14t, and a selector that falls back to `scripts/mutation_probe.py`'s `TARGETS` and then to the full suite.

**Tech Stack:** pytest 9 hooks in `tests/conftest.py`; `coverage` 7.16 (`Coverage.current().switch_context`, `CoverageData.contexts_by_lineno`); stdlib `ast`, `subprocess`, `json`, `statistics`; GitHub Actions matrix. **No new dependency.**

**Spec:** `docs/superpowers/specs/2026-09-17-test-suite-selection-and-shards-design.md`. Read it first; §3 holds the measurements this plan argues from.

## Global Constraints

- No new dependency, in any extra. No `pytest-xdist`, no `pytest-testmon`, no `pytest-split` (spec §2 ruling 4, §3.1).
- Test files stay flat in `tests/`. No directories, no per-module markers (spec §2 ruling 1).
- `scripts/mutation_probe.py`'s `TARGETS` and `NOT_PROBED` remain the only hand-maintained module-to-tests map. Import them; never copy them; never parse CLAUDE.md (spec §6.3).
- The merge gate and the release gate stay the **full suite**. `--changed` is advisory and must print that it is (spec §1, §6.4).
- `publish.yml` is not edited. Its filename and environment names are pinned by PyPI's publisher config.
- Interpreters, named explicitly (the `python3` on `PATH` is a pyenv shim without the dependencies): `/Users/kevin/Developer/Isocenter/.venv/bin/python` (3.12) and `/Users/kevin/Developer/Isocenter/.venv314t/bin/python` (3.14.7t, run with `PYTHON_GIL=0`).
- Every hand-run sets `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH=<worktree>`, then prints `isocenter.__file__` and reads it (CLAUDE.md, "Running one mutation by hand"). `isocenter` is installed editable from the main checkout; a worktree imports the wrong tree without this.
- Run `pytest -v` with no pipe when a hang is possible; `-q` buffers.
- New test files that import an `isocenter` module must be added to the matching `TARGETS` row or `tests/test_mutation_probe_targets.py` goes red. The files this plan creates import only `support.*`, `scripts.*` and the stdlib, so none needs a row; if an implementer adds an `isocenter` import, add the row in the same commit.
- Skips are body-level `pytest.skip(...)` calls, never `@pytest.mark.skipif` -- `tests/test_skip_contract.py` flags the marker spelling.
- Helper modules go in `tests/support/` and are imported as `from support.x import y` (there is no `tests/__init__.py`; `tests/` is on `sys.path`). A top-level `tests/*.py` that pytest does not collect fails `test_every_top_level_tests_module_is_one_pytest_collects`.
- Commits are conventional-commit style with a trailing `(#707)`. One PR per Part. Each PR goes through the local gate in `RELEASING.md`: full suite on 3.12 and 3.14t at the pushed SHA, then an adversarial review naming that SHA.
- CHANGELOG: one `### Internal` entry per PR. None of this is user-visible.

## File Structure

| File | Part | Responsibility |
| --- | --- | --- |
| `tests/conftest.py` (modify) | 1, 2, 3 | Wiring only: the chdir fixture, the three options, the hooks. Logic lives in `support/`. |
| `pytest.ini` (modify) | 1 | Register the `repo_root` marker; trim the `testpaths` comment. |
| `tests/support/root_guard.py` (create) | 1 | `snapshot(root)`, `new_entries(root, before)`. Pure; no pytest import. |
| `tests/test_every_test_runs_in_its_own_directory.py` (create) | 1 | The chdir contract, the opt-out, worker cwd inheritance. |
| `tests/test_the_repo_root_stays_clean.py` (create) | 1 | Unit tests for `root_guard`. |
| `tests/test_coverage_keeps_worker_data_under_chdir.py` (create) | 1 | Worker coverage data survives the chdir (#380 must not regress). |
| `tests/support/shards.py` (create) | 2 | `parse`, `assign`, `TimingRecorder`. Pure; no pytest import. |
| `tests/shard_timings.json` (create, generated, checked in) | 2 | `{"tests/test_x.py": seconds}`. |
| `tests/test_shards_partition_the_suite.py` (create) | 2 | The partition contract and the `tests.yml` pin. |
| `.github/workflows/tests.yml` (modify) | 2 | `shard` matrix axis, run command, summary line, caps. |
| `tests/test_packaging_contract.py` (modify) | 2 | `_RUN_TESTS_STEP_MINUTES_FLOOR` and its comment. |
| `scripts/test_map.py` (create) | 3 | `build`, `changed_regions`, `dispatch_sites`, `select`, CLI. |
| `tests/test_changed_code_selects_its_tests.py` (create) | 3 | Rule-by-rule unit tests and the three spike probes as permanent tests. |
| `.gitignore`, `CLAUDE.md`, `RELEASING.md`, `CHANGELOG.md` (modify) | 1, 2, 3 | As each Part states. `CLAUDE.md` is untracked (local only); edit it in the main checkout, not the worktree. |

---

# Part 1 -- Isolation (PR 1)

Branch: `test/707-isolation`. Spec §4.

### Task 1: every test starts in its own directory

**Files:**
- Modify: `tests/conftest.py` (add one fixture directly after `redirect_logging`, which ends at line 253)
- Modify: `pytest.ini` (add `markers`)
- Test: `tests/test_every_test_runs_in_its_own_directory.py`

**Interfaces:**
- Produces: the autouse fixture `_own_working_directory`; the marker `@pytest.mark.repo_root`, which opts a test out of it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_every_test_runs_in_its_own_directory.py`:

```python
"""Every test runs in its own directory; `repo_root` opts out (#707).

Tests used to write `*.db`, `*_pixels.bin` and `*.lock` wherever pytest
was started, which is why two runs in one tree collided and why the
3.14t gate needed a `git archive` copy. The fixture under test moves
the working directory, so a stray relative write lands in `tmp_path`.
"""
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest


def test_a_test_starts_in_its_own_tmp_path(tmp_path):
    assert Path.cwd().resolve() == tmp_path.resolve()


def test_a_relative_write_lands_in_tmp_path(tmp_path):
    Path("stray.db").write_bytes(b"x")
    assert (tmp_path / "stray.db").exists()


@pytest.mark.repo_root
def test_a_repo_root_test_is_left_where_pytest_was_started(request, tmp_path):
    started = Path(request.config.invocation_params.dir).resolve()
    assert Path.cwd().resolve() == started
    assert Path.cwd().resolve() != tmp_path.resolve()


def test_a_spawned_worker_inherits_the_tests_directory(tmp_path):
    # `os.getcwd` is a builtin, so it pickles without this module being
    # importable in the child. spawn, because that is what the session's
    # pool uses and fork would inherit the cwd trivially.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        child_cwd = pool.submit(os.getcwd).result(timeout=120)
    assert Path(child_cwd).resolve() == tmp_path.resolve()
```

- [ ] **Step 2: Run them and watch three fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /Users/kevin/Developer/Isocenter/.venv/bin/python -m pytest -v tests/test_every_test_runs_in_its_own_directory.py`
Expected: `test_a_test_starts_in_its_own_tmp_path`, `test_a_relative_write_lands_in_tmp_path` and `test_a_spawned_worker_inherits_the_tests_directory` FAIL on the cwd assertion; the `repo_root` test errors or warns with `PytestUnknownMarkWarning`. Delete the `stray.db` the second test left in the repo root.

- [ ] **Step 3: Register the marker**

In `pytest.ini`, after the `filterwarnings` block, add:

```ini
markers =
    repo_root: run this test where pytest was started rather than in its own tmp_path. For the few tests that build or launch from the repository root (#707). Everything else runs in tmp_path so nothing is written into the tree.
```

- [ ] **Step 4: Add the fixture**

In `tests/conftest.py`, directly after the `redirect_logging` fixture:

```python
@pytest.fixture(autouse=True)
def _own_working_directory(request, tmp_path, monkeypatch):
    """Run every test in its own `tmp_path` (#707).

    A relative `Session("foo.db")` used to land in the repository root,
    so two runs in one tree shared `foo.db`, its sidecar and both lock
    files. Spawned workers inherit the cwd, so the pools follow.

    `repo_root` opts out, for a test that builds or launches from the
    root. Do not widen the opt-out to silence a failure: a test that
    breaks here was reading a file an earlier test happened to leave
    behind, and that is the defect.
    """
    if request.node.get_closest_marker("repo_root") is None:
        monkeypatch.chdir(tmp_path)
    yield
```

- [ ] **Step 5: Run them and watch all four pass**

Run the Step 2 command. Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py pytest.ini tests/test_every_test_runs_in_its_own_directory.py
git commit -m "test: run every test in its own working directory (#707)"
```

### Task 2: find what depended on the repository root

This task is a triage, so its steps are a procedure and a decision rule rather than code written in advance. Its deliverable is a green full suite and a list, in the PR body, of every test that changed and why.

**Files:**
- Modify: whichever `tests/test_*.py` the run names. Known first candidate: `tests/test_packaging_contract.py` (the sdist build stages `isocenter-<version>/` in the repo root and spawns a build subprocess).

- [ ] **Step 1: Full run on 3.12, unpiped, to a log**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /Users/kevin/Developer/Isocenter/.venv/bin/python -u -m pytest -v > /tmp/707-isolation-312.log 2>&1` (about 20 minutes). Then `grep -E "FAILED|ERROR" /tmp/707-isolation-312.log`.

- [ ] **Step 2: Classify each failure with this rule, in this order**

1. **The test launches a subprocess with a relative module or script path** (`python -m scripts.…`, `python -m tests.…`, `python setup.py`, `python -m build`) **or builds a distribution.** Prefer passing `cwd=REPO` to that one `subprocess` call. Only if the test needs the root for its whole body, add `@pytest.mark.repo_root` with a one-line comment saying what it builds or launches.
2. **The test opens a repo file by a relative path.** Anchor it on `Path(__file__).resolve().parent.parent` as 31 files already do. Never mark it.
3. **The test reads a file an earlier test wrote** (a `*.db`, a config, a CSV). This is a pre-existing order dependence and the chdir exposed it. Fix the test to create what it reads, in `tmp_path`. Never mark it. Name it in the PR body.
4. **Anything else:** stop and report it; do not mark it to make it green.

- [ ] **Step 3: Re-run only the files you changed, then the full suite on both interpreters**

Run the Step 1 command again, then the same with `/Users/kevin/Developer/Isocenter/.venv314t/bin/python` and `PYTHON_GIL=0`. Expected: both green. Record both wall times in the PR body.

- [ ] **Step 4: Commit**

```bash
git add -u tests/
git commit -m "test: stop the tests that relied on the repository root from relying on it (#707)"
```

### Task 3: the repository root stays clean

**Files:**
- Create: `tests/support/root_guard.py`
- Modify: `tests/conftest.py` (`pytest_sessionstart` is new; `pytest_sessionfinish` exists at line 238)
- Test: `tests/test_the_repo_root_stays_clean.py`

**Interfaces:**
- Produces: `root_guard.snapshot(root: Path) -> frozenset[str]`; `root_guard.new_entries(root: Path, before: frozenset[str]) -> list[str]`; `root_guard.ALLOWED_PREFIXES: tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_the_repo_root_stays_clean.py`:

```python
"""A run that leaves a new file in the repository root fails (#707).

The chdir fixture moved the stray writes; this is what stops the next
one. It fails the *run*, naming the entry, because by session finish
the test that wrote it is no longer identifiable from the listing.
"""
from support import root_guard


def test_an_unchanged_root_reports_nothing(tmp_path):
    (tmp_path / "setup.py").write_text("")
    before = root_guard.snapshot(tmp_path)
    assert root_guard.new_entries(tmp_path, before) == []


def test_a_new_file_is_named(tmp_path):
    before = root_guard.snapshot(tmp_path)
    (tmp_path / "stray.db").write_bytes(b"")
    (tmp_path / "stray_pixels.bin.lock").write_bytes(b"")
    assert root_guard.new_entries(tmp_path, before) == [
        "stray.db", "stray_pixels.bin.lock"]


def test_tooling_artifacts_are_not_reported(tmp_path):
    before = root_guard.snapshot(tmp_path)
    for name in (".pytest_cache", "__pycache__", ".coverage",
                 ".coverage.host.123.abc", ".test-map.json"):
        (tmp_path / name).mkdir() if "cache" in name else (
            tmp_path / name).write_bytes(b"")
    assert root_guard.new_entries(tmp_path, before) == []


def test_a_file_that_was_already_there_is_not_reported(tmp_path):
    (tmp_path / "old.db").write_bytes(b"")
    before = root_guard.snapshot(tmp_path)
    (tmp_path / "old.db").write_bytes(b"changed")
    assert root_guard.new_entries(tmp_path, before) == []
```

- [ ] **Step 2: Run and watch them fail**

Run: `… -m pytest -v tests/test_the_repo_root_stays_clean.py`
Expected: collection error, `ModuleNotFoundError: No module named 'support.root_guard'`.

- [ ] **Step 3: Write `tests/support/root_guard.py`**

```python
"""What a test run added to the repository root (#707).

Pure functions over a directory listing, so they are testable against a
`tmp_path` and carry no pytest import. `conftest.py` calls them from
`pytest_sessionstart` and `pytest_sessionfinish`.
"""
from pathlib import Path

#: Entries tooling is entitled to create in the root during a run.
#: Prefix match. Extend this only for tooling -- never for a test's own
#: output, which belongs in `tmp_path`. Each `repo_root` test that
#: legitimately leaves something here gets its entry with the test's
#: name beside it.
ALLOWED_PREFIXES = (
    ".pytest_cache",
    "__pycache__",
    ".coverage",          # `.coverage` and `.coverage.<host>.<pid>.<rand>`
    ".test-map.json",     # scripts/test_map.py (#707)
)


def snapshot(root: Path) -> frozenset:
    return frozenset(entry.name for entry in Path(root).iterdir())


def new_entries(root: Path, before: frozenset) -> list:
    return sorted(
        name for name in snapshot(root) - before
        if not name.startswith(ALLOWED_PREFIXES))
```

- [ ] **Step 4: Run and watch them pass.** Expected: 4 passed.

- [ ] **Step 5: Wire it into `conftest.py`**

Add near the other module-level state, and replace the existing one-line `pytest_sessionfinish`:

```python
from support import root_guard

_root_before = None


def pytest_sessionstart(session):
    # One snapshot per run: a nested in-process pytest calls this again,
    # and the inner run's start is not the outer run's baseline.
    global _root_before
    if _root_before is None:
        _root_before = root_guard.snapshot(session.config.rootpath)


def pytest_sessionfinish(session, exitstatus):
    _mark("sessionfinish")
    if _root_before is None:
        return
    strays = root_guard.new_entries(session.config.rootpath, _root_before)
    if strays:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                "this run left new entries in the repository root: "
                + ", ".join(strays)
                + " -- a test wrote outside its tmp_path (#707)", red=True)
        session.exitstatus = 1
```

If Task 2 added `repo_root` tests that leave something in the root (the sdist staging directory, `build/`, `dist/`, an `*.egg-info`), add each prefix to `ALLOWED_PREFIXES` with the test's name in a comment on the same line.

- [ ] **Step 6: Prove the wiring by hand**

Create `tests/test_zz_scratch_guard_probe.py` containing:

```python
import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

@pytest.mark.repo_root
def test_scratch():
    (REPO / "zz_guard_probe.db").write_bytes(b"")
```

Run: `… -m pytest -v tests/test_zz_scratch_guard_probe.py; echo "exit=$?"`
Expected: the test passes, the red line names `zz_guard_probe.db`, and `exit=1`. Then `rm tests/test_zz_scratch_guard_probe.py zz_guard_probe.db` and confirm `git status` shows neither.

- [ ] **Step 7: Commit**

```bash
git add tests/support/root_guard.py tests/conftest.py tests/test_the_repo_root_stays_clean.py
git commit -m "test: fail a run that leaves a new entry in the repository root (#707)"
```

### Task 4: worker coverage data survives the chdir

`.coveragerc` has `parallel = True`; each process writes `.coverage.<host>.<pid>.<rand>` relative to where it resolves `data_file`. A spawned worker's cwd is now a `tmp_path` pytest deletes. If the worker resolves the path against its cwd, `coverage combine` in the root finds nothing from workers and the documented coverage command loses every worker line while staying green (#380 regressed). Part 3's map is built from exactly this data.

**Files:**
- Modify: `tests/conftest.py` (module scope, above `pytest_configure`)
- Test: `tests/test_coverage_keeps_worker_data_under_chdir.py`

- [ ] **Step 1: Measure before fixing**

```bash
cd <worktree> && rm -f .coverage .coverage.*
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /Users/kevin/Developer/Isocenter/.venv/bin/python -m coverage run -m pytest -q tests/test_multiprocessing.py
ls -a | grep -c '^\.coverage\.'
PYTHONPATH=$PWD /Users/kevin/Developer/Isocenter/.venv/bin/python -m coverage combine
PYTHONPATH=$PWD /Users/kevin/Developer/Isocenter/.venv/bin/python - <<'EOF'
import coverage
d = coverage.CoverageData(".coverage"); d.read()
f = next(p for p in d.measured_files() if p.endswith("io_handlers.py"))
src = open(f).read().split("\n")
start = next(n for n, l in enumerate(src, 1) if l.startswith("def ingest_worker"))
print("ingest_worker body lines measured:",
      sum(1 for ln in d.lines(f) if start < ln < start + 80))
EOF
```

Record both numbers in the PR body. On `main` (before Task 1) the first is 49 and the second is 37. Expect 0 here: `CoverageData` makes its path absolute in the process that constructs it, and a spawned child constructs its own. If it is nevertheless still about 37, coverage resolves the path in the parent and **this task reduces to Steps 2, 4 and 6** (the test stays; the conftest change is not made; say so in the PR body and in spec §10). If it is 0, continue.

- [ ] **Step 2: Write the failing test**

Create `tests/test_coverage_keeps_worker_data_under_chdir.py`. It runs in a scratch project so it neither reads nor writes the real root's `.coverage*`:

```python
"""Coverage still sees spawned workers after the chdir fixture (#707, #380).

A worker's coverage data file is written relative to the worker's cwd
unless `COVERAGE_FILE` is absolute. Since every test now runs in its
own `tmp_path`, a relative path puts worker data in a directory pytest
deletes, and `coverage combine` reports a suite in which no worker line
ever ran -- green, and wrong.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_a_worker_executed_line_survives_combine(tmp_path):
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    for name in (".coveragerc", "pytest.ini"):
        shutil.copy(REPO / name, proj / name)
    shutil.copy(REPO / "tests" / "conftest.py", proj / "tests")
    shutil.copy(REPO / "tests" / "test_multiprocessing.py", proj / "tests")
    shutil.copytree(REPO / "tests" / "support", proj / "tests" / "support")

    env = {k: v for k, v in os.environ.items()
           if not k.startswith("COVERAGE_")}
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    run = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q",
         "tests/test_multiprocessing.py"],
        cwd=proj, env=env, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout + run.stderr
    subprocess.run([sys.executable, "-m", "coverage", "combine"],
                   cwd=proj, env=env, check=True, timeout=120)

    import coverage
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
```

`coverage` ships in the `dev` extra, not `tests`. If `import coverage` fails, the test must `pytest.skip("coverage is in the dev extra")` from its body -- add that guard at the top of the function with a `try/except ImportError`, and add the file to `tests/test_skip_contract.py`'s recognised module-gated skips if that test asks for it.

- [ ] **Step 3: Run and watch it fail.** Expected: FAIL on `assert body`.

- [ ] **Step 4: Pin the data file absolute, in `conftest.py`**

At module scope, above `pytest_configure`:

```python
# Spawned workers inherit the environment but resolve a relative coverage
# data file against *their* cwd, and since #707 that is a tmp_path pytest
# deletes. An absolute COVERAGE_FILE set before the first spawn is what
# every worker then writes beside. Set whether or not coverage is
# running: it is inert without it, and keying it on another variable is
# one more name to be wrong about. Anchored on this file, not on the cwd,
# so a scratch copy of the tree writes into the copy. Respects a
# COVERAGE_FILE the caller set.
if not os.environ.get("COVERAGE_FILE"):
    os.environ["COVERAGE_FILE"] = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".coverage")
```

- [ ] **Step 5: Run and watch it pass**, then repeat Step 1's measurement and record the after-number.

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_coverage_keeps_worker_data_under_chdir.py
git commit -m "test: keep spawned workers' coverage data out of the per-test directory (#707)"
```

### Task 5: the documents that just went stale

**Files:** `CLAUDE.md` (main checkout, untracked), `pytest.ini`, `RELEASING.md`, `CHANGELOG.md`.

- [ ] **Step 1: `CLAUDE.md`.** Replace the paragraph beginning "Tests write `*.db`, `*_pixels.bin`, `*.lock`" with:

> Every test runs in its own `tmp_path` (an autouse `chdir` in `tests/conftest.py`, #707), so nothing a test writes by a relative path reaches the repo. `@pytest.mark.repo_root` opts out, for a test that builds or launches from the root; a run that leaves a new entry in the root fails at session finish, naming it (`tests/support/root_guard.py`). Do not add cleanup and do not widen `ALLOWED_PREFIXES` for a test's own output.

- [ ] **Step 2: `pytest.ini`.** In the `testpaths` comment, the sdist staging directory is still real; add one sentence: "Since #707 that test is `repo_root`-marked and the root guard allows its directory by name."
- [ ] **Step 3: `RELEASING.md`.** Find the 3.14t `git archive` step. Keep it. Change its stated reason to: the copy proves which tree was tested (SHA purity). Remove any sentence giving file collisions as the reason.
- [ ] **Step 4: `CHANGELOG.md`.** Under the unreleased heading, `### Internal`: what changed, that no public behaviour did, and the before/after wall times from Task 2.
- [ ] **Step 5: Commit** (`docs: say where tests write now (#707)`), run the local gate, open the PR with the Task 2 list and the Task 4 numbers in the body.

---

# Part 2 -- Balanced shards (PR 2)

Branch: `ci/707-shards`, from `main` after Part 1 merges. Spec §5.

**Deviation from the spec, recorded here and to be added to spec §10:** the spec names `python -m scripts.shard_timings`. Timings are instead recorded by a conftest option, `--record-shard-timings=PATH`, because parsing `--durations` text is fragile and pytest already hands each phase's duration to a hook. One fewer script; same file.

### Task 6: the assignment function

**Files:**
- Create: `tests/support/shards.py`
- Test: `tests/test_shards_partition_the_suite.py`

**Interfaces:**
- Produces: `shards.parse(spec: str) -> tuple[int, int]` (1-based index, count); `shards.assign(files: Iterable[str], timings: Mapping[str, float], n: int) -> list[list[str]]`; `shards.suite_files(repo: Path) -> list[str]` (posix paths like `tests/test_x.py`); `shards.load_timings(repo: Path) -> dict[str, float]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shards_partition_the_suite.py`:

```python
"""`--shard=I/N` splits the suite into N jobs that together run it once (#707).

The failure this file exists to catch is green: a matrix that lists
three of four shards, or an assignment that drops a file, runs fewer
tests and reports success.
"""
from pathlib import Path

import pytest

from support import shards

REPO = Path(__file__).resolve().parent.parent


def test_parse_reads_index_and_count():
    assert shards.parse("2/4") == (2, 4)


@pytest.mark.parametrize("bad", ["0/4", "5/4", "2", "a/b", "2/0", "-1/4"])
def test_parse_refuses_a_shard_that_cannot_exist(bad):
    with pytest.raises(ValueError):
        shards.parse(bad)


@pytest.mark.parametrize("n", [2, 4, 7])
def test_the_shards_partition_the_real_suite(n):
    files = shards.suite_files(REPO)
    assigned = shards.assign(files, shards.load_timings(REPO), n)
    assert len(assigned) == n
    flat = [f for shard in assigned for f in shard]
    assert sorted(flat) == sorted(files), "a file is missing or duplicated"
    assert len(flat) == len(set(flat)), "a file is in two shards"
    assert all(assigned), "a shard is empty"


def test_assignment_is_deterministic():
    files = [f"tests/test_{c}.py" for c in "abcdefgh"]
    timings = {f: float(i) for i, f in enumerate(files)}
    assert shards.assign(files, timings, 3) == shards.assign(
        list(reversed(files)), dict(reversed(list(timings.items()))), 3)


def test_the_longest_files_are_spread_not_stacked():
    timings = {"tests/test_a.py": 100.0, "tests/test_b.py": 100.0,
               "tests/test_c.py": 1.0, "tests/test_d.py": 1.0}
    assigned = shards.assign(timings, timings, 2)
    loads = sorted(sum(timings[f] for f in shard) for shard in assigned)
    assert loads == [101.0, 101.0]


def test_a_file_with_no_timing_gets_the_median_not_zero():
    timings = {"tests/test_a.py": 10.0, "tests/test_b.py": 10.0,
               "tests/test_c.py": 10.0}
    files = list(timings) + ["tests/test_new.py"]
    assigned = shards.assign(files, timings, 2)
    # 4 files of equal weight over 2 shards: 2 and 2. With a zero
    # default the new file would ride along with two others: 3 and 1.
    assert sorted(len(s) for s in assigned) == [2, 2]
```

- [ ] **Step 2: Run and watch them fail** (`ModuleNotFoundError: support.shards`).

- [ ] **Step 3: Write `tests/support/shards.py`**

```python
"""Which test files run in which CI shard (#707).

File granularity, so module-scoped fixtures stay together and the
partition can be checked without collecting. Pure functions: the
assignment is a deterministic function of (files, timings, n), which is
what lets a contract test assert the shards partition the suite.
"""
import json
import statistics
from pathlib import Path

TIMINGS_FILE = "tests/shard_timings.json"


def parse(spec):
    try:
        index, count = (int(part) for part in spec.split("/"))
    except ValueError:
        raise ValueError(f"--shard wants I/N, e.g. 2/4; got {spec!r}") from None
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"--shard={spec}: I must be within 1..N and N >= 1")
    return index, count


def suite_files(repo):
    return sorted(path.relative_to(repo).as_posix()
                  for path in (Path(repo) / "tests").glob("test_*.py"))


def load_timings(repo):
    path = Path(repo) / TIMINGS_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def assign(files, timings, n):
    """Longest-processing-time greedy; ties broken by name, then index."""
    files = sorted(set(files))
    known = [timings[f] for f in files if f in timings]
    default = statistics.median(known) if known else 1.0
    weight = {f: float(timings.get(f, default)) for f in files}
    result = [[] for _ in range(n)]
    loads = [0.0] * n
    for name in sorted(files, key=lambda f: (-weight[f], f)):
        lightest = min(range(n), key=lambda k: (loads[k], k))
        result[lightest].append(name)
        loads[lightest] += weight[name]
    return [sorted(shard) for shard in result]
```

- [ ] **Step 4: Run and watch them pass.** `test_the_shards_partition_the_real_suite` passes with an empty timings file (every file gets 1.0); `all(assigned)` holds because there are more than 7 files.

- [ ] **Step 5: Commit** (`test: a deterministic assignment of test files to shards (#707)`).

### Task 7: `--shard` and `--record-shard-timings`

**Files:**
- Modify: `tests/conftest.py`, `tests/support/shards.py`
- Create: `tests/shard_timings.json`
- Test: `tests/test_shards_partition_the_suite.py` (append)

**Interfaces:**
- Consumes: `shards.parse`, `shards.assign`, `shards.suite_files`, `shards.load_timings`.
- Produces: `shards.TimingRecorder` with `.add(nodeid: str, seconds: float)` and `.write(path: Path)`; pytest options `--shard=I/N`, `--record-shard-timings=PATH`.

- [ ] **Step 1: Write the failing tests** (append):

```python
def test_the_recorder_sums_every_phase_per_file(tmp_path):
    recorder = shards.TimingRecorder()
    recorder.add("tests/test_a.py::test_x", 1.5)          # setup
    recorder.add("tests/test_a.py::test_x", 2.0)          # call
    recorder.add("tests/test_a.py::TestK::test_y[p0]", 0.5)
    recorder.add("tests/test_b.py::test_z", 4.0)
    out = tmp_path / "t.json"
    recorder.write(out)
    import json
    assert json.loads(out.read_text()) == {
        "tests/test_a.py": 4.0, "tests/test_b.py": 4.0}


def test_a_shard_run_collects_only_its_own_files():
    import subprocess, sys
    seen = []
    for index in (1, 2):
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             f"--shard={index}/2", "tests/test_crypto.py",
             "tests/test_shards_partition_the_suite.py"],
            cwd=REPO, capture_output=True, text=True, timeout=300)
        assert out.returncode in (0, 5), out.stdout + out.stderr
        seen.append({line.split("::")[0] for line in out.stdout.splitlines()
                     if "::" in line})
    assert not (seen[0] & seen[1]), "a file was collected by both shards"
    assert seen[0] | seen[1] == {
        "tests/test_crypto.py", "tests/test_shards_partition_the_suite.py"}
```

- [ ] **Step 2: Run and watch them fail** (`AttributeError: TimingRecorder`; `unrecognized arguments: --shard`).

- [ ] **Step 3: Add `TimingRecorder` to `shards.py`**

```python
class TimingRecorder:
    """Per-file wall time, every phase summed (setup + call + teardown)."""

    def __init__(self):
        self._seconds = {}

    def add(self, nodeid, seconds):
        name = nodeid.split("::", 1)[0]
        self._seconds[name] = self._seconds.get(name, 0.0) + seconds

    def write(self, path):
        rounded = {name: round(total, 2)
                   for name, total in sorted(self._seconds.items())}
        Path(path).write_text(json.dumps(rounded, indent=1) + "\n",
                              encoding="utf-8")
```

- [ ] **Step 4: Wire the options into `conftest.py`**

```python
from support import shards

_timing_recorder = None


def pytest_addoption(parser):
    group = parser.getgroup("isocenter")
    group.addoption(
        "--shard", default=None, metavar="I/N",
        help="run only the test files assigned to shard I of N (#707)")
    group.addoption(
        "--record-shard-timings", default=None, metavar="PATH",
        help="write per-file wall time to PATH, for tests/shard_timings.json")


def pytest_collection_modifyitems(config, items):
    spec = config.getoption("--shard")
    if not spec:
        return
    index, count = shards.parse(spec)
    repo = config.rootpath
    # Assigned over the whole suite, never over `items`: a run restricted
    # to two paths must put each file in the shard the full run would.
    mine = set(shards.assign(shards.suite_files(repo),
                             shards.load_timings(repo), count)[index - 1])
    keep, drop = [], []
    for item in items:
        name = item.path.relative_to(repo).as_posix()
        (keep if name in mine else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_runtest_logreport(report):
    if _timing_recorder is not None:
        _timing_recorder.add(report.nodeid, report.duration)
```

In `pytest_configure`, inside the existing one-configure guard, add:

```python
    global _timing_recorder
    if config.getoption("--record-shard-timings"):
        _timing_recorder = shards.TimingRecorder()
```

In `pytest_sessionfinish`, before the root-guard block, add:

```python
    target = session.config.getoption("--record-shard-timings")
    if _timing_recorder is not None and target:
        _timing_recorder.write(target)
```

A later Part adds to `pytest_addoption` and `pytest_collection_modifyitems`; keep both as single functions.

- [ ] **Step 5: Run and watch them pass.**

- [ ] **Step 6: Generate the first timings file** (one full run on 3.12, about 20 minutes):

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /Users/kevin/Developer/Isocenter/.venv/bin/python -u -m pytest -v --record-shard-timings=$PWD/tests/shard_timings.json`
The path is absolute so the file lands in the tree whatever directory pytest was started from. Then print the four loads and confirm the largest is within 10% of the smallest:

```bash
PYTHONPATH=$PWD/tests /Users/kevin/Developer/Isocenter/.venv/bin/python - <<'EOF'
from pathlib import Path
from support import shards
repo = Path(".").resolve(); t = shards.load_timings(repo)
for i, s in enumerate(shards.assign(shards.suite_files(repo), t, 4), 1):
    print(i, len(s), round(sum(t.get(f, 0) for f in s)), "s")
EOF
```

- [ ] **Step 7: Commit** (`test: run one shard of the suite, and record the timings that balance them (#707)`), including `tests/shard_timings.json`.

### Task 8: the workflow, and the pin that it lists every shard

**Files:**
- Modify: `.github/workflows/tests.yml`
- Test: `tests/test_shards_partition_the_suite.py` (append)

- [ ] **Step 1: Write the failing test** (append):

```python
def test_the_gate_workflow_runs_every_shard_it_divides_into():
    import re
    import yaml
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "tests.yml").read_text("utf-8"))
    job = workflow["jobs"]["test"]
    listed = job["strategy"]["matrix"]["shard"]
    run = next(s for s in job["steps"] if s.get("id") == "suite")["run"]
    match = re.search(r"--shard=\$\{\{ matrix\.shard \}\}/(\d+)", run)
    assert match, f"the Run Tests step does not pass --shard: {run!r}"
    count = int(match.group(1))
    assert listed == list(range(1, count + 1)), (
        f"tests.yml divides the suite into {count} shards and runs "
        f"{listed}: every shard not listed is a quarter of the suite "
        "that no job runs, behind a green check")
```

- [ ] **Step 2: Run and watch it fail** (`KeyError: 'shard'`).

- [ ] **Step 3: Edit `tests.yml`.** Three changes, comments included:

Under `matrix:`, after `python-version:`:

```yaml
        # Four duration-balanced shards per version (#707). Four because
        # the release runs four versions: 16 jobs stays under the plan's
        # 20-job concurrency limit. Membership is a pure function of
        # tests/shard_timings.json (tests/support/shards.py), and
        # tests/test_shards_partition_the_suite.py pins that this list is
        # exactly 1..N for the N the run step divides by -- a shard left
        # off this list is a quarter of the suite nobody runs, and green.
        #
        # The concurrency group above needs no shard in it: it is declared
        # at workflow level, so it scopes the run, and matrix jobs inside
        # one run do not compete for it.
        shard: [1, 2, 3, 4]
```

The run step's command becomes:

```yaml
      run: |
        pytest -v --shard=${{ matrix.shard }}/4
```

The summary step's `echo` becomes:

```yaml
        echo "- Python ${{ matrix.python-version }} (GIL=${{ endsWith(matrix.python-version, 't') && '0' || '1' }}) shard ${{ matrix.shard }}/4: $mark" \
```

Leave both `timeout-minutes` values alone in this task.

- [ ] **Step 4: Run the new test and the whole of `tests/test_packaging_contract.py`.** Expected: all pass (the caps have not moved).

- [ ] **Step 5: Commit** (`ci: run the suite as four balanced shards per version (#707)`).

### Task 9: set the caps from a measured run

**Files:**
- Modify: `.github/workflows/tests.yml`, `tests/test_packaging_contract.py` (`_RUN_TESTS_STEP_MINUTES_FLOOR` at line 1363 and the comment block above it)

- [ ] **Step 1: Push the branch and dispatch the gate on it**

```bash
git push -u origin ci/707-shards
gh workflow run tests.yml --ref ci/707-shards
gh run list --workflow tests.yml --branch ci/707-shards --limit 1
```

- [ ] **Step 2: Read the eight Run Tests durations**

`gh run view <id> --json jobs --jq '.jobs[] | "\(.name)\t\([.steps[] | select(.name=="Run Tests")][0] | (.completedAt|fromdate) - (.startedAt|fromdate))s"'`
Expected: eight rows, all green. Record them. Let `peak` be the largest, in seconds.

- [ ] **Step 3: Compute the new step cap**

`cap = max(20, ceil(peak / 0.6 / 60 / 5) * 5)` minutes: the measured peak at no more than 60% of the cap, rounded up to 5, and never under 20 because `faulthandler_timeout = 300` must stay under half of it with room. Job cap = `3 + 3 + 3 + 3 + cap + 1`, plus 7, matching the existing margin (88 -> 95).

- [ ] **Step 4: Apply it.** In `tests.yml` set both `timeout-minutes`, and append a dated paragraph to the Run Tests comment in the file's existing voice: the run id, the eight durations' range per version, the peak, the percentage the new cap puts it at, the new step sum. In `test_packaging_contract.py` set `_RUN_TESTS_STEP_MINUTES_FLOOR` to `cap` and append the same measurement to its comment block. Update the f-string in `test_the_run_tests_step_keeps_the_headroom_the_suite_needs` that names "measured peak of 1643s" to the new peak.

- [ ] **Step 5: Run `tests/test_packaging_contract.py` in full.** Expected: pass -- in particular `test_the_job_cap_cannot_fire_before_a_steps_own_timeout`, `test_a_hang_dumps_tracebacks_before_any_timeout_kills_it`, `test_the_stall_watchdog_fires_inside_the_run_tests_step`.

- [ ] **Step 6: Dispatch once more and confirm eight greens under the new caps.** Then CHANGELOG `### Internal` entry with both sets of numbers, commit (`ci: size the step and job caps to a sharded run (#707)`), local gate, PR.

---

# Part 3 -- `pytest --changed` (PR 3)

Branch: `test/707-changed`, from `main` after Parts 1 and 2 merge. Spec §6.

### Task 10: coverage contexts become a map

**Files:**
- Create: `scripts/test_map.py`
- Modify: `.gitignore` (add `.test-map.json`)
- Test: `tests/test_changed_code_selects_its_tests.py`

**Interfaces:**
- Produces: a `pytest_runtest_protocol` hookwrapper in `conftest.py` that labels coverage with the running test's nodeid; `test_map.MAP_FILE = ".test-map.json"`; `test_map.context_to_nodeid(context: str) -> str`; `test_map.split_contexts(contexts_by_lineno: dict) -> tuple[dict, list]`; `test_map.from_coverage(data_path: Path, repo: Path, sha: str, python: str) -> dict` with keys `sha`, `python`, `lines` (`{path: {str(lineno): [nodeid, ...]}}`) and `workers` (`{path: [lineno, ...]}`).

**Why a hook and not `dynamic_context = test_function`** (found in plan review, measured 2026-09-17; spec §10 items 4-6). Coverage's `test_function` context starts when a `test*` frame is entered and ends when it returns, so everything a **fixture** executes -- `Session()` construction, an ingest inside `reloaded_redaction_session` -- is recorded under the empty context, exactly like a worker line, and would be mis-filed as worker-only. Switching the context ourselves around the whole test protocol (setup, call, teardown) is what `pytest-cov --cov-context=test` does, and it needs no dependency. Measured: a fixture-executed line in `builders.py` is recorded under `tests/test_zz_fixture_probe.py::test_uses_fixture`. Contexts are then nodeids, parametrize id included. Known limit, to be recorded and not fixed: a module- or session-scoped fixture is attributed to the first test that triggers it.

`dynamic_context` and `switch_context` must not both be set; coverage warns about conflicting contexts. The build uses `.coveragerc` as it stands.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_changed_code_selects_its_tests.py`:

```python
"""`pytest --changed` runs the tests that exercise what was edited (#707).

pytest-testmon was tried first and rejected: it traces the pytest
process only, so an edit to `ingest_worker` selected no tests at all.
The last three tests in this file are that experiment, kept.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# As tests/test_mutation_probe_targets.py does for the probe: the bare
# `pytest` console script does not put the repo root on sys.path, so
# `from scripts import …` works under `python -m pytest` and not under it.
sys.path.insert(0, str(REPO / "scripts"))
import test_map  # noqa: E402


@pytest.mark.parametrize("context, nodeid", [
    ("tests/test_crypto.py::test_wrong_key",
     "tests/test_crypto.py::test_wrong_key"),
    ("tests/test_a.py::TestK::test_y[p0-True]", "tests/test_a.py::TestK::test_y"),
])
def test_a_context_is_a_nodeid_without_its_parametrize_id(context, nodeid):
    # Selection is per test function: a change that one parameter set
    # exercises re-runs them all, and ids with brackets in them cannot
    # then break the match in conftest.
    assert test_map.context_to_nodeid(context) == nodeid


def test_lines_with_a_test_go_to_lines_and_the_rest_to_workers():
    contexts = {10: ["tests/test_a.py::test_one[x]", ""], 11: [""],
                12: ["tests/test_a.py::test_two"]}
    lines, workers = test_map.split_contexts(contexts)
    assert lines == {"10": ["tests/test_a.py::test_one"],
                     "12": ["tests/test_a.py::test_two"]}
    assert workers == [11]
```

The `sys.path` line mirrors `tests/test_mutation_probe_targets.py:33`; read it before changing the import.

- [ ] **Step 2: Run and watch them fail** (`ImportError: cannot import name 'test_map'`).

- [ ] **Step 3: Write the first half of `scripts/test_map.py`**

```python
"""Which tests exercise which source lines, and what a change selects (#707).

    python -m scripts.test_map build     # 3.14t, clean tree at a known SHA
    python -m scripts.test_map select    # print what --changed would run

The map is generated, gitignored and never edited. `TARGETS` in
scripts/mutation_probe.py stays the one maintained module-to-tests map;
this only narrows inside it.

Contexts are nodeids, switched by a hook in tests/conftest.py around each
test's whole protocol, so what a fixture runs is attributed to the test.

Built on 3.14t because coverage over spawned workers is what is slow on
3.12 -- 5.96 s bare against 68.3 s under .coveragerc, contexts or not,
for tests/test_multiprocessing.py + tests/test_crypto.py -- where the
free-threaded build pays 0.64 s against 0.89 s (spec §10). Lines a
spawned worker runs are recorded under the empty context on both, since
the context lives in the parent; they are kept apart as `workers` and
selected through their dispatch sites.
"""
import json
import os
from pathlib import Path

MAP_FILE = ".test-map.json"


def context_to_nodeid(context):
    return context.split("[", 1)[0]


def split_contexts(contexts_by_lineno):
    lines, workers = {}, []
    for lineno in sorted(contexts_by_lineno):
        named = sorted({context_to_nodeid(c)
                        for c in contexts_by_lineno[lineno] if c})
        if named:
            lines[str(lineno)] = named
        else:
            workers.append(lineno)
    return lines, workers


def from_coverage(data_path, repo, sha, python):
    import coverage
    data = coverage.CoverageData(str(data_path))
    data.read()
    repo = Path(repo).resolve()
    result = {"sha": sha, "python": python, "lines": {}, "workers": {}}
    for measured in sorted(data.measured_files()):
        try:
            rel = Path(measured).resolve().relative_to(repo).as_posix()
        except ValueError:
            continue
        lines, workers = split_contexts(data.contexts_by_lineno(measured))
        if lines:
            result["lines"][rel] = lines
        if workers:
            result["workers"][rel] = workers
    return result
```

- [ ] **Step 4: Run and watch them pass.** Add `.test-map.json` to `.gitignore` under the coverage block.

- [ ] **Step 5: Add the context hook to `tests/conftest.py`**

```python
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Label coverage with the running test, fixtures included (#707).

    `dynamic_context = test_function` stops at the test function's own
    frame, so what a fixture executes is recorded under no test at all.
    Inert unless coverage is measuring this process: `current()` is None.
    """
    try:
        import coverage
    except ImportError:
        return (yield)
    cov = coverage.Coverage.current()
    if cov is not None:
        cov.switch_context(item.nodeid)
    try:
        return (yield)
    finally:
        if cov is not None:
            cov.switch_context("")
```

- [ ] **Step 6: Prove it by hand.** `find . -maxdepth 1 -name '.coverage.*' -delete` (never `rm -f .coverage*` -- that glob deletes `.coveragerc`, which silently turns off worker measurement; it cost this plan two invalid measurements), then `… -m coverage run -m pytest -q tests/test_crypto.py && … -m coverage combine`, then print `sorted(CoverageData(".coverage").measured_contexts())[:3]` after `.read()`. Expected: the empty string, then nodeids such as `tests/test_crypto.py::test_wrong_key`.

- [ ] **Step 7: Commit** (`test: label coverage with the running test, and turn the labels into a line-to-tests map (#707)`).

### Task 11: from a diff to changed functions

**Files:** Modify `scripts/test_map.py`; test: append to `tests/test_changed_code_selects_its_tests.py`.

**Interfaces:**
- Produces: `test_map.parse_hunks(diff_text: str) -> dict[str, list[tuple[int, int]]]` (old-side line ranges per path; a pure insertion after old line `a` is `(a, a)`: the line it follows, so an insertion at the end of a function still belongs to that function); `test_map.enclosing_function(source: str, start: int, end: int) -> tuple[int, int] | None`; `test_map.Region` = `namedtuple("Region", "path start end")` where `start is None` means "no enclosing function"; `test_map.changed_regions(repo: Path, sha: str) -> tuple[list[Region], list[str]]` (regions in tracked `.py` files under `isocenter/`; every other changed or untracked path).

- [ ] **Step 1: Write the failing tests** (append):

```python
DIFF = """\
diff --git a/isocenter/session.py b/isocenter/session.py
--- a/isocenter/session.py
+++ b/isocenter/session.py
@@ -70,0 +71 @@ def scan_worker(args):
+    probe = 1
@@ -1612,2 +1613,3 @@ class DicomSession:
-        a = 1
-        b = 2
+        a = 3
diff --git a/docs/environment.md b/docs/environment.md
--- a/docs/environment.md
+++ b/docs/environment.md
@@ -5 +5 @@
-old
+new
"""


def test_hunks_are_read_in_old_side_numbering():
    hunks = test_map.parse_hunks(DIFF)
    assert hunks["isocenter/session.py"] == [(70, 70), (1612, 1613)]
    assert hunks["docs/environment.md"] == [(5, 5)]


SOURCE = '''\
import os

LIMIT = 3


class Session:
    flag = True

    def compact(self):
        """Doc."""
        return 1

    def export(self):
        def inner():
            return 2
        return inner()
'''


def test_a_changed_line_widens_to_its_innermost_function():
    assert test_map.enclosing_function(SOURCE, 11, 11) == (9, 11)
    assert test_map.enclosing_function(SOURCE, 15, 15) == (14, 15)


def test_a_module_or_class_body_line_has_no_enclosing_function():
    # Widening to the class would select every test that touches it --
    # for Session, the suite. These fall to the module's TARGETS row.
    assert test_map.enclosing_function(SOURCE, 3, 3) is None
    assert test_map.enclosing_function(SOURCE, 7, 7) is None
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement** (append to `scripts/test_map.py`):

```python
import ast
import re
import subprocess
from collections import namedtuple

Region = namedtuple("Region", "path start end")

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def parse_hunks(diff_text):
    hunks, path = {}, None
    for line in diff_text.splitlines():
        # `--- a/` exactly: a removed source line that happens to begin
        # `-- ` also renders as `--- …`, and must not be read as a header.
        if line.startswith("--- a/"):
            path = line[6:].strip()
        elif line.startswith("--- /dev/null"):
            path = None  # a new file has no old side; see `--diff-filter=A` below
        else:
            match = _HUNK.match(line)
            if match and path:
                start = int(match.group(1))
                count = 1 if match.group(2) is None else int(match.group(2))
                end = start if count == 0 else start + count - 1
                hunks.setdefault(path, []).append((start, end))
    return hunks


def enclosing_function(source, start, end):
    best = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= start and end <= node.end_lineno:
                if best is None or node.lineno >= best[0]:
                    best = (node.lineno, node.end_lineno)
    return best


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def changed_regions(repo, sha):
    # Diffed against the map's own SHA, so old-side numbers are the map's
    # numbers however stale the working tree is. Working tree vs SHA
    # covers committed, staged and unstaged edits in one diff.
    hunks = parse_hunks(_git(repo, "diff", "-U0", sha, "--", "."))
    regions, other = [], []
    for path, ranges in sorted(hunks.items()):
        if not (path.startswith("isocenter/") and path.endswith(".py")):
            other.append(path)
            continue
        source = _git(repo, "show", f"{sha}:{path}")
        for start, end in ranges:
            span = enclosing_function(source, start, end)
            regions.append(Region(path, *(span or (None, None))))
    added = _git(repo, "diff", "--name-only", "--diff-filter=A", sha, "--", ".")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard")
    other.extend(sorted(set(added.split()) | set(untracked.split())))
    return sorted(set(regions), key=lambda r: (r.path, r.start or 0)), sorted(set(other))
```

A file added since the SHA has no old side, so it yields no hunk; it arrives through `--diff-filter=A` in `other`, and Task 12's rules send an added `isocenter/*.py` to its `TARGETS` row or the full suite.

- [ ] **Step 4: Run and watch them pass.**
- [ ] **Step 5: Commit** (`test: read a diff as the functions it changed (#707)`).

### Task 12: the selection rules

**Files:** Modify `scripts/test_map.py`; test: append.

**Interfaces:**
- Consumes: `Region`, the map dict from Task 10.
- Produces: `test_map.dispatch_sites(repo: Path) -> dict[str, set[int]]`; `test_map.Selection` = dataclass(`full: bool`, `nodeids: set[str]`, `files: set[str]`, `reasons: list[str]`); `test_map.select(mapping: dict | None, regions: list[Region], other: list[str], targets: dict, repo: Path) -> Selection`. `targets` is `mutation_probe.TARGETS` as it stands: `{module_path: ([test_file, ...], budget)}`.

- [ ] **Step 1: Write the failing tests** (append). One per rule in spec §6.3, plus the order:

```python
MAP = {
    "sha": "abc", "python": "3.14.7t",
    "lines": {
        "isocenter/session.py": {
            "1612": ["tests/test_compaction.py::TestCompaction::test_a"],
            "900": ["tests/test_multiprocessing.py::test_parallel"],
        },
    },
    "workers": {"isocenter/io_handlers.py": [4030, 4031]},
}
TARGETS = {
    "isocenter/session.py": (["tests/test_session.py"], 80),
    "isocenter/io_handlers.py": (["tests/test_io.py"], 80),
}
SITES = {"isocenter/session.py": {900}}


def _select(regions=(), other=(), mapping=MAP):
    return test_map.select(mapping, list(regions), list(other), TARGETS,
                           REPO, sites=SITES)


def test_rule_1_a_function_with_named_contexts_selects_those_tests():
    sel = _select([test_map.Region("isocenter/session.py", 1609, 1700)])
    assert not sel.full
    assert sel.nodeids == {
        "tests/test_compaction.py::TestCompaction::test_a"}


def test_rule_2_a_worker_only_function_selects_through_the_dispatch_sites():
    sel = _select([test_map.Region("isocenter/io_handlers.py", 4021, 4100)])
    assert sel.nodeids == {"tests/test_multiprocessing.py::test_parallel"}


def test_rule_3_a_region_with_no_record_falls_to_the_targets_row():
    sel = _select([test_map.Region("isocenter/session.py", 5000, 5010)])
    assert sel.files == {"tests/test_session.py"} and not sel.full
    sel = _select([test_map.Region("isocenter/session.py", None, None)])
    assert sel.files == {"tests/test_session.py"}


def test_rule_4_a_module_with_no_row_selects_the_full_suite():
    sel = _select([test_map.Region("isocenter/profiles.py", None, None)])
    assert sel.full and "isocenter/profiles.py" in " ".join(sel.reasons)


def test_rule_5_a_changed_test_file_selects_itself():
    sel = _select(other=["tests/test_crypto.py"])
    assert sel.files == {"tests/test_crypto.py"} and not sel.full


@pytest.mark.parametrize("path", [
    "tests/conftest.py", "tests/support/shards.py", "setup.py",
    "pytest.ini", ".coveragerc"])
def test_rule_6_shared_test_machinery_selects_the_full_suite(path):
    assert _select(other=[path]).full


def test_rule_7_any_other_path_selects_the_tests_that_name_it():
    sel = _select(other=["docs/environment.md"])
    assert "tests/test_documented_env_vars.py" in sel.files


def test_no_map_degrades_to_targets_rows_and_says_so():
    sel = _select([test_map.Region("isocenter/session.py", 1609, 1700)],
                  mapping=None)
    assert sel.files == {"tests/test_session.py"}
    assert any("no map" in reason for reason in sel.reasons)


def test_the_dispatch_site_finder_sees_every_worker_in_the_live_source():
    found = test_map.dispatched_workers(REPO)
    assert {"scan_worker", "_verify_worker", "_discover_worker",
            "ingest_worker", "_export_instance_worker"} <= found, (
        "a worker function has no dispatch site the finder recognises, so "
        "an edit to it would select nothing through rule 2")
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement** (append):

```python
from dataclasses import dataclass, field

FULL_SUITE_PATHS = ("tests/conftest.py", "tests/support/", "setup.py",
                    "pytest.ini", ".coveragerc")


@dataclass
class Selection:
    full: bool = False
    nodeids: set = field(default_factory=set)
    files: set = field(default_factory=set)
    reasons: list = field(default_factory=list)


def _worker_calls(repo):
    """(path, lineno-range, worker-name) for every call handed a *_worker.

    A dispatch site is a call one of whose positional arguments is a bare
    name ending `_worker`: `run_parallel(ingest_worker, batch, ...)`, or
    the export pool's call with `_export_instance_worker` on its own
    line. By convention rather than by list, so a sixth worker is found
    the day it is written.
    """
    for path in sorted((Path(repo) / "isocenter").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(repo).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id.endswith("_worker"):
                        yield rel, range(node.lineno, node.end_lineno + 1), arg.id


def dispatch_sites(repo):
    sites = {}
    for rel, lines, _name in _worker_calls(repo):
        sites.setdefault(rel, set()).update(lines)
    return sites


def dispatched_workers(repo):
    return {name for _rel, _lines, name in _worker_calls(repo)}


def _tests_naming(repo, path):
    needle = Path(path).name
    return {test.relative_to(repo).as_posix()
            for test in (Path(repo) / "tests").glob("test_*.py")
            if needle in test.read_text(encoding="utf-8")}


def select(mapping, regions, other, targets, repo, sites=None):
    sel = Selection()
    if mapping is None:
        sel.reasons.append(
            f"no map at {MAP_FILE}: falling back to TARGETS rows "
            "(build one with `python -m scripts.test_map build`)")
    lines = (mapping or {}).get("lines", {})
    workers = (mapping or {}).get("workers", {})
    if sites is None:
        sites = dispatch_sites(repo)

    def row(path, why):
        if path in targets:
            sel.files.update(targets[path][0])
            sel.reasons.append(f"{path}: {why} -> its TARGETS row")
        else:
            sel.full = True
            sel.reasons.append(
                f"{path}: {why} and no TARGETS row -> the full suite")

    for region in regions:
        if region.start is None:
            row(region.path, "changed outside any function")
            continue
        span = range(region.start, region.end + 1)
        named = {node for ln in span
                 for node in lines.get(region.path, {}).get(str(ln), ())}
        if named:
            sel.nodeids |= named
            sel.reasons.append(
                f"{region.path}:{region.start}-{region.end}: "
                f"{len(named)} tests ran it")
        elif any(ln in span for ln in workers.get(region.path, ())):
            via = {node for site_path, site_lines in sites.items()
                   for ln in site_lines
                   for node in lines.get(site_path, {}).get(str(ln), ())}
            if via:
                sel.nodeids |= via
                sel.reasons.append(
                    f"{region.path}:{region.start}-{region.end}: runs only "
                    f"in workers -> the {len(via)} tests that dispatch one")
            else:
                row(region.path, "runs only in workers, no dispatch recorded")
        else:
            row(region.path, f"{region.start}-{region.end} has no record")

    for path in other:
        if path.startswith(FULL_SUITE_PATHS):
            sel.full = True
            sel.reasons.append(f"{path}: shared test machinery -> the full suite")
        elif path.startswith("tests/test_") and path.endswith(".py"):
            sel.files.add(path)
            sel.reasons.append(f"{path}: a changed test file -> itself")
        elif path.startswith("isocenter/") and path.endswith(".py"):
            row(path, "new since the map")
        else:
            named = _tests_naming(repo, path)
            sel.files |= named
            sel.reasons.append(
                f"{path}: {len(named)} test files name it")
    return sel
```

- [ ] **Step 4: Run and watch them pass.** If `test_the_dispatch_site_finder_sees_every_worker_in_the_live_source` fails, read how the missing worker is dispatched (`grep -n "<name>" isocenter/*.py`) and extend `_worker_calls` to that spelling (a keyword argument, an `Attribute`) with a test case for it; do not weaken the assertion.

- [ ] **Step 5: Commit** (`test: the rules that turn changed functions into a selection (#707)`).

### Task 13: `build`, `select`, and `pytest --changed`

**Files:** Modify `scripts/test_map.py`, `tests/conftest.py`; test: append.

**Interfaces:**
- Consumes: everything above; `mutation_probe.TARGETS`.
- Produces: `test_map.load(repo) -> dict | None`; `test_map.selection_for(repo) -> Selection`; `test_map.main(argv)`; pytest option `--changed`.

- [ ] **Step 1: Write the failing test** (append):

```python
def test_changed_with_nothing_changed_runs_nothing_and_says_why(tmp_path):
    import subprocess, sys
    out = subprocess.run(
        [sys.executable, "-m", "scripts.test_map", "select"],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "advisory" in out.stdout
    assert "the merge gate is the full suite on 3.12 and 3.14t" in out.stdout
```

- [ ] **Step 2: Run and watch it fail** (`No module named scripts.test_map.__main__` / no `main`).

- [ ] **Step 3: Implement the CLI** (append):

```python
ADVISORY = ("advisory -- the merge gate is the full suite on 3.12 and 3.14t")


def load(repo):
    path = Path(repo) / MAP_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def selection_for(repo):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mutation_probe import TARGETS  # as tests/test_mutation_probe_targets.py does
    mapping = load(repo)
    sha = mapping["sha"] if mapping else _git(repo, "merge-base", "HEAD",
                                              "origin/main").strip()
    regions, other = changed_regions(repo, sha)
    return select(mapping, regions, other, TARGETS, repo), mapping, sha


def describe(sel, mapping, sha, repo):
    behind = _git(repo, "rev-list", "--count", f"{sha}..HEAD").strip()
    out = [f"map: {sha[:9]} ({mapping['python']}), {behind} commits behind HEAD"
           if mapping else f"map: none; diffing against {sha[:9]}"]
    out += [f"  {reason}" for reason in sel.reasons] or ["  nothing changed"]
    out.append("selected: the full suite" if sel.full else
               f"selected: {len(sel.nodeids)} tests + {len(sel.files)} files")
    out.append(ADVISORY)
    return "\n".join(out)


def build(repo, out_dir):
    """Run the suite under per-test contexts and write the map to out_dir."""
    import sys
    import tempfile
    repo = Path(repo).resolve()
    if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise SystemExit("build wants a clean tree: the map's line numbers "
                         "are only meaningful at a commit")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory() as scratch:
        # .coveragerc as it stands: the contexts come from conftest's
        # pytest_runtest_protocol hook, not from a dynamic_context line.
        rc = repo / ".coveragerc"
        env = dict(os.environ, COVERAGE_FILE=str(Path(scratch) / ".coverage"),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(repo))
        subprocess.run([sys.executable, "-m", "coverage", "run",
                        f"--rcfile={rc}", "-m", "pytest", "-q"],
                       cwd=repo, env=env, check=False)
        subprocess.run([sys.executable, "-m", "coverage", "combine",
                        f"--rcfile={rc}"], cwd=repo, env=env, check=True)
        mapping = from_coverage(Path(scratch) / ".coverage", repo, sha,
                                sys.version.split()[0]
                                + ("t" if not sys._is_gil_enabled() else ""))
    target = Path(out_dir) / MAP_FILE
    target.write_text(json.dumps(mapping), encoding="utf-8")
    print(f"wrote {target}: {len(mapping['lines'])} files with named lines, "
          f"{len(mapping['workers'])} with worker-only lines")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="python -m scripts.test_map")
    sub = parser.add_subparsers(dest="command", required=True)
    built = sub.add_parser("build")
    built.add_argument("--out", default=".",
                       help="directory to write .test-map.json into; when "
                            "building in a `git archive` copy, the main checkout")
    sub.add_parser("select")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parent.parent
    if args.command == "build":
        build(repo, args.out)
    else:
        sel, mapping, sha = selection_for(repo)
        print(describe(sel, mapping, sha, repo))
        for name in sorted(sel.nodeids | sel.files):
            print(name)


if __name__ == "__main__":
    main()
```

`sys._is_gil_enabled` exists on 3.13+; on 3.12 guard it with `getattr(sys, "_is_gil_enabled", lambda: True)()`. A `git archive` copy is not a git repository, so `build` in one needs the SHA passed in: add `--sha` to the `build` parser and, when given, skip the clean-tree check and the `rev-parse`. Write that now, not later -- the gate copy is where this runs.

- [ ] **Step 4: Wire `--changed` into `conftest.py`.** In `pytest_addoption`:

```python
    group.addoption(
        "--changed", action="store_true",
        help="run only the tests that exercise what changed since the "
             "test map was built (#707); advisory, never the gate")
```

At the top of `pytest_collection_modifyitems`, before the `--shard` block (so the two compose: select, then shard):

```python
    if config.getoption("--changed"):
        sys.path.insert(0, str(config.rootpath / "scripts"))
        import test_map
        sel, mapping, sha = test_map.selection_for(config.rootpath)
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                test_map.describe(sel, mapping, sha, config.rootpath))
        if not sel.full:
            keep, drop = [], []
            for item in items:
                name = item.path.relative_to(config.rootpath).as_posix()
                node = item.nodeid.split("[", 1)[0]
                (keep if name in sel.files or node in sel.nodeids
                 else drop).append(item)
            config.hook.pytest_deselected(items=drop)
            items[:] = keep
```

- [ ] **Step 5: Run the test, then try it by hand.** Add a blank line inside `Session.compact`'s body, run `… -m pytest --changed --collect-only -q`, read the reasons, `git checkout isocenter/session.py`. With no map yet, expect the `TARGETS` row for `session.py` and the "no map" reason.

- [ ] **Step 6: Commit** (`test: pytest --changed (#707)`).

### Task 14: the three spike probes, kept

These are the two cases testmon got wrong and the one it got right (spec §3.1, §6.5). They build a small real map, so they are slow; they are the reason this was built rather than adopted.

**Files:** test: append to `tests/test_changed_code_selects_its_tests.py`.

- [ ] **Step 1: Write the tests**

```python
PROBE_FILES = ["tests/test_multiprocessing.py", "tests/test_compaction.py",
               "tests/test_crypto.py"]


@pytest.fixture(scope="module")
def small_real_map(tmp_path_factory):
    """A map built from three real test files, in a scratch copy."""
    import os, shutil, subprocess, sys
    try:
        import coverage  # noqa: F401
    except ImportError:
        pytest.skip("coverage is in the dev extra")
    proj = tmp_path_factory.mktemp("maprepo")
    subprocess.run(f"git archive HEAD | tar -x -C {proj}", shell=True,
                   cwd=REPO, check=True)
    rc = proj / ".coveragerc"  # contexts come from the copy's conftest hook
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env.update(PYTHONPATH=str(proj), PYTHONDONTWRITEBYTECODE="1",
               COVERAGE_FILE=str(proj / ".coverage"))
    subprocess.run([sys.executable, "-m", "coverage", "run", f"--rcfile={rc}",
                    "-m", "pytest", "-q", *PROBE_FILES],
                   cwd=proj, env=env, check=True, timeout=1500)
    subprocess.run([sys.executable, "-m", "coverage", "combine",
                    f"--rcfile={rc}"], cwd=proj, env=env, check=True)
    return proj, test_map.from_coverage(proj / ".coverage", proj, "HEAD", "probe")


def _region_of(proj, path, def_line_prefix):
    source = (proj / path).read_text(encoding="utf-8")
    start = next(n for n, line in enumerate(source.split("\n"), 1)
                 if line.startswith(def_line_prefix))
    return test_map.Region(path, *test_map.enclosing_function(
        source, start + 1, start + 1))


def _files(sel):
    return {n.split("::")[0] for n in sel.nodeids} | sel.files


def test_a_compact_edit_selects_the_compaction_tests_only(small_real_map):
    proj, mapping = small_real_map
    region = _region_of(proj, "isocenter/session.py", "    def compact")
    sel = test_map.select(mapping, [region], [], {}, proj)
    assert _files(sel) == {"tests/test_compaction.py"}


@pytest.mark.parametrize("path, prefix", [
    ("isocenter/session.py", "def scan_worker"),
    ("isocenter/io_handlers.py", "def ingest_worker"),
])
def test_a_worker_edit_selects_the_test_that_runs_it_in_a_pool(
        small_real_map, path, prefix):
    # pytest-testmon selected 0 of 28 for the ingest_worker edit.
    proj, mapping = small_real_map
    sel = test_map.select(mapping, [_region_of(proj, path, prefix)], [], {}, proj)
    assert "tests/test_multiprocessing.py" in _files(sel)
    assert "tests/test_crypto.py" not in _files(sel)
```

On 3.12 the fixture's run pays for coverage over spawned workers (about 70 s for `test_multiprocessing.py` alone, against 6 s bare -- contexts themselves are free); the 1500 s timeout covers it. Measure the fixture's wall time on both interpreters and put both in the PR body. If it exceeds 5 minutes on 3.12, the fixture must `pytest.skip` from its body when `sys._is_gil_enabled()` is true, with the measured figure in the message, and `tests/test_skip_contract.py` is updated to account for it.

- [ ] **Step 2: Run on both interpreters.** Expected: 3 passed each. `git archive HEAD` exports the last commit, so commit Tasks 10-13 first.

- [ ] **Step 3: Commit** (`test: keep the three probes that decided against testmon (#707)`).

### Task 15: build the first real map, close the open points, write it down

- [ ] **Step 1: Build on 3.14t in a `git archive` copy, timing it**

```bash
S=$(mktemp -d) && git archive HEAD | tar -x -C $S && cd $S
time PYTHON_GIL=0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$S \
  /Users/kevin/Developer/Isocenter/.venv314t/bin/python -m scripts.test_map build \
  --sha $(git -C /Users/kevin/Developer/Isocenter rev-parse HEAD) \
  --out /Users/kevin/Developer/Isocenter
```

Compare against a plain 3.14t full run. This closes spec §9's second point. If the ratio is under about 2x, `RELEASING.md` says the 3.14t gate run *is* the map build; if not, it says the map is built on demand, and spec §10 records which.

- [ ] **Step 1b: Find out how much of `workers` is really threads.** Measured while planning: with `.coveragerc`'s `concurrency = multiprocessing`, a 3.14t run of `test_multiprocessing.py` still left `ingest_worker` under the empty context and wrote 15 data files -- so that ingest spawned processes even on 3.14t -- while a run with **no** rcfile (coverage's default `concurrency = thread`) recorded 10 lines of `scan_worker` under the test's nodeid. `concurrency` replaces the default rather than adding to it, so worker *threads* may currently go unmeasured in the map. Build once more with a scratch rc whose line reads `concurrency = multiprocessing,thread`, diff the two maps' `workers` sections, and if lines move from `workers` to `lines`, have `build()` use that rc (written to its temp directory; `.coveragerc` itself is not changed, since its SIGTERM comment was measured under the present setting). Record the counts in spec §10.

- [ ] **Step 2: Measure rule 2's breadth** (spec §9, third point). Insert a line in `ingest_worker`, run `python -m scripts.test_map select | tail -n +1 | grep -c '^tests/'`, revert. If the selected files exceed a third of `tests/test_*.py`, implement the per-worker refinement before the PR: `_worker_calls` already yields the worker name, so key `dispatch_sites` by name and, in rule 2, use only the sites of workers whose own function range contains or transitively calls the region -- and if "transitively" cannot be had cheaply, record the measured breadth in spec §10 and leave the coarse rule.

- [ ] **Step 3: Check whether export's pool takes threads on 3.14t** (spec §9, fourth point): `grep -n "maxtasksperchild\|multiprocessing.Pool\|ctx.Pool" isocenter/io_handlers.py`, read the strategy branch, and record the answer in spec §10.

- [ ] **Step 4: Documents.**
  - `CLAUDE.md` (main checkout): in the Commands block add `pytest --changed` and `python -m scripts.test_map build`. In the tier list, "run the tests for what you touched (see the mapping below)" becomes "run `pytest --changed`". **Delete the module-to-tests table** and the sentence introducing it, keeping the pointer to `TARGETS` and `NOT_PROBED`. Then run `tests/test_source_citations.py`: the deletion moves every line below it, and any citation into CLAUDE.md must still hold.
  - `RELEASING.md`: where the map is built (per Step 1's finding).
  - Spec §10: every deviation and measurement from this Part.
  - `CHANGELOG.md`: `### Internal`.

- [ ] **Step 5: Commit, local gate on both interpreters, PR.** The PR body carries: the build time and ratio, rule 2's measured breadth, the Task 14 fixture times, and one worked example (`compact()` edit -> what `--changed` printed).

---

## Self-review against the spec

| Spec section | Task |
| --- | --- |
| §4 chdir fixture, `repo_root` opt-out | 1 |
| §4 candidates found by running, order-dependence fixed not marked | 2 |
| §4 root-is-clean guard | 3 |
| §4 coverage `data_file` trap, (a) measure (b) pin (c) test | 4 |
| §4 stale docs, `git archive` copy stays | 5 |
| §5 `--shard`, file granularity, LPT, median default | 6, 7 |
| §5 timings file (mechanism deviates: conftest option, recorded above) | 7 |
| §5 partition contract + workflow lists `1..N` | 6, 8 |
| §5 `tests.yml` axis, summary line, concurrency group unchanged, `publish.yml` untouched | 8 |
| §5 caps from a dispatched run; the four named pins | 9 |
| §6.1 map on 3.14t, gitignored, `lines`/`workers`, no map degrades | 10, 13 |
| §6.2 diff against the map's SHA, old-side numbers, widen to function | 11 |
| §6.3 rules 1-7 and their order | 12 |
| §6.4 `--changed`, reasons, SHA and age, advisory line, `select` CLI | 13 |
| §6.5 unit tests per rule, three probes kept, dispatch finder against live source | 12, 14 |
| §8 CLAUDE.md table deleted, Commands block | 15 |
| §9 four open points | 4 (first), 15 (other three) |

Names checked across tasks: `Region(path, start, end)`, `Selection(full, nodeids, files, reasons)`, `select(mapping, regions, other, targets, repo, sites=None)`, `dispatch_sites`/`dispatched_workers`, `from_coverage`, `split_contexts`, `context_to_nodeid`, `shards.parse/assign/suite_files/load_timings/TimingRecorder`, `root_guard.snapshot/new_entries/ALLOWED_PREFIXES` -- each is defined once and used with that signature.

One inconsistency found and fixed inline: spec §6.5 lists four workers for the dispatch finder; the live source has five (`_discover_worker` at `session.py:205`). Task 12 asserts five.

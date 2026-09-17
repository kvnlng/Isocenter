# A smaller set to test a change against, and a suite that runs as shards

**Date:** 2026-09-17
**Milestone:** v1.0.0 -- Production Release (tooling that lands *before* the
L1--L14 bunches; not a promise item -- see §1)
**Issues:** #707 (split the suite so a change runs its own tests, locally and
on GitHub Actions). Related: #699 (gate wall time doubled), #611 (`Session()`
replaces the log handler).
**Base:** `main` at `7cee3a9`
**Status:** design brief, owner-approved in conversation on 2026-09-17. Four
owner rulings are recorded in §2. Two spikes were run before writing and
their measurements are in §3, because they are the evidence for §6. Three
PRs, in the order of §8. §10 is the Amendments log, empty until
implementation fills it.

## 1. The problem, and why it is scheduled ahead of 1.0

The suite is 323 files and about 4,800 tests in a flat `tests/`, with no
markers and no sharding. It takes ~20 minutes per interpreter locally and
45--75 minutes on a GitHub runner. Under `RELEASING.md` the full suite on
3.12 and 3.14t is the merge gate for every push.

Two different things are wanted, and they need different structures:

- **A developer wants a smaller set to run against their change.** This is
  the more important half (owner, 2026-09-17). It needs a *cover*: changed
  code -> the tests that exercise it. Overlap is fine.
- **GitHub wants the suite as parallel jobs.** This needs a *partition*:
  every test in exactly one job.

#707 breaks none of the four 1.0 promises, so by the v1.0.0 milestone's own
rule it would be v1.0.1. It is scheduled first anyway because it repays
itself only *during* L1--L14: thirty gate issues each pay the two-interpreter
gate on every push, and a local selection that is right shortens every
red-green cycle inside them. After 1.0 it is worth much less.

**The tier rule is unchanged: the tier decides breadth, never depth.** The
local selection is advisory. The merge gate and the release gate remain the
whole suite. A wrong selection costs a late discovery at the gate, never a
shipped regression.

## 2. Owner rulings (2026-09-17)

1. **Files stay flat; no directory split.** `TARGETS` is a cover -- a test
   file legitimately sits under several modules -- and a directory forces one
   home per file. It would also move every `tests/test_*.py` path cited in
   CLAUDE.md, CHANGELOG, the specs and `TARGETS` itself.
2. **GitHub shards are balanced by duration, not named areas.** A named-area
   partition is a second hand-kept map beside `TARGETS` (which #707 warns
   against) and is lopsided: `io_handlers.py` alone has 126 files.
3. **Isolation is an autouse `chdir(tmp_path)` plus a root-is-clean guard**,
   not a 58-file rewrite and not checkout-per-shard.
4. **Local parallelism (`pytest-xdist`) is out of scope.** xdist distributes
   a selection; it does not make one. No new dependency.

## 3. What the spikes measured

All in a `git archive` copy of `7cee3a9` in a scratch directory, `.venv`
untouched, `isocenter.__file__` printed and confirmed to be the copy.

### 3.1 `pytest-testmon` 2.2.0 -- rejected

Baseline of 28 tests over six files, then one statement inserted into a
function body and `--testmon --collect-only` asked what it would run.

| Edit | Selected | Verdict |
| --- | --- | --- |
| `Session.compact()` body | 6 of 28, the two compaction files | Correct, and the reduction wanted |
| `scan_worker` body | 4 of 28 | Missed `test_multiprocessing.py`, which audits through the pool |
| `ingest_worker` body | **0 of 28** | Missed `test_multiprocessing.py`, which ingests through the pool |

testmon traces the pytest process only. On 3.12 `run_parallel()` sends
ingest, scan, verify and export to spawned workers, so the transitive closure
of four worker functions -- most of `io_handlers.py`, `privacy.py`,
`pixel_analysis.py` -- is invisible to it. "Nothing to run" for an
`ingest_worker` edit is the expensive kind of wrong.

It did establish that **function-level selection works and is the right
size**: 6 tests, against the 207 files `session.py`'s `TARGETS` row selects.

### 3.2 Our own coverage configuration with `dynamic_context = test_function`

`.coveragerc` as it stands plus that one line, over
`tests/test_multiprocessing.py tests/test_crypto.py` (5 tests):

| Interpreter | Plain | Coverage, no contexts | Coverage + contexts |
| --- | --- | --- | --- |
| 3.12 (process workers) | 5.96 s | 6.06 s | **69.27 s** (twice, 68.91 and 69.27) |
| 3.14.7t, `PYTHON_GIL=0` (thread workers) | 0.64 s | not measured | **0.93 s** |

- Worker lines **are** recorded on both (37 lines of `ingest_worker`, 18/8 of
  `scan_worker`).
- On both they are recorded under the **empty context** `''`. On 3.12 the
  spawned worker has no test frame; on 3.14t the worker *thread's* stack
  starts at the thread bootstrap and has none either. The hypothesis that
  the threads path would attribute worker lines to the test was tested and
  is false.
- The 11.6x on 3.12 is confined to contexts and to this pool-heavy pair; it
  was not measured over the whole suite. The likely cause (a process that
  never enters a test context re-evaluates the context question on every new
  frame) was **not verified**.

Consequence: **the map is generated on 3.14t**, where contexts cost ~1.45x
on the pair measured, and the design must handle `''` structurally (§6.2).

## 4. PR 1 -- isolation: every test runs in its own directory

**What.** One autouse fixture in `tests/conftest.py`, beside
`redirect_logging`, doing `monkeypatch.chdir(tmp_path)`. Spawned workers
inherit the cwd, so `run_parallel()` pools follow.

**Why it is low-risk (measured over `tests/test_*.py`).** 0 files open a repo
path relatively, 31 anchor on `__file__`, 1 reads the cwd, 58 never touch
`tmp_path`/`tempfile`. Almost nothing depends on *being* in the root; tests
only happen to write there.

**Opt-out.** A registered marker, `@pytest.mark.repo_root`, skips the chdir.
Candidates are found by running the suite once under the fixture, not by
guessing. The first is known: `test_packaging_contract.py`'s sdist staging,
which builds `isocenter-<version>/` in the root and spawns build
subprocesses; anything that launches `python -m scripts.…` or `python -m
tests.…` with a relative module path is the second class.

**The guard.** A session-scoped check in `conftest.py`: snapshot the repo
root's directory listing at session start, compare at session finish, and
fail the run naming every new entry not produced by a `repo_root` test. This
makes the class uncollectable rather than merely fixed -- the 59th file that
writes `foo.db` to the cwd cannot land.

**The trap this PR must not walk into: coverage's worker data.**
`.coveragerc` has `parallel = True`, and each process writes
`.coverage.<host>.<pid>.<rand>` relative to where it resolves `data_file`.
If a spawned worker resolves it against its cwd, then after the chdir its
data lands in a `tmp_path` that pytest deletes, `coverage combine` in the
root finds no worker files, and CLAUDE.md's documented coverage command
silently loses every worker line -- #380 regressed with a green run. PR 1
must (a) determine which process resolves the path and when, (b) pin it
absolute (`COVERAGE_FILE`, or `data_file` made absolute before the first
spawn), and (c) carry a test that runs a pool-using test under coverage and
asserts a worker-executed line is present after `combine`. This is also the
PR 1 <-> PR 3 dependency: the map in §6 is built from exactly this data.

**Docs that go stale and are rewritten in this PR.** CLAUDE.md's "Tests write
`*.db` … into the repo root … leave them alone" paragraph; the part of
`pytest.ini`'s `testpaths` comment that explains the root sdist staging
(still true, now for one marked test). `RELEASING.md`: the 3.14t gate's
`git archive` copy **stays**, but its stated reason narrows to SHA purity --
the log proves which tree was tested -- since collision is no longer one.

**Expected fallout.** Tests sharing state through the root by accident
(one test's `foo.db` read by the next) will go red. Those are fixed in this
PR; each is a pre-existing order dependence and is named in the PR body.

## 5. PR 2 -- balanced shards on GitHub

**Mechanism, no new dependency.** A `--shard=I/N` option in
`tests/conftest.py`, applied in `pytest_collection_modifyitems`: keep the
items whose **file** is assigned to shard `I`. File granularity keeps
module-scoped fixtures together, keeps the timings file small, and lets the
partition be checked without collecting.

**Assignment.** `tests/support/shards.py`, pure function
`assign(files, timings, n) -> list[list[str]]`: longest-processing-time
greedy, ties broken by filename, so the result is deterministic from its
inputs. A file absent from the timings gets the median duration -- a new
test file is sharded on the day it is added, not the day someone remembers
the timings.

**Timings.** `tests/shard_timings.json`, checked in: `{file: seconds}`,
summed per file from one `pytest --durations=0` full run on 3.12 (the slower
interpreter). Regenerated by `python -m scripts.shard_timings` when the
balance drifts; drift costs wall time, never correctness, so nothing forces
it.

**The partition contract** (`tests/test_shards_partition_the_suite.py`): for
`N` in the value `tests.yml` uses and two others, over a glob of
`tests/test_*.py`: the union of shards equals the glob, shards are pairwise
disjoint, and no shard is empty. Plus an AST/text pin that `tests.yml`'s
shard list is exactly `1..N` for the `N` it passes -- a matrix that lists
three of four shards is the silent failure here, and it is green.

**`tests.yml`.** A second matrix axis, `shard: [1, 2, 3, 4]`; the run step
becomes `pytest -v --shard=${{ matrix.shard }}/4`. `N = 4` because the
release runs four versions: 16 jobs stays under the 20-job concurrency limit
of the plan, and puts the measured 45-minute peak near 12--15 minutes plus
~5 of setup.

What changes and what does not:

- **Concurrency group: unchanged.** It is declared at workflow level, so it
  scopes the *run*; matrix jobs inside one run do not compete for it. The
  run-33032212241 failure was two *calls* sharing a group, and the
  version-list discriminator that fixed it still discriminates.
- **`publish.yml`: unchanged.** `needs: [build, test-floor]` reads the called
  workflow's aggregate result, which is red if any shard is.
  `test_every_version_classifier_is_run_by_the_release_matrix` and
  `test_the_release_floor_runs_the_floor_python_requires_declares` read the
  version list, which does not move.
- **The step summary** gains the shard in each line
  (`Python 3.12 (GIL=1) shard 2/4: pass`). Sixteen lines instead of four; no
  aggregation job, because a red shard is more useful named than folded.
- **The Run Tests cap drops**, and the number is set from the first
  dispatched sharded run rather than guessed here. The pins that move with
  it, by name: `test_the_run_tests_step_keeps_the_headroom_the_suite_needs`
  (a floor -- its number changes, its reason is rewritten with the new
  measurement), `test_the_job_cap_cannot_fire_before_a_steps_own_timeout`
  (the inequality holds; the job cap drops with the step sum),
  `test_a_hang_dumps_tracebacks_before_any_timeout_kills_it` and
  `test_the_stall_watchdog_fires_inside_the_run_tests_step`
  (`faulthandler_timeout = 300` must stay under half the new cap, so the cap
  cannot go below 11 minutes and should not go below 20).
  `test_the_gate_workflow_cannot_cancel_its_own_release_matrix` is
  untouched.

**Locally** `--shard` is available and unadvertised. The local gate stays a
single serial run per interpreter.

## 6. PR 3 -- selection: `pytest --changed`

### 6.1 The map

Generated by `python -m scripts.test_map build`, **on 3.14t** (§3.2), in a
clean tree at a known SHA -- in practice the gate's `git archive` copy, which
is already both. It runs the suite under coverage with
`dynamic_context = test_function`, combines, and writes
`.test-map.json` to the main checkout (gitignored):

```
{"sha": "<commit>", "python": "3.14.7t",
 "lines":   {"isocenter/session.py": {"1612": ["tests/test_compaction.py::…", …]}},
 "workers": {"isocenter/io_handlers.py": [4023, 4024, …]}}
```

`lines` holds lines with at least one named context. `workers` holds lines
recorded **only** under `''` -- executed, by no test frame.

A fresh clone has no map. Selection then degrades to §6.3 rule 4 for
everything, and says so.

### 6.2 From a change to a selection

`git diff -U0 <map sha>` (plus staged, unstaged and untracked) gives hunks in
**old-side numbering, which is the map's numbering** -- diffing against the
map's own SHA is what makes a stale working tree harmless. Each hunk is
widened to its enclosing `def`/`class` body using the AST of
`git show <sha>:<file>`; a pure insertion takes the enclosing body of its
insertion point. Function granularity, because §3.1 showed it is the right
size and because a changed line's neighbours are what the change can break.

### 6.3 The rules, first match wins per changed region

1. **Region has named contexts** -> those tests. (`compact()` -> 6 tests.)
2. **Region is `workers`-only** -> every test with a named context on a
   **dispatch site**: a line in the parent that hands a worker function to
   `run_parallel()` or the export pool. Dispatch sites are found by AST (a
   call whose first argument names one of the module-scope worker
   functions), not listed by hand. Coarse -- every test that ingests through
   a pool, for an `ingest_worker` edit -- and correct, and still far under a
   126-file row. A finer worker->dispatch-site pairing (an `ingest_worker`
   region selects only tests covering the *ingest* dispatch) is the obvious
   refinement; it is in scope only if the coarse set proves too large in
   use.
3. **Region has no record at all** (new code, module-scope lines, a path
   only the process branch executes and so absent from a 3.14t map) -> the
   module's `TARGETS` row.
4. **Module has no `TARGETS` row** -- the 15 `NOT_PROBED` modules -> the
   **full suite**. Fail-safe, and printed as the reason.
5. **A changed `tests/test_*.py`** -> that file, whole.
6. **`tests/conftest.py`, `tests/support/`, `setup.py`, `pytest.ini`,
   `.coveragerc`** -> the full suite.
7. **Any other changed path** (docs, workflows, `scripts/`, `CLAUDE.md` is
   untracked and never appears) -> the test files whose text names the
   path's basename. This is how `docs/environment.md` reaches
   `test_documented_env_vars.py` and a workflow reaches
   `test_packaging_contract.py`.

`TARGETS` stays the one maintained map. The coverage map only narrows inside
it and is generated, never edited -- there is no second hand-kept list for
#707's warning to apply to. `scripts/test_map.py` imports `TARGETS` and
`NOT_PROBED` from `scripts/mutation_probe.py`; nothing parses CLAUDE.md.

### 6.4 Interface

`pytest --changed` (conftest option; deselects in
`pytest_collection_modifyitems`, composes with `-k`, paths and `--shard`).
Before the run it prints the selection and the rule that produced each part,
the map's SHA and age in commits, and one fixed line: *advisory -- the merge
gate is the full suite on 3.12 and 3.14t.* `python -m scripts.test_map
select` prints the same without running.

### 6.5 Testing the selector

- Unit tests over synthetic maps and diffs for each rule in §6.3, including
  the fall-through order.
- **The three §3.1 probes become permanent tests**, run against a small real
  map built in `tmp_path` from a handful of test files: a `compact()` edit
  selects the compaction tests; a `scan_worker` edit and an `ingest_worker`
  edit each select `test_multiprocessing.py`. These are the two cases
  testmon got wrong and are the reason this was built rather than adopted.
  They are `repo_root`-marked and slow; they are the point.
- The dispatch-site finder is tested against the live source: it must find a
  site for each of `scan_worker`, `_verify_worker`, `ingest_worker`,
  `_export_instance_worker`. A fifth worker function with no dispatch site
  found is red.

## 7. Out of scope

xdist and any in-tree parallel runner (ruling 4); named areas or markers per
module; a directory split (ruling 1); making the map a CI artifact; a
coverage threshold; changing what the merge gate or the release gate runs;
#699's question of *why* wall time doubled -- shards hide it on GitHub and do
nothing for the local gate, so it stays open.

## 8. Build order

One PR each, in this order, each through the local gate:

1. **Isolation** (§4). First, because it is independently useful, because
   its fallout is unknown until run, and because §6's map depends on the
   coverage `data_file` fix it carries.
2. **Shards** (§5). Independent of 3; second because the first dispatched
   run supplies the cap.
3. **Selection** (§6).

CHANGELOG: none of this is user-visible; one `### Internal` entry per PR.
CLAUDE.md: the Commands block gains `pytest --changed` and
`python -m scripts.test_map build`; the tier paragraph's "run the tests for
what you touched (see the mapping below)" becomes `pytest --changed`, and
the hand-copied module table beneath it -- already documented as incomplete
-- is deleted in PR 3 in favour of the pointer to `TARGETS`.

## 9. Open points the implementation must close with a measurement

- Which process resolves coverage's `data_file`, and when (§4).
- Contexts overhead on 3.14t over the **whole** suite, not five tests. If it
  is far above the 1.45x measured, the map is built by a separate occasional
  run rather than alongside the 3.14t gate run, and §6.1 says so in an
  amendment.
- How much of the suite rule 2 selects for an `ingest_worker` edit. If it is
  above roughly a third of the files, do the per-worker refinement in PR 3
  rather than later.
- Whether export's `multiprocessing.Pool` takes threads on 3.14t. If it does
  not, its worker lines are the 11.6x case in miniature inside the map run.

## 10. Amendments

*(empty)*

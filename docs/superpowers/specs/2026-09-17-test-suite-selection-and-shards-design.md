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
PRs, in the order of §8. §10 is the Amendments log.
**Corrected the same day, before any code (§10 items 4-6):** §3.2's 11.6x
is the cost of coverage over spawned workers, not of contexts; and
`dynamic_context = test_function` is replaced by a conftest hook, because
it files everything a fixture runs under no test. §3.2 and §6.1 are marked
in place.
**Superseded in part:** the owner's merge-rule ruling of 2026-09-17
(`RELEASING.md`, "Changes land on `main`") -- the full suite is the
integration test at release, not a merge gate. §1's tier paragraph, §6.1's
build point and line keys, §6.2's diff, §6.4's advisory line and §8's "local
gate" are struck in place, as are the clauses of §4, §5, §6.3, §6.5, §7 and §9 that the
same ruling or the review of PR #719 falsified; §10 items 7-12 say what
replaces them.
**Amended at implementation of PR 1 (2026-09-21), §10 items 14-15:** §4's
opt-out has no users yet, its "0 files open a repo path relatively" was
one short, and work done between tests is a class it did not name.

## 1. The problem, and why it is scheduled ahead of 1.0

The suite is 323 files and about 4,800 tests in a flat `tests/`, with no
markers and no sharding. It takes ~20 minutes per interpreter locally and
45--75 minutes on a GitHub runner. ~~Under `RELEASING.md` the full suite on
3.12 and 3.14t is the merge gate for every push.~~ **Superseded, §10 item 7:**
it was, until the day this was written.

Two different things are wanted, and they need different structures:

- **A developer wants a smaller set to run against their change.** This is
  the more important half (owner, 2026-09-17). It needs a *cover*: changed
  code -> the tests that exercise it. Overlap is fine.
- **GitHub wants the suite as parallel jobs.** This needs a *partition*:
  every test in exactly one job.

#707 breaks none of the four 1.0 promises, so by the v1.0.0 milestone's own
rule it would be v1.0.1. It is scheduled first anyway because it repays
itself only *during* L1--L14: ~~thirty gate issues each pay the two-interpreter
gate on every push, and~~ a local selection that is right shortens every
red-green cycle inside them **and, since §10 item 7, is the pre-merge check
for all thirty.** After 1.0 it is worth much less.

~~**The tier rule is unchanged: the tier decides breadth, never depth.** The
local selection is advisory. The merge gate and the release gate remain the
whole suite. A wrong selection costs a late discovery at the gate, never a
shipped regression.~~ **Superseded, §10 item 7:** the selection is the
pre-merge check; a wrong selection reaches `main` and the release
integration run is what finds it.

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
- ~~The 11.6x on 3.12 is confined to contexts and to this pool-heavy pair~~
  **Wrong -- see §10 item 4.** The "coverage, no contexts" cell above was
  measured after a cleanup glob had deleted the scratch tree's
  `.coveragerc`, so it ran with no multiprocessing measurement at all.
  Re-measured with the file intact: 68.3 s plain, 70.9 s with contexts. It
  was not measured over the whole suite. ~~The likely cause (a process that
  never enters a test context re-evaluates the context question on every new
  frame) was **not verified**.~~ It was not the cause.

Consequence: **the map is generated on 3.14t**, ~~where contexts cost ~1.45x
on the pair measured~~ where coverage over workers costs ~1.4x against
~11.5x on 3.12 (§10 item 4), and the design must handle `''` structurally
(§6.2).

## 4. PR 1 -- isolation: every test runs in its own directory

**What.** One autouse fixture in `tests/conftest.py`, beside
`redirect_logging`, doing ~~`monkeypatch.chdir(tmp_path)`~~ `os.chdir(tmp_path)`
and restoring it (§10 item 14). Spawned workers
inherit the cwd, so `run_parallel()` pools follow.

**Why it is low-risk (measured over `tests/test_*.py`).** ~~0 files open a repo
path relatively~~ 1 file did (§10 item 14), 31 anchor on `__file__`, 1 reads the cwd, 58 never touch
`tmp_path`/`tempfile`. Almost nothing depends on *being* in the root; tests
only happen to write there.

**Opt-out.** A registered marker, `@pytest.mark.repo_root`, skips the chdir.
Candidates are found by running the suite once under the fixture, not by
guessing. ~~The first is known: `test_packaging_contract.py`'s sdist staging,
which builds `isocenter-<version>/` in the root and spawns build
subprocesses;~~ (**§10 item 14:** it passes `cwd=REPO` and needs no mark; its
build directories are allowed by name.) Anything that launches `python -m scripts.…` or `python -m
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
(still true, now for one marked test). ~~`RELEASING.md`: the 3.14t gate's~~
~~`git archive` copy **stays**, but its stated reason narrows to SHA purity --~~
~~the log proves which tree was tested -- since collision is no longer one.~~

**Superseded, §10 item 7:** `RELEASING.md` no longer runs a suite in a `git
archive` copy at all; step 3 runs the two interpreters in the checkout, and
this PR removes that step's shared-root-files reason.

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

~~**Locally** `--shard` is available and unadvertised. The local gate stays a~~
~~single serial run per interpreter.~~

**Superseded, §10 item 7:** `--shard` is available and unadvertised locally;
there is no local full-suite gate for it to leave serial.

## 6. PR 3 -- selection: `pytest --changed`

### 6.1 The map

~~Generated by `python -m scripts.test_map build`, **on 3.14t** (§3.2), in a~~
~~clean tree at a known SHA -- in practice the gate's `git archive` copy, which~~
~~is already both. It runs the suite under coverage with~~

**Superseded, §10 items 8-10:** still generated by that command on 3.14t in a
clean tree at a known SHA, but no gate run builds it (item 9), and its keys
are function names, not lines (item 8). It runs the suite under coverage with
~~`dynamic_context = test_function`~~ per-test contexts switched by a conftest
hook (§10 item 5), combines, and writes
`.test-map.json` to the main checkout (gitignored):

```
{"sha": "<commit>", "python": "3.14.7t",
 "lines":   {"isocenter/session.py": {"1612": ["tests/test_compaction.py::…", …]}},
 "workers": {"isocenter/io_handlers.py": [4023, 4024, …]}}
```

~~`lines` holds lines with at least one named context. `workers` holds lines~~
~~recorded **only** under `''` -- executed, by no test frame.~~

~~A fresh clone has no map. Selection then degrades to §6.3 rule 4 for~~
~~everything, and says so.~~

**The block above and the two paragraphs under it are superseded, §10 items 8
and 10** (a fenced block cannot be struck). The shape is
`{"sha", "python", "functions": {path: {qualname: [nodeid]}}, "workers":
{path: [qualname]}, "unmapped": [nodeid]}`; `workers` holds functions a
spawned process ran, whether or not a test also ran them. With no usable map
every changed function has no record, which is rule **3** -- its module's
`TARGETS` row -- not rule 4 as written above.

### 6.2 From a change to a selection

~~`git diff -U0 <map sha>` (plus staged, unstaged and untracked) gives hunks in~~
~~**old-side numbering, which is the map's numbering** -- diffing against the~~
~~map's own SHA is what makes a stale working tree harmless. Each hunk is~~
~~widened to its enclosing `def`/`class` body using the AST of~~
~~`git show <sha>:<file>`; a pure insertion takes the enclosing body of its~~
~~insertion point. Function granularity, because §3.1 showed it is the right~~
~~size and because a changed line's neighbours are what the change can break.~~

**Superseded, §10 items 8 and 10:** the diff is the working tree against the
merge-base with the branch the work merges into; **both** sides are read, the
old against `git show <base>:<file>` and the new against the working tree,
and each changed line resolves to the name of its innermost function.
Function granularity stands, for the reason given.

### 6.3 The rules, ~~first match wins per changed region~~ per changed function

**Amended, §10 item 10:** "region" below reads "function"; rules 1 and 2 are
no longer exclusive; rule 7 widens; and two additions follow rule 7.

1. **Region has named contexts** -> those tests. (`compact()` -> 6 tests.)
2. ~~**Region is `workers`-only** -> every test with a named context on a~~
   ~~**dispatch site**: a line in the parent that hands a worker function to~~
   ~~`run_parallel()` or the export pool. Dispatch sites are found by AST (a~~
   ~~call whose first argument names one of the module-scope worker~~
   ~~functions), not listed by hand.~~
   **Superseded, §10 item 10:** a function a spawned worker ran -> also every
   test recorded for a **dispatching function**: one containing a call that
   hands a `*_worker` to a pool, found by AST. In addition to rule 1, not
   instead of it. Coarse -- every test that ingests through
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
~~7. **Any other changed path** (docs, workflows, `scripts/`, `CLAUDE.md` is~~
   ~~untracked and never appears) -> the test files whose text names the~~
   ~~path's basename. This is how `docs/environment.md` reaches~~
   ~~`test_documented_env_vars.py` and a workflow reaches~~
   ~~`test_packaging_contract.py`.~~

   **Superseded, §10 items 10 and 13:** as written, a path no test names
   selected nothing. Now: test files naming the path's basename (and, for a
   `.py` file, its stem); if none, nothing for `docs/` and `*.md`, else the
   full suite; any non-Python file under `isocenter/` (package data every
   session loads), `pyproject.toml` and `MANIFEST.in` -> the full suite.

`TARGETS` stays the one maintained map. The coverage map only narrows inside
it and is generated, never edited -- there is no second hand-kept list for
#707's warning to apply to. `scripts/test_map.py` imports `TARGETS` and
`NOT_PROBED` from `scripts/mutation_probe.py`; nothing parses CLAUDE.md.

### 6.4 Interface

`pytest --changed` (conftest option; deselects in
`pytest_collection_modifyitems`, composes with `-k`, paths and `--shard`).
Before the run it prints the selection and the rule that produced each part,
~~the map's SHA and age in commits, and one fixed line: *advisory -- the merge~~
~~gate is the full suite on 3.12 and 3.14t.* `python -m scripts.test_map~~
~~select` prints the same without running.~~

**Superseded, §10 item 7:** the fixed line says the selection is the pre-merge
check of `RELEASING.md` step 3, to be run on both interpreters. `python -m
scripts.test_map select` prints the same without running, and `--changed-base`
names the target branch when it is not `main` (§10 item 11).

### 6.5 Testing the selector

- Unit tests over synthetic maps and diffs for each rule in §6.3, including
  the fall-through order.
- **The three §3.1 probes become permanent tests**, run against a small real
  map built in `tmp_path` from a handful of test files: a `compact()` edit
  selects the compaction tests; a `scan_worker` edit and an `ingest_worker`
  edit each select `test_multiprocessing.py`. These are the two cases
  testmon got wrong and are the reason this was built rather than adopted.
  They are `repo_root`-marked and slow; they are the point.
~~- The dispatch-site finder is tested against the live source: it must find a~~
  ~~site for each of `scan_worker`, `_verify_worker`, `ingest_worker`,~~
  ~~`_export_instance_worker`. A fifth worker function with no dispatch site~~
  ~~found is red.~~

  **Superseded, §10 items 1 and 10:** five workers, and the finder yields
  dispatching *functions*; a worker handed to a pool at module scope is red.

## 7. Out of scope

xdist and any in-tree parallel runner (ruling 4); named areas or markers per
module; a directory split (ruling 1); making the map a CI artifact; a
coverage threshold; ~~changing what the merge gate or the release gate runs~~
(**superseded, §10 item 7:** the owner changed the merge gate the same day);
#699's question of *why* wall time doubled -- shards hide it on GitHub and do
nothing for ~~the local gate~~ the release integration run, so it stays open.

## 8. Build order

One PR each, in this order, ~~each through the local gate~~ each by
`RELEASING.md`'s "Changes land on `main`" (**superseded, §10 item 7**):

1. **Isolation** (§4). First, because it is independently useful, because
   its fallout is unknown until run, and because §6's map depends on the
   coverage `data_file` fix it carries.
2. **Shards** (§5). Independent of 3; second because the first dispatched
   run supplies the cap.
3. **Selection** (§6).

CHANGELOG: none of this is user-visible; one ~~`### Internal`~~ `### Changed`
entry per PR (the file has no `Internal` heading; #704's procedure entry is
the precedent).
CLAUDE.md: the Commands block gains `pytest --changed` and
`python -m scripts.test_map build`; the tier paragraph's "run the tests for
what you touched (see the mapping below)" becomes `pytest --changed`, and
the hand-copied module table beneath it -- already documented as incomplete
-- is deleted in PR 3 in favour of the pointer to `TARGETS`.

## 9. Open points the implementation must close with a measurement

- Which process resolves coverage's `data_file`, and when (§4).
- Contexts overhead on 3.14t over the **whole** suite, not five tests. If it
  ~~is far above the 1.45x measured, the map is built by a separate occasional~~
  ~~run rather than alongside the 3.14t gate run, and §6.1 says so in an~~
  ~~amendment.~~

  **Superseded, §10 item 9:** there is no gate run to build alongside; the
  measurement decides whether "Cutting a release" step 1's 3.14t run doubles
  as the build.
- How much of the suite rule 2 selects for an `ingest_worker` edit. If it is
  above roughly a third of the files, do the per-worker refinement in PR 3
  rather than later.
- Whether export's `multiprocessing.Pool` takes threads on 3.14t. If it does
  not, its worker lines are the 11.6x case in miniature inside the map run.

## 10. Amendments

Found while writing the implementation plan, 2026-09-17, before any code:

1. **§6.5 names four worker functions; the source has five.**
   `_discover_worker` (`session.py`) is dispatched the same way. The plan's
   dispatch-finder test asserts all five.
2. **§5's `python -m scripts.shard_timings` is not built.** Timings are
   recorded by a conftest option, `--record-shard-timings=PATH`, from
   pytest's own per-phase durations: parsing `--durations` text is fragile,
   and the hook already has the numbers. Same file, one fewer script.
3. **§6.1's `build` needs the SHA passed in** (`--sha`) when it runs in a
   `git archive` copy, which is not a git repository.

Found in the plan's review and measured the same day:

4. **§3.2's cost attribution was wrong.** Same two files, `.coveragerc`
   intact, 49 data files on 3.12 and 15 on 3.14t:

   | Interpreter | Bare | `.coveragerc` | + `dynamic_context` | + hook contexts |
   | --- | --- | --- | --- | --- |
   | 3.12 | 5.96 s | 68.33 s | 70.88 s | 70.98 s |
   | 3.14.7t | 0.64 s | 0.89 s | 1.02 s | 0.90 s |

   The 11.5x on 3.12 is coverage tracing ~49 spawned interpreters; contexts
   are free on both. "Build the map on 3.14t" stands, for this reason and
   not the one §3.2 gives.
5. **`dynamic_context = test_function` is not used.** Its context begins at
   the test function's frame and ends when it returns, so every line a
   fixture executes is recorded under `''` and would be mis-filed as
   worker-only -- selecting the pool tests instead of the tests that use the
   fixture. A `pytest_runtest_protocol` hookwrapper calls
   `Coverage.current().switch_context(item.nodeid)` around setup, call and
   teardown (what `pytest-cov --cov-context=test` does; no dependency).
   Measured: a fixture-run line in `builders.py` lands under its test's
   nodeid. Contexts are nodeids; the parametrize id is stripped. Known
   limit, accepted: a module- or session-scoped fixture is attributed to
   the first test that triggers it.
6. **On 3.14t that test's ingest still spawned processes** (15 data files;
   `ingest_worker` under `''`), and worker *threads* may be unmeasured
   because `concurrency = multiprocessing` replaces coverage's default
   `thread` rather than adding to it: with no rcfile, `scan_worker` was
   recorded under the test's nodeid. The plan's Task 15 measures a build
   with `multiprocessing,thread`. The two-tier design is unchanged; how
   much lands in `workers` is what moves.

Owner's merge-rule ruling, 2026-09-17, after the spec was approved
(`RELEASING.md`, "Changes land on `main`"; CHANGELOG `[Unreleased]`):

7. **`--changed` is the pre-merge check, not advice.** A change merges on
   its own tests -- the new ones and those covering what it touched, on
   3.12 and 3.14t -- and an adversarial review of the change as rebased on
   current `main`. The full suite is the integration test, run when a
   release is cut. Code on `main` has not met the whole suite, and the
   owner accepts regressions surfacing at integration. So §1's "a wrong
   selection costs a late discovery at the gate, never a shipped
   regression" is false: it reaches `main`. The fallbacks were already
   fail-safe (no record -> `TARGETS` row -> full suite) and stay; §6.4's
   fixed line changes from *advisory -- the merge gate is the full suite*
   to one saying the selection is `RELEASING.md` step 3 and is run on both
   interpreters. §8's "each through the local gate" means that procedure.
8. **The map is keyed by function name and the diff is the developer's
   own.** §6.1-§6.2 keyed the map by line and diffed the working tree
   against the map's SHA, which is only right if the map is rebuilt almost
   every push -- it rode on the 3.14t gate run, which no longer exists. A
   map weeks old would select everyone's merged work. Instead: `functions`
   is `{path: {qualname: [nodeid]}}` and `workers` is `{path: [qualname]}`;
   the diff is working tree against the merge-base with `origin/main`, read
   in new-side numbering against the working-tree source and resolved to
   qualnames. An old map stays right for every function that still exists
   under its name; a new or renamed one has no record and falls to its
   `TARGETS` row. ~~A map ages by getting less sharp, never by selecting
   wrongly.~~ **False -- item 10.** One trap found while prototyping: a `def` line executes at
   import under no test, so counting it files every never-called function
   as worker-only; body lines only.
9. **The map's build points are prescribed, since no gate run builds it:**
   "Cutting a release" step 1 (its 3.14t integration run, if the measured
   overhead allows), and on demand. §7's "making the map a CI artifact"
   stays out of scope. No step hangs off a bunch or a wave: the owner's
   ruling is that those are a logical grouping, not a process boundary.

Adversarial review of PR #719 at `3115aca9`, 2026-09-17 (24 of the plan's own
tests run by the reviewer, then adversarial git repositories and a real
coverage run):

10. **Item 8's claim that an old map only gets less sharp was false, and
    under item 7 every miss reaches `main`.** Counterexamples found, and what
    the design now does about each -- all prototyped and run (30 tests,
    scratch git repositories included) before the plan was revised:
    - *A test added since the build, a test that skipped in the build* (the
      map is built on 3.14t; 69 test files mention the GIL or
      `FORCE_PROCESSES`), *and a function whose body changed on `main` since
      the build, so its tests now reach code the map never saw.* The map
      records `unmapped` tests at build time; at selection,
      `cannot_speak_for()` adds test files changed since the map's SHA and
      the tests recorded against functions changed between the map's SHA and
      the merge-base. `select()` adds any of those that sit in a touched
      module's `TARGETS` row. An old map therefore selects **more**, toward
      the row -- never less than the row; **still less than a fresh map** for a
      call path added across modules (item 13). A map whose SHA is not in the clone is treated as no map.
    - *A test renamed or deleted since the build* left a nodeid that matched
      nothing, so everything was deselected: zero tests, no fallback.
      Selected nodeids are now checked against the collection; any that are
      gone send the touched modules to their rows.
    - *Deleting a function* was attributed to the function before it
      (measured), so the deleted function's tests -- the ones that now fail
      -- were never selected. Both sides of the diff are read; the old side
      names it.
    - *A helper one unit test calls and many pool tests reach inside a
      worker* selected only the unit test. `workers` now lists functions a
      spawned process ran whether or not a test also did, and rules 1 and 2
      union.
    - *Worker threads were not traced at all*: `concurrency =
      multiprocessing` replaces coverage's default `thread`. Measured with
      `multiprocessing,thread` on 3.14t: `scan_worker`'s body lands under
      its test's nodeid (1.00 s against 0.89 s). `build()` uses a scratch rc
      with both; `.coveragerc` is unchanged.
    - *Default-argument lines and functions called at import* were filed as
      worker-run. Only lines from the first body statement on count, and
      everything outside a test is labelled `<startup>`, so the empty
      context means a spawned process and nothing else.
    - *Rule 7 selected nothing* for a path no test names. It now selects the
      suite; so does package data under `isocenter/`. (Narrowed by item 13.)
    - *git config could blind it*: a pure rename has no hunk, and
      `diff.noprefix`/`mnemonicprefix` defeat the header match (all
      measured). The diff runs with those pinned, `--no-renames`, and `-z`.
    - Still accepted, and measured in the plan's Task 14 Step 1b rather than
      assumed: a module- or session-scoped fixture is attributed to the
      first test that triggers it (11 fixtures, 4 files). If any package
      function is reachable only that way, those four files join
      `cannot_speak_for` unconditionally.
11. **Release-branch work.** A patch branch off `release/X.Y` must diff
    against that branch, not `main`: `--changed-base` / `select --base`.
12. **Git-dependent tests skip from their bodies in a non-git tree**, since a
    `git archive` copy has no diff to read.

Second review pass of PR #719, at `461beeb6` (the reviewer re-ran the
regenerated module: 30 passed, 31 with the live source, on 3.12.14 and
3.14.7t; then new adversarial repositories):

13. **Item 10's rule 7 over-corrected, and this PR was its first casualty.**
    "If no test names it, the whole suite" selected the whole suite for the
    two dated documents this PR changes -- on both interpreters, by the
    plan's own `selection_for()` -- because no test names a dated spec. Every
    spec, plan or new docs page would cost the suite twice, which is the
    per-PR full run item 7's ruling ended. Now: documentation no test names
    (`docs/`, any `*.md`) selects nothing; any other unnamed path still
    selects the suite. The needle is the basename, plus the stem **for `.py`
    only** (the stem of `docs/session.md` is a word half the suite contains;
    measured, `CHANGELOG.md` matched 4 files by basename and 25 with its
    stem), and `RELEASING.md` step 3 states the same needle, so the procedure
    does not change meaning when #707 lands.
    **The bound item 10 overstated, recorded:** what the map cannot speak
    for is intersected with the touched modules' rows, so a call path added
    *across modules* since the build is still missed (measured: `b.k()`
    changed on `main` to call `a.g()`; the developer edits `g`; `test_k` is
    dropped because only `a.py` is touched). That is rule 3's own bound -- a
    row is the files that import or name the module, not a closure over
    callers -- and the release integration run is what finds it. The sound
    alternative, unintersected, grows toward the whole suite with map age;
    it was not taken. Second residual: a test that ran in the 3.14t build
    but reaches a function only on 3.12's process path.
    **Also from this pass:** rule 2 falls to the row when *any* dispatcher
    lacks a record, not only when none has one (an export-worker edit was
    selecting ingest and audit tests and no export test); a module that does
    not parse falls to its row instead of raising; `---`/`+++` are read as
    headers only between `diff --git` and the first hunk (a deleted line
    reading `-- a/x` switched files, measured); the context labelling is
    gated on `TEST_MAP_CONTEXTS=1`, which only `build()` sets, so the
    documented coverage command writes the data file it always wrote; a
    one-line `def` cannot tell its signature from its body and is counted,
    which over-selects (none exist in `isocenter/`). 36 tests.

Implementing PR 1 (isolation), 2026-09-21. The plan's **Deviations at
implementation** list is the full record; what it changes here:

14. **§4 as built.** (a) No test needed `repo_root`:
    `tests/test_packaging_contract.py` already gives every subprocess
    `cwd=REPO`. It still builds in the root, so the guard allows
    setuptools' `build/` and `isocenter.egg-info/` by exact name
    (`root_guard.ALLOWED_NAMES`); moving `egg_info` to scratch was
    measured and refused, as it drops `isocenter.egg-info/` from the
    sdist. (b) §4's "0 files open a repo path relatively" was one short:
    `tests/test_ctp_integration.py`. (c) A class §4 did not name: work
    done **between** tests. A `setup_module` (`tests/test_naming_structure.py`)
    and a module-scoped fixture (`tests/test_private_tag_vr_roundtrip.py`,
    which logged to `./isocenter.log` once `redirect_logging` had deleted
    `ISOCENTER_LOG_FILE`) run in the root, not in a `tmp_path`. The first is
    fixed at the test; the second for every wide-scoped fixture, by a
    session default for `ISOCENTER_LOG_FILE` that `redirect_logging` now
    restores. The cwd itself is not moved between tests, so a wide-scoped
    fixture that writes a relative path still lands in the root, and the
    guard is what names it. (d) The chdir fixture calls `os.chdir`, not
    `monkeypatch.chdir`: an autouse `monkeypatch` reorders teardown ahead of
    `_pixel_analysis_ocr_is_not_left_replaced` (measured). (e) §4's
    coverage trap was real: under the chdir and without the fix, 1 data
    file and no `ingest_worker` line; with an absolute `COVERAGE_FILE` set
    at `conftest.py` import, 49 files and 38 lines. The spawned child, not
    the parent, resolves the relative path. (f) `RELEASING.md` step 3's
    reason for running the two interpreters one after the other is
    narrowed, not removed: runs that both include the packaging test
    still share its build directories.
15. **§4's guard, after the review of #720.**
    - It names a pre-existing root *file* that was rewritten as well as a new entry, because in a pre-#707 checkout the stale `isocenter.log` and `test_*.db` names are where a relative write would go.
    - It repeats its line from `pytest_unconfigure` so the line is the run's last. `RELEASING.md` step 3 records each run's exit status as well.
    - It still watches only the root's top level (plan deviation 12).
    - `conftest.py` also puts the tree under test first on `PYTHONPATH`, for child interpreters started from a `tmp_path`.

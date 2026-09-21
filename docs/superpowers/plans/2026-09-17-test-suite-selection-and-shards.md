# Test Selection by Change, and Balanced CI Shards: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Date:** 2026-09-17
**Issue:** #707
**Status:** plan only. **No development starts until the owner says go** (owner, 2026-09-17).
**Revised the same day** for the owner's merge-rule ruling (`RELEASING.md`, "Changes land on `main`"): a change merges on its own tests, run on 3.12 and 3.14t, and an adversarial review; the full suite is the integration test at release. Part 3 is rewritten around a function-keyed map, and every "local gate" below means that procedure. Spec §10 items 7-9.
**Scope:** internal. Excluded from the docs site by `exclude_docs`. A dated record: deviations found at implementation are listed under a `**Deviations at implementation**` heading at the top, as `2026-09-06-bunch-e-hang-probe-and-export-sweep.md` does, and the spec's §10 Amendments log gets the same entries.

**Deviations at implementation** (measured, and recorded here so the record says what was actually built). Part 1, PR 1, 2026-09-21; spec §10 items 14-15 carry the same list:

1. **Order.** Tasks 1, 3 and 4 were built before Task 2's triage, so the triage ran with the root guard on and named strays as well as failures. Task 5 Step 5's "Task 2's two full runs already cover this PR" does not hold once Tasks 3-5 change `conftest.py` after them; `RELEASING.md` step 3's two runs were made at the pushed SHA instead. Both were chunked (the whole suite, as step 3's rules select for a `tests/conftest.py` change) because one tool call is capped at ten minutes; each chunk's command, SHA and last line are in the PR body.
2. **Task 1's fixture does not use `monkeypatch`.** An autouse fixture that requests `monkeypatch` instantiates it ahead of every autouse fixture defined after it, so its undo ran after `_pixel_analysis_ocr_is_not_left_replaced`'s teardown, which then saw a test's own `monkeypatch.setattr` still in place (measured: two teardown errors in `tests/test_scan_reports_what_it_could_not_read.py`). The fixture calls `os.chdir` and restores in a `finally`.
3. **Task 2 found no subprocess test to mark.** `tests/test_packaging_contract.py` already passes `cwd=REPO` to every subprocess, so it passes under the chdir unmarked and **no test in the suite carries `repo_root`** beyond the contract's own. It still builds in the root: its `build/` and `isocenter.egg-info/` are allowed by exact name in a new `root_guard.ALLOWED_NAMES`, not by prefix (a prefix `build` would pass a test's own `build.db`). Pointing `egg_info --egg-base` at scratch was measured and refused: the sdist loses `isocenter.egg-info/` (397 entries, not 403). Task 5 Step 2's sentence ("that test is `repo_root`-marked") is reworded to what is true.
4. **Two failures and one stray, none of them a relative subprocess:** `tests/test_ctp_integration.py::test_ctp_rules_file_exists` read `isocenter/resources/ctp_rules.json` relatively (rule 2; spec §4's "0 files open a repo path relatively" was one short); `tests/test_naming_structure.py` made its input directory in `setup_module`, which runs in the root, outside any test (rule 3); and `isocenter.log` appeared in the root from `tests/test_private_tag_vr_roundtrip.py`'s module-scoped `reloaded` fixture, set up between tests, where `redirect_logging` had deleted `ISOCENTER_LOG_FILE`. The last is fixed for every wide-scoped fixture, not the one: a session-scoped autouse fixture points `ISOCENTER_LOG_FILE` at scratch, `redirect_logging` restores the previous value instead of deleting it, and `test_a_module_scoped_fixture_logs_outside_the_root` pins it.
5. **Task 3's hand probe is also a permanent test**, `test_a_run_that_writes_into_its_root_fails_naming_the_entry`: this `conftest.py` run by `pytester` in a subprocess, asserting the exit status and the named entry, so the wiring and not just the pure functions is pinned.
6. **`tests/test_hang_probe_hooks.py` copies `tests/support/`** into its pytester directory beside the verbatim `conftest.py`, which now imports `support.root_guard`.
7. **Task 4.** Measured with the chdir and without the fix: 1 data file, 0 `ingest_worker` body lines; with the fix: 49 files, 38 lines (the plan's `main` figure is 37 over the same window; not re-measured on `main`). The test skips from its body without `coverage`, which is in `dev` and which the release matrix (`.[tests,ocr]`) does not install; `tests/test_skip_contract.py` counts `dev` among the extras a documented environment may lack. The test's inner run passes `-p no:cacheprovider`.
8. **Task 5 Step 3 narrowed rather than deleted.** The two step-3 runs may overlap, except that two runs both including `tests/test_packaging_contract.py` build in the same root directories and go one after the other.
9. **Subprocesses launched from a test's own directory.** Five tests start `python -c`/`-m` children that import `isocenter` without `cwd=` (`test_an_unusable_key_file_is_not_cached.py`, `test_codecs_strict.py`, `test_ctp_integration.py`, `test_no_global_warning_filter.py`, `test_sidecar_gate_crosses_processes.py`). Before #707 they found the tree under test through the root on the child's `sys.path`. From a `tmp_path` they fall through to the editable install, which is the main checkout. First recorded and left as it was; changed after the review of #720: `conftest.py` puts the tree under test first on `PYTHONPATH`, anchored on its own file like `COVERAGE_FILE`, and `test_a_python_subprocess_imports_the_tree_under_test` pins it. That test is red when run without `PYTHONPATH` and the prepend removed.
10. **CLAUDE.md is untracked**, so spec §4's "Docs that go stale and are rewritten in this PR" cannot be done by this PR for CLAUDE.md's "Tests write `*.db` … leave them alone" paragraph. The owner replaces it locally with:

    > Every test runs in its own `tmp_path` (an autouse `os.chdir` in `tests/conftest.py`, #707). Spawned workers inherit it, and conftest puts the tree under test first on `PYTHONPATH` so a `python -c` child imports that tree. `@pytest.mark.repo_root` opts a test out; nothing uses it yet. A run that adds an entry to the repo root, or rewrites a file already there, fails and ends with a line naming each (`tests/support/root_guard.py`). The only entries allowed by name are setuptools' `build/` and `isocenter.egg-info/`, from `tests/test_packaging_contract.py`'s build. Do not add cleanup, and do not widen `ALLOWED_PREFIXES` or `ALLOWED_NAMES` for a test's own output. The `*.db`, `*_pixels.bin`, `*.lock`, `isocenter.log` and CSV files in a pre-#707 root are output nothing writes any more; delete them. The guard watches the root's top level only: a write into `tests/` or any other subdirectory is not caught, and neither is a deleted file.
11. **Review of #720 at `13509110`.** Four changes:
    - The guard's line is repeated from `pytest_unconfigure`, so it is the run's last line, and `RELEASING.md` step 3 now records the exit status beside the last line. Before, pytest's summary printed after `pytest_sessionfinish`, so a stray run's last line read green.
    - The guard reports a pre-existing root **file** whose `(mtime_ns, size)` changed as well as a new entry, so a pre-#707 root's stale `isocenter.log` or `test_*.db` no longer hides a write into it. Directories are listed for newness only, because a directory's mtime moves whenever anything is created inside it (`tests/__pycache__`, `.git/index.lock`).
    - The module-fixture log test runs as its own pytester session. In-process, it passed against the reverted fix when run alone.
    - The skip contract subtracts required modules from the optional set and drops the package's self-reference. Only `coverage` and `pylint` become skippable. ~~The reviewer's premise that `dev` admits `pytest` was measured false: `isocenter[tests]` reads as `isocenter` alone, and the `tests` modules are not expanded into it.~~ **Corrected in PR 2 (the #720 reviewer's note 3):** the review never claimed `dev` admits `pytest`. Its finding 6 was that `isocenter` (read from `isocenter[tests]`) and `pylint` had become skippable, plus any future module in both groups. The `isocenter` half was real and is what the self-reference discard fixed; that discard's mutant is killed on the real extras. The subtraction guards the next overlap and is pinned on a synthetic one, because on the real extras its mutant survives.
12. **Known limits, recorded, not changed:** (a) the guard sees only the root's top level, so a write anchored on `__file__` into `tests/` is not seen, and diffing `git ls-files --others --ignored --exclude-standard` across the run would cover the tree. (b) The chdir fixture's `finally` changes back to where it started, so it silently repairs a test that changes directory and never changes back. None does today.
13. **Filed separately, not fixed here:** `SqliteStore` keeps a relative sidecar path and reopens it against the current cwd on every write. The review of #720 found it, and it is a library defect in its own right. (Filed as #722; number added in PR 2.)

Part 2, PR 2, 2026-09-21 (spec §10 item 16 carries the same list):

14. **Branch `test/707-shards`**, not `ci/707-shards`, as the implementation brief named it.
15. **`--shard` refuses a collected file the partition does not list.** `shards.suite_files` is a glob, and pytest's collection is a separate answer with its own configuration. A file one gives and the other does not would be deselected by all N shards and run by none, behind green. `pytest_collection_modifyitems` raises `pytest.UsageError` naming it, and exits 4. A malformed `--shard` value is a `UsageError` too, not a traceback. Pinned by `test_a_collected_file_outside_the_partition_is_refused`.
16. **Three of Task 6's fixtures passed with their rule removed.** Measured against a scratch re-implementation. `test_the_longest_files_are_spread_not_stacked` balanced under plain name order (100, 100, 1, 1 is balanced either way). `test_a_file_with_no_timing_gets_the_median_not_zero` gave 2 and 2 with a zero default: the new file goes last, onto the lighter shard, which is where the median put it. `test_assignment_is_deterministic` used distinct weights, so no tie ever reached the tie-break. The fixtures were changed to inputs that tell the rules apart (1, 1, 2 for spread; 40, 10, 20 plus an unknown for the median; equal weights for determinism). One mutant still survives, and it is equivalent: dropping only the name tie-break from the sort key. `assign` sorts its input first and Python's sort is stable, so the order is still deterministic. Dropping both is killed.
17. **The workflow pin also reads the summary line.** It asserts the line names `shard ${{ matrix.shard }}/N`, because a red shard that is not named cannot be told apart from the other three.
18. **The timings came from five interleaved chunks**, merged, because one tool call is capped at ten minutes. Each chunk was a 3.12 run with `--record-shard-timings`. Per-file sums are unaffected, except that a session-scoped fixture's setup is charged once per chunk rather than once per run. Total 1218 s. The four local loads are 304 s each. **On a runner they are not balanced:** dispatched run 35636320365 took 435-751 s per shard on 3.12 and 483-715 s on 3.14t. Part of the reason is known. The heaviest local file, `test_coverage_keeps_worker_data_under_chdir.py` (86.6 s), skips on CI, because `.[tests,ocr]` has no `coverage`. Timings recorded on a runner would balance better. That costs wall time only and was not done here.
19. **The subprocess tests pass `-p no:cacheprovider`**, and the orphan test names `PYTHONPATH=REPO` for its child, as `test_coverage_keeps_worker_data_under_chdir.py` does.
20. **Task 9's caps:** peak 751 s, so `cap = max(20, ceil(751 / 0.6 / 60 / 5) * 5) = 25`. The job cap is 3+3+3+3+25+1+7 = 45. `pytest.ini`'s faulthandler comment still said "30-minute cap", stale since v0.9.8, and now names the shard cap.
21. **Carry-overs done here** (#707's checklist and the PR 1 comment, where this PR touches the file): `tests.yml`'s trigger comments no longer argue for a required check; "§10 items 7-12" reads 7-13 (struck in place in the spec front matter and this plan's Part 3 preamble, edited in the unreleased CHANGELOG entry); deviation 11 corrected in place; deviation 13 cites #722; `.DS_Store` is allowed in the root by exact name (`test_finders_ds_store_is_not_a_stray`). The rest of the checklist (`--changed-base=`, the "selects more" qualifier, Task 14's per-dispatcher row-fallback count, the one-line-`def` note) belongs to Part 3's files.
22. **Review of #727 at `1245c225`.**
    - **Blocking.** Task 7's `test_a_shard_run_collects_only_its_own_files` passed two mutants. One assigned over the collected items instead of the whole suite. The other ran shard I+1's files as shard I. Disjoint-and-covering holds for any one-to-one mapping of files to shards. Renamed `test_a_shard_run_collects_what_the_full_assignment_gives_it`, it now compares each run with `set(given) & set(assign(suite_files, timings, 2)[I - 1])`, and both mutants are killed.
    - The orphan refusal names the rootdir, so a `-c` or `--rootdir` run reads as what it is.
    - A relative `--record-shard-timings` path is refused. Resolved instead, it still wrote into the root when pytest started there, and the guard then blamed a test for the recorder's file.
    - The workflow pin refuses a matrix `include`/`exclude`. An `exclude` of one version's shard 4 passed it.
    - The partition test reads N from `tests.yml` and adds two others (N+1, N+3), as spec §5 says, where the plan hardcoded 2, 4 and 7.
    - One `tests.yml` comment corrected. On a dispatch the `inputs` context exists; it declares no `python-versions`.
    - **Recorded, not changed:** the caps' largest figure in this PR is local, not the runner's. 3.14t shard 4/4 took 893.79 s locally, 59.6% of 25 minutes, against the 751 s runner peak the floor's comment cites. The reviewer puts within-configuration variance at about 1.25x (the 2026-09-14 runs), which takes the runner peak to about 63%. Timings recorded on a runner would bring the peak toward the mean, about 655 s. That is a follow-up if the peak grows.
    - **Rebased onto `2b2c70d0`** (#726, the output fingerprint), which added `tests/test_output_fingerprint.py` and `tests/test_output_fingerprint_release_step.py`. Their times were measured on 3.12 (0.35 s and 26.1 s) and added to `tests/shard_timings.json`, not left at the median. The median is 0.9 s, and the release-step file weighs 29 times that.

Part 3, PR 3, 2026-09-21 (spec §10 item 17 carries the same list):

23. **Stacking.** The branch was cut from `test/707-shards` and rebased onto `main` at `b763a7c8` once #727 was squash-merged. The trees are identical, so the map built at the pre-rebase `1b4f2a77` carries `417b761d`, the same tree's rebased commit.
24. **Task 11's three passes were run red and then green in order, and committed as one commit**, so that no commit on the branch carries a red test. The plan's test count held: 37.
25. **The #707 checklist, done here.**
    - `--changed-base=`: the plan's conftest hook decided "whole collection" by sniffing argv for a word without a leading `-`. That read `--changed-base main` *and* `-p no:cacheprovider` as paths, which skipped the vanished-test fallback. The run then selected nothing and exited 5. The hook now asks pytest (`config.args_source is not ArgsSource.ARGS`). `test_a_vanished_test_widens_whichever_way_the_base_is_spelled` pins both spellings end to end, in a scratch repository with a map naming a test that no longer exists. The plan's version fails both parameters. `RELEASING.md` and `merge_base`'s message now spell it `--changed-base=release/X.Y`.
    - "Selects more" is qualified with the cross-module bound: in `scripts/test_map.py`'s docstring, in `RELEASING.md`, and struck in place in Task 14 Step 1.
    - The one-line-`def` note: an edit to one selects the dispatchers' tests (rule 2) instead of the row (rule 3). That is a different set, not a strictly larger one. The test is renamed `test_a_one_line_function_is_counted_as_run_by_a_worker`, and `test_the_package_has_no_one_line_function` turns "there are none in `isocenter/`" into a live check.
    - Task 14 Step 2's row-fallback count per dispatcher is item 28 below.
26. **Rule 2 asks only the edited worker's own dispatchers** (spec §6.3 rule 2's "obvious refinement", taken now; spec §10 item 17). Task 13's `ingest_worker` probe went to the full suite on its first run. Rule 2 asked all six dispatching functions, and a map built from three test files had recorded three of them. The same shape would have killed rule 2 for every worker whenever any one dispatcher never records. `dispatchers()` now returns `{worker: {(path, function)}}`. A helper that only workers run still asks every dispatcher, because there is no single worker to key on. The tests are restructured around the #719 reviewer's actual case: an export-worker edit with export's dispatcher blind goes to the row and does not select the ingest tests. There are two new tests for the converse and for the helper.
27. **Task 13 fixture times:** 89.7 s on 3.12 and 2.4 s on 3.14t, both under the plan's 5-minute limit, so there is no skip. `git archive HEAD` exports the *committed* tree, so Tasks 10-12 were committed first, as the plan says.
28. **Task 14 measurements**, from the first real map (3.14.7t, `1b4f2a77` = `417b761d`):
    - **Build time:** 1899 s wall for `build`, against about 1450 s for the same suite unsharded and without coverage, taken from the sum of the eight 3.14t step-3 shards at `9ab993d0`. That is about 1.3x, under the plan's 2x, so "Cutting a release" step 1's 3.14t run *is* the build. `build` now exits with the suite's status, pinned by `test_a_build_whose_suite_failed_exits_with_the_suites_status`. The build ran 4951 passed and 13 skipped. `coverage combine` reported 27 data files errored (workers killed mid-write by the worker-loss tests) and 3372 skipped.
    - **The map:** 733 functions in 33 modules with a recorded test, and 493 functions a spawned process ran. 5 functions only a worker ran, all worker-side helpers: `_worker_init`, `_waveform_bits`, `_ambiguous_veto_clause`, `_icon_label_arity_warning` and `_no_frame_boundary_words`. 386 tests the map cannot speak for, in 70 files.
    - **Step 1b (ii), the wide-scoped-fixture limit:** 7 files carry a module- or session-scoped fixture, and 0 functions are recorded only from them. No change.
    - **Step 2, rule 2's breadth, per worker** (files out of 330, with the module's `TARGETS` row for comparison):

      | Edit | Files selected | Its row |
      | --- | --- | --- |
      | `DicomSession.compact` (rule 1, for comparison: 22 tests) | 7 | 207 |
      | `ingest_worker` | 138 | 126 |
      | `_export_instance_worker` | 126 | 126 |
      | `scan_worker` | 93 | 207 |
      | `_verify_worker` | 8 (its one dispatcher) | 207 |
      | `_discover_worker` | 7 | 207 |
      | a helper only workers run (`parallel._worker_init`) | 195 | 13 |

      `ingest_worker` exceeds a third, and exceeds its row, even after the per-worker refinement. `_ingest_results` is its only dispatcher, and every ingest in the suite goes through it, so no per-worker keying can narrow it further. Only a call-site-level trace could. That is recorded, not built. A worker edit now selects fewer files than the full suite, but not fewer than its row. **Row fallbacks per dispatcher on this map: 0.** All six dispatching functions have records, so rule 2 never falls to a row for any of the five workers, or for the helper union.
    - **Step 3, export's pool:** `session.export()` passes `maxtasksperchild=25`, and `parallel._resolve_execution_choice` returns processes whenever `maxtasksperchild` is set, on 3.14t included. Export's workers are therefore spawned processes on every interpreter, and their lines land under the empty context. That is exactly the case rule 2 covers.
    - **How often to rebuild:** not measurable from a map built today. The number that says how often is the per-week widening from `cannot_speak_for`, and it waits for a map with age.
29. **`tests/test_shards_partition_the_suite.py` names `tests/shard_timings.json` in its docstring**, so that regenerating the timings selects that file by rule 7 and not the whole suite. The selector's own test file is added to the timings: 105 s on 3.12, measured while the map build was also running, so high.
30. **The #727 reviewer's nit:** the relative-timings refusal test runs its child from `tmp_path`, so a regression writes there and not into the root.
31. **Rebased again onto `d47f2b43` (#728).** That PR added `tests/test_config_scalar_types.py`, `tests/test_config_schema_version.py` and `tests/test_config_unknown_keys.py` without timings. They were measured on 3.12 (0.39 s, 0.51 s and 1.05 s) and added to `tests/shard_timings.json`, so they are not weighed at the 0.91 s median. The effect on the partition is small either way. It is recorded because a file weighed by the fallback is a guess, and the guess is invisible. `.test-map.json` is still the map built at `417b761d`. That commit is no longer an ancestor of HEAD, so `cannot_speak_for` ages it through #728's diff, which is what a stale map is for.

**Goal:** give a developer `pytest --changed` -- the tests that exercise the code they touched -- and run the suite on GitHub as four duration-balanced shards per Python version, with every test isolated in its own working directory.

**Architecture:** three PRs in order. (1) An autouse `chdir(tmp_path)` fixture plus a session guard that fails the run if the repo root gained a file. (2) A `--shard=I/N` option backed by a pure, deterministic assignment function over a checked-in timings file, and a second matrix axis in `tests.yml`. (3) A generated, gitignored map from functions to the tests that ran them, built from per-test coverage contexts (switched by a conftest hook, so fixtures count) on 3.14t, and a selector that falls back to `scripts/mutation_probe.py`'s `TARGETS` and then to the full suite.

**Tech Stack:** pytest 9 hooks in `tests/conftest.py`; `coverage` 7.16 (`Coverage.current().switch_context`, `CoverageData.contexts_by_lineno`); stdlib `ast`, `subprocess`, `json`, `statistics`; GitHub Actions matrix. **No new dependency.**

**Spec:** `docs/superpowers/specs/2026-09-17-test-suite-selection-and-shards-design.md`. Read it first; §3 holds the measurements this plan argues from.

## Global Constraints

- No new dependency, in any extra. No `pytest-xdist`, no `pytest-testmon`, no `pytest-split` (spec §2 ruling 4, §3.1).
- Test files stay flat in `tests/`. No directories, no per-module markers (spec §2 ruling 1).
- `scripts/mutation_probe.py`'s `TARGETS` and `NOT_PROBED` remain the only hand-maintained module-to-tests map. Import them; never copy them; never parse CLAUDE.md (spec §6.3).
- **`--changed` is the pre-merge check, not advice** (`RELEASING.md` step 3; spec §10 item 7). Nothing runs the full suite between a merge and a release, so a selection that misses a test puts a regression on `main`. Every fallback widens -- no record, then the module's `TARGETS` row, then the suite -- and the tool prints which one it took.
- `publish.yml` is not edited. Its filename and environment names are pinned by PyPI's publisher config.
- Interpreters, named explicitly (the `python3` on `PATH` is a pyenv shim without the dependencies): `/Users/kevin/Developer/Isocenter/.venv/bin/python` (3.12) and `/Users/kevin/Developer/Isocenter/.venv314t/bin/python` (3.14.7t, run with `PYTHON_GIL=0`).
- Every hand-run sets `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH=<worktree>`, then prints `isocenter.__file__` and reads it (CLAUDE.md, "Running one mutation by hand"). `isocenter` is installed editable from the main checkout; a worktree imports the wrong tree without this.
- Run `pytest -v` with no pipe when a hang is possible; `-q` buffers.
- New test files that import an `isocenter` module must be added to the matching `TARGETS` row or `tests/test_mutation_probe_targets.py` goes red. The files this plan creates import only `support.*`, `scripts.*` and the stdlib, so none needs a row; if an implementer adds an `isocenter` import, add the row in the same commit.
- Skips are body-level `pytest.skip(...)` calls, never `@pytest.mark.skipif` -- `tests/test_skip_contract.py` flags the marker spelling.
- Helper modules go in `tests/support/` and are imported as `from support.x import y` (there is no `tests/__init__.py`; `tests/` is on `sys.path`). A top-level `tests/*.py` that pytest does not collect fails `test_every_top_level_tests_module_is_one_pytest_collects`.
- Commits are conventional-commit style with a trailing `(#707)`. One PR per Part. Each PR follows `RELEASING.md`, "Changes land on `main`": rebase on current `main`; run the new tests and the tests covering what was touched on **both** 3.12 and 3.14t at the pushed SHA; an adversarial review of the change as rebased, naming that SHA; a merge pinned to it. **The full suite is not a merge requirement.** Where a task below runs the whole suite, it is because that task's own work needs it (Part 1's triage, Part 2's timings, Part 3's map), not as a gate.
- CHANGELOG: one `### Changed` entry per PR under `[Unreleased]`, as #704's procedure entry was filed. None of this is user-visible, and each entry says so.

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
| `scripts/test_map.py` (create) | 3 | `from_coverage`, `changed`, `dispatchers`, `select`, `build`, CLI. |
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
- [ ] **Step 3: `RELEASING.md`.** Step 3 of "Changes land on `main`" says to run the two interpreters one after the other in the checkout *because* they share repo-root `*.db` and `*.lock` files. After this PR they do not: delete that clause, and say the two runs may overlap.
- [ ] **Step 4: `CHANGELOG.md`.** Under the unreleased heading, `### Changed`: what changed, that no public behaviour did, and the before/after wall times from Task 2.
- [ ] **Step 5: Commit** (`docs: say where tests write now (#707)`), follow `RELEASING.md` steps 3-6, and open the PR with the Task 2 list and the Task 4 numbers in the body. Task 2's two full runs already cover this PR's blast radius -- a fixture every test uses -- so they are its step 3.

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

- [ ] **Step 6: Dispatch once more and confirm eight greens under the new caps.** Then CHANGELOG `### Changed` entry with both sets of numbers, commit (`ci: size the step and job caps to a sharded run (#707)`), `RELEASING.md` steps 3-6, PR.

---

# Part 3 -- `pytest --changed` (PR 3)

Branch: `test/707-changed`, from `main` after Parts 1 and 2 merge. Spec §6.

**Revised twice on 2026-09-17, the day it was written** (spec §10 items ~~7-12~~ 7-13): first for the owner's merge-rule ruling (`RELEASING.md`, "Changes land on `main`"), then for the adversarial review of PR #719, which found the first revision's central claim false. The full suite no longer runs before a merge, so nothing rebuilds the map on every push and `--changed` is the pre-merge check. What shapes every task below:

- **The map is keyed by function name, not line number**, and the diff is the developer's own: working tree against the merge-base with the branch the work will merge into.
- **An old map is ignorant, not merely blunt.** It does not know tests added since it was built, tests that skipped in the build (the map is built on 3.14t; some tests run only with the GIL), or what a function reaches now that its body has changed. `cannot_speak_for()` computes all three from git at selection time, and `select()` adds them back wherever they fall in a touched module's `TARGETS` row. So an old map selects *more*, toward the row. **It is still narrower than a fresh map in one measured case, and that is accepted:** a call path added *across modules* since the build (`b.k()` now calls `a.g()`; the developer edits `g`) -- `test_k` is in what the map cannot speak for, but only `a.py` is touched and `test_k` is not in `a.py`'s row, so it is dropped. That is the same bound rule 3 has always had, since a row is "files that import or name the module", not a closure over callers; the release integration run is what finds it. The sound alternative -- add every test recorded against any function changed since the build, unintersected -- grows toward the whole suite with map age, which is the per-PR full run the ruling ended. A second residual: a test that ran in the 3.14t build, so is not `unmapped`, but reaches a function only on 3.12's process path.
- **A selected test that no longer exists** (renamed or deleted since the build) is detected against the collection, and the touched modules fall to their rows.
- **Both sides of a diff are read.** A deleted function is found on the old side under its own name; resolving a pure deletion on the new side attributes it to whatever function precedes it (measured in review).
- **A function both a test and a worker ran selects both** (a helper one unit test calls directly and forty pool tests reach inside a worker).
- **Only a function's body counts as a call.** Its `def` line and default-argument lines run at import. Everything outside a test is labelled `<startup>`, so the empty context means exactly one thing: a spawned process.
- **Rule 7 widens, except for prose.** A changed path no test names selects the suite, and so does any package data file -- but documentation no test names (`docs/`, any `*.md`) selects nothing. The first revision sent those to the suite too, and this plan's own PR then selected the whole suite for itself: every dated spec would cost it twice. The needle is the basename, plus the stem for `.py` only, stated identically in `RELEASING.md` step 3.
- **git is pinned** against the developer's config: no rename pairing (a pure rename has no hunk), no prefix options, `-z` everywhere.

`scripts/test_map.py`'s core and its test file below were written and run before this plan was revised, and again after the second review pass of PR #719: **36 passed**, including scratch git repositories for every diff case, and the dispatch finder was run against the live source (five workers; six dispatching functions: `DicomExporter.export_batch`, `DicomExporter.write_tree`, `_ingest_results`, `DicomSession.audit`, `DicomSession.discover_redaction_zones`, `DicomSession.scan_pixel_content`). Task 12's wiring and Task 13's probes have **not** been run by the author; the reviewer ran Task 12's three tests (two passed, the third needed the module in place) and loaded both modules by path on 3.12.14 and 3.14.7t.

### Task 10: label coverage with the running test

**Files:**
- Modify: `tests/conftest.py`, `.gitignore` (add `.test-map.json` under the coverage block)

**Interfaces:**
- Produces: coverage contexts of `<startup>` outside any test and the test's nodeid around its whole protocol. Spawned processes are untouched by either hook and record under `""`.

**Why hooks and not `dynamic_context = test_function`** (measured 2026-09-17; spec §10 items 4-6). That context starts when a `test*` frame is entered and ends when it returns, so everything a **fixture** executes is recorded under the empty context, exactly like a worker line. Switching the context around the whole protocol is what `pytest-cov --cov-context=test` does, with no dependency. Measured: a fixture-executed line in `builders.py` lands under its test's nodeid. `dynamic_context` and `switch_context` must not both be set.

**Accepted limit, re-examined now that a miss reaches `main`:** a module- or session-scoped fixture is attributed to the first test that triggers it. There are 11 such fixtures in 4 test files. It stays accepted because the miss is bounded: what such a fixture runs is also run by ordinary tests in every case checked, and rule 3 catches a function nothing else runs. Task 14 Step 1b measures it: functions whose only recorded tests come from one of those four files.

- [ ] **Step 1: Add the hooks to `tests/conftest.py`**

At module scope, **above** the `from isocenter…` imports, so the package's own import is labelled:

```python
#: Set by `scripts/test_map.py build` and by nothing else. Without it the
#: hooks below do nothing, so the documented `coverage run -m pytest`
#: keeps writing the data file it always wrote -- per-test contexts for
#: ~4,800 tests were only ever costed on five. Not an ISOCENTER_ name:
#: those are the library's and tests/test_documented_env_vars.py wants a
#: docs/environment.md row for each.
_MAP_CONTEXTS_VAR = "TEST_MAP_CONTEXTS"
_coverage_label = ""


def _label_coverage(label):
    """Switch coverage's context; return the label that was in force."""
    global _coverage_label
    previous = _coverage_label
    if os.environ.get(_MAP_CONTEXTS_VAR) != "1":
        return previous
    try:
        import coverage
    except ImportError:
        return previous
    cov = coverage.Coverage.current()
    if cov is not None:
        cov.switch_context(label)
        _coverage_label = label
    return previous


# Everything outside a test -- imports, collection, session fixtures'
# teardown -- is "<startup>", so the empty context is left meaning one
# thing: a spawned process, where no hook reaches (#707).
_label_coverage("<startup>")
```

`os` is already imported at the top of `conftest.py`; this block goes below that import. And with the other hooks:

```python
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Label coverage with the running test, fixtures included (#707).

    `dynamic_context = test_function` stops at the test function's own
    frame, so what a fixture executes is recorded under no test at all.
    Restores the label it found rather than assuming "<startup>", so an
    in-process nested run does not relabel the rest of its outer test.
    """
    previous = _label_coverage(item.nodeid)
    try:
        return (yield)
    finally:
        _label_coverage(previous)
```

- [ ] **Step 2: Prove it by hand.** `find . -maxdepth 1 -name '.coverage.*' -delete` (never `rm -f .coverage*` -- that glob deletes `.coveragerc`, which silently turns off worker measurement; it cost this plan two invalid measurements), then `TEST_MAP_CONTEXTS=1 … -m coverage run -m pytest -q tests/test_crypto.py tests/test_multiprocessing.py && … -m coverage combine`, then after `.read()` print `sorted(CoverageData(".coverage").measured_contexts())[:4]`. Expected: `''`, `'<startup>'`, then nodeids such as `tests/test_crypto.py::test_wrong_key`.

- [ ] **Step 3: Commit** (`test: label coverage with the running test, and everything else as startup (#707)`).

### Task 11: the selector's core -- map, diff, rules

One module and one test file, given whole because they were run whole. Write them test-first in three passes, committing after each: **(A)** `context_to_nodeid`, `functions_in`, `function_at`, `split_functions`, `from_coverage` with the tests down to `test_a_signature_is_not_a_call`; **(B)** `parse_hunks`, `changes_in`, `changed`, `merge_base` with the tests down to `test_paths_with_spaces_and_modes`; **(C)** `Selection`, the dispatch finder, `select`, `cannot_speak_for`, `unmatched` with the rest. For each pass: add that pass's tests, run them and watch them fail on the missing name, add that pass's functions, run and watch them pass, commit.

**Files:**
- Create: `scripts/test_map.py`
- Test: `tests/test_changed_code_selects_its_tests.py`

**Interfaces:**
- Consumes: Task 10's contexts.
- Produces: `MAP_FILE`, `PURPOSE`, `FULL_SUITE_PATHS`; `Change(path, qualname)` with `qualname=None` for "outside any function"; `context_to_nodeid(str) -> str`; `functions_in(source) -> list[(qualname, def_line, last_line, first_body_line)]`; `function_at(source, line) -> str | None`; `split_functions(source, contexts_by_lineno) -> (dict[str, list[str]], list[str])`; `from_coverage(data_path, repo, sha, python, collected=()) -> dict` with keys `sha`, `python`, `functions` `{path: {qualname: [nodeid]}}`, `workers` `{path: [qualname]}`, `unmapped` `[nodeid]`; `parse_hunks(text) -> (old, new)`; `changes_in(path, source, ranges) -> set[Change]`; `changed(repo, old, new=None) -> (set[Change], list[str])`; `merge_base(repo, upstream=None) -> str`; `Selection(full, nodeids, files, reasons, touched)`; `dispatchers(repo) -> set[(path, qualname | None)]`; `dispatched_workers(repo) -> set[str]`; `select(mapping, changes, other, targets, repo, dispatching=None, unspoken=frozenset()) -> Selection`; `cannot_speak_for(mapping, repo, base) -> set[str]`; `unmatched(sel, collected) -> set[str]`. `targets` is `mutation_probe.TARGETS` as it stands: `{module_path: ([test_file, ...], budget)}`.

- [ ] **Step 1: The test file**, `tests/test_changed_code_selects_its_tests.py`:

```python
"""`pytest --changed` runs the tests that exercise what was edited (#707).

Since 2026-09-17 this selection is the pre-merge check (RELEASING.md,
step 3): the full suite runs when a release is cut, not before a merge.
A test this misses lets a regression onto `main`, so every doubt widens
-- to the module's TARGETS row, then to the suite -- and several tests
below are counterexamples an adversarial review found against an
earlier, narrower design (PR #719).

pytest-testmon was tried first and rejected: it traces the pytest
process only, so an edit to `ingest_worker` selected no tests at all.
The last three tests in this file are that experiment, kept.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# By path and appended, never inserted at 0: a future scripts/<name>.py
# must not be able to shadow a stdlib module inside the pytest process.
sys.path.append(str(REPO / "scripts"))
import test_map  # noqa: E402

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

    def never_called(
            self,
            mode=dict(x=1)):
        return 3


def scan_worker(args):
    return args
'''

def test_functions_are_named_the_way_a_reader_would_name_them():
    assert test_map.functions_in(SOURCE) == [
        ("Session.compact", 9, 11, 10), ("Session.export", 13, 16, 14),
        ("Session.export.inner", 14, 15, 15), ("Session.never_called", 18, 21, 21),
        ("scan_worker", 24, 25, 25)]

def test_a_line_belongs_to_its_innermost_function_or_to_none():
    assert test_map.function_at(SOURCE, 11) == "Session.compact"
    assert test_map.function_at(SOURCE, 15) == "Session.export.inner"
    assert test_map.function_at(SOURCE, 16) == "Session.export"
    assert test_map.function_at(SOURCE, 3) is None
    assert test_map.function_at(SOURCE, 7) is None

def test_functions_split_into_what_tests_ran_and_what_workers_ran():
    contexts = {9: ["<startup>"], 11: ["tests/test_c.py::test_a[x]", ""],
                18: ["<startup>"], 20: ["<startup>"], 24: [""], 25: [""],
                15: ["<startup>", "tests/test_c.py::test_b"],
                3: ["tests/test_c.py::test_a"]}
    functions, workers = test_map.split_functions(SOURCE, contexts)
    assert functions == {"Session.compact": ["tests/test_c.py::test_a"],
                         "Session.export.inner": ["tests/test_c.py::test_b"]}
    assert workers == ["Session.compact", "scan_worker"]

def test_a_signature_is_not_a_call():
    assert test_map.split_functions(SOURCE, {18: [""], 19: [""], 20: [""]}) == ({}, [])

DIFF = """\
diff --git a/isocenter/session.py b/isocenter/session.py
--- a/isocenter/session.py
+++ b/isocenter/session.py
@@ -70,0 +71 @@ def scan_worker(args):
+    probe = 1
@@ -1612,2 +1613,1 @@ class DicomSession:
-        a = 1
-        b = 2
+        a = 3
@@ -1700,2 +1699,0 @@ class DicomSession:
-        gone = 1
-        gone = 2
diff --git a/isocenter/old.py b/isocenter/old.py
--- a/isocenter/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
"""

def test_each_side_of_a_hunk_is_read_in_its_own_numbering():
    old, new = test_map.parse_hunks(DIFF)
    assert old == {"isocenter/session.py": [(1612, 1613), (1700, 1701)],
                   "isocenter/old.py": [(1, 2)]}
    assert new == {"isocenter/session.py": [(71, 71), (1613, 1613)]}

@pytest.fixture
def repo(tmp_path):
    def git(*a): subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "isocenter").mkdir(); (tmp_path / "tests").mkdir()
    (tmp_path / "isocenter" / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n\n\ndef h():\n    return 3\n")
    (tmp_path / "isocenter" / "b.py").write_text("def k():\n    return 1\n")
    (tmp_path / "tests" / "test_a.py").write_text("def test_f(): pass\n")
    git("add", "-A"); git("commit", "-qm", "base")
    return tmp_path, git

def test_deleting_a_function_names_it(repo):
    path, git = repo
    (path / "isocenter" / "a.py").write_text("def f():\n    return 1\n\n\ndef h():\n    return 3\n")
    changes, other = test_map.changed(path, "HEAD")
    assert test_map.Change("isocenter/a.py", "g") in changes
    assert test_map.Change("isocenter/a.py", "f") not in changes

def test_a_rename_is_a_delete_and_an_add(repo):
    path, git = repo
    git("mv", "isocenter/b.py", "isocenter/c.py")
    changes, other = test_map.changed(path, "HEAD")
    # No rename pairing: the old name's functions are found on the old
    # side and the new name's on the new, so both sets of tests run.
    assert changes == {test_map.Change("isocenter/b.py", "k"),
                       test_map.Change("isocenter/c.py", "k")}
    assert other == []

def test_git_config_cannot_blind_it(repo):
    path, git = repo
    git("config", "diff.noprefix", "true")
    (path / "isocenter" / "a.py").write_text("def f():\n    return 10\n\n\ndef g():\n    return 2\n\n\ndef h():\n    return 3\n")
    changes, _ = test_map.changed(path, "HEAD")
    assert changes == {test_map.Change("isocenter/a.py", "f")}

def test_a_deleted_line_that_looks_like_a_header_does_not_switch_files():
    spoof = ("diff --git a/isocenter/a.py b/isocenter/a.py\n"
             "--- a/isocenter/a.py\n+++ b/isocenter/a.py\n"
             "@@ -3,2 +3,1 @@\n"
             "--- a/isocenter/zzz.py\n-x = 1\n+y = 2\n"
             "@@ -9 +8 @@\n-p\n+q\n")
    old, new = test_map.parse_hunks(spoof)
    assert set(old) == set(new) == {"isocenter/a.py"}


def test_a_module_that_does_not_parse_falls_to_its_row():
    assert test_map.changes_in("isocenter/a.py", "def broken(:\n", [(1, 1)]) == {
        test_map.Change("isocenter/a.py", None)}


def test_a_one_line_function_is_counted_which_over_selects():
    source = "def f(): return 1\n"
    assert test_map.split_functions(source, {1: [""]}) == ({}, ["f"])


def test_paths_with_spaces_and_modes(repo):
    path, git = repo
    (path / "tests" / "test new.py").write_text("x = 1\n")
    (path / "isocenter" / "b.py").chmod(0o755)
    _, other = test_map.changed(path, "HEAD")
    assert "tests/test new.py" in other and "isocenter/b.py" in other

MAP = {"sha": "abc", "python": "3.14.7t",
       "functions": {"isocenter/session.py": {
           "DicomSession.compact": ["tests/test_compaction.py::TestCompaction::test_a"],
           "DicomSession.ingest": ["tests/test_multiprocessing.py::test_parallel"],
           "helper": ["tests/test_unit.py::test_helper"]}},
       "workers": {"isocenter/io_handlers.py": ["ingest_worker"],
                   "isocenter/session.py": ["helper"]},
       "unmapped": []}
TARGETS = {"isocenter/session.py": (["tests/test_session.py", "tests/test_new.py"], 80),
           "isocenter/io_handlers.py": (["tests/test_io.py"], 80)}
DISPATCHING = {("isocenter/session.py", "DicomSession.ingest")}
C = test_map.Change

def _select(changes=(), other=(), mapping=MAP, **kw):
    return test_map.select(mapping, set(changes), list(other), TARGETS, REPO,
                           dispatching=kw.pop("dispatching", DISPATCHING), **kw)

def test_rule_1_a_function_a_test_ran_selects_those_tests():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")])
    assert not sel.full and not sel.files
    assert sel.nodeids == {"tests/test_compaction.py::TestCompaction::test_a"}

def test_rule_2_a_worker_function_selects_through_its_dispatchers():
    assert _select([C("isocenter/io_handlers.py", "ingest_worker")]).nodeids == {
        "tests/test_multiprocessing.py::test_parallel"}

def test_a_function_tests_and_workers_both_ran_selects_both():
    assert _select([C("isocenter/session.py", "helper")]).nodeids == {
        "tests/test_unit.py::test_helper", "tests/test_multiprocessing.py::test_parallel"}

def test_a_dispatch_at_module_scope_widens_to_the_row():
    sel = _select([C("isocenter/io_handlers.py", "ingest_worker")],
                  dispatching=DISPATCHING | {("isocenter/x.py", None)})
    assert "tests/test_io.py" in sel.files


def test_one_dispatcher_without_a_record_widens_even_when_another_has_one():
    # The reviewer's case: export's dispatcher renamed since the build,
    # audit's still recorded. `via` is non-empty and used to be trusted.
    sel = _select([C("isocenter/io_handlers.py", "ingest_worker")],
                  dispatching=DISPATCHING | {
                      ("isocenter/io_handlers.py", "DicomExporter.export_batch")})
    assert "tests/test_io.py" in sel.files
    assert "tests/test_multiprocessing.py::test_parallel" in sel.nodeids

def test_rule_3_no_record_falls_to_the_targets_row():
    assert _select([C("isocenter/session.py", "DicomSession.brand_new")]).files == {"tests/test_session.py", "tests/test_new.py"}
    assert _select([C("isocenter/session.py", None)]).files == {"tests/test_session.py", "tests/test_new.py"}

def test_rule_4_no_row_selects_the_full_suite():
    assert _select([C("isocenter/profiles.py", None)]).full

def test_rule_5_a_changed_test_file_selects_itself():
    sel = _select(other=["tests/test_crypto.py"])
    assert sel.files == {"tests/test_crypto.py"} and not sel.full

@pytest.mark.parametrize("path", ["tests/conftest.py", "tests/support/shards.py", "setup.py", "pytest.ini", ".coveragerc", "pyproject.toml", "MANIFEST.in"])
def test_rule_6_shared_machinery_selects_the_full_suite(path):
    assert _select(other=[path]).full

def test_rule_7_a_path_no_test_names_selects_the_full_suite():
    # Built at run time, or this file would be the test that names it.
    assert _select(other=[".github/workflows/" + "nobody-" + "names-this.yml"]).full
    assert _select(other=["scripts/" + "nobody_" + "names_this.py"]).full


def test_rule_7_documentation_no_test_names_selects_nothing():
    # Every dated spec has a basename no test names. Sending those to the
    # suite is the per-PR full run the 2026-09-17 ruling ended -- found
    # when the PR that wrote this rule selected the suite for itself.
    for path in ("docs/superpowers/specs/" + "nobody-" + "names-this.md",
                 "NOBODY_" + "NAMES_THIS.md"):
        sel = _select(other=[path])
        assert not sel.full and not sel.files and not sel.nodeids
        assert "nothing" in sel.reasons[-1]


def test_rule_7_matches_a_python_file_by_its_stem_and_nothing_else_by_it(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("import helper_mod\nsession = 1\n")
    named = test_map._tests_naming(tmp_path, "scripts/helper_mod.py")
    assert named == {"tests/test_x.py"}
    assert test_map._tests_naming(tmp_path, "docs/session.md") == set()

def test_package_data_selects_the_full_suite():
    assert _select(other=["isocenter/resources/redaction_rules.json"]).full

def test_a_module_with_no_hunk_falls_to_its_row_or_the_suite():
    assert _select(other=["isocenter/session.py"]).files == {"tests/test_session.py", "tests/test_new.py"}
    assert _select(other=["isocenter/brand_new.py"]).full

def test_no_map_degrades_to_targets_rows_and_says_so():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")], mapping=None)
    assert "tests/test_session.py" in sel.files
    assert any("no usable map" in r for r in sel.reasons)

def test_unspoken_widens_within_rows_only():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")],
                  unspoken={"tests/test_new.py", "tests/test_elsewhere.py"})
    assert sel.files == {"tests/test_new.py"}

def test_a_selected_test_that_no_longer_exists_is_reported():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")])
    assert test_map.unmatched(sel, ["tests/test_other.py::test_x[1]"]) == sel.nodeids
    assert not test_map.unmatched(sel, ["tests/test_compaction.py::TestCompaction::test_a[p]"])

def test_an_old_map_names_what_it_cannot_speak_for(repo):
    path, git = repo
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()
    (path / "isocenter" / "a.py").write_text("def f():\n    return g()\n\n\ndef g():\n    return 2\n\n\ndef h():\n    return 3\n")
    (path / "tests" / "test_added.py").write_text("def test_n(): pass\n")
    git("add", "-A"); git("commit", "-qm", "main moved")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()
    mapping = {"sha": sha, "functions": {"isocenter/a.py": {"f": ["tests/test_f.py::test_f"], "g": ["tests/test_g.py::test_g"]}},
               "workers": {}, "unmapped": ["tests/test_skipped.py::test_s"]}
    assert test_map.cannot_speak_for(mapping, path, base) == {
        "tests/test_skipped.py", "tests/test_added.py", "tests/test_f.py"}
```

`test_rule_7_…` builds its path at run time because `_tests_naming` greps the test files, and this one would otherwise be the file that names it. `test_the_dispatch_finder…` is added in pass C:

```python
def test_the_dispatch_finder_sees_every_worker_in_the_live_source():
    found = test_map.dispatched_workers(REPO)
    assert {"scan_worker", "_verify_worker", "_discover_worker",
            "ingest_worker", "_export_instance_worker"} <= found, (
        "a worker function has no hand-off the finder recognises, so an "
        "edit to it would fall past rule 2")
    assert all(name for _path, name in test_map.dispatchers(REPO)), (
        "a worker is handed to a pool at module scope; rule 2 cannot "
        "reach its tests and widens to the row instead -- decide whether "
        "that is wanted before accepting it")
```

If the first assertion fails, read how the missing worker is dispatched (`grep -n "<name>" isocenter/*.py`) and extend `_worker_calls` to that spelling (a keyword argument, an `Attribute`) with a test for it; do not weaken the assertion.

- [ ] **Step 2: The module**, `scripts/test_map.py` (the CLI half is Task 12):

```python
"""Which tests exercise which functions, and what a change selects (#707).

    python -m scripts.test_map build     # 3.14t, clean tree at a known SHA
    python -m scripts.test_map select    # print what --changed would run

Since 2026-09-17 the selection is the pre-merge check (RELEASING.md,
step 3); the full suite runs when a release is cut. A test this misses
lets a regression onto `main`, so every doubt widens: no record -> the
module's TARGETS row -> the suite, and whatever the map cannot speak for
is added back from the touched modules' rows (`cannot_speak_for`).

The map is generated, gitignored and never edited. `TARGETS` in
scripts/mutation_probe.py stays the one maintained module-to-tests map;
this only narrows inside it.

Keyed by function name, not line number, because nothing rebuilds it on
every push. **An old map is not merely less sharp: it is ignorant** of
tests added since, of tests that skipped in the build, and of what a
function calls now that its body has changed. Those are computed from
git at selection time and widened, so an old map selects more, never
less than its module rows would justify.

Contexts are nodeids, switched by hooks in tests/conftest.py: "<startup>"
for everything outside a test, the nodeid around each test's whole
protocol, so what a fixture runs is attributed to the test. What is left
under the empty context ran in a spawned process, where no hook reaches.

Built on 3.14t with `concurrency = multiprocessing,thread`, because
coverage over spawned workers is what is slow on 3.12 -- 5.96 s bare
against 68.3 s for tests/test_multiprocessing.py + tests/test_crypto.py,
contexts or not -- where the free-threaded build pays 0.64 s against
1.00 s, and because `.coveragerc`'s `multiprocessing` alone replaces
coverage's default and leaves worker threads untraced (spec §10).
"""
import ast
import json
import os
import re
import subprocess
from collections import namedtuple
from dataclasses import dataclass, field
from pathlib import Path

MAP_FILE = ".test-map.json"
PURPOSE = ("this selection is the pre-merge check (RELEASING.md step 3): run "
           "it on 3.12 and 3.14t; the full suite runs when a release is cut")
FULL_SUITE_PATHS = ("tests/conftest.py", "tests/support/", "setup.py",
                    "pytest.ini", ".coveragerc", "pyproject.toml",
                    "MANIFEST.in")
Change = namedtuple("Change", "path qualname")


def context_to_nodeid(context):
    return context.split("[", 1)[0]


def functions_in(source):
    """(qualname, def line, last line, first body line), outermost first."""
    found = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    found.append((name, child.lineno, child.end_lineno,
                                  child.body[0].lineno))
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(ast.parse(source), "")
    return found


def _span_at(spans, line):
    best = None
    for span in spans:
        if span[1] <= line <= span[2] and (best is None or span[1] >= best[1]):
            best = span
    return best


def function_at(source, line):
    """The innermost function holding `line`, or None."""
    span = _span_at(functions_in(source), line)
    return span[0] if span else None


def split_functions(source, contexts_by_lineno):
    """({function: [tests]}, [functions a spawned worker ran])."""
    spans = functions_in(source)
    named, in_workers = {}, set()
    for lineno, contexts in contexts_by_lineno.items():
        span = _span_at(spans, lineno)
        # The signature -- the `def` line and every default-argument line
        # under it -- runs when the module or class body does, not when
        # the function is called. Counting it would file every function
        # that was never called as "ran in a worker". A one-line
        # `def f(): return 1` has no line to tell the two apart, so it is
        # counted: a spawned process importing it files it under workers,
        # which over-selects. There are none in isocenter/ (2026-09-17).
        if span is None or lineno < span[3]:
            continue
        for context in contexts:
            if context == "":
                in_workers.add(span[0])
            elif not context.startswith("<"):
                named.setdefault(span[0], set()).add(context_to_nodeid(context))
    return ({name: sorted(tests) for name, tests in sorted(named.items())},
            sorted(in_workers))


def from_coverage(data_path, repo, sha, python, collected=()):
    import coverage
    data = coverage.CoverageData(str(data_path))
    data.read()
    repo = Path(repo).resolve()
    result = {"sha": sha, "python": python, "functions": {}, "workers": {}}
    for measured in sorted(data.measured_files()):
        try:
            rel = Path(measured).resolve().relative_to(repo).as_posix()
        except ValueError:
            continue
        functions, workers = split_functions(
            Path(measured).read_text(encoding="utf-8"),
            data.contexts_by_lineno(measured))
        if functions:
            result["functions"][rel] = functions
        if workers:
            result["workers"][rel] = workers
    ran = {node for per_file in result["functions"].values()
           for tests in per_file.values() for node in tests}
    # Collected but never seen running anything in the package: skipped on
    # this interpreter, or a test of the repo rather than of the code. The
    # map cannot speak for these, and says so.
    result["unmapped"] = sorted({context_to_nodeid(n) for n in collected} - ran)
    return result


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text):
    """({path: [old-side ranges]}, {path: [new-side ranges]}).

    A side with no lines -- the old side of a pure insertion, the new
    side of a pure deletion -- contributes nothing: a deleted function is
    found on the old side, under its own name, not by guessing from the
    line the deletion happens to sit after.
    """
    old, new, old_path, new_path = {}, {}, None, None
    in_header = False
    for line in diff_text.splitlines():
        # `---`/`+++` are headers only between `diff --git` and the first
        # hunk. A deleted source line reading `-- a/x` renders as
        # `--- a/x` inside a hunk and must not switch the path (measured).
        if line.startswith("diff --git "):
            in_header, old_path, new_path = True, None, None
        elif in_header and line.startswith("--- "):
            old_path = line[6:].strip() if line.startswith("--- a/") else None
        elif in_header and line.startswith("+++ "):
            new_path = line[6:].strip() if line.startswith("+++ b/") else None
        else:
            match = _HUNK.match(line)
            if not match:
                continue
            in_header = False
            for path, side, start, count in (
                    (old_path, old, match.group(1), match.group(2)),
                    (new_path, new, match.group(3), match.group(4))):
                count = 1 if count is None else int(count)
                if path and count:
                    side.setdefault(path, []).append(
                        (int(start), int(start) + count - 1))
    return old, new


def changes_in(path, source, ranges):
    try:
        spans = functions_in(source)
    except SyntaxError:
        # Mid-edit and unparseable: no function can be named, so the whole
        # module is "changed outside any function" and falls to its row.
        return {Change(path, None)}
    out = set()
    for start, end in ranges:
        for line in range(start, end + 1):
            span = _span_at(spans, line)
            out.add(Change(path, span[0] if span else None))
    return out


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


# Whatever the developer's git config says: no a/ b/ prefix games, no
# rename pairing (a 100% rename has no hunk at all), no external differ.
_DIFF = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicprefix=false",
         "diff", "--no-renames", "--no-ext-diff", "--no-color")


def _is_module(path):
    return path.startswith("isocenter/") and path.endswith(".py")


def changed(repo, old, new=None):
    """(function changes, every other changed path) between two trees.

    `new=None` is the working tree, untracked files included.
    """
    ends = [old] if new is None else [old, new]
    names = _git(repo, *_DIFF, "--name-only", "-z", *ends, "--", ".")
    paths = {p for p in names.split("\0") if p}
    if new is None:
        untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        paths |= {p for p in untracked.split("\0") if p}
    old_side, new_side = parse_hunks(_git(repo, *_DIFF, "-U0", *ends, "--", "."))
    changes = set()
    for path in paths:
        if not _is_module(path):
            continue
        if path in old_side:
            source = _git(repo, "show", f"{old}:{path}")
            changes |= changes_in(path, source, old_side[path])
        if path in new_side:
            source = (_git(repo, "show", f"{new}:{path}") if new
                      else (Path(repo) / path).read_text(encoding="utf-8"))
            changes |= changes_in(path, source, new_side[path])
    # A module with hunks is spoken for by its functions. One without --
    # added empty, deleted, mode-only, untracked -- stays in `other`.
    return changes, sorted(paths - {c.path for c in changes})


def merge_base(repo, upstream=None):
    for candidate in ([upstream] if upstream else ["origin/main", "main"]):
        try:
            return _git(repo, "merge-base", "HEAD", candidate).strip()
        except subprocess.CalledProcessError:
            continue
    raise SystemExit("--changed needs the branch this work will merge into; "
                     "pass --changed-base for a release branch")


@dataclass
class Selection:
    full: bool = False
    nodeids: set = field(default_factory=set)
    files: set = field(default_factory=set)
    reasons: list = field(default_factory=list)
    touched: set = field(default_factory=set)


def _worker_calls(repo):
    """(path, dispatching function, worker name) for every pool hand-off.

    A hand-off is a call one of whose positional arguments is a bare name
    ending `_worker`: `run_parallel(ingest_worker, batch, ...)`, or the
    export pool's call with `_export_instance_worker` on its own line. By
    convention rather than by list, so a sixth worker is found the day it
    is written.
    """
    for path in sorted((Path(repo) / "isocenter").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(repo).as_posix()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id.endswith("_worker"):
                        yield rel, function_at(source, node.lineno), arg.id


def dispatchers(repo):
    return {(rel, name) for rel, name, _worker in _worker_calls(repo)}


def dispatched_workers(repo):
    return {worker for _rel, _name, worker in _worker_calls(repo)}


def _tests_naming(repo, path):
    # The basename, and the stem only for Python: `import test_map` names
    # scripts/test_map.py without its suffix, but the stem of
    # docs/session.md is a word half the suite contains. RELEASING.md
    # step 3 states the same needle; keep the two identical.
    needles = {Path(path).name}
    if path.endswith(".py"):
        needles.add(Path(path).stem)
    return {test.relative_to(repo).as_posix()
            for test in (Path(repo) / "tests").glob("test_*.py")
            if any(n in test.read_text(encoding="utf-8") for n in needles)}


def select(mapping, changes, other, targets, repo, dispatching=None,
           unspoken=frozenset()):
    """`unspoken`: test files the map cannot speak for (`cannot_speak_for`)."""
    sel = Selection()
    if mapping is None:
        sel.reasons.append(
            f"no usable map at {MAP_FILE}: falling back to TARGETS rows "
            "(build one with `python -m scripts.test_map build`)")
    functions = (mapping or {}).get("functions", {})
    workers = (mapping or {}).get("workers", {})
    if dispatching is None:
        dispatching = dispatchers(repo)

    def row(path, why):
        if path in targets:
            sel.files.update(targets[path][0])
            sel.reasons.append(f"{path}: {why} -> its TARGETS row")
        else:
            sel.full = True
            sel.reasons.append(
                f"{path}: {why} and no TARGETS row -> the full suite")

    for change in sorted(changes, key=lambda c: (c.path, c.qualname or "")):
        sel.touched.add(change.path)
        where = f"{change.path}::{change.qualname}"
        tests = set(functions.get(change.path, {}).get(change.qualname, ()))
        in_worker = change.qualname in workers.get(change.path, ())
        if change.qualname is None:
            row(change.path, "changed outside any function")
            continue
        if in_worker:
            # Not `elif`: a helper one unit test calls directly and forty
            # pool tests reach inside a worker is both.
            via, blind = set(), []
            for path, name in sorted(dispatching, key=str):
                recorded = functions.get(path, {}).get(name, ()) if name else ()
                via.update(recorded)
                if not recorded:
                    blind.append(f"{path}::{name or '<module scope>'}")
            if blind:
                # One dispatcher with a record is not enough: if export's
                # has none, an export-worker edit would select the ingest
                # and audit tests and not one export test.
                row(change.path, f"{change.qualname} runs in workers and "
                                 f"{len(blind)} dispatcher(s) have no record "
                                 f"({blind[0]})")
            tests |= via
        if tests:
            sel.nodeids |= tests
            sel.reasons.append(
                f"{where}: {len(tests)} tests ran it"
                + (" or dispatch the pool it runs in" if in_worker else ""))
        elif not in_worker:
            row(change.path, f"{change.qualname} has no record")

    for path in other:
        if path.startswith(FULL_SUITE_PATHS):
            sel.full = True
            sel.reasons.append(f"{path}: shared test machinery -> the full suite")
        elif path.startswith("tests/test_") and path.endswith(".py"):
            sel.files.add(path)
            sel.reasons.append(f"{path}: a changed test file -> itself")
        elif _is_module(path):
            sel.touched.add(path)
            row(path, "added, deleted, or changed with no hunk")
        elif path.startswith("isocenter/"):
            sel.full = True
            sel.reasons.append(
                f"{path}: package data every session loads -> the full suite")
        else:
            named = _tests_naming(repo, path)
            if named:
                sel.files |= named
                sel.reasons.append(f"{path}: {len(named)} test files name it")
            elif path.startswith("docs/") or path.endswith(".md"):
                # Prose no test reads cannot break one. Without this every
                # dated spec costs the whole suite twice, which is the
                # per-PR full run the 2026-09-17 ruling ended.
                sel.reasons.append(
                    f"{path}: documentation no test names -> nothing")
            else:
                sel.full = True
                sel.reasons.append(
                    f"{path}: no test names it -> the full suite")

    rows = {f for path in sel.touched for f in targets.get(path, ([],))[0]}
    widened = (set(unspoken) & rows) - sel.files
    if widened:
        sel.files |= widened
        sel.reasons.append(
            f"{len(widened)} test files in the touched modules' rows are ones "
            "the map cannot speak for (new, skipped in the build, or running "
            "code that changed since it) -> added")
    return sel


def cannot_speak_for(mapping, repo, base):
    """Test files an old map is ignorant of, as of `base`.

    Three kinds, each an under-selection if left out: tests that did not
    run in the build (`unmapped`); test files changed since the build;
    and tests recorded against a function whose body has changed since --
    what they reach now is not what the map saw.
    """
    files = {node.split("::")[0] for node in mapping.get("unmapped", ())}
    moved, other = changed(repo, mapping["sha"], base)
    files |= {p for p in other if p.startswith("tests/test_")}
    ahead, other = changed(repo, base)
    files |= {p for p in other if p.startswith("tests/test_")}
    for change in moved:
        tests = mapping["functions"].get(change.path, {}).get(change.qualname, ())
        files |= {node.split("::")[0] for node in tests}
    return files


def unmatched(sel, collected):
    """Selected tests that no longer exist: renamed or deleted since the build."""
    known = {context_to_nodeid(n) for n in collected}
    return {n for n in sel.nodeids if n not in known}
```

- [ ] **Step 3: Run the whole file on both interpreters.** Expected: 37 passed each (36 plus the live-source test).

- [ ] **Step 4: Commit** each pass as it goes green (`test: a function-to-tests map from coverage (#707)`, `test: read both sides of the developer's diff as functions (#707)`, `test: the rules that turn changed functions into a selection, and what an old map cannot speak for (#707)`).

### Task 12: `build`, `select`, and `pytest --changed`

Not run before this plan was written; treat the code as a careful draft and let the tests decide.

**Files:** Modify `scripts/test_map.py`, `tests/conftest.py`; test: append.

**Interfaces:**
- Consumes: Task 11; `mutation_probe.TARGETS`.
- Produces: `load(repo) -> dict | None` (None too when the map's SHA is not in this clone); `selection_for(repo, upstream=None) -> (Selection, dict | None, targets)`; `fall_back_for_missing(sel, missing, targets)`; `describe(sel, mapping, repo) -> str`; `build(repo, out_dir, sha=None)`; `main(argv)`; pytest options `--changed`, `--changed-base=BRANCH`.

- [ ] **Step 1: Write the failing tests** (append):

```python
def _git_tree(path):
    return subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                          cwd=path, capture_output=True).returncode == 0


def test_select_prints_its_reasons_and_what_it_is_for():
    if not _git_tree(REPO):
        pytest.skip("not a git work tree: a `git archive` copy has no diff")
    out = subprocess.run(
        [sys.executable, "-m", "scripts.test_map", "select"],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "the pre-merge check (RELEASING.md step 3)" in out.stdout
    assert "the full suite runs when a release is cut" in out.stdout


def test_a_selected_test_that_is_gone_sends_its_modules_to_their_rows():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")])
    test_map.fall_back_for_missing(sel, set(sel.nodeids), TARGETS)
    assert {"tests/test_session.py", "tests/test_new.py"} <= sel.files
    assert any("no longer exist" in reason for reason in sel.reasons)


def test_a_map_whose_commit_is_not_here_is_no_map(tmp_path):
    (tmp_path / test_map.MAP_FILE).write_text(
        '{"sha": "0000000000000000000000000000000000000000", "python": "x", '
        '"functions": {}, "workers": {}, "unmapped": []}')
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert test_map.load(tmp_path) is None
```

The body-level skip is the project's spelling (`tests/test_skip_contract.py`); account for it there if that test asks.

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement** (append to `scripts/test_map.py`):

```python
def load(repo):
    path = Path(repo) / MAP_FILE
    if not path.exists():
        return None
    mapping = json.loads(path.read_text(encoding="utf-8"))
    try:
        # A map built at a commit this clone does not have cannot be
        # aged: `cannot_speak_for` needs the diff from it.
        _git(repo, "cat-file", "-e", mapping["sha"] + "^{commit}")
    except subprocess.CalledProcessError:
        return None
    return mapping


def selection_for(repo, upstream=None):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mutation_probe", Path(__file__).resolve().parent / "mutation_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    mapping = load(repo)
    base = merge_base(repo, upstream)
    changes, other = changed(repo, base)
    unspoken = cannot_speak_for(mapping, repo, base) if mapping else set()
    # TARGETS rides along: the conftest hook needs it for
    # `fall_back_for_missing`, and loading the probe twice is waste.
    return (select(mapping, changes, other, probe.TARGETS, repo,
                   unspoken=unspoken), mapping, probe.TARGETS)


def fall_back_for_missing(sel, missing, targets):
    """Selected tests that are gone: their modules fall to their rows."""
    if not missing:
        return
    sel.nodeids -= missing
    for path in sorted(sel.touched):
        if path in targets:
            sel.files.update(targets[path][0])
        else:
            sel.full = True
    sel.reasons.append(
        f"{len(missing)} selected tests no longer exist (renamed or deleted "
        "since the map was built) -> the touched modules' TARGETS rows")


def describe(sel, mapping, repo):
    if mapping:
        behind = _git(repo, "rev-list", "--count",
                      f"{mapping['sha']}..HEAD").strip()
        out = [f"map: built at {mapping['sha'][:9]} on {mapping['python']}, "
               f"{behind} commits behind HEAD"]
    else:
        out = ["map: none usable"]
    out += [f"  {reason}" for reason in sel.reasons] or ["  nothing changed"]
    out.append("selected: the full suite" if sel.full else
               f"selected: {len(sel.nodeids)} tests + {len(sel.files)} files")
    out.append(PURPOSE)
    return "\n".join(out)


def build(repo, out_dir, sha=None):
    """Run the suite under per-test contexts and write the map to out_dir."""
    import sys
    import tempfile
    repo = Path(repo).resolve()
    if sha is None:
        # Untracked files do not move a tracked function; modified ones do.
        if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
            raise SystemExit("build wants a clean tree, so the map describes "
                             "a commit and not an edit in progress")
        sha = _git(repo, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory() as scratch:
        # `.coveragerc` plus `thread`: its `concurrency = multiprocessing`
        # replaces coverage's default, so worker threads -- what
        # run_parallel() uses on 3.14t -- go untraced. Measured with both:
        # scan_worker's body lands under its test's nodeid. A scratch copy,
        # because .coveragerc's SIGTERM comment was measured as it stands.
        rc = Path(scratch) / "coveragerc"
        text = (repo / ".coveragerc").read_text(encoding="utf-8")
        if text.count("\nconcurrency = multiprocessing\n") != 1:
            raise SystemExit(".coveragerc no longer has the one `concurrency = "
                             "multiprocessing` line build() rewrites")
        rc.write_text(text.replace(
            "\nconcurrency = multiprocessing\n",
            "\nconcurrency = multiprocessing,thread\n"), encoding="utf-8")
        env = dict(os.environ, COVERAGE_FILE=str(Path(scratch) / ".coverage"),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(repo),
                   TEST_MAP_CONTEXTS="1")  # turns conftest's labelling on
        listing = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=repo, env=env, capture_output=True, text=True, check=True)
        collected = [line for line in listing.stdout.splitlines() if "::" in line]
        subprocess.run([sys.executable, "-m", "coverage", "run",
                        f"--rcfile={rc}", "-m", "pytest", "-q"],
                       cwd=repo, env=env, check=False)
        subprocess.run([sys.executable, "-m", "coverage", "combine",
                        f"--rcfile={rc}"], cwd=repo, env=env, check=True)
        gil = getattr(sys, "_is_gil_enabled", lambda: True)()
        mapping = from_coverage(Path(scratch) / ".coverage", repo, sha,
                                sys.version.split()[0] + ("" if gil else "t"),
                                collected)
    target = Path(out_dir) / MAP_FILE
    target.write_text(json.dumps(mapping), encoding="utf-8")
    print(f"wrote {target}: {len(mapping['functions'])} files with tested "
          f"functions, {len(mapping['workers'])} with functions a worker ran, "
          f"{len(mapping['unmapped'])} tests it cannot speak for")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="python -m scripts.test_map")
    sub = parser.add_subparsers(dest="command", required=True)
    built = sub.add_parser("build")
    built.add_argument("--out", default=".",
                       help="directory to write .test-map.json into; when "
                            "building in a `git archive` copy, the main checkout")
    built.add_argument("--sha", default=None,
                       help="the commit this tree is, for a `git archive` "
                            "copy, which is not a git repository")
    chosen = sub.add_parser("select")
    chosen.add_argument("--base", default=None,
                        help="the branch this work merges into, when it is "
                             "not main (a patch onto release/X.Y)")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parent.parent
    if args.command == "build":
        build(repo, args.out, args.sha)
    else:
        sel, mapping, _targets = selection_for(repo, args.base)
        print(describe(sel, mapping, repo))
        for name in sorted(sel.nodeids | sel.files):
            print(name)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Wire `--changed` into `conftest.py`.** In `pytest_addoption`:

```python
    group.addoption(
        "--changed", action="store_true",
        help="run the tests that exercise what this branch changed since it "
             "left the branch it merges into (#707); the pre-merge check in "
             "RELEASING.md step 3")
    group.addoption(
        "--changed-base", default=None, metavar="BRANCH",
        help="the branch this work merges into, when it is not main")
```

At the top of `pytest_collection_modifyitems`, before the `--shard` block (so the two compose: select, then shard):

```python
    if config.getoption("--changed"):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_map", config.rootpath / "scripts" / "test_map.py")
        test_map = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(test_map)
        sel, mapping, targets = test_map.selection_for(
            config.rootpath, config.getoption("--changed-base"))
        # Only against a whole collection: under `pytest --changed
        # tests/test_x.py` every selected test elsewhere would read as gone.
        if not config.invocation_params.args or all(
                arg.startswith("-") for arg in config.invocation_params.args):
            test_map.fall_back_for_missing(
                sel, test_map.unmatched(sel, [item.nodeid for item in items]),
                targets)
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(test_map.describe(sel, mapping, config.rootpath))
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

The guard treats "no positional argument" as "the whole suite was collected"; an option that takes a separate value (`-k expr`) defeats it harmlessly, by skipping the check. **An empty selection is a result, not an error**: a change to documentation no test names selects nothing, by rule 7. pytest exits 5; `RELEASING.md` step 3 says what to record.

- [ ] **Step 5: Run the tests, then try it by hand.** Add a blank line inside `DicomSession.compact`'s body, run `… -m pytest --changed --collect-only -q`, read the reasons, `git checkout isocenter/session.py`. With no map yet, expect `session.py`'s `TARGETS` row and the "no usable map" reason.

- [ ] **Step 6: Commit** (`test: pytest --changed (#707)`).

### Task 13: the three spike probes, kept

These are the two cases testmon got wrong and the one it got right (spec §3.1, §6.5). They build a small real map, so they are slow; they are the reason this was built rather than adopted. **Not run before this plan was written.**

**Files:** test: append to `tests/test_changed_code_selects_its_tests.py`.

- [ ] **Step 1: Write the tests**

```python
PROBE_FILES = ["tests/test_multiprocessing.py", "tests/test_compaction.py",
               "tests/test_crypto.py"]


@pytest.fixture(scope="module")
def small_real_map(tmp_path_factory):
    """A map built from three real test files, in a scratch copy."""
    import os
    try:
        import coverage  # noqa: F401
    except ImportError:
        pytest.skip("coverage is in the dev extra")
    if not _git_tree(REPO):
        pytest.skip("not a git work tree: nothing to `git archive`")
    proj = tmp_path_factory.mktemp("maprepo")
    subprocess.run(f"git archive HEAD | tar -x -C {proj}", shell=True,
                   cwd=REPO, check=True)
    rc = proj / "both.rc"
    rc.write_text((proj / ".coveragerc").read_text().replace(
        "\nconcurrency = multiprocessing\n",
        "\nconcurrency = multiprocessing,thread\n"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env.update(PYTHONPATH=str(proj), PYTHONDONTWRITEBYTECODE="1",
               COVERAGE_FILE=str(proj / ".coverage"), TEST_MAP_CONTEXTS="1")
    subprocess.run([sys.executable, "-m", "coverage", "run", f"--rcfile={rc}",
                    "-m", "pytest", "-q", *PROBE_FILES],
                   cwd=proj, env=env, check=True, timeout=1500)
    subprocess.run([sys.executable, "-m", "coverage", "combine",
                    f"--rcfile={rc}"], cwd=proj, env=env, check=True)
    return proj, test_map.from_coverage(proj / ".coverage", proj, "HEAD", "probe")


def _files(sel):
    return {n.split("::")[0] for n in sel.nodeids} | sel.files


def test_a_compact_edit_selects_the_compaction_tests_only(small_real_map):
    proj, mapping = small_real_map
    sel = test_map.select(
        mapping, {C("isocenter/session.py", "DicomSession.compact")}, [],
        {}, proj)
    assert not sel.full, sel.reasons
    assert _files(sel) == {"tests/test_compaction.py"}


@pytest.mark.parametrize("path, qualname", [
    ("isocenter/session.py", "scan_worker"),
    ("isocenter/io_handlers.py", "ingest_worker"),
])
def test_a_worker_edit_selects_the_test_that_runs_it_in_a_pool(
        small_real_map, path, qualname):
    # pytest-testmon selected 0 of 28 for the ingest_worker edit.
    proj, mapping = small_real_map
    sel = test_map.select(mapping, {C(path, qualname)}, [], {}, proj)
    assert not sel.full, sel.reasons
    assert "tests/test_multiprocessing.py" in _files(sel)
    assert "tests/test_crypto.py" not in _files(sel)
```

`assert not sel.full` comes first on purpose: with `targets={}` a function with no record sets `full` and leaves `_files` empty, and the later assertions would then fail for a reason that says nothing. If `scan_worker` has no record on 3.14t, that is Task 10's thread attribution not working under this rc -- fix that, not the test. `git archive HEAD` exports the last commit, so commit Tasks 10-12 first.

On 3.12 the fixture's run pays for coverage over spawned workers (about 70 s for `test_multiprocessing.py` alone, against 6 s bare). Measure the fixture's wall time on both interpreters and put both in the PR body. If it exceeds 5 minutes on 3.12, the fixture must `pytest.skip` from its body when `getattr(sys, "_is_gil_enabled", lambda: True)()` is true, with the measured figure in the message.

- [ ] **Step 2: Run on both interpreters.** Expected: 3 passed each.

- [ ] **Step 3: Commit** (`test: keep the three probes that decided against testmon (#707)`).

### Task 14: build the first real map, close the open points, write it down

- [ ] **Step 1: Build on 3.14t in a `git archive` copy, timing it**

```bash
S=$(mktemp -d) && git archive HEAD | tar -x -C $S && cd $S
time PYTHON_GIL=0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$S \
  /Users/kevin/Developer/Isocenter/.venv314t/bin/python -m scripts.test_map build \
  --sha $(git -C /Users/kevin/Developer/Isocenter rev-parse HEAD) \
  --out /Users/kevin/Developer/Isocenter
```

Compare against a plain 3.14t full run. This closes spec §9's second point. Nothing runs the full suite before a merge any more, so the map has exactly two prescribed build points, and `RELEASING.md` gets both in Step 4: **(a)** "Cutting a release", step 1 -- if the ratio is under about 2x, that step's 3.14t integration run *is* `python -m scripts.test_map build`, so every release leaves a fresh map; if not, the build is a separate command in the same step. **(b)** On demand, by whoever finds `--changed` falling to `TARGETS` rows too often. An old map is ignorant of tests and call paths added since it was built; `cannot_speak_for` widens for exactly that at selection time, so an old map selects more ~~rather than less~~ **toward the touched modules' rows, and still misses a call path added across modules since the build (spec §10 item 13; qualifier added at implementation, #707 checklist)**. Record, from this first build, how many test files it widens by per week of age on a real branch -- that number is what says how often (b) is needed.

- [ ] **Step 1b: Measure the two accepted limits.** (i) Thread attribution is no longer optional -- `build()` measures with `multiprocessing,thread` (Task 12) -- so record how many functions sit in `workers` and spot-check five: each should be reachable only through a spawned process (ingest, export's pool). (ii) The wide-scoped-fixture limit (Task 10): list the functions whose every recorded test comes from one of the four files with module- or session-scoped fixtures (`grep -ln 'scope="session"\|scope="module"' tests/*.py`). If any function in `isocenter/` is reachable only that way, it is an under-selection waiting to happen: add those four files to `cannot_speak_for`'s result unconditionally and record it in spec §10.

- [ ] **Step 2: Measure rule 2's breadth** (spec §9, third point). Insert a line in `ingest_worker`, run `python -m scripts.test_map select | grep -c '^tests/'`, revert. If the selected files exceed a third of `tests/test_*.py`, implement the per-worker refinement before the PR: `_worker_calls` already yields the worker name, so have `dispatchers` return `{worker: {(path, qualname)}}` and, in rule 2, use only the dispatchers of the worker being edited. That handles an edit to a worker function itself; an edit to a helper only workers call has no worker name to key on, so it keeps the coarse rule -- record the measured breadth of both in spec §10.

- [ ] **Step 3: Check whether export's pool takes threads on 3.14t** (spec §9, fourth point): `grep -n "maxtasksperchild\|multiprocessing.Pool\|ctx.Pool" isocenter/io_handlers.py`, read the strategy branch, and record the answer in spec §10.

- [ ] **Step 4: Documents.**
  - `CLAUDE.md` (main checkout): in the Commands block add `pytest --changed` and `python -m scripts.test_map build`. In the tier list, "run the tests for what you touched (see the mapping below)" becomes "run `pytest --changed`". **Delete the module-to-tests table** and the sentence introducing it, keeping the pointer to `TARGETS` and `NOT_PROBED`. Then run `tests/test_source_citations.py`: the deletion moves every line below it, and any citation into CLAUDE.md must still hold.
  - `RELEASING.md`: where the map is built (per Step 1's finding).
  - Spec §10: every deviation and measurement from this Part.
  - `CHANGELOG.md`: `### Changed`.

- [ ] **Step 5: Commit, `RELEASING.md` steps 3-6, PR.** Step 3 here is this PR's own test file plus `pytest --changed` run on itself, on both interpreters. The PR body carries: the build time and ratio, rule 2's measured breadth, the Task 13 fixture times, and one worked example (`compact()` edit -> what `--changed` printed).

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
| §6.1 map on 3.14t, gitignored, `functions`/`workers`/`unmapped` keyed by name (§10 items 8, 10), no map degrades | 10, 11, 12 |
| §6.2 as amended by §10 item 8: diff against the merge-base with `main`, new-side numbers, both sides resolved to function names (§10 item 10) | 11 |
| §6.3 rules 1-7 as amended by §10 item 10 (both-ran union, rule 7 widens, what the map cannot speak for, vanished tests) | 11, 12 |
| §6.4 `--changed`, reasons, map SHA and age, the purpose line (pre-merge check, per §10 item 7), `select` CLI, `--changed-base` | 12 |
| §6.5 unit tests per rule, three probes kept, dispatch finder against live source | 11, 13 |
| §8 CLAUDE.md table deleted, Commands block | 14 |
| §9 four open points | 4 (first), 14 (other three) |

Names checked across tasks: `Change(path, qualname)`, `Selection(full, nodeids, files, reasons, touched)`, `select(mapping, changes, other, targets, repo, dispatching=None, unspoken=frozenset())`, `dispatchers`/`dispatched_workers`, `functions_in`/`function_at`/`split_functions`, `from_coverage(..., collected=())`, `parse_hunks -> (old, new)`/`changes_in`/`changed(repo, old, new=None)`/`merge_base(repo, upstream=None)`, `cannot_speak_for`/`unmatched`/`fall_back_for_missing`, `load`/`selection_for -> 3-tuple`/`describe(sel, mapping, repo)`/`build`, `context_to_nodeid`, `shards.parse/assign/suite_files/load_timings/TimingRecorder`, `root_guard.snapshot/new_entries/ALLOWED_PREFIXES` -- each is defined once and used with that signature.

One inconsistency found and fixed inline: spec §6.5 lists four workers for the dispatch finder; the live source has five (`_discover_worker` at `session.py:205`). Task 11 asserts five.

# Bunch E: the #250 hang probe and the #343 export sweep

**Date:** 2026-09-06
**Issues:** #250 (CI hang -- instrumentation, not a fix; stays open until the probe has run), #343 (load-dependent export failure -- fixed)
**Status:** Implemented as written, on branch `bunch-e-hang-probe-and-export-sweep`, with the deviations listed below.
**Scope:** internal. Excluded from the docs site by `exclude_docs`. A dated record: when a later change falsifies a clause, add a `**Superseded in part:**` line here and strike the clause in place rather than rewriting it.
**Superseded in part:** the hang probe of §2.1 and its conftest hooks were removed after 1.0.0rc1 (#250 closed; see CHANGELOG, Unreleased, Removed); §2.1 describes what was built, not what exists. **Superseded in part:** #427 (v0.9.6). Two clauses of §2.1a are struck in place. The `iterations` clause's "Hard ceiling stated in the file: 22" is gone: the loop script now stops itself with a `BUDGET` row at its own 330-minute budget, whatever the inputs. The `per_iteration_minutes` clause, "The deadline after which one iteration is declared a hang. Runner suite time is 5m30-6m", is struck too, and so is its "default `15`". The suite now takes 721–836 s on a runner, and the input is now the wall-clock ceiling for a run that is still progressing (default 45), which reads as `SLOW`, not `HANG`. A hang is now called on silence, by the new `stall_minutes` input. See `.github/workflows/hang-probe.yml` and CHANGELOG's #427 entry.

**Deviations at implementation** (measured, and recorded here so the record says what was actually built):

1. §4 T1 asks that the recorded save thread be `PersistenceWorker`. `save(sync=True)` drains the worker and then runs `save_all` on the *caller's* thread, so under the fix the save lands on the exporting thread, not the worker. T1 asserts the ordered event list instead -- save returned on the exporting thread, then the sweep, then `(True, True, 0)` -- which is the property reviewer item 1 wanted checked. Measured red before the fix as `[('release_memory', 'MainThread'), ('after sweep', False, False, 1), ('save_all returned', 'PersistenceWorker')]`.
2. §2.1a interpolates `${{ inputs.selection }}` into the shell script. The free-form input reaches the script through the environment (`PROBE_SELECTION`) instead, word-split unquoted; behaviour identical, no script interpolation of free text. The numeric and choice inputs are interpolated as written.
3. §4 T6 is said to be green on both sides. On the probe's own fork arm its premise (variable unset) is false, so it skips itself with a body-level `pytest.skip` when `ISOCENTER_HANG_PROBE_START_METHOD` is `fork`, rather than being added to the workflow's `--deselect` list.
4. §2.2 P2's message says "a stored frame of {arr.size} bytes"; `arr.size` counts elements, so the message uses `len(raw)`.
5. §4 T5's raw-text trigger check also names `schedule:`.
6. §1.3's plan of a separate small PR for the workflow file was overridden by the coordinating brief: one PR carries everything, and the workflow still cannot be dispatched until that PR is merged.
7. §4 says "No `scripts/mutation_probe.py` TARGETS change". `tests/test_mutation_probe_targets.py` requires every test file that imports a target module to be listed, and `tests/test_export_flushes_before_it_sweeps.py` imports `io_handlers`; it was added to TARGETS and to CLAUDE.md's `io_handlers.py` row (the two are pinned together).
8. The probe hooks' skips are body-level `pytest.skip` calls, not `skipif` markers: `tests/test_skip_contract.py`'s visitor does not recognise the `pytest.mark.skipif` spelling, and its text scan flagged both markers as an unaccounted form.
9. The brief's line citations (`session.py:3173`, `io_handlers.py:2822-2829`) were pre-change; the new comments cite files and names, not lines.
10. §2.1a's script began `set -u`. GitHub runs a `shell: bash` step as `bash --noprofile --norc -eo pipefail {0}`, so `-e` was live and the first non-zero pytest exited the script before its row was written (reviewer measurement with a fake pytest: no `failed(rc=1)` row, no `LOCKED`, no `HANG`, no `::error::`). The script begins `set +e -u`, and T5 asserts the line is present.
11. §2.2 P2 places the guard "before the padding fallback", inside the reshape's `except`. An empty frame reshapes to `(0, 0)` without raising, so that placement never saw it; the guard runs ahead of the reshape and a second test pins the 0-byte case. The brief's justification "`rows == 0 or cols == 0` alone misses `frames == 0`" is false (`frames` enters the shape only when > 1) and is not repeated.
12. §2.1's upload step says `if-no-files-found: error`; it is `warn`, so a setup failure before the loop is one red rather than two.

What follows is the architect's implementation brief, verbatim, as it stood when the developer began. Its line numbers are against `17f0c2e` and are not maintained; `tests/test_source_citations.py` deliberately does not sweep this directory.

---

# Bunch E implementation brief: #250 (CI hang probe) and #343 (load-dependent export failure)

Worktree measured: `/Users/kevin/Developer/Isocenter/.claude/worktrees/agent-ae07c9490b0d7d4a2` at `17f0c2e` (main). Interpreter: `/Users/kevin/Developer/Isocenter/.venv/bin/python` (3.14.6, GIL on). `isocenter.__file__` was printed and confirmed to resolve to the worktree before every measurement below. All line numbers are against this tree.

---

## 1. Diagnosis with measurements

### 1.1 #343 -- the mechanism is a race inside `_export_dicom`, not session lifetime

**The fact offered by the issue is true and is not the cause.** `tests/test_wfdb_writer.py` constructs `DicomSession(":memory:")` at lines 158 and 343 and never closes either. But a never-closed session after #312/#314/#316/#318 is a session whose manager is reachable only through a weakref from `atexit` (persistence_manager.py:410), whose persistence worker and audit worker each hold a weakref to their owner (persistence_manager.py:456; persistence.py:677 and :723), and which is therefore collectable; at interpreter exit, if still referenced, `_flush_at_exit` runs `shutdown()`. None of that can remove a strongly-referenced numpy array from a live `Instance` -- garbage collection does not null dataclass fields on reachable objects, under memory pressure or otherwise. The "collected under memory pressure" hypothesis has no mechanism and was not pursued.

**What actually empties the array.** `isocenter/session.py:3173-3174`:

    self.save()
    self.release_memory()

`save()` without `sync=True` is `persistence_manager.save_async(...)` (session.py:789-822): it enqueues and returns. `release_memory()` (session.py:848-960) immediately sweeps every instance with `unload_pixel_data()`, which nulls `pixel_array` when, and only when, a `_pixel_loader` exists (entities.py:569-640, via `discard_pixel_data` at 642-680). The loader is attached by the background save (`_persist_pixels`, persistence.py:3063). So the sweep's outcome for each instance depends on which thread got there first:

- Worker loses (the idle case): no loader yet, unload refused, the resident array reaches the export worker. Green.
- Worker wins (the loaded case): loader attached, array nulled, the export worker calls `get_pixel_data()` -> `SidecarPixelLoader.__call__`.

**Why the reload comes back empty.** `io_handlers.py:2808-2829`. The test's instance is built with `instance.pixel_array = np.zeros((1, 1), ...)` -- a direct field assignment, so no Rows/Columns descriptors were ever written. The loader reads `rows = cols = 0`, computes `target_shape = (0, 0)`, `arr.reshape((0, 0))` raises, and the padding fallback (io_handlers.py:2822-2827) does this:

    target_size = 1
    for d in target_shape:
        target_size *= d
    if arr.size >= target_size:
        arr = arr[:target_size]
        arr_reshaped = arr.reshape(target_shape)

`target_size` is 0, `arr.size >= 0` is always true, `arr[:0]` is empty, and a `(0, 0)` uint8 array is returned. The integrity hash passed (it is computed over the raw bytes before the reshape). The export worker then hits `Compression failed: cannot write empty image`, zero of one instance is written, and `ExportError` is raised at session.py:3248. That is the issue's exact text.

**The waveform half of the symptom is constant noise, not part of the failure.** `WARNING ... Waveform Sequence present but no samples are available to export` comes from the DICOM export worker (io_handlers.py:2519), which writes waveform samples only via `inst.get_waveform_bytes()` (needs a loader with `read_raw`, entities.py:896-913). The test's instance holds `waveform_array` inline and has no loader, so the DATA_LOSS row is filed on every run, green or red; pytest shows captured logs only on failure, which is why the issue saw it beside the pixel error. Measured: the idle, successful export of the same graph logs the warning too (M3). `save_all` never persists a resident `waveform_array` (the only `_waveform_loader =` assignment in persistence.py is hydration, line 917), so this is a real gap, filed in section 7 A.

**The tree already met this race once and fixed the wrong half.** CHANGELOG.md:553 (#183, float16): "`session.export()`'s `save()` is asynchronous, so the unload in `release_memory()` was usually refused -- no loader had been bound yet ... Once the save won that race, the reload returned `uint16`". The dtype carrier was fixed; the ordering was left as it was.

### 1.2 Measurements (all with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<worktree>`)

| # | What | Result |
|---|---|---|
| M1 | The pair `tests/test_wfdb_writer.py tests/test_close_warns_about_unsaved_instances.py`, idle | 34 passed in 2.57s (wall 3.2s) |
| M2 | Same graph as the colocated test; `sess.save(sync=True)` then `sess.release_memory()` | pixel loader attached, `pixel_array` nulled, `get_pixel_data()` returns shape **(0, 0)** uint8. Waveform: no loader attached by the save, array stays resident, reloads (50, 8). |
| M3 | Same graph, real `export(format="dicom")` with `run_parallel` patched inline (as the test does), save left **async** | written 1, failures [] -- and the waveform warning is logged anyway (1 hit in the log) |
| M4 | Same, with `save` forced to `sync=True` | `ExportError: ... wrote 0 of 1 planned instances ... Compression failed: cannot write empty image` -- the issue's signature, deterministically |
| M5 | Direct race loop: build instance, `with DicomSession(":memory:")`, `save()`, `release_memory()`, check `pixel_array is None`; 200 iterations, **idle** | **0/200** unloaded |
| M6 | Same loop, 300 iterations, **under load** (two full suites running from the same worktree; load average 11-34 on 14 cores) | **2/300** unloaded; both reloaded as an empty array. The loop closes every session (`with`), so closing does not remove the race. |
| M7 | The test pair, 15 iterations, **under the same load** | **11 pass / 4 fail**; every failure is `test_wfdb_records_are_colocated_with_the_dicom_export_tree` with the M4 text |
| M8 | M4's forced ordering, but the instance built with `inst.set_pixel_data(np.zeros((1, 1), uint8))` | `set_pixel_data` wrote `0028,0010=1, 0028,0011=1, 0028,0002=1, 0028,0100=8`; DICOM export written 1, failures []; WFDB export 1 record |
| M9 | Both production fixes simulated by a throwaway plugin (`scratchpad/simfix_plugin.py`: sync save inside `_export_dicom`; loader raises on zero geometry) over ten files: `test_float_pixel_data_export, test_pixel_geometry_pipeline, test_pydicom_deprecations, test_redaction_failure_is_reported, test_wfdb_writer, test_structured_export, test_close_warns_about_unsaved_instances, test_export_contract, test_export_loss_audit, test_release_memory` | **179 passed, 1 failed**: the #343 test itself, now deterministic and named -- `Pixel Loader failed for 1.2.3.4.SOP: Integrity Error: ... declares no pixel geometry (Rows=0, Columns=0)` |
| M10 | Full suite under the simulated fixes | Running at brief time; result in the addendum at the end of this file. Recipe: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$W:<scratchpad> <venv>/python -u -m pytest -v -p simfix_plugin -p no:cacheprovider tests`. Expected: only the #343 test red. The plugin patches the parent process only; spawned export workers import the unpatched loader, so a test whose only reload happens inside a child is not exercised -- the developer's real P1+P2 run is the authority. |

Counts for the blast-radius argument: 12 test files assign `.pixel_array =` directly (18 sites); six of those also call `.export(`: `test_float_pixel_data_export, test_pixel_geometry_pipeline, test_pydicom_deprecations, test_redaction_failure_is_reported, test_wfdb_writer, test_structured_export`. All six are in M9.

### 1.3 #250 -- state of the evidence, and what the tree offers a loop

All 11 comments read. Occurrences 1-5: `test (3.12)` / ubuntu / **fork**, a 900s stall ending in `sqlite3.OperationalError: database is locked` from a forked child's `persist_pixel_data`; three of them killed as "cancelled" before #252's instrumentation. Two readings retracted by their author (the "session teardown" stack; the chaos-test sentinel explanation). #260 pinned spawn at every pool: `parallel.py:379` (`_run_on_recycling_pool`), `parallel.py:408` (`_run_on_new_executor`), `session.py:611` and `session.py:844` (`Session._executor` and its OOM restart) -- all four are the literal `multiprocessing.get_context("spawn")`, read through the module attribute at call time. `tests/test_parallel_contract.py::test_the_per_call_process_pool_pins_spawn` and `::test_the_shared_session_executor_pins_spawn` pin two of them. **There is no way to force fork in this tree today** -- no env var, no conftest hook.

Instrumentation already in place, all pinned by `tests/test_packaging_contract.py`: `faulthandler_timeout = 300` (pytest.ini), `_WORKER_FAULTHANDLER_TIMEOUT_S = 240` armed by `ISOCENTER_WORKER_FAULTHANDLER=1` (tests.yml sets it), `_SQLITE_BUSY_TIMEOUT_S = 120`, `tests/conftest.py`'s stall watchdog `_STALL_S = 120` re-dumping every 120s while stalled. From the issue's own retraction: pytest's faulthandler registers SIGSEGV/SIGFPE/SIGABRT/SIGBUS/SIGILL only, so a TERM signal yields no dump. The loop must signal something that does dump (section 2.1).

The 2026-09-02/03 sightings (macOS, 3.14.6, spawn, plain `main`) are a different platform, start method and interpreter from occurrences 1-5; the issue itself says it is unknown whether they are the same defect. The probe runs where the issue was opened (ubuntu/3.12); a macOS arm is an extension (section 7 F).

**Dispatching a workflow that exists only on a branch.** GitHub's docs (events-that-trigger-workflows, workflow_dispatch): *"This event will only trigger a workflow run if the workflow file exists on the default branch."*, and (manually-run-a-workflow): *"To trigger the workflow_dispatch event, your workflow must be in the default branch"*, with `--ref BRANCH` for running a default-branch-known workflow against another ref. `gh workflow list` on this repo shows only default-branch workflows. **Plan: one small PR carrying the workflow file, the two conftest hooks and the contract tests, merged first; then dispatch with `-r main -f iterations=10 -f start_method=both`.** After that merge the probe can be pointed at any branch with `-r`.

The FORCE_PROCESSES population: `pytest --collect-only -k FORCE_PROCESSES` selects **24** tests across 8 files. On the issue's Docker measurement the whole suite ran in 46-77s and never opened the window; 24 tests would run in seconds and leak none of the accumulated state (hundreds of idle `_worker`/`_audit_worker` pairs) the occurrence-four dump shows. Default is therefore the full suite; the victims-only selection is an input for fast re-probing once a hit exists.

---

## 2. Design

### 2.1 The #250 probe: `.github/workflows/hang-probe.yml`

**Trigger:** `workflow_dispatch` only. Never `pull_request`, never `push`, never `workflow_call`. `permissions: contents: read`. `concurrency: group: hang-probe-${{ github.run_id }}`, `cancel-in-progress: false` -- a probe must never be cancelled by a second dispatch; a cancelled run is exactly the "log naming nothing" this issue is about.

**Inputs (`workflow_dispatch.inputs`):**
- `iterations` -- `number`, default `10`. ~~Hard ceiling stated in the file: 22 (22 x 15 + 25 = 355 minutes, under the 360-minute job maximum).~~ (superseded by #427: no input ceiling; the loop script stops itself at its own 330-minute budget; see the front matter)
- `start_method` -- `choice`: `spawn`, `fork`, `both`; default `both`.
- `selection` -- `string`, default `tests`. Passed verbatim to pytest after `-v`; the victims preset is `tests -k FORCE_PROCESSES`.
- `per_iteration_minutes` -- `number`, ~~default `15`~~ (default 45 since #427). ~~The deadline after which one iteration is declared a hang. Runner suite time is 5m30-6m~~ (superseded by #427: now a ceiling that reads as `SLOW`, default 45; see the front matter); the old 900s stall would have fit inside 15 as well, and the current busy timeout ends a lock stall in 120s.

**Matrix:** one job `probe`, `strategy.matrix.start_method: ${{ fromJSON(inputs.start_method == 'both' && '["spawn","fork"]' || format('["{0}"]', inputs.start_method)) }}`, `fail-fast: false`. `runs-on: ubuntu-latest`, `python-version: "3.12"`, `PYTHON_GIL: "1"`. Each arm is an independent job so the two answers arrive in parallel.

**Caps:** literal step caps checkout 3, setup-python 3, tesseract 3, install 3, upload 3 (sum 15); loop step `timeout-minutes: ${{ fromJSON(inputs.iterations) * fromJSON(inputs.per_iteration_minutes) + 5 }}`; job `timeout-minutes: ${{ fromJSON(inputs.iterations) * fromJSON(inputs.per_iteration_minutes) + 25 }}`, so the job cap exceeds the sum of every step cap (loop + 5 + 15 = loop + 20 < loop + 25). Expressions are permitted in both places: the contexts reference's availability table lists `github, needs, strategy, matrix, vars, inputs` for `jobs.<job_id>.timeout-minutes` and a superset for step caps. T5 computes the inequality rather than restating it.

**Setup steps:** copy tests.yml's tesseract and install steps verbatim, comments included -- the probe must run the same suite the gate runs, including the OCR test #44 saw silently skipped.

**Loop step** (`id: loop`, `shell: bash`), env `ISOCENTER_WORKER_FAULTHANDLER=1`, `PYTHONDONTWRITEBYTECODE=1`, `ISOCENTER_HANG_PROBE_START_METHOD=${{ matrix.start_method }}`. The script is in section 2.1a below. Its contract: one iteration is one `python -u -m pytest -v` run (no `-q`, no pipe, per the issue's methodological note) started with `setsid` so it owns a process group; a poll loop watches the pid against the deadline; at the deadline the parent is sent SIGUSR1 (which `tests/conftest.py` will register with faulthandler, so every thread dumps), 45 seconds later the whole group is SIGKILLed, and the iteration is classified `HANG`; a run that exits non-zero with `database is locked` in its log is `LOCKED` (occurrence 4/5's signature); any other non-zero is `failed(rc=N)`; zero is `clean`. Every iteration appends a row (iteration, outcome, seconds, last test item to start) to `$GITHUB_STEP_SUMMARY`. `HANG` and `LOCKED` stop the loop with `::error::` and exit 1 -- the logs are the deliverable and further iterations only bury them; a plain failure is recorded and the loop continues, because a named failing test is a result, not a hang. On the fork arm the two spawn-pin tests are passed to `--deselect`; they are red by construction there and are not what the arm measures.

**Artifact:** `actions/upload-artifact@v7`, `if: always()`, `name: hang-probe-${{ matrix.start_method }}-${{ github.run_id }}`, `path: probe-logs/`, `if-no-files-found: error`.

#### 2.1a The loop script (bash; the developer transcribes this into the `run:` block)

    set -u
    mkdir -p probe-logs
    deadline=$(( ${{ inputs.per_iteration_minutes }} * 60 ))
    deselect=""
    if [ "${{ matrix.start_method }}" = "fork" ]; then
      deselect="--deselect tests/test_parallel_contract.py::test_the_per_call_process_pool_pins_spawn --deselect tests/test_parallel_contract.py::test_the_shared_session_executor_pins_spawn"
    fi
    printf '| iter | outcome | seconds | last item |\n|---|---|---|---|\n' >> "$GITHUB_STEP_SUMMARY"
    for i in $(seq 1 ${{ inputs.iterations }}); do
      log="probe-logs/${{ matrix.start_method }}-iter-$i.log"
      start=$(date +%s)
      setsid python -u -m pytest -v -p no:cacheprovider ${{ inputs.selection }} $deselect > "$log" 2>&1 &
      pid=$!
      outcome=""
      while :; do
        if ! kill -0 "$pid" 2>/dev/null; then wait "$pid"; rc=$?; break; fi
        if [ $(( $(date +%s) - start )) -ge "$deadline" ]; then
          kill -USR1 "$pid" 2>/dev/null; sleep 45
          kill -KILL -- "-$pid" 2>/dev/null; wait "$pid" 2>/dev/null
          rc=124; outcome="HANG"; break
        fi
        sleep 5
      done
      secs=$(( $(date +%s) - start ))
      last=$(grep -Eo 'tests/[^ ]+::[^ ]+' "$log" | tail -1)
      if [ -z "$outcome" ]; then
        if [ "$rc" -eq 0 ]; then outcome="clean"
        elif grep -q "database is locked" "$log"; then outcome="LOCKED"
        else outcome="failed(rc=$rc)"; fi
      fi
      printf '| %s | %s | %s | %s |\n' "$i" "$outcome" "$secs" "$last" >> "$GITHUB_STEP_SUMMARY"
      echo "iteration $i: $outcome in ${secs}s (last: $last)"
      case "$outcome" in HANG|LOCKED) echo "::error::$outcome on iteration $i"; exit 1;; esac
    done

Comments to carry into the workflow file, one per trap: why `-u -v` and no pipe; why `setsid` (the KILL must reach spawned pool children, which are in the same group); why USR1 and not TERM (pytest's faulthandler handles fatal signals only -- the issue's retraction measured this); why the 45s gap (the parent's dump plus, if a child is stuck, its own 240s watchdog has long since fired); why `LOCKED` stops the loop (it is the recorded occurrence signature, and after #260 it should be impossible under spawn).

**Two conftest hooks (`tests/conftest.py`, inside the existing `pytest_configure`, behind its existing one-configure guard):**

1. `faulthandler.register(signal.SIGUSR1, file=_stderr_file, all_threads=True, chain=False)`, guarded by `hasattr(signal, "SIGUSR1")`. It uses the fd the watchdog already duplicated while capture was suspended (the measured channel), so the dump reaches the log. Unconditional: it is diagnostics, costs nothing, and closes the "a TERM produced no dump" confusion on the issue.

2. The fork override. When `os.environ.get("ISOCENTER_HANG_PROBE_START_METHOD") == "fork"`, rebind `multiprocessing.get_context` to a wrapper that returns `multiprocessing.get_context("fork")` for `"spawn"` and delegates otherwise, and write one line to the watchdog fd saying so. All four pins read `multiprocessing.get_context` through the module attribute at pool-construction time, so the wrapper reaches `ProcessPoolExecutor(mp_context=...)`, `ctx.Pool(...)` and `Session._executor` alike. Inert when the variable is unset or any other value. **Test-only variable:** `tests/test_documented_env_vars.py` sweeps `isocenter/` only (`PACKAGE = REPO / "isocenter"`, read-site regex anchored on the four read calls inside that tree), so no `docs/environment.md` row is demanded, and none should be written -- the registry documents levers the package reads, and this one must never become one. The conftest comment says exactly that, and that if the read ever moves into `isocenter/` the sweep goes red and the row plus registry test move with it.

**Alternatives rejected.**
- *`sed`-patching the four `"spawn"` literals in the workflow before running.* Brittle (four sites, comments too), and the run then tests a tree nobody can check out.
- *A package-level env var to choose the start method.* Adds a production lever to bring back the population #260 removed, needs a registry row, and "one spelling per behaviour" argues against a second way to pick a pool.
- *Looping only the 24 FORCE_PROCESSES tests by default.* Too fast to open the window; drops the leaked-thread state the dump shows. Offered as `selection` instead.
- *`timeout(1)` around pytest.* GNU timeout signals the whole group unless `--foreground`, so SIGUSR1 would kill spawned children (no handler there) and turn a stall into a broken pool before the dump. The explicit USR1-to-parent, KILL-to-group sequence is unambiguous.
- *Putting the probe into tests.yml behind an input.* tests.yml is the PR gate and is `workflow_call`-ed by publish.yml; every edit there is an edit to the release path. A separate file is the whole point.

**Decision table the loop feeds.** At the 2026-08-31 CI hit rate (3 hits in 4 runs; 2 in 3), P(0 hits in 10 fork iterations) is about 0.25^10, negligible; 0 in 5 is about 0.001.

| spawn arm | fork arm | Verdict |
|---|---|---|
| >= 20 clean | HANG/LOCKED within 10 | **#250 closes as fixed by #260**: the population was fork and spawn does not carry it. Attach the fork arm's parent and child dumps to the issue as the first stack of the mechanism; keep the workflow as the regression probe; if the dump names a line, open a fresh, narrow issue for that line. |
| >= 20 clean | >= 10 clean | The historical population no longer reproduces even on fork. #250 closes as "mitigated by #260, not reproducible on current runners"; workflow kept; the macOS 3.14.6 sightings get their own issue (section 7 F). |
| HANG within 20 | any | New evidence and the goal of this item: a stack under spawn. #250 stays open and the diagnosis restarts from the dump, **not** from the fork hypothesis. |
| LOCKED (errors, continues) | any | Occurrence 4/5's signature under spawn: the fork hypothesis is falsified and `_SQLITE_BUSY_TIMEOUT_S = 120` did its job (a named error, not a kill). Open a targeted issue with the child dump. |
| failed(rc) on an unrelated test | -- | Not a #250 result. Note it, keep iterating; a repeat is its own issue. |

### 2.2 #343: two production changes and one test change

**P1 -- `session.py:3173`: `self.save()` becomes `self.save(sync=True)`.** The comment above it already states the intent ("Flush before the walk ... to free memory"); an asynchronous save does not flush before the walk, it flushes concurrently with it, and `release_memory()` then frees whatever the worker happened to have reached. `audit()` and `redact()` already drain the persistence manager on entry (CLAUDE.md, #297/#274); export was the third verb and did not. The cost is the one `save()`'s docstring already argues for: `sync=True` inherits `flush()`'s never-return-early property, so a wedged worker wedges the export instead of racing it. The call-site comment must record the race (which thread wins decides whether an instance is swept), cite CHANGELOG's #183 entry as the first sighting, and cite #343.

One docstring becomes false and is corrected in the same change: `tests/test_close_warns_about_unsaved_instances.py::test_an_exported_session_reaches_close_with_clean_instances` ("`export()` calls `self.save()` -- not `save(sync=True)`"). The test stays green; its prose must now say the save is synchronous and that the measurement is kept because clean-at-close is still a property of `shutdown()`'s reconciliation and the save walk's `mark_persisted`. CHANGELOG lines 529 and 553 are historical and are not edited; the new entry names them.

**P2 -- `io_handlers.py:2822-2829`: the loader refuses a zero-geometry frame.** Before the padding fallback, if `target_size == 0` raise `RuntimeError(f"Integrity Error: {uid} declares no pixel geometry (Rows={rows}, Columns={cols}, Frames={frames}); a stored frame of {arr.size} bytes cannot be reshaped to nothing")`. Same exception family and prefix as the hash mismatch six lines up, so the export worker's existing `Pixel Loader failed for ...` handling files an `ERROR` row per instance and the run grades `REVIEW_REQUIRED` rather than shipping an empty image or -- worse -- a healthy-looking `(0, 0)` array to a caller who never exports. The 1-byte DICOM pad case (`arr.size == target_size + 1`) is kept and pinned (T3). The broader surplus-truncation and the silent `return arr  # Fallback to 1D` are filed, not fixed (section 7 B). The guard must be inside `__call__` itself, because the loader pickles into spawned workers (reviewer item 3).

**T -- `tests/test_wfdb_writer.py`.** (a) Both `DicomSession(":memory:")` sites (158, 343) become `with DicomSession(":memory:") as sess:`; the `WfdbExporter._write_instance` monkeypatch in the second test moves inside the block. (b) Line 145 `instance.pixel_array = np.zeros((1, 1), dtype=np.uint8)` becomes `instance.set_pixel_data(np.zeros((1, 1), dtype=np.uint8))`, with the comment rewritten: the DICOM export saves and then sweeps, so an instance must be self-describing (Rows/Columns) to survive its own export; a direct field assignment writes no descriptors and, after P1, deterministically fails with P2's error. `waveform_array` stays inline -- the WFDB exporter reads `get_waveform_data()`, which returns the resident array (M8: 1 record). The DATA_LOSS row the DICOM export files for that hollow waveform is pre-existing and is section 7 A.

**Alternatives rejected for #343.**
- *Only close the sessions.* M6 shows the race with every session closed. Correct hygiene, zero effect on the defect.
- *Keep the async save and make `release_memory()` skip instances "in flight".* There is no such state; the worker holds no per-instance marker the sweep could read, and inventing one is a second answer to "has this been written" beside `_pixel_array_unwritten`/`_persisted_revision`.
- *`persistence_manager.flush()` after `save()` instead of `sync=True`.* Same behaviour, second spelling.
- *Make the loader return `None` for zero geometry.* `get_pixel_data()` would fall through to `file_path` (absent here) and return `None`, and the export worker's `arr is None` arm would export a non-image modality with no pixels -- silent. A raise is the only spelling the worker cannot misread.
- *Have `release_memory()` refuse instances without geometry.* Treats the symptom in the sweep while `get_pixel_data()` still returns `(0, 0)` for any caller.

---

## 3. Files to touch, in order

1. `tests/test_packaging_contract.py` -- T5 (probe workflow contract), red because the file does not exist.
2. `.github/workflows/hang-probe.yml` -- new; T5 green.
3. `tests/test_hang_probe_hooks.py` -- new; T6, T7, T8 (T7 and T8 red).
4. `tests/conftest.py` -- SIGUSR1 registration and the fork override inside `pytest_configure`; the watchdog comment block gains a paragraph on both. T7, T8 green.
5. `tests/test_pixel_geometry_pipeline.py` -- T2 (red), T3.
6. `isocenter/io_handlers.py:2820-2829` -- P2; T2 green.
7. `tests/test_export_flushes_before_it_sweeps.py` -- new; T1 (red).
8. `isocenter/session.py:3173` and its comment -- P1; T1 green.
9. `tests/test_wfdb_writer.py` -- T4 (the test edit; red-first evidence is the P1+P2 run before the edit, see T4).
10. `tests/test_close_warns_about_unsaved_instances.py` -- docstring of `test_an_exported_session_reaches_close_with_clean_instances`.
11. `CHANGELOG.md` -- `[Unreleased]`, three entries (section 5).
12. `docs/superpowers/plans/2026-09-06-bunch-e-hang-probe-and-export-sweep.md` -- this brief, dated, as the historical record (never rewritten later; `Superseded in part` front-matter lines only).

Local tier per CLAUDE.md: `session.py` -> `tests/test_session.py` plus the io_handlers list; `io_handlers.py` -> the io_handlers list (the six M9 files are the minimum); then the full suite before pushing, `python -u -m pytest -v`, no pipe. Run `tests/test_documented_env_vars.py` and `tests/test_source_citations.py` after every comment edit -- this brief's line citations go stale the moment a line is inserted above them, and comments written from it will be swept.

---

## 4. TDD test plan

**T1 -- `tests/test_export_flushes_before_it_sweeps.py::test_the_export_save_has_landed_before_release_memory_runs`.** File-backed session (`tmp_path / "sweep.db"`), one instance built with `set_pixel_data(np.full((8, 8), 7, uint8))` plus the CT_REQUIRED set from `test_close_warns_about_unsaved_instances.py` so the export plan is non-empty. Slow the worker: `monkeypatch.setattr(SqliteStore, "save_all", <wrapper that records threading.current_thread().name, sleeps 0.5s, then delegates>)`. Wrap `session.release_memory` with a function that calls through and then records `(inst._pixel_loader is not None, inst.pixel_array is None, session.persistence_manager.queue.unfinished_tasks)`. Run `session.export(out, show_progress=False)` with `run_parallel` patched inline as the colocated test does (the point is the parent's ordering, not the workers). Assert the record is `(True, True, 0)`, that the recorded save thread was `PersistenceWorker` (so the test is about the worker -- reviewer item 1), and that the export wrote 1 (the reload path was taken and produced a real image). **Red before P1**: with the async save and a 0.5s worker the sweep runs first and the record is `(False, False, 1)`. A "wedged save is visible" test is deliberately not written: `flush()` never returning is documented behaviour and a test that waits forever is not a test.

**T2 -- `tests/test_pixel_geometry_pipeline.py::test_a_stored_frame_whose_geometry_is_zero_refuses_to_load`.** Write a 1-byte frame with `SidecarManager(tmp_path / "s.bin").write_frame(b"\x07", "raw")`, build `SidecarPixelLoader(path, offset, length, "raw", instance=Instance("1.2.3.zero", SC, 1))` (no attributes), `pytest.raises(RuntimeError, match="declares no pixel geometry")`; assert the message names the UID and `Rows=0`. **Red before P2**: returns a `(0, 0)` array (M2).

**T3 -- `...::test_a_frame_with_one_byte_of_dicom_padding_still_reshapes`.** rows=cols=2, bits 8, 5-byte raw frame; loader returns shape `(2, 2)` with the pad dropped. Green on both sides; it guards that P2 is not widened to "any surplus", which would break every odd-length source.

**T4 -- `tests/test_wfdb_writer.py::test_wfdb_records_are_colocated_with_the_dicom_export_tree` (edited).** Cannot be red-first in the ordinary sense because it is load-dependent as written. Red-first evidence is M9: with P1 and P2 in place and the fixture unedited it fails deterministically with P2's error (1 failed / 179 passed across ten files). The developer reproduces that before editing: apply P1+P2, run the file, see the named failure, then change line 145 to `set_pixel_data` and add the `with` blocks, see green. Record both runs in the commit message.

**T5 -- `tests/test_packaging_contract.py::test_the_hang_probe_never_runs_on_the_gate`.** `yaml.safe_load(REPO / ".github/workflows/hang-probe.yml")`; assert `set(workflow[True].keys()) == {"workflow_dispatch"}` (PyYAML parses the bare `on` key as boolean `True`; say so in a comment or the next reader "fixes" it); assert the inputs include `iterations`, `start_method`, `selection`, `per_iteration_minutes`; assert every step has `timeout-minutes`; assert the loop step (id `loop`) and the job both carry an expression cap (`str(...).startswith("${{")`) and, parsing the `+ N` tail of each, that job constant > loop constant + sum of literal step caps. Also assert `"pull_request" not in text and "push:" not in text and "workflow_call" not in text` on the raw file, so a second trigger cannot hide in a YAML alias. **Red first**: the file does not exist -- wrap in an assertion naming the path.

**T6 -- `tests/test_hang_probe_hooks.py::test_the_fork_override_is_inert_without_its_variable`.** In-process: `monkeypatch.delenv("ISOCENTER_HANG_PROBE_START_METHOD", raising=False)`; assert `multiprocessing.get_context("spawn").get_start_method() == "spawn"`. Green on both sides; the cry-wolf guard, and a real test because of T7.

**T7 -- `...::test_the_fork_override_reaches_every_pool_pin`.** `pytest_plugins = ["pytester"]` at the top of this file only. `pytester.makeconftest((REPO / "tests" / "conftest.py").read_text())`; `pytester.makepyfile(...)` with a test asserting `multiprocessing.get_context("spawn").get_start_method() == "fork"` **and** capturing the `mp_context` handed to `concurrent.futures.ProcessPoolExecutor` by `run_parallel(lambda x: x, [1], max_workers=1)` under `ISOCENTER_FORCE_PROCESSES=1` (the capture pattern `test_the_per_call_process_pool_pins_spawn` already uses) and asserting its start method is `fork`. `monkeypatch.setenv("ISOCENTER_HANG_PROBE_START_METHOD", "fork")`, `pytester.runpytest_subprocess("-p", "no:cacheprovider")`, `result.assert_outcomes(passed=1)`. `skipif(sys.platform == "win32")`. Subprocess rather than in-process because the override installs in `pytest_configure`, which has already run for the outer session. **Red first**: the inner test fails on its first assertion.

**T8 -- `...::test_sigusr1_dumps_the_parents_threads`.** `pytester` again: an inner test that sends itself `signal.SIGUSR1` via `os.kill` then sleeps 0.5s; `runpytest_subprocess`; assert `"Current thread" in result.stderr.str()` (faulthandler's dump header) and `passed=1`. **Red first**: the default SIGUSR1 disposition terminates the process -- non-zero return and no dump.

No `scripts/mutation_probe.py` TARGETS change: `session.py` and `io_handlers.py` are already targets. The developer runs the probe by hand on the two obvious mutants (`sync=True` -> `sync=False`; the `target_size == 0` guard deleted) per CLAUDE.md's runbook and records the kills in the commit.

---

## 5. CHANGELOG guidance (`[Unreleased]`)

**Fixed -- `session.export()` completes its save before it frees memory, so which instances reach the export workers no longer depends on a thread race (#343).** State the old behaviour exactly: `_export_dicom` called `save()` (asynchronous) and then `release_memory()`; the sweep frees only instances whose loader the background save had already attached, so under CPU contention a different subset of the graph was unloaded on every run, and an instance that could not survive a reload -- `pixel_array` assigned directly, no Rows/Columns -- exported as an empty image with `Compression failed: cannot write empty image` and an `ExportError`. Quote the numbers: 0/200 idle, 2/300 and 4/15 under two concurrent full suites, deterministic with the order forced. Name #183's entry as the first sighting ("once the save won that race") and say it fixed the dtype and left the ordering. State the trade taken (`flush()`'s never-return-early, as `save()`'s docstring argues) and that `audit()`/`redact()` already drained on entry. Say plainly that closing the sessions in the test file -- the issue's offered fact -- was measured and does not change the rate, and that it was done anyway because an unclosed session leaks an executor. Do **not** say "export could lose pixels": for ingested instances the race was benign (the reload is correct); what was at stake is nondeterministic memory behaviour, and a wrong image for instances without descriptors.

**Fixed -- `SidecarPixelLoader` refuses a frame whose declared geometry is zero instead of returning an empty image with the integrity hash passing (#343).** Quote the fallback (`arr[:target_size]` with `target_size == 0`) and the returned `(0, 0)` array; state the new exception text and that it rides the export worker's existing `Pixel Loader failed` channel into an `ERROR` row and `REVIEW_REQUIRED`. State what is deliberately kept (the one-byte pad) and what is filed rather than fixed (surplus truncation, the 1-D fallback), with issue numbers once filed.

**Added -- a manual hang probe for #250: `.github/workflows/hang-probe.yml`, `workflow_dispatch` only (#250).** Instrumentation, not a fix, and the entry says so in its first sentence, the way the 0.9.2 #250 entry does. State: what it loops, the two arms and how fork is forced (`tests/conftest.py`, the test-only variable, why it is not in `docs/environment.md`), the SIGUSR1 dump and the TERM-signal fact it corrects, the budget arithmetic, the decision table, and that it must be merged before it can be dispatched (quote the docs sentence). State that the PR gate is untouched and `test_the_hang_probe_never_runs_on_the_gate` holds it so.

---

## 6. What the reviewer should attack

1. **P1 green by the slow-worker patch rather than by the ordering.** T1's `save_all` wrapper sleeps; a `sync=True` that merely waits on the wrong thing would still pass if the sleep were on the caller's thread. T1 records the thread name; check it is asserted.
2. **The clean-at-close measurement kept for the wrong reason.** `test_an_exported_session_reaches_close_with_clean_instances` stays green under P1 trivially. Its docstring must say why it is kept, or it describes a mechanism that no longer exists -- the #303/#304 defect class.
3. **P2's raise reached only in the parent.** `SidecarPixelLoader` pickles into spawned export workers; a guard in a wrapper installed at import time would not be in the child. It must be in `__call__`'s body. T2 cannot see the difference; read the diff.
4. **P2 too wide or too narrow.** `target_size == 0` and nothing else. `arr.size != target_size` breaks every odd-length source (T3 pins); `rows == 0 or cols == 0` alone misses `frames == 0` on a multi-frame declaration. Check the guard matches the message.
5. **T4 edited to pass rather than to be right.** Confirm the colocation assertions (`rel_parts`) are untouched and that `hea_paths` is 1 because the resident `waveform_array` was read, not because the WFDB exporter tolerated an empty one.
6. **T5's `on` parsing.** PyYAML yields the key as `True`; `workflow["on"]` raises `KeyError`, and a broad `assert "on" in workflow` passes vacuously. Check the raw-text trigger assertions are on the file text, not the parsed dict.
7. **The fork override installed after the first pool, or twice.** `pytest_configure` precedes collection and `Session._executor` is built in `__init__`, so ordering holds unless something constructs a session at import time -- `grep -rn "DicomSession(" tests/conftest.py` must show fixture bodies only. The install must sit behind the existing one-configure guard so `pytester`'s nested runs do not stack it.
8. **The probe's `LOCKED` classification.** `grep -q "database is locked"` also matches a passing run whose test logs the phrase as expected output. `grep -rl "database is locked" tests/*.py` lists the candidates; if any logs it on green, key on a `FAILED` block containing `sqlite3.OperationalError` instead.
9. **The job-cap arithmetic.** Expressions are permitted (verified against the contexts availability table), but the constants must satisfy job > loop + 5 + 15. T5 must parse them, not restate them.
10. **"Kept as a regression probe" is only true if someone runs it.** Nothing schedules it (correctly -- a scheduled multi-hour job is a CI cost decision). The issue-closing comment must say who dispatches it and when (before each release, minimum).
11. **Citations.** Every `path.py:N` in the new comments is swept by `tests/test_source_citations.py`; the numbers in this brief are pre-change and shift once P2 inserts lines above 2829. Cite after editing, not from this document.

---

## 7. File rather than fix

**A. A resident `waveform_array` with no loader never reaches a DICOM export, and never reaches the store.** `save_all` persists pixels (`_prepare_pixel_frames` / `_persist_pixels`) and has no waveform counterpart -- the only `_waveform_loader` assignment in `persistence.py` is hydration (line 917) -- and the DICOM export worker writes samples only from `get_waveform_bytes()`, which needs a loader with `read_raw` (io_handlers.py:2492-2519). So a graph built in memory with `waveform_array` set -- the shape `scripts/` generators and `tests/test_wfdb_writer.py` use -- exports a Waveform Sequence with no samples (a DATA_LOSS row on every run of the colocated test today) and, after a save and reopen, has no samples at all. Ingested instances are unaffected (ingest writes the frame directly, io_handlers.py:1624). Draft: "Waveform samples assigned in memory are never written by `save_all` and never exported to DICOM; the WFDB exporter reads them, the DICOM exporter and the store do not. Either `save_all` grows a `_persist_waveform` twin of `_persist_pixels` and the export worker falls back to encoding `get_waveform_data()` when no raw bytes exist (with #34's byte-exact argument scoped to the loader case), or `Instance` refuses direct assignment and offers `set_waveform_data()` that writes a frame. One spelling; decide which."

**B. `SidecarPixelLoader` tolerates any surplus and returns a 1-D array on any shortfall.** io_handlers.py:2825-2829: `arr[:target_size]` silently drops every byte past the declared geometry, not just the one DICOM pad byte, and a frame shorter than declared is returned flat with no error. Both are descriptor damage (#186/#214) reaching a caller as a healthy array; `check_pixel_geometry()` catches the raw-frame case at open only. Draft: "Bound the pad tolerance to one byte and make the shortfall a raise with the same `Integrity Error` prefix; pin both with frames of `target_size + 2` and `target_size - 1`. Consider whether `verify_readback` should also compare shape."

**C. Twelve test files construct `DicomSession(":memory:")` and never close it.** Measured: `test_feature_regression, test_ingestion_normalization, test_parallel_export, test_io_no_pixels, test_reporting_features, test_safe_export_jitter, test_scaffold_profiles, test_pixel_integrity (3), test_safe_export, test_import_validation, test_pixel_export, test_profile_end_to_end (2)` have zero `.close()` calls; none of the 30 `:memory:` sites in `tests/` uses `with`. Each leaks an idle `ProcessPoolExecutor` (workers spawn lazily) plus threads until exit. Draft: "Convert every `:memory:` session in tests/ to `with`; add a suite-level guard -- a session-scoped autouse fixture counting live `DicomSession` objects via `gc.get_objects()` at teardown -- or decide explicitly that leaked in-memory sessions are acceptable in tests and say so in conftest." Not in this bunch: 30 sites across 17 files is its own PR, and none is load-dependent.

**D. The full-suite simulation (M10).** If it shows a red beyond the #343 test, that test either assigns `pixel_array` directly and exports through the parent (fix the fixture the T4 way) or reloads inside a spawned worker (the plugin could not see it; the real P1+P2 run decides). Result in the addendum below when available.

**E. `profile_memory.py`** is a FORCE_PROCESSES reader by grep but is not collected (its own docstring says so per the #185 entry). Nothing to do; noted so the 24-test count is not questioned.

**F. The 2026-09-02/03 macOS / 3.14.6 / spawn hangs on plain `main`.** Three in a dozen runs, never with a stack, on a platform and interpreter the probe does not run. Draft: "Second arm of the hang probe on `macos-latest` with 3.14 and 3.14t, same loop, same SIGUSR1 dump; the decision table's spawn column applies. Until it runs, #250's closure should say it closes the Linux/fork population only."

---

## Addendum: M10 result (full suite under the simulated fixes)

`2 failed, 1464 passed in 381.73s`.

- `tests/test_wfdb_writer.py::test_wfdb_records_are_colocated_with_the_dicom_export_tree` -- the #343 test, red deterministically with P2's `Integrity Error ... declares no pixel geometry` text. This is the expected red and T4 resolves it.
- `tests/test_api_coherence.py::test_export_offers_one_name_per_behaviour` -- **a plugin artefact, not a finding.** The simulation wrapped `DicomSession._export_dicom` with a `(self, *args, **kwargs)` function, and that test inspects the signature for `check_burned_in`. The real P1 edits one line inside the method and leaves the signature alone, so this cannot occur in the developer's run. If it does, the developer has wrapped rather than edited.

No other test reddened, so no other fixture assigns `pixel_array` directly and exports through the parent. Section 7 D is therefore closed with no additional fixture work; the caveat stands that a reload inside a spawned worker was not exercised by the plugin, and the developer's real P1+P2 full-suite run is the authority.

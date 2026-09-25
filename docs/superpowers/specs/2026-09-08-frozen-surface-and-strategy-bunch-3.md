# The Frozen Surface and the Parallel Strategy, Bunch 3: what 1.0 promises, and which lever reaches which pool

**Date:** 2026-09-08
**Superseded in part:** #384 (2026-09-09). The clause at §3's "A print
becomes false" and its restatement in §12's Step 1 item 2 — that the
redaction banner is reworded to `Executing using {max_workers}
workers...`, with the parenthetical dropped as "a claim the line could
not keep" — no longer holds. The parenthetical returns, carrying the
strategy `_resolve_strategy` actually resolved: `(threads)` or
`(processes)`, read off the `_Strategy` the pool is built from. Both
clauses are marked in place.
**Superseded in part:** #393 (v0.9.6). "Nothing warns." in §2 (the
`ingest()` lever row) and again in the #185 discussion no longer holds:
`ingest()` now logs one `WARNING` per call that dispatches work when
`ISOCENTER_FORCE_THREADS` is set. The characterization of
`test_force_threads_does_not_reach_ingest` in §7.2 and in the PR-body
quote under Step 1 also changed: the test now asserts that warning as
well. The two sentences are struck in place, and both test passages
carry a note.
**Superseded in part:** #510 (v0.9.6). `Instance.date_shifted` is no
longer a frozen field, because it is no longer a field: it was cut, and
reading it now raises `AttributeError`. The clause in §5.3's entity
sentence that lists it inside `Instance(...)`, and §11 item 9's
restatement of the same list (including "the nine frozen `Instance`
fields"), no longer hold -- there are eight. `Study.date_shifted` is
unchanged. Both clauses are marked in place.
**Superseded in part:** 1.0.0rc2 docs round (2026-09-24). §5.2's rule,
that tier-2 names "are rendered on this site", no longer holds:
`docs/api/stability.md` now defines tier 2 as the names listed on that
page, rendered in the API reference or named by a guide where a reader
needs them, because the API pages stopped rendering the recording
helpers and the store's internals. The clause is struck in place.

**Status:** Determinations MADE, with evidence. §1–§5 are the
recommendations; §0.2 lists the calls that are the owner's, each as
options with the recommendation first. No production code was changed
(`git diff -- isocenter/` empty at the end, §11); the mutation probe ran
in a copy of the tree, never in the worktree.
**Tracking:** #381 (`:memory:` stores cannot redact on the processes
path), #365 (`parallel.py` joins `scripts/mutation_probe.py`'s
`TARGETS`), #363 (the `ISOCENTER_MAX_WORKERS` row states one default
where the code has two), #380 (a line-coverage run that counts the
export subprocess), #379 (the frozen surface #26 needs). Folds in #390
(bunch 2's finding: `ISOCENTER_FORCE_THREADS` does not reach
`Session.ingest()`). Rests on #26 (the v1.0.0 freeze; the owner's
comment of 2026-09-08 — "the facade is what gets frozen, and the
internal seams behind it are not covered by the freeze" — is the
tiebreaker §5 applies), #368 (the two behaviours §5.6 records), #185
(export keeps processes by decision), #341/#335/#333 (the worker-count
rows), #234 (why a signature pin does not live in
`tests/test_api_coherence.py`), #344 (how an equivalent mutant is
marked).
**Supersedes in part:** nothing. Every earlier dated spec was grepped
for the claims this bunch measures; none was falsified. The sidecar
spec (`2026-09-08-sidecar-concurrency-contract.md` §0.2 E) *predicted*
#381 and is confirmed; the export-fidelity spec
(`2026-09-08-export-fidelity-bunch-2.md` §10, amendment 1) already
records #390. Two **issue** claims are overturned (§6), which is not
the same thing and is marked there, not in a front-matter line.
**Base:** `main` at `05d69e8` (the bunch-2 merge, PR #391). Every
`path.py:N` below is that commit. Nothing grades these line numbers
(`tests/test_source_citations.py` excludes `docs/superpowers/`); the
tests in §7 carry the content-pins instead.
**Measured with:** three interpreters, because the five issues are
about which interpreter takes which path. `/Users/kevin/Developer/Isocenter/.venv/bin/python`
is CPython **3.14.6, GIL build** (not 3.12, whatever its directory name
suggests — printed and checked). The `python_requires` floor,
**3.12.13**, and the free-threaded **3.14.7t** were built from pyenv into
two throwaway venvs under the session scratchpad (`venv312`, `venv314t`,
each `pip install -e ".[tests]"` from the worktree; `venv312` also
carries coverage 7.16.0, the project venv coverage 7.15.4 — present
there only as a dependency of `mutmut`, §4.5). Every probe and every
pytest ran as
`env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<worktree> <python> -u ...`
in worktree
`/Users/kevin/Developer/Isocenter/.claude/worktrees/agent-a35d1a6596ad906a2`
— or, for the mutation probe and the coverage run, in a `cp -R` copy of
it with `.git` removed and `.venv` symlinked (`probe_tree/`,
`probe_tree2/`; `diff -rq` against `isocenter/` and `tests/` clean),
because the probe writes mutants into `isocenter/parallel.py` and the
coverage run needed a cwd the uninstrumented suite was not using — and
printed `isocenter.__file__` **first**; it resolved to that tree's
`isocenter/__init__.py` every time. Probes that spawn workers
are real files with `if __name__ == "__main__":` (`probe_381.py`,
`probe_390.py`, `probe_pool.py`, `run_pool_variants.py`,
`run_export_variants.py`, `sigs.py`, `shapes.py`, `cov_funcs.py`), in
the scratchpad and **not in the repo**; they are named so numbers can
be attributed. The probe's DICOM fixture (`fixtures.py`) writes three
valid CT files — valid because the first version was not, §11 — with
`DeviceSerialNumber = "SN_PROBE"`, which is what the redaction rule
keys on.

---

## 0. Determinations, and what is the owner's to decide

### 0.1 One line each

1. **#381 — only `redact()` is affected, and the fix is one keyword
   argument at one call.** Measured on 3.12.13 (processes by default):
   `ingest()`, `audit()`, `save()` and `export()` on a `:memory:` session
   all succeed; `redact()` raises `RedactionError` wrapping
   `sqlite3.OperationalError: no such table: instance_blobs`. The worker
   is the only one of the four that *writes to the store* from inside
   the child (`services.py:487`, `self.store_backend.persist_pixel_data`),
   so it is the only one that meets `__setstate__`'s empty `:memory:`
   database. Fix: `force_threads=self.store_backend.db_path == ":memory:"`
   on the `run_parallel(` call at `session.py:2876`, per #390 the
   *argument*, not the environment variable. Three residuals named
   (§1.4); the recycling corner is the owner's (Q1).
2. **#390/#363 — `ISOCENTER_FORCE_THREADS` reaches `audit()`,
   `scan_pixel_content()` and `redact()`; it reaches neither `export()`
   nor `ingest()`, for two different reasons.** Measured per verb on
   3.14.6 with a file-backed store (§2.1). `export()` is #185's decision
   (`maxtasksperchild=25` beats every lever, with the warning). `ingest()`
   is different in kind: the variable is **read and then ignored** — the
   strategy resolves `use_threads=True` and `_run_on_shared_executor`
   uses the session's own `ProcessPoolExecutor` regardless, because a
   caller-supplied `executor` beats every lever silently. ~~Nothing warns.~~
   (Superseded by #393, v0.9.6: `ingest()` now logs one `WARNING` per
   call that dispatches work when the variable is set.)
   `discover_redaction_zones()` is threads on every interpreter
   (`force_threads=True`, `session.py:1985`).
3. **#363 — two defaults, two owners, and the row conflated them.**
   `_resolve_strategy` computes one-per-CPU for `run_parallel`;
   `_redaction_worker_count` (`session.py:497`) computes
   `max(1, min((os.cpu_count() or 1) // 2, 8))` for `redact()` because
   each redaction worker holds a decoded frame — a memory ceiling, its
   docstring says, after one-per-CPU exhausted memory on large studies.
   The row's "the only place it is computed" is true of the one-per-CPU
   *expression* and false of "the default". Row rewritten (§2.3); sibling
   test with **fixed CPU counts**, because on this 14-CPU box the cap is
   never reached and a `8 → 16` mutant would survive by hardware (§7.3).
4. **#365 — `parallel.py` has 68 mutation sites; a full pass is
   affordable, so the budget gives stride 1 with headroom.** The
   issue's three test files are **not enough**:
   `tests/test_mutation_probe_targets.py::test_every_test_that_imports_a_target_module_is_listed`
   demands every test file whose text matches `isocenter\.parallel\b`,
   and that set is `test_logging.py`, `test_parallel_config.py`,
   `test_parallel_contract.py`, `test_shared_executor_lifecycle.py`
   (`test_redaction_worker_count.py` does *not* match — it writes
   `isocenter/parallel.py` — but the issue is right to want it, so it is
   listed too). Five files, 64 tests, 8.2 s a run under load. Twelve
   survivors at stride 1 against the issue's three files (§3.2): **ten
   are test gaps**, closed by seven specified tests (§7.4; two tests kill
   more than one), and **two are equivalent mutants**, marked as #344
   marks its one. The five-file run reproduces the **same twelve** survivors (the two extra files kill nothing new) and turns up one thing the three-file run did not: the line-259 mutant (`if maxtasksperchild is None` → `is not None`) **hangs** a test in the five-file set for the probe's whole 900 s timeout and is reported `skipped`, where the three files killed it in 7 s (§3.3).
5. **#380 — the run works, the numbers are in §4.3, and the one
   load-bearing line is `core = ctrace`.** `concurrency = multiprocessing`
   reaches both spawn paths (the shared `ProcessPoolExecutor` and the
   recycling `multiprocessing.Pool`); `sigterm = True` is what saves the
   recycling pool's data, because `with ctx.Pool(...)` exits through
   `terminate()`; and on 3.14 coverage's default `sys.monitoring` core
   self-deadlocks under that SIGTERM (§4.2, one worker in ~460), which
   `core = ctrace` avoids. `patch = subprocess` double-instruments every
   child and is left out. `coverage` joins the **`dev`** extra, not
   `tests` (§4.5). Not CI, no threshold.
6. **#379 — one list, three tiers, and the tiebreaker is the owner's
   own sentence.** Tier 1 (frozen at 1.0) is the `__all__` five plus
   `__version__`, the 28 public `Session` methods with their parameter
   names, three `Session` attributes, the shapes the frozen methods
   return, the two exceptions and their attributes, the environment
   registry, and the output vocabularies (§5.3). Tier 2 is everything the
   site renders that is a seam behind the facade — `SqliteStore` and
   `store_backend`, `TrackedEntity` bookkeeping, `DicomExporter.write_tree`,
   the exporter registry, the OCR page classes (§5.4). Tier 3 is the
   rest (§5.5). Pinned by a new `tests/test_frozen_surface.py` (§7.5),
   not by `tests/test_api_coherence.py` as the issue asks, for the
   reason `tests/test_documented_api_exists.py`'s docstring gives (Q6).
   The API reference renders **16 of the 28** frozen methods; the twelve
   it omits include six the README and quickstart teach (§5.7).
7. **Two bunch-1 behaviours recorded as contract** (§5.6): `compact()`
   raises `RuntimeError` and has done nothing while a `redact()` or
   `ingest()` pass is open; a pass waits, bounded, behind a compaction
   and then proceeds. Class and "has done nothing" are frozen; the
   message text and the lock-file names are not.

### 0.2 Open questions for the owner (options; recommendation first)

**Q1. #381's recycling corner.** With `ISOCENTER_MAX_TASKS_PER_CHILD`
set, `_use_threads` overrides `force_threads=True` (that is #185's
precedence, and it is right), so a `:memory:` `redact()` still fails
with the same `no such table` after the fix — measured (§1.1, row 3).
- **(a) Recommended: document it, in the `ISOCENTER_MAX_TASKS_PER_CHILD`
  row and the `:memory:` sentence, and stop.** The corner needs two
  deliberate choices at once (an in-memory store *and* worker
  recycling, whose whole point is reclaiming memory over a long run),
  and the failure is loud: `RedactionError`, first line naming the
  table. A refusal would be a third branch in `_apply_redaction_rules`
  for a configuration nobody has asked for.
- (b) Refuse up front: when `db_path == ":memory:"` and
  `_env_int("ISOCENTER_MAX_TASKS_PER_CHILD", minimum=1) is not None`,
  raise **`ValueError("redact() on a :memory: store runs in threads, and
  ISOCENTER_MAX_TASKS_PER_CHILD asks for worker recycling, which only
  processes implement; unset it or use a file-backed store")`** before
  any task is queued. Not `RedactionError` — its constructor is
  `(failures, attempted)` and it means "something unsafe is still in the
  graph", which is false here (nothing ran). `ValueError` is what
  `generate_report` raises for an unknown format, so it is the class the
  facade already uses for a caller's contradictory request.
- (c) Make `force_threads` beat recycling for `:memory:` only. Rejected:
  it puts a store-type branch inside `_use_threads`, whose docstring
  says it is "the whole of the precedence".

**Q2. Is `":memory:"` itself a frozen spelling?** It is accepted by
`SqliteStore.__init__` (`persistence.py:687`, the docstring names it),
used by 29 test files, and named nowhere in README, `docs/` or
`Session.__init__`'s docstring.
- **(a) Recommended: tier 1, and document it in one sentence in
  `Session.__init__`'s docstring and the `ISOCENTER_DB_PATH` row** —
  the #381 fix is precisely a promise about it, and a promise the tests
  lean on 29 times should be one users can read.
- (b) Tier 2: keep it working, leave it undocumented. Then the #381 fix
  is a fix to an internal, and the `docs/environment.md` sentence the
  issue asks for has nowhere honest to sit.

**Q3. `DicomSession`, the class's own name.** `isocenter.Session` is
the documented spelling (`from .session import DicomSession as
Session`, `__init__.py:25`); tests import `DicomSession` 93 times and
`Session` 11 times; it is what `repr()`, tracebacks and mkdocstrings
headings show.
- **(a) Recommended: tier 2** — importable, unrenamed for 1.x by
  courtesy (renaming it would move every heading on the API reference),
  but the freeze is on `isocenter.Session`. "One spelling per
  behaviour" (CLAUDE.md) says a second frozen spelling is the thing this
  project deletes, and the tests are the project's own to migrate.
- (b) Tier 1 both names. Costs nothing today; forecloses the rename.

**Q4. `Builder` and `Equipment` are in `__all__` and taught nowhere.**
`grep Builder README.md docs/*.md` is empty. `DicomBuilder`'s public
surface is `start_patient()` and a fluent chain; `Equipment` is
exported "for type hinting" (`__init__.py`), and `Equipment.from_parts`
is pinned by two `tests/test_api_coherence.py` tests already.
- **(a) Recommended: tier 1 for the names and for `Equipment`'s three
  fields (`manufacturer`, `model_name`, `device_serial_number`) and
  `Builder.start_patient`; tier 2 for the rest of the fluent chain.**
  `__all__` is a statement of intent the project already made; the tag
  is not the moment to unmake it.
- (b) Drop `Builder` from `__all__` before the tag (a pre-1.0 deletion,
  allowed by the convention). Then `scripts/generate_*.py` are its only
  callers and it is tier 2 with `write_tree`.
- (c) Tier 1 for the whole fluent chain — which nobody has written down,
  so the freeze would be of an enumeration made for the purpose.

**Q5. `SqliteStore.get_flattened_instances` — a prior determination
points the other way.** `tests/test_api_coherence.py:577`'s docstring
says "this is the surface #26 will freeze" (#142). The owner's
2026-09-08 comment on #26 says the internal seams are not covered.
- **(a) Recommended: tier 2, on the owner's own words**, with that
  docstring reworded to "#26 ruled this documented-but-internal (#379)".
  The 0.9.1 CHANGELOG names it as the migration path from
  `export_to_parquet`, so it stays rendered and stays pinned by the
  existing test — a change goes through a red test and a CHANGELOG
  entry, which is exactly tier 2's rule.
- (b) Tier 1 for this one method, as the docstring promised. Then
  `page_size` is frozen too, which the same docstring calls "#26's
  call".

**Q6. Where the frozen-surface test lives.** #379 says "a test in
`tests/test_api_coherence.py`".
- **(a) Recommended: a new `tests/test_frozen_surface.py`.**
  `test_api_coherence.py` is under `io_handlers.py` in `TARGETS`, so
  every test in it runs against every io_handlers mutant; a pure
  `inspect.signature` pin buys zero kill signal there and costs on every
  mutant. `tests/test_documented_api_exists.py`'s module docstring
  records exactly this reasoning for #234, and the new file, like that
  one, imports no target module and needs no `TARGETS` entry.
- (b) In `test_api_coherence.py` as asked, accepting the cost.

**Q7. `lock_identities`'s signature has a private-named parameter and
an open `**kwargs`**: `(self, patient_id, persist=False,
_patient_obj=None, verbose=True, **kwargs)`. Pinning the parameter
names as they stand freezes `_patient_obj` by name.
- **(a) Recommended: freeze it as it is, and file the cleanup as its
  own issue.** The freeze test exists to make an API change a red test;
  it should not be the vehicle of one. The docstring already calls
  `_patient_obj` an optimisation argument, which is the tell that it
  wants to be keyword-only or gone.
- (b) Strip `_patient_obj` (make it a private helper's argument) and
  close `**kwargs` before the tag, then pin. A behaviour change inside
  a docs-and-tests PR; the developer would have to measure what passes
  through `**kwargs` to `lock_identities_batch` today.

**Q8. Rendered names not on the frozen list.** §5.7's rule is: a name
the site renders that is not tier 1 is tier 2 *by construction*, and
the page says so in one sentence at its top. The alternative — filter
`docs/api/persistence.md` and `docs/api/entities.md` down to the tier-1
names with a `members:` list — hides the seams from the readers who
need them most (the 0.9.1 CHANGELOG sends `export_to_parquet` callers to
`get_flattened_instances`). **Recommended: the sentence, not the
filter.**

**Q9. The output vocabularies.** Audit `action_type` (DATA_LOSS, ERROR,
EXPORT, RECONCILE_PRIVATE, REDACTION, REMOVE_TAG, REPLACE_TAG,
REVERSIBLE_EXPORT, RISK, SCAN_GAP, SHIFT_DATE, WARNING,
COMPLIANCE_CHECK), `loss_scope` (STANDARD, PRIVATE, SIGNAL) and the
grade (PASS, REVIEW_REQUIRED; there is no FAIL) reach users through
tier-1 outputs — the report, `get_cohort_report()` — but the *method*
that returns the rows (`store_backend.get_audit_losses()`) is tier 2.
- **(a) Recommended: the vocabularies are tier 1 (an existing string
  is never renamed or removed in 1.x; new strings may be added with a
  CHANGELOG entry); the access path stays tier 2.** A user who filters
  a report on `DATA_LOSS` must not find it spelled differently at 1.3.
- (b) Tier 2 throughout. Then the report is frozen but not what it
  says.

---

## 1. #381 — `:memory:` stores and the processes path

### 1.1 Measured

`probe_381.py`: `Session(":memory:")`, ingest three CT files, `audit()`,
`save(sync=True)`, `redact_by_machine("SN_PROBE", [0, 10, 0, 10])` then
`redact()`, `export(tmp)`. Dispatch path recorded by wrapping the three
`parallel._run_on_*` functions; the store's `db_path` read back from the
session. `python_requires` floor first, because processes are its
default.

| # | Interpreter | Levers | `ingest()` | `audit()` | `redact()` | `export()` |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 3.12.13 | none (processes) | OK, 3 ingested, shared executor | OK, new executor, processes | **`RedactionError`: `Redaction failed for 3 of 3 instances ... First: <uid>: no such table: instance_blobs`**; new executor, processes | OK, 3 files, recycling pool |
| 2 | 3.12.13 | `ISOCENTER_FORCE_THREADS=1` | OK (shared executor, still processes) | OK, threads | **OK, 3 redacted**, threads | OK, 3 files, processes (the #185 warning) |
| 3 | 3.12.13 | `ISOCENTER_FORCE_THREADS=1` + `ISOCENTER_MAX_TASKS_PER_CHILD=5` | OK | OK, recycling pool (warning fires) | **same `RedactionError`**, recycling pool | OK |
| 4 | 3.14.7t | none (threads) | OK | OK, threads | OK, threads | OK, processes |
| 5 | 3.14.7t | `ISOCENTER_FORCE_PROCESSES=1` | OK | OK, processes | **same `RedactionError`** | OK |

The traceback under the `RedactionError` in rows 1, 3 and 5 is the
same: `services.py:487` (`execute_redaction_task` →
`self.store_backend.persist_pixel_data(inst)`) → `persistence.py:2780`
(`_swap_pixels_under_gate`) → `:2853` → `record_blob_ref` at `:2676`,
`sqlite3.OperationalError: no such table: instance_blobs`.

**Why the other three verbs are unaffected**, measured rather than
assumed: `ingest_worker` (`io_handlers.py:1365`) reads a file and
returns a result the *parent* persists; `_export_instance_worker`
(`io_handlers.py:2726`) reads pixel bytes through the sidecar loader —
and a `:memory:` store's sidecar is a real temporary file
(`persistence.py:691-716`) — and writes a file; `scan_worker` reads a
lightweight copy. Only `execute_redaction_task` carries the store into
the child *and writes to it*. `SqliteStore.__getstate__/__setstate__`
(`persistence.py:785-830`) hand the child `_memory_conn = None`
(`:821`, "Connection lost on pickle transfer") and `_get_connection`
(`:970`) then opens a fresh, empty `:memory:` database with no
`instance_blobs` table. The 2026-09-08 sidecar spec §0.2 E predicted
exactly this and is confirmed.

### 1.2 Determination

**Force threads for `redact()` on a `:memory:` store, through the
argument.** At `session.py:2876`, the `run_parallel(` call in
`_apply_redaction_rules` gains

```python
force_threads=self.store_backend.db_path == ":memory:",
```

`db_path` is set unconditionally at `persistence.py:689`. Threads share
the parent's `_memory_conn`, so the worker's `persist_pixel_data` writes
to the one database that exists. Row 2 above is this fix measured
through the environment; the argument takes the same branch in
`_use_threads` (`parallel.py:327`, `if force_threads or _env_is(...)`).

**Why the argument and not the environment variable (#390):** the
variable is process-global — set from inside `redact()` it would leak
into every later `run_parallel` in the process — and, as §2 measures,
it does not reach every pool anyway. The argument is per call and is
the mechanism `discover_redaction_zones()` already uses
(`session.py:1985`).

**A print becomes false.** `session.py:2833` prints
`Executing using {max_workers} workers (Process Isolation)...`. It is
already false on 3.14t (threads by default) and becomes false on every
interpreter for `:memory:`. ~~Reword to
`Executing using {max_workers} workers...` — the parenthetical was a
claim the line could not keep.~~ Developer step; no test pins the string.

> **Superseded in part by #384 (2026-09-09).** The struck reword landed
> in 0.9.4 and converted a lie into a *silence*: measured across seven
> store-and-lever combinations on two interpreters, three dispatch paths
> printed that one identical sentence. The line now reads
> `Executing using {max_workers} workers (threads)...` or
> `(processes)`, taken from the resolved `_Strategy` rather than derived
> at the print site. A test pins the string now, whole-line.

**Documentation:** one sentence in the `ISOCENTER_FORCE_PROCESSES` row
(§2.3) — "`redact()` on a `:memory:` store runs in threads on every
interpreter, this variable notwithstanding: the worker writes redacted
frames to the store, and a process cannot share an in-memory database"
— and, if Q2(a), one in `Session.__init__`'s docstring.

### 1.3 Rejected alternatives

- **Refuse `redact()` outright on `:memory:` + processes, naming the
  workaround.** Rejected: the workaround *is* one keyword argument the
  session can pass itself; making the user set an environment variable
  to get behaviour the library can choose is the #185 anti-pattern in
  reverse.
- **Raise in `SqliteStore.__getstate__` for `:memory:`.** Rejected,
  measured: `tests/test_persistence.py` and others pickle stores
  directly, and a pickling error raised from `multiprocessing.Pool`'s
  task handler thread does not surface as a task failure — the
  `imap_unordered` consumer waits on a result that never comes. The
  probe's recycling variant of that mutation hung until killed.
- **Document only.** Rejected by the issue and by measurement: this is
  a front-door failure on the floor interpreter with the default
  configuration, and the fix is one line.
- **Reconnect the child to the parent's database** (shared-cache URI,
  `file::memory:?cache=shared`). Rejected: shared cache does not cross a
  process boundary; that is what "in-memory" means.

### 1.4 Residuals, named

1. **The recycling corner** (row 3): `ISOCENTER_MAX_TASKS_PER_CHILD`
   set → `_use_threads` overrides `force_threads=True` (with #185's
   warning, whose text names the right remedy: unset it) → the same
   `RedactionError`. Owner's Q1; recommendation is to document.
2. **`ISOCENTER_FORCE_PROCESSES=1`** is beaten by `force_threads=True`
   (`parallel.py:327` before `:329`), so it does *not* reopen the
   failure. Measured for the file store in §2.1; the strategy test
   (§7.1) asserts it for `:memory:`.
3. **A `:memory:` store's sidecar is still a temp file** that the store
   owns and unlinks on `stop()` since #376 — unchanged here.

---

## 2. #390 and #363 — which lever reaches which pool, and the two defaults

### 2.1 Measured: dispatch path per verb

`probe_390.py`, file-backed store, 3.14.6 GIL build (the interpreter
whose default is processes, so a lever that works is visible). Each
cell is the `parallel._run_on_*` function that ran and the strategy's
`use_threads`.

| Verb | Call site (`session.py`) | unset | `ISOCENTER_FORCE_THREADS=1` |
| --- | --- | --- | --- |
| `ingest()` | `:1433`, `executor=self._executor` | shared executor, **processes** | shared executor, **processes** — strategy says `use_threads=True`, path ignores it |
| `audit()` | `:1761` | new executor, processes | new executor, **threads** |
| `scan_pixel_content()` | `:1901` | new executor, processes | new executor, **threads** |
| `discover_redaction_zones()` | `:1985-1989`, `force_threads=True` | new executor, **threads** | threads |
| `redact()` | `:2876` | new executor, processes | new executor, **threads** |
| `export()` | `:3810`, `maxtasksperchild=25` | recycling pool, processes | recycling pool, **processes**, #185 warning |

So `ISOCENTER_FORCE_THREADS` **reaches `audit()`, `scan_pixel_content()`
and `redact()`**. It reaches neither `export()` nor `ingest()`, and the
two are different facts:

- `export()` is a **decision with a warning** (#185): recycling needs a
  process, the row already says so.
- `ingest()` is a **silent ignore**: `_run_on_shared_executor`
  (`parallel.py`, the `executor is not None` path) calls
  `executor.map(...)` on whatever it was handed and consults no lever;
  `_resolve_strategy` still runs and still resolves `use_threads=True`,
  which nothing reads. ~~Nothing warns.~~ (Superseded by #393, v0.9.6:
  `import_files` reads that strategy's `threads_requested_by` and warns.)
  The #185 warning's text —
  "elsewhere, unset ISOCENTER_MAX_TASKS_PER_CHILD to get threads"
  (`parallel.py:322`) — is one word too broad: "elsewhere" includes
  `ingest()`, where unsetting anything gets no threads. Production
  text, developer step (§10, step 2).

Whether `ingest()` *should* honour the lever is out of scope: the
shared executor exists so ingest does not pay a pool start-up per call
(`tests/test_shared_executor_lifecycle.py`), and a thread pool there is
a design change, not a documentation fix. The row says what is.

### 2.2 The two worker-count defaults

| Path | Where | Expression | Why |
| --- | --- | --- | --- |
| `run_parallel` (audit, scan, discover, export, ingest's executor) | `_resolve_strategy`, `parallel.py` | `os.cpu_count() or 1` | one per CPU; the 1.5× an earlier version used was abandoned on purpose (#333) |
| `redact()` | `_redaction_worker_count`, `session.py:497-520` | `max(1, min((os.cpu_count() or 1) // 2, 8))` | each worker holds a decoded frame; one-per-CPU "has exhausted memory on large studies" (its docstring); `max(1, …)` because `1 // 2 == 0` |

Both read `ISOCENTER_MAX_WORKERS` through `_env_int(..., minimum=1)`
(#341), so the override and its floor are one behaviour; only the
default differs. On this box (14 CPUs) the redaction default is 7 — the
cap is not reached, which is why the sibling test must set the CPU
count itself (§7.3).

### 2.3 The `docs/environment.md` rows, rewritten

Replace the **`ISOCENTER_MAX_WORKERS`** row's second and third
sentences ("The default is **one per CPU** … is the only place it is
computed.") with:

> There are **two defaults, one per pool**. `run_parallel()`'s is **one
> per CPU** (`1` when `os.cpu_count()` cannot answer); `_resolve_strategy`
> in `parallel.py` owns that expression and is the only place *it* is
> computed. **`redact()`'s is half the CPUs, capped at eight, never below
> one** — `max(1, min((os.cpu_count() or 1) // 2, 8))` in
> `_redaction_worker_count` (`session.py`) — because each redaction
> worker holds a decoded frame, so that number is a memory ceiling, not
> a throughput choice: one-per-CPU exhausted memory on large studies.
> This variable overrides both, with the same floor.
> `tests/test_parallel_contract.py` holds the first number still and
> `tests/test_redaction_worker_count.py` the second, at fixed CPU counts
> so the cap is exercised on any box.

Leave the rest of that row (the 1.5× history, the below-1 handling, the
#341 sentence) as it is; the last sentence already says `redact()`
reads the variable.

Replace the **`ISOCENTER_FORCE_THREADS`** row's first two sentences
onward with:

> Set to `1` to run in threads instead of processes. Useful for
> debugging, coverage, or environments without `fork`. **It reaches
> `audit()`, `scan_pixel_content()` and `redact()`. It reaches neither
> `export()` nor `ingest()`, for different reasons.** `export()` passes
> `maxtasksperchild=25` and therefore runs in processes on every
> interpreter, free-threaded builds included — a decision, not an
> oversight: recycling a worker every 25 tasks reclaims memory leaked by
> the imaging C libraries, and a thread pool has no process to recycle
> (#185); the request is reported with a warning naming both levers.
> `ingest()` runs on the session's own `ProcessPoolExecutor`, handed to
> `run_parallel()` as `executor=`, and a caller-supplied executor is
> used as given: **the variable is read and ignored there, and nothing
> warns** (#390). `discover_redaction_zones()` runs in threads
> regardless (it asks for them itself). So `export()` and `ingest()`
> are the two paths you cannot debug, breakpoint or coverage-measure in
> the calling process — see `.coveragerc` for how the spawned workers
> are measured instead (#380).

Add to the **`ISOCENTER_FORCE_PROCESSES`** row, after "This pins
processes anyway.":

> **Except `redact()` on a `:memory:` store**, which runs in threads on
> every interpreter, this variable notwithstanding: the redaction worker
> writes redacted frames back to the store, and a process cannot share
> an in-memory database (#381).

Add to the **`ISOCENTER_MAX_TASKS_PER_CHILD`** row, after "see the
ordering note in the `ISOCENTER_FORCE_PROCESSES` row below.":

> That includes the threads `redact()` asks for on a `:memory:` store
> (#381): with this set, that call runs in processes and fails with
> `RedactionError` naming `no such table: instance_blobs`. Unset it, or
> use a file-backed store. (Q1(b) would replace this sentence with the
> refusal it specifies.)

`tests/test_documented_env_vars.py` reads only the `**\`NAME\`**` cells
(`_REGISTRY_ROW`), so no row is added or removed and it stays green;
`tests/test_doc_anchors.py` renders the page, so the backticks must
balance.

---

## 3. #365 — `parallel.py` in `TARGETS`

### 3.1 The files, and the budget

`scripts/mutation_probe.py` counts **68** mutation sites in
`isocenter/parallel.py` (the `### isocenter/parallel.py (68 mutation
sites, ...)` header of each run). Stride is `max(1, total // budget)`
(`mutation_probe.py:447`), so any budget ≥ 68 is a full pass;
**80** is recommended — stride 1 with room for a dozen more sites before
the probe starts skipping, and cheap: 68 runs × 8.2 s ≈ **9.3 min**
under load (§3.3 has the measured wall time), against 5.8 s a run for
the issue's three files uncontended. That is affordable here for the
reason it is not for `io_handlers.py` (~5× the sites, and every test in
its list touches a pool).

The `TARGETS` entry (insert **first**, before `"isocenter/crypto.py"`;
the CLAUDE.md table below gets its row first too, so the two stay in one
order — `test_the_claude_md_mapping_matches_targets` looks rows up by
name and does not care, but a reader comparing the two lists does):

```python
    "isocenter/parallel.py": (["tests/test_logging.py",
                               "tests/test_parallel_config.py",
                               "tests/test_parallel_contract.py",
                               "tests/test_redaction_worker_count.py",
                               "tests/test_shared_executor_lifecycle.py"], 80),
```

and the CLAUDE.md row, which
`tests/test_mutation_probe_targets.py::test_the_claude_md_mapping_matches_targets`
requires to list the same files:

```
| `parallel.py` | `test_logging.py`, `test_parallel_config.py`, `test_parallel_contract.py`, `test_redaction_worker_count.py`, `test_shared_executor_lifecycle.py` |
```

**Why five and not the issue's three.**
`test_every_test_that_imports_a_target_module_is_listed` builds the
required set from file *text* with `isocenter\.parallel\b`
(`test_mutation_probe_targets.py:30-38`). Measured against the tree:
`test_logging.py` (`import isocenter.parallel` to patch `tqdm`, line
41), `test_parallel_config.py`, `test_parallel_contract.py`,
`test_shared_executor_lifecycle.py` (`from isocenter.parallel import
run_parallel`, line 5). With the issue's three the test is **red** on
`test_logging.py` and `test_shared_executor_lifecycle.py`.
`test_redaction_worker_count.py` is not demanded (its text says
`isocenter/parallel.py`, with a slash) and is listed because the issue
is right that it tests this module's `_env_int` through `redact()`'s
read. The #381 strategy test (§7.1) patches `isocenter.session`'s name
for `run_parallel`, not `isocenter.parallel`, so it is not demanded and
not listed; if the developer spies on `isocenter.parallel` instead, it
must be added to both lists or the completeness test goes red.

### 3.2 Survivors at stride 1, and what each is

First run: budget 68, the issue's three files, in a copy of the tree
(never the worktree), under a concurrent coverage run: **killed 56/68,
survived 12/68**, ~7–8 s a mutant. Second run against the five files:
**killed 55/67, survived 12/67, 1 skipped** — the same twelve lines (§11), so `test_logging.py` and `test_shared_executor_lifecycle.py` add kill signal for nothing the three files missed; they are listed because the completeness test demands them, not because they earn it. The denominator is 67 because one mutant, **line 259 `is` → `is not`**, did not finish: the probe's 900 s pytest timeout (`mutation_probe.py:407`) expired and it was reported `skipped   line 259: Is -> IsNot: TimeoutExpired`. The three-file run killed the same mutant in 7 s. See §3.3.

| Line | Mutation | Verdict | Why, and what kills it |
| --- | --- | --- | --- |
| 37 | `_worker_init(disable_gc=False, …)` → `True` | **equivalent** | Every caller is the `functools.partial` built at `parallel.py:80`, which passes both keywords; the default is never read. Mark in CHANGELOG as #344 marks `_value_fits_vr`'s guard (§10 step 4). |
| 55 | `gc.disable()` deleted | **test gap** | Nothing runs the initializer: `_assert_disables_gc` asserts the partial's shape on purpose (calling it would disable the test process's collector). §7.4 T2 spawns one worker and reads `gc.isenabled()` there. |
| 61 | `resolve_worker_initializer(disable_gc=False)` → `True` | **test gap** | `Session.__init__` calls it bare (`session.py:624`, `:853`), so the mutant disables GC in every shared-executor worker and no test looks at that executor's initializer. §7.4 T4. |
| 75 | `disable_gc or _env_is(…)` → `and` (in `resolve_worker_initializer`) | **test gap** | Survives because `_resolve_strategy:269` already folded the env var in, so both operands are true in every existing test. The argument-only path (`disable_gc=True`, env unset) tells them apart. §7.4 T1. |
| 128 | `@dataclass(frozen=True)` → `False` | **equivalent** | No path assigns to a `_Strategy`; `frozen` is the guard against a future one. A test asserting `FrozenInstanceError` would pin an implementation choice, not a behaviour. Marked like line 37. |
| 269 | same `or` → `and` (in `_resolve_strategy`) | **test gap** | Mirror of 75: survives because `:75` rescues it when the env var is set. Same test, T1, kills both. |
| 335 | `return (hasattr(…) and not …)` → `or` | **test gap** | On a GIL build the default becomes threads and nothing asserts that a GIL build defaults to processes. §7.4 T5. |
| 335 | same `return` → `return None` | **test gap** | `None` is falsy, so on a GIL build it is behaviour-equivalent; on 3.14t it silently turns the free-threaded default into processes. T5's `is False` / `is True` identity assertions kill it on any build. |
| 336 | `not sys._is_gil_enabled()` → dropped `not` | **test gap** | Inverts the default on both builds. T5. |
| 347 | `return strategy.total` → `return None` | **test gap** | `test_the_progress_bar_is_told_how_many_items_to_expect` covers the sized-iterable arm only. §7.4 T6 covers the explicit-`total` arm. |
| 428 | `run_parallel(…, show_progress=True, …)` → `False` | **test gap** | Every test passes `show_progress` explicitly, to keep output quiet; nothing pins the documented default (`ISOCENTER_SHOW_PROGRESS` row: `1`). §7.4 T7. |
| 434 | `run_parallel(…, disable_gc=False, …)` → `True` | **test gap** | `test_without_the_env_var_no_initializer_is_forced_on_workers` calls `_resolve_strategy` directly with `False`, so `run_parallel`'s default is never exercised. §7.4 T3. |

Ten test gaps closed by seven tests (T1 and T5 each kill more than
one); two equivalent mutants. **Expected after the PR: 66/68 killed,
2 survived, both named in the CHANGELOG.** The developer must re-run
the probe after adding the tests and report that line (§10 step 3).

### 3.3 Wall time

Three files, budget 68, under a concurrent coverage run: **68 mutants in roughly 8 min**, 7–8 s a survivor, kills faster (`-x`). Five files, budget 68, under the coverage run and the uninstrumented suite: **19 min 18 s wall** (19:01:22 → 19:20:40), of which **900 s is one mutant hanging to the probe's timeout** — without it, 67 runs in about 4.3 min, i.e. under 4 s a run averaged over fast kills and 7–11 s survivors; the five files' control run was 64 passed in 8.18 s contended (5.77 s for the three files uncontended). So the budget of 80 buys a stride-1 pass in under five minutes **unless a mutant hangs**, and one does: `parallel.py:259` `is None` → `is not None` makes `_resolve_strategy` consult `ISOCENTER_MAX_TASKS_PER_CHILD` exactly when an explicit `maxtasksperchild` argument was given and skip it otherwise, so `export()`'s `25` becomes the environment's `None` and the recycling pool becomes a `ProcessPoolExecutor`. Something in the five-file set then waits 15 minutes; the three-file set fails fast. **Reproduced by hand** (`run_hang259.sh`, the mutant applied to the copy, `pytest -v -x` unpiped, 240 s cap): the culprit is `tests/test_parallel_config.py::TestParallelConfig::test_run_parallel_disable_gc_maxtasks`, parked in `_run_on_new_executor` → `ProcessPoolExecutor.map` → `Future.result()` (`parallel.py:419` → `:354`), with the stall watchdog reporting `nothing has happened for 129s` and faulthandler dumping at 200 s. The test patches `multiprocessing.get_context` with a `Mock` and sets `ISOCENTER_MAX_TASKS_PER_CHILD=5`; under the mutant the variable is never read, the call takes the *unpatched* `ProcessPoolExecutor` path with the mocked context, and waits forever on a queue that is a `MagicMock`. The three-file run passed its files in the order contract, config, worker_count and `-x` stopped at a contract kill before reaching it; the five-file run is alphabetical and reaches it first. **Order-dependent, and a test-hardening item for step 3:** that test (and its `_executor` sibling, the mirror case) should patch *both* pool constructors so a mutant that reroutes the call fails on an assertion in milliseconds instead of hanging — the killing mutation for that hardening is this one, line 259 `is` → `is not`, which must then be *killed*, not *skipped*, by the five-file run. A mutant that hangs is not a test gap — it *is* killed, by the clock — but it costs the budget 900 s each time, and `tests/conftest.py`'s `_STALL_S = 120` watchdog dumps rather than kills, so it did not end it. The developer should confirm the culprit under `pytest -v` unpiped (CLAUDE.md's runbook) and decide whether that test wants its own timeout; the probe's 900 s is #250's number and should not move for this.

---

## 4. #380 — line coverage with the export subprocess counted

### 4.1 The configuration, and why each line

Prototyped in the scratchpad (`coveragerc`); the developer lands it as
**`.coveragerc` at the repo root** with the absolute scratch `data_file`
**removed** (the default `.coverage` is what `.gitignore:64-65` already
ignores, `.coverage` and `.coverage.*`). The comments are part of the
file, not decoration — a future reader will "clean up" `core = ctrace`
and get a hang that reproduces once in ~460 workers.

```ini
# Line coverage that counts the spawned workers (#380). Every child --
# the session's ProcessPoolExecutor(mp_context=spawn) and export's
# multiprocessing.Pool(maxtasksperchild=25) -- is reached through
# `concurrency = multiprocessing`, which patches BaseProcess._bootstrap
# and pickles the configuration into the spawn preparation data so the
# child starts measuring before it runs a task.
#
# `sigterm = True` is what saves the recycling pool's data: `with
# ctx.Pool(...)` exits through terminate(), which SIGTERMs idle workers
# before their _bootstrap `finally` can run, and a killed worker writes
# nothing without a handler.
#
# `core = ctrace` is load-bearing on 3.14. coverage defaults to the
# sys.monitoring core there (env.SYSMON_DEFAULT), whose callback holds a
# non-reentrant threading.Lock on the first sight of every code object,
# and stop() takes the same lock; a SIGTERM handler that runs while the
# main thread is inside that region self-deadlocks the worker, and the
# parent then waits forever in Pool._terminate_pool -> p.join().
# Measured once in ~460 spawned workers on 3.14.6. The C tracer takes a
# lock only while registering a new file.
#
# No `patch = subprocess`: with it every spawned child is instrumented
# twice (the .pth hook at interpreter start and the multiprocessing
# patch), 82 data files where 22 are expected, and two chained SIGTERM
# handlers.
[run]
source = isocenter
parallel = True
concurrency = multiprocessing
core = ctrace
sigterm = True

[report]
show_missing = False
skip_empty = True
```

**The documented invocation**, for CLAUDE.md's Commands block (needs the
`dev` extra, §4.5):

```bash
coverage run -m pytest tests/ && coverage combine && coverage report   # line coverage, spawned workers included (.coveragerc says why core=ctrace)
```

`parallel = True` makes every process write its own
`.coverage.<host>.<pid>.<n>`; `combine` merges them into `.coverage`;
`coverage report` prints the per-module table (`coverage json -o
<somewhere outside the tree>/coverage.json` for the per-function
numbers, §4.4 — `.gitignore:64-68` covers `.coverage`, `.coverage.*`
and `coverage.xml`, not `coverage.json`). The run is **not** in CI and has **no
threshold**: the issue asked for a first number, and a gate on a number
nobody has yet argued about is a gate on a guess.

### 4.2 What was measured to get there

1. **`concurrency = multiprocessing` alone**, `probe_pool.py` (a
   `spawn` `Pool(processes=2, maxtasksperchild=25)` with one failing
   task): exits cleanly, **no data file** from the workers — they die
   under `terminate()` before `_bootstrap`'s `finally`. With
   `sigterm = True`: worker data files appear. Five rcfile variants
   (`run_pool_variants.py`), none hung at this scale.
2. **First full run**, rcfile `patch = subprocess, _exit` +
   `sigterm = True` + the default core (sys.monitoring on 3.14.6):
   **stalled** at
   `tests/test_export_contract.py::test_a_partial_export_returns_its_summary_and_does_not_raise`.
   Parent parked in `multiprocessing/pool.py:732`
   (`_terminate_pool → p.join()`); the child `SpawnPoolWorker-459`
   (pid 31647, `sample31647.txt`) parked in `lock_PyThread_acquire_lock`
   under `_PyErr_CheckSignalsTstate`, nested twice, entered from
   `_Py_call_instrumentation` — i.e. the signal handler ran *inside* a
   sys.monitoring callback and tried to take the lock that callback
   held. 82 data files were on disk against 22 for the same tests
   without `patch`. Killed, orphan reaped. Bisected on
   `tests/test_export_contract.py` alone (`run_export_variants.py`,
   `export_variants.log`): **no variant hung in isolation** — the
   deadlock needs the handler to land in the window, which a full-suite
   load supplies and a single file does not. The diagnosis is from
   coverage's source (`sysmon.py`: `SysMonitor.lock` taken in
   `sysmon_py_start` on first sight of a code object and in `stop()`;
   `env.py`: `SYSMON_DEFAULT = CPYTHON and PYVERSION >= (3, 14)`;
   `core.py`: `ctrace` takes `data_lock` only on new-file registration)
   and the stack, not from a second reproduction. It is a hazard, not a
   certainty: the fallback if `core = ctrace` ever stalls the same way
   is a measurement-only pytest plugin (`-p`) that patches
   `multiprocessing.pool.Pool.__exit__` to `close()` + `join()`, so idle
   workers exit through `_bootstrap`'s `finally` and `sigterm` becomes
   unnecessary — measurement-only because in production a wedged worker
   would hang that `join()`, which is #250's shape.
3. **Second full run**, the configuration above, 3.14.6, in a copy of
   the tree identical to `05d69e8` (`diff -rq` against `isocenter/` and
   `tests/` clean) so the uninstrumented suite could run in the worktree
   at the same time: **1657 passed, 1 skipped in 649 s (10:49)**, no stall, no `STALL WATCHDOG` line, with the uninstrumented suite running in the worktree alongside (1658 passed in 528 s there). The one skip is `tests/test_packaging_contract.py::test_every_shipped_resource_is_named_by_the_package`, which lists tracked files with `git` and the copy has no `.git`; it passed in the worktree. **1379** data files were written, of which **190** carried data — the other 1189 carried no `isocenter` line (spawned processes of every kind: pool workers torn down before a task, the interpreters `tests/test_packaging_contract.py` launches) — and `coverage combine` merged them in one step (`Combined 190 files, skipped 1189`).

### 4.3 The numbers

3.14.6 GIL build, `05d69e8`, the whole of `tests/`, spawned workers counted. `coverage report` after `combine`; statements, not branches.

| Module | Stmts | Miss | Cover |
| --- | ---: | ---: | ---: |
| `isocenter/__init__.py` | 13 | 4 | 69% |
| `isocenter/_version.py` | 1 | 0 | 100% |
| `isocenter/automation.py` | 71 | 6 | 92% |
| `isocenter/blob_kind.py` | 23 | 0 | 100% |
| `isocenter/builders.py` | 49 | 0 | 100% |
| `isocenter/config_manager.py` | 110 | 9 | 92% |
| `isocenter/configuration.py` | 68 | 3 | 96% |
| `isocenter/crypto.py` | 26 | 0 | 100% |
| `isocenter/discovery.py` | 191 | 12 | 94% |
| `isocenter/entities.py` | 309 | 11 | 96% |
| `isocenter/exporters/__init__.py` | 18 | 1 | 94% |
| `isocenter/exporters/dicom.py` | 5 | 0 | 100% |
| `isocenter/exporters/wfdb.py` | 209 | 18 | 91% |
| `isocenter/imagecodecs_handler.py` | 65 | 10 | 85% |
| `isocenter/io_handlers.py` | 1087 | 51 | 95% |
| `isocenter/logger.py` | 25 | 0 | 100% |
| `isocenter/manifest.py` | 39 | 1 | 97% |
| `isocenter/murmur.py` | 122 | 15 | 88% |
| `isocenter/parallel.py` | 129 | 0 | 100% |
| `isocenter/persistence.py` | 1145 | 119 | 90% |
| `isocenter/persistence_manager.py` | 185 | 71 | 62% |
| `isocenter/pixel_analysis.py` | 94 | 18 | 81% |
| `isocenter/pixel_geometry.py` | 116 | 4 | 97% |
| `isocenter/privacy.py` | 163 | 11 | 93% |
| `isocenter/profiles.py` | 2 | 0 | 100% |
| `isocenter/remediation.py` | 193 | 8 | 96% |
| `isocenter/reporting.py` | 75 | 1 | 99% |
| `isocenter/reversibility.py` | 60 | 2 | 97% |
| `isocenter/services.py` | 287 | 27 | 91% |
| `isocenter/session.py` | 1235 | 193 | 84% |
| `isocenter/sidecar.py` | 53 | 2 | 96% |
| `isocenter/store.py` | 47 | 9 | 81% |
| `isocenter/utils/__init__.py` | 0 | 0 | 100% |
| `isocenter/utils/ctp_parser.py` | 58 | 20 | 66% |
| `isocenter/validation.py` | 20 | 0 | 100% |
| `isocenter/verification.py` | 66 | 5 | 92% |
| `isocenter/waveform.py` | 167 | 11 | 93% |
| **TOTAL** | 6526 | 642 | 90% |

Two rows worth a sentence each, outside the two the issue asks for: `persistence_manager.py` at 62%: 45 of its 71 missed statements are in `_persistence_worker_loop`, the background save thread's error and shutdown arms (by AST attribution of the missed lines; the rest are in `_report_unreconciled`, `flush`, `_drain_recoverable_saves`, `_drain_queued_saves`, `_flush_at_exit`, `_requeue_orphans`); `utils/ctp_parser.py` at 66% is the CTP rules importer, named by two test files. `parallel.py` is at 100% *statements*, which is why §3's twelve survivors are the more informative number for that module — every line ran, and twelve mutations of them changed no test's verdict.

### 4.4 The two worker functions, singled out

Computed from `coverage json` and the functions' AST line ranges
(`cov_funcs.py`): statements executed ÷ statements in the function's
span.

| Function | Span | Statements executed | Cover | Not executed |
| --- | --- | ---: | ---: | --- |
| `io_handlers._export_instance_worker` | `io_handlers.py:2726-3322` | 95 / 97 | **98%** | `:3312-3313` — the `except OSError: pass` arm of the temp-file unlink after a failed `save_as` (the file was never created) |
| `io_handlers.ingest_worker` | `io_handlers.py:1365-1618` | 61 / 61 | **100%** | — |
| `services.execute_redaction_task` (the redaction worker, for §1) | `services.py:369-588` | 29 / 31 | 94% | `:441`, `:492` |
| `session._verify_worker` (the `scan_pixel_content()` worker) | `session.py:92-104` | 1 / 7 | **14%** | the whole body: only the `def` line ran |

The last row is a finding the issue did not ask for and the closing comment should carry: **no test in the suite dispatches `scan_pixel_content()`'s worker.** `RedactionVerifier` itself is at 92% through `tests/test_ocr_formal.py` and friends, but the front-door path from `Session.scan_pixel_content()` into a worker never runs. Not this bunch's to fix; file it.

**What the worker measurement contributes**, isolated from suite
breadth by running the same three files twice
(`tests/test_export_contract.py`, `tests/test_ingest_failure_audit.py`,
`tests/test_io.py`; `cov_noconc/`, `cov_conc3/`):

| Function | 3 files, no `concurrency` | 3 files, `concurrency = multiprocessing` | whole suite, with |
| --- | ---: | ---: | ---: |
| `_export_instance_worker` | 42% (41/97) | 48% (47/97) | 98% (95/97) |
| `ingest_worker` | **2%** (1/61 — the `def` line) | **66%** (40/61) | 100% (61/61) |

`ingest_worker` is the clean case: it runs only in the session's
`ProcessPoolExecutor`, and without the patch the only line coverage
sees is the definition. `_export_instance_worker` is **not 0% without
the patch** — 41 of its statements run in the test process on those
three files, so some path calls it in-process (the developer should
attribute which; `tests/test_export_worker_graph_purity.py` is the
obvious candidate and is not in these three), which means the
`ISOCENTER_FORCE_THREADS` row's "you cannot … coverage-measure
`export()` in the calling process" was already partly false of the
*worker* before #380, if true of the *pool*. The patch's contribution
is the 42 → 48 and 2 → 66 columns; the rest is the suite.

### 4.5 `coverage` joins the `dev` extra

`coverage` is in the project venv only as a dependency of `mutmut`
(`pip show coverage` → `Required-by: mutmut`); it is in no `setup.py`
extra. It goes in **`dev`**, next to `pylint`, as `"coverage>=7.10"` —
contributor tooling that `pip install isocenter` and `pip install
isocenter[tests]` must never pull in, which is the sentence CLAUDE.md
already writes for that extra. Not `tests`: nothing in `tests/` imports
it, and `tests/test_packaging_contract.py::test_optional_dependencies_are_not_also_required`
only checks that no extra duplicates `install_requires`, which this does
not. `>=7.10` because `sigterm` and the `core` setting both exist by
then (7.15.4 and 7.16.0 measured); nothing older was tried.

---

## 5. #379 — the frozen surface

### 5.1 How the two enumerations were made

**Every name mkdocs renders**: `mkdocs build` into the scratchpad
(`site/`, `mkdocs-build.log`), then every heading with an mkdocstrings
`doc-heading` class on the five API pages — **112** headings:
`api/configuration` 7, `api/entities` 36, `api/ocr` 15,
`api/persistence` 36, `api/session` 18. The `::: isocenter.persistence`
and `::: isocenter.entities` directives are **unfiltered**, so those
pages render every public name in both modules, `SqliteStore.__getstate__`
and `__setstate__` included.

**Every name tests import from `isocenter`**: the `from isocenter…
import` lines across `tests/*.py` — `DicomSession` 93×, `Session` 11×,
`SqliteStore` 34×, plus the `io_handlers`, `entities`, `builders`,
`services`, `remediation`, `privacy`, `parallel`, `persistence_manager`,
`exporters`, `pixel_analysis`, `imagecodecs_handler`, `utils.ctp_parser`
module imports. Tests import seams; that is what tests are for, and it
is why the second enumeration decides nothing about tier on its own.

**Every call the guides teach**: README and the eleven guide pages,
grepped for member calls (`shapes.py` for the shapes). The guides teach
`session.configuration` (9), `.save()` (7), `.to_zones()` (4),
`.patient_name` (4), `.to_dataframe()` (2), `.store` (2),
`.set_phi_tag()` (2), `.patients` (2), `.add_rule()` (2), and once each
`.write_tree()`, `.store_backend`, `.get_audit_losses()`, `.filter()`,
`.delete_rule()`. **`Builder` appears in none of them** (Q4).

**Tiebreaker**: the owner on #26, 2026-09-08 — the facade is frozen,
the seams behind it are not. Applied literally: a name is tier 1 if a
user reaches it *through the facade or a facade result* and a guide
teaches it or `__all__` exports it; tier 2 if the site renders it or a
guide names it but it is a seam; tier 3 otherwise.

### 5.2 The rule, in one paragraph, for `docs/api/stability.md`

> **Frozen (tier 1)** names keep their spelling, their parameter names,
> their return shapes and their documented behaviour for every 1.x
> release; a change is a 2.0. **Documented but internal (tier 2)** names
> ~~are rendered on this site~~ *(superseded: are listed on the stability
> page, and rendered or named by a guide where a reader needs them)* and
> safe to call, and may change in a 1.x
> release with a CHANGELOG entry that names the old spelling and the new
> one; they exist so a reader can see the seams, not so a program can
> lean on them. **Private (tier 3)** names — everything with a leading
> underscore, and every module not listed above — may change without
> notice. Optional extras (`ocr`, `nlp`) degrade to the documented
> fallback; the fallback is frozen, the extra's internals are not.

### 5.3 Tier 1 — frozen at 1.0

**Package.** `isocenter.Session`, `isocenter.Builder`,
`isocenter.Equipment`, `isocenter.RedactionError`,
`isocenter.ExportError` (the `__all__` five, `__init__.py`);
`isocenter.__version__`.

**`Session` construction and lifetime.** `Session(persistence_file=None)`,
`None` meaning `ISOCENTER_DB_PATH` then `isocenter.db`; `":memory:"`
accepted (Q2). `with Session(...) as s:` (`__enter__` returns the
session, `__exit__` closes); `close()` idempotent and releases the
executor and both threads.

**`Session` methods — all 28 public names, with parameter names** (the
literal `tests/test_frozen_surface.py` pins, §7.5; `self` omitted):

| Method | Parameters |
| --- | --- |
| `ingest` | `directory` |
| `save` | `sync=False` |
| `close` | — |
| `examine` | — |
| `create_config` | `output_path` |
| `load_config` | `config_file` |
| `preview_config` | — |
| `audit` | `config_path=None` |
| `auto_remediate_config` | `report` |
| `anonymize` | `findings=None` |
| `enable_reversible_anonymization` | `key_path='isocenter.key'` |
| `lock_identities` | `patient_id, persist=False, _patient_obj=None, verbose=True, **kwargs` (Q7) |
| `lock_identities_batch` | `patient_ids, auto_persist_chunk_size=0` |
| `recover_patient_identity` | `patient_id, restore=True` |
| `redact` | `show_progress=True, force=False` |
| `redact_by_machine` | `serial_number, roi` |
| `scan_pixel_content` | `serial_number=None` |
| `discover_redaction_zones` | `serial_number, sample_size=50, min_confidence=80.0` |
| `reconcile_private_tags` | — |
| `export` | `folder, format='dicom', **options` |
| `export_dataframe` | `output_path='export_metadata.csv', expand_metadata=False, patient_ids=None` |
| `get_cohort_report` | `expand_metadata=False, patient_ids=None` |
| `phi_status_summary` | — |
| `generate_report` | `output_path, format='markdown'` |
| `generate_manifest` | `output_path, format='html'` |
| `save_analysis` | `report` |
| `compact` | — |
| `release_memory` | — |

`export(format=)` accepts `'dicom'` and `'wfdb'`; the `dicom` options
are `use_compression=True, check_burned_in=False,
check_reversibility=True, patient_ids=None, show_progress=True,
subset=None, verify_readback=False` (`_export_dicom`, `session.py`) and
the `wfdb` options `patient_ids`, `include_annotation_text`. Those
option names are frozen with the method. `generate_report(format=)`
accepts `'markdown'` only and raises `ValueError` otherwise.

**`Session` attributes.** `store` (a `DicomStore` whose `.patients` is
the `List[Patient]` the quickstart indexes), `configuration` (an
`IsocenterConfiguration`), `persistence_file`.

**Shapes the frozen methods return** (attribute names; `shapes.py`):
`IngestSummary(ingested, failures, declined, skipped)` + `failed`;
`ExportSummary(written_uids, failures)` + `written`, `failed`;
`PhiReport(findings)` with `__len__`, `__iter__`, `__getitem__`,
`to_dataframe()`; `PhiFinding(entity_uid, entity_type, field_name,
value, reason, tag, patient_id, entity, remediation_proposal, metadata,
entity_path)`; `DiscoveryResult.filter(...)`, `.to_zones()`,
`.to_dataframe()`; `LockingResult` (a `list` of `Instance`);
`get_cohort_report()` → `pandas.DataFrame`; `phi_status_summary()` →
`Dict[str, Counter]`; `redact()`, `reconcile_private_tags()`,
`auto_remediate_config()` → `int`.

**Entities, as reached from `session.store`**: the graph
`Patient(patient_id, patient_name, studies)` → `Study(study_instance_uid,
study_date, study_time, date_shifted, series)` →
`Series(series_instance_uid, modality, series_number, equipment,
instances)` → `Instance(sop_instance_uid, sop_class_uid,
instance_number, file_path, source_path, attributes, sequences,
attribute_vrs, ~~date_shifted~~)` — **superseded in part by #510
(v0.9.6): `Instance.date_shifted` was cut, so eight fields are frozen
here, not nine; `Study.date_shifted` is unchanged**; `attributes` keyed by lowercase
`"gggg,eeee"` strings; `Equipment(manufacturer, model_name,
device_serial_number)`; on `Instance`: `get_pixel_data()`,
`set_pixel_data()`, `unload_pixel_data()`, `discard_pixel_data()`,
`get_waveform_data()`, and the two-names-two-behaviours rule between
`unload` and `discard` (CLAUDE.md). On `DicomItem`: `set_attr()`.

**`IsocenterConfiguration`** as `session.configuration`: `save()`,
`add_rule()`, `update_rule()`, `delete_rule()`, `set_phi_tag()`,
`get_rule()`, and the fields `rules`, `phi_tags`, `date_jitter`,
`remove_private_tags`, `privacy_profile`.

**Exceptions.** `RedactionError(failures, attempted)`, a `RuntimeError`,
with `.failures` (list of `(entity_uid, details)`) and `.attempted`,
raised after the whole pass; `ExportError(failures, attempted,
folder=None)`, a `RuntimeError`, raised last and only when zero of N
reached disk. `compact()` raises `RuntimeError` while a pass is open
(§5.6). `ValueError` from `generate_report` on an unknown format.

**Environment.** Every `ISOCENTER_*` name in `docs/environment.md`, its
default and its documented semantics — including the order the three
threads-or-processes levers resolve in, which paths each reaches (§2.3),
and that a value below a variable's floor is reported and replaced by
the default.

**Data promises.** A store written by 1.0 opens under every 1.x (the
`user_version` migration chain); the sidecar and schema *layout* are
not frozen, their forward compatibility is. A DICOM file exported with
reversible anonymization by 1.0 is recoverable by every 1.x with its
key: the private tags `(0400,0500)`, `(0400,0510)`, `(0400,0520)` and
the key file's format (raw Fernet key bytes). Date jitter stays
deterministic per patient.

**Output vocabularies** (Q9): the grade (`PASS`, `REVIEW_REQUIRED`),
audit `action_type` and `loss_scope` strings as listed in Q9 — never
renamed or removed in 1.x; additions allowed with a CHANGELOG entry.

**Behaviours.** The call order CLAUDE.md documents and its consequences
(a report before any export carries a boundary note; export-time
`DATA_LOSS` rows are in a report generated after it); `audit()` and
`redact()` drain the persistence manager on entry; nothing reaches
disk before `export()`; source files are never modified; the two #368
behaviours (§5.6).

### 5.4 Tier 2 — documented but internal

Rendered by the site or named by a guide, safe to call, may change in
1.x with a CHANGELOG entry naming both spellings:

- **`DicomSession`**, the class's own name (Q3).
- **`session.store_backend` and `SqliteStore`** — everything
  `api/persistence.md` renders: `__init__(db_path)`, `__getstate__`,
  `__setstate__`, and the 32 public methods including
  `get_flattened_instances(patient_ids, instance_uids, page_size)` (Q5),
  `get_audit_losses()` (the one guide use, `docs/configuration.md:191`),
  the other `get_audit_*`, `persist_pixel_data`, `save_all`,
  `compact_sidecar`, `stop`. The store's *forward compatibility* is tier
  1; its API is tier 2.
- **`session.key_manager`, `session.persistence_manager`,
  `session.reversibility_service`** — attributes that expose services.
- **`TrackedEntity` bookkeeping**: `has_unsaved_changes`, `phi_status`,
  `mark_modified()`, `mark_persisted()`, `mark_subtree_persisted()`,
  `record_phi_status()`; `PhiStatus`; `DicomItem.add_sequence_item()`,
  `record_attr_vr()`; `DicomSequence`; `Instance.regenerate_uid()`,
  `get_waveform_bytes()`, `unload_waveform_data()`, `pixel_array`,
  `waveform_array`; `Equipment.from_parts()`.
- **`entities` helpers** `clone_sequences`, `iter_item_tree`,
  `normalize_study_date`, `resolve_item_path`.
- **The OCR page**: `ZoneDiscoverer.group_boxes`,
  `RedactionVerifier` (`__init__`, `get_matching_rule`, `is_covered`,
  `verify_instance`), `ConfigAutomator.suggest_config_updates`,
  `pixel_analysis.analyze_pixels`, `pixel_analysis.detect_text_regions`,
  `pixel_analysis.HAS_OCR`; `DiscoveryResult.get_density_matrix`,
  `visualize_heatmap`, `analyze_temporal_stability`, `inspect_clusters`.
- **`DicomExporter.write_tree()`** (`docs/waveforms.md:319`, the
  fixture generators) and the exporter registry `Exporter`,
  `register()`, `get_exporter()`, `available_formats()`.
- **`RedactionService.apply_redaction_to_array`** (static,
  `services.py:858`).
- **`Builder`'s fluent chain beyond `start_patient()`** (Q4).
- **`ComplianceReport`'s fields** and the report's section layout and
  wording; log messages and `print` lines; the manifest's HTML.
- **The `.pass.lock` / `.lock` file names**, the sidecar's `_pixels.bin`
  suffix, the audit table's columns, the schema's table names.

### 5.5 Tier 3 — private

Every leading-underscore name, and wholesale: `parallel.py`
(`run_parallel` included — the environment registry is the contract,
the function is not), `io_handlers.py` except `DicomExporter.write_tree`
and the two summaries, `privacy.py` except `PhiFinding`/`PhiReport`,
`remediation.py`, `services.py` except `RedactionError` and
`apply_redaction_to_array`, `crypto.py`, `reversibility.py`,
`sidecar.py`, `persistence_manager.py`, `pixel_geometry.py`,
`murmur.py`, `reporting.py` except `ComplianceReport`, `discovery.py`
except `DiscoveryResult` and `ZoneDiscoverer.group_boxes`,
`verification.py` except `RedactionVerifier`, `automation.py` except
`ConfigAutomator.suggest_config_updates`, `config_manager.py`,
`builders.py` internals, `utils/`, `logger.py`, `_version.py`'s module
(the `__version__` string is tier 1, its module is not), `_ExportOptions`
and `_print_suggested_config` (two tests import them; they are private
and stay so).

### 5.6 The two bunch-1 behaviours, recorded

From `tests/test_compact_refuses_during_a_pass.py`'s docstring and the
#368 CHANGELOG entry, as contract:

1. **`compact()` raises `RuntimeError` while a `redact()` or `ingest()`
   pass is open on the same store, from any thread of this session, and
   has done nothing when it does** — no save, no rewrite, every blob row
   and the sidecar's inode as they were. Frozen: the class, the timing
   ("before its leading save"), and "has done nothing". Not frozen: the
   message text (`compact() refused: a redact() or ingest() pass is open
   on <sidecar>.pass.lock; wait for it to return`) and the lock file's
   name.
2. **`redact()` and `ingest()` block while a `compact()` is saving or
   rewriting, bounded, and then proceed.** Frozen: that they wait and
   then proceed, and that the wait is bounded and its expiry is a
   `RuntimeError` raised before any worker is dispatched or UID
   regenerated. Not frozen: the bound (`_SIDECAR_GATE_TIMEOUT_S = 180`),
   which sits in the #280 inequality and may move with it.

Both are already pinned by the tests in that file on both interpreters;
`docs/api/stability.md` states them, and the `compact()`, `redact()`
and `ingest()` docstrings already do (the #368 entry says so).

### 5.7 Rendered names that are not frozen, and the twelve that are not rendered

**Rule (Q8):** every name the site renders that is not in §5.3 is tier 2
by construction, and each of `api/persistence.md`, `api/entities.md`
and `api/ocr.md` opens with one sentence saying so, linking
`api/stability.md`. No `members:` filter is added to the unfiltered
pages — the seams are rendered *because* the CHANGELOG sends people to
them.

**The API reference omits twelve of the 28 frozen methods.**
`docs/api/session.md` lists 16 methods plus `configuration` under
`members:`; not rendered: `anonymize`, `enable_reversible_anonymization`,
`lock_identities`, `lock_identities_batch`, `recover_patient_identity`,
`redact_by_machine`, `generate_report`, `generate_manifest`,
`get_cohort_report`, `export_dataframe`, `phi_status_summary`,
`save_analysis`. Six of those — `anonymize`, `lock_identities`,
`enable_reversible_anonymization`, `recover_patient_identity`,
`generate_report`, `export_dataframe` — are in the README's and
quickstart's pipeline. That is a determination, not a footnote: the
reference documents half the documented pipeline. Fix in §10 step 5:
extend `members:` to all 28 (in the pipeline order CLAUDE.md gives,
lifecycle first), and §7.5 T-F3 parses that block so the two lists
cannot drift apart again.

---

## 6. Claims overturned by measurement

1. **#365's file list.** "Add `parallel.py` with `test_parallel_contract.py`,
   `test_parallel_config.py`, `test_redaction_worker_count.py`" turns
   `tests/test_mutation_probe_targets.py::test_every_test_that_imports_a_target_module_is_listed`
   **red**: `test_logging.py` and `test_shared_executor_lifecycle.py`
   name `isocenter.parallel` and must be listed (§3.1).
2. **#363's premise, sharpened.** The row's "the only place it is
   computed" is not simply wrong: it is true of the one-per-CPU
   *expression* and wrong about the *default*, because there are two.
   The issue asks for the redaction default to be stated; the
   measurement adds that the existing `_documented_default()` helper in
   `tests/test_redaction_worker_count.py` computes from the live
   `cpu_count()` and is therefore blind to the cap on any box under 16
   CPUs — the sibling test needs fixed counts (§7.3).
3. **#381's framing "`:memory:` store cannot redact on the processes
   path"** is exactly right and is narrower than it might be read:
   `ingest()`, `audit()`, `save()` and `export()` on `:memory:` under
   processes all succeed (§1.1). Only the verb whose worker writes to the
   store fails.
4. **#380's implicit "coverage cannot see the export subprocess."** It
   can, with `concurrency = multiprocessing` + `sigterm = True`; what
   nobody had measured is that on 3.14 the default core deadlocks under
   that `sigterm` about once per 460 workers (§4.2). The blocker was the
   tracer core, not the subprocess.
5. **#379's "a test in `tests/test_api_coherence.py`"** — the natural
   home, and the wrong one for the reason #234 already recorded (Q6).

---

## 7. Tests — what each pins, and the mutation that kills it

Every test below was designed against the correct-by-accident list
(`memory/correct-by-accident-shapes.md`): each names its red state, and
none passes with the production change reverted unless the row says
"characterization".

### 7.1 #381 — new `tests/test_memory_store_redaction_strategy.py`

Fixture: `Session(":memory:")`, three CT files with
`DeviceSerialNumber = "SN_PROBE"` written into `tmp_path` (the pattern
of `tests/test_export_failure_audit.py::_session`), `ingest`,
`redact_by_machine("SN_PROBE", [0, 10, 0, 10])`. **Every test sets
`ISOCENTER_FORCE_PROCESSES=1` and deletes `ISOCENTER_FORCE_THREADS` and
`ISOCENTER_MAX_TASKS_PER_CHILD`**, so the processes path is selected
structurally on 3.14t as well as 3.12 — otherwise the free-threaded
default decides the test before the fix is consulted.

- **`test_a_memory_store_redacts_through_the_front_door_under_processes`**:
  `session.redact()` returns 3 and, for each instance,
  `get_pixel_data()[0:10, 0:10]` is all zero. **Red on `05d69e8`** with
  `RedactionError ... no such table: instance_blobs` on both
  interpreters (rows 1 and 5). Killing mutation: the `force_threads=`
  keyword deleted at `session.py:2876`.
- **`test_the_memory_store_asks_for_threads_per_call_and_a_file_store_does_not`**:
  wrap `isocenter.session.run_parallel` with a spy that records
  `kwargs.get("force_threads")` and calls through
  (`monkeypatch.setattr(session_module, "run_parallel", spy)`); assert
  `True` for the `:memory:` session and `False`/absent for a file-backed
  one on `tmp_path`. Killing mutations: the keyword deleted (first half
  red); `force_threads=True` unconditionally (second half red — a file
  store on the floor interpreter must keep processes, the only path
  whose recycling and memory behaviour #185 measured); `os.environ[...]
  = "1"` in place of the argument (first half red: the spy sees no
  argument). The spy patches `isocenter.session`'s binding, so this
  file does not match `isocenter\.parallel\b` and needs no `TARGETS`
  entry (§3.1).
- If Q1(b): **`test_a_memory_store_with_worker_recycling_is_refused_before_any_task_runs`**
  — `ISOCENTER_MAX_TASKS_PER_CHILD=5`, `pytest.raises(ValueError,
  match="ISOCENTER_MAX_TASKS_PER_CHILD")`, and the spy records **no**
  `run_parallel` call. Killing mutation: the refusal deleted →
  `RedactionError` instead of `ValueError`.

### 7.2 #390/#363 — `tests/test_shared_executor_lifecycle.py`

- **`test_force_threads_does_not_reach_ingest`** (characterization, the
  row is written from it): `ISOCENTER_FORCE_THREADS=1`, a session on
  `tmp_path`, one file, spy on `isocenter.session.run_parallel` as in
  §7.1; assert `kwargs["executor"] is session._executor` and
  `isinstance(session._executor, concurrent.futures.ProcessPoolExecutor)`.
  Killing mutation: `executor=self._executor,` deleted at
  `session.py:1433` → red. Green on `05d69e8`, and that is the finding
  (the #333 convention). *Note (#393, v0.9.6): the test keeps its name
  and now also asserts the one `WARNING` `ingest()` logs for the lever,
  emitted before the dispatch; it is no longer a pure characterization.*

### 7.3 #363 — `tests/test_redaction_worker_count.py`

- **`test_the_default_redaction_worker_count_is_half_the_cpus_capped_at_eight`**,
  `@pytest.mark.parametrize("cpus, expected", [(32, 8), (16, 8),
  (14, 7), (6, 3), (2, 1), (1, 1), (None, 1)])`;
  `monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)`;
  `monkeypatch.setattr(os, "cpu_count", lambda: cpus)` (the function
  reads `os.cpu_count()` through the module, `session.py:520`); assert
  `session_module._redaction_worker_count() == expected`. Killing
  mutations, each hardware-independent because the count is set:
  `8 → 16` (32 → 16), `// 2 → // 1` (6 → 6), `min → max` (6 → 8),
  `max(1, …)` removed (1 → 0, `None` → 0), `or 1` removed (`None` →
  `TypeError`). Sibling of
  `tests/test_parallel_contract.py::test_the_default_worker_count_is_one_per_cpu`
  in purpose; placed in this file because `_documented_default()` lives
  here and two spellings of the expression in two files is the drift
  this bunch is about. Its docstring should say `_documented_default()`
  stays as the oracle for the *override* tests and is blind to the cap
  on this box, which is what the fixed counts are for.

### 7.4 #365 — the seven tests that close the ten gaps

In `tests/test_parallel_config.py` (mocked pools, the file's idiom):

- **T1 `test_disable_gc_as_an_argument_alone_reaches_the_initializer`**:
  `ISOCENTER_DISABLE_GC` and `ISOCENTER_MAX_TASKS_PER_CHILD` deleted,
  `ProcessPoolExecutor` patched as in
  `test_run_parallel_disable_gc_executor`,
  `run_parallel(identity, [1], show_progress=False, disable_gc=True)`,
  `_assert_disables_gc(call_kwargs["initializer"])`. Kills **269
  `or → and`** (strategy folds to `False`) and **75 `or → and`**
  (resolver folds to `False`).
- **T3 `test_without_any_lever_run_parallel_hands_the_pool_no_initializer`**:
  `ISOCENTER_DISABLE_GC` and `ISOCENTER_WORKER_FAULTHANDLER` deleted,
  same patch, `run_parallel(identity, [1], show_progress=False)`, assert
  `call_kwargs.get("initializer") is None`. Kills **434 default `True`**.
- **T7 `test_progress_is_on_by_default`**: `ISOCENTER_SHOW_PROGRESS`
  deleted, `ISOCENTER_FORCE_THREADS=1`, `tqdm` captured as at
  `test_parallel_contract.py:117-133`, `run_parallel(identity, [1, 2])`
  with **no** `show_progress`; assert the capture ran. Kills **428
  default `False`**.

In `tests/test_parallel_contract.py`:

- **T2 `test_the_initializer_actually_disables_the_collector_in_a_worker`**:
  module-scope `def _collector_enabled(_): import gc; return
  gc.isenabled()`; `ISOCENTER_FORCE_PROCESSES=1`, `FORCE_THREADS` and
  `MAX_TASKS_PER_CHILD` deleted (threads get no initializer by rule, so
  processes structurally); `run_parallel(_collector_enabled, [0],
  disable_gc=True, max_workers=1, show_progress=False) == [False]` and
  the control without `disable_gc` `== [True]`. One spawn (~1–2 s).
  Kills **55 `gc.disable()` deleted**. Also the only test that proves
  the feature does anything.
- **T4 `test_the_shared_executor_gets_no_initializer_without_a_lever`**,
  beside `test_the_shared_session_executor_pins_spawn`: both env vars
  deleted, `with DicomSession(str(tmp_path / "s.db")) as s: assert
  s._executor._initializer is None`. Kills **61 default `True`**.
  (Reads a private attribute of the executor, as the spawn test reads
  `_mp_context`.)
- **T5 `test_the_default_path_is_processes_under_a_gil_and_threads_without_one`**:
  all three lever variables deleted;
  `monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True,
  raising=False)` → `parallel._use_threads(False, None) is False`;
  `lambda: False` → `is True`; `monkeypatch.delattr(sys,
  "_is_gil_enabled", raising=False)` → `is False`. Kills **336 dropped
  `not`** (first arm), **335 `and → or`** (first arm: `True or …`), and
  **335 `return None`** — only because the assertions are identity
  (`is False`), not truthiness; `assert not result` would let `None`
  through. This is also the first test that says what the free-threaded
  default *is*, which the PR gate's 3.14t job has been relying on
  without a pin.
- **T6 `test_an_explicit_total_reaches_the_progress_bar`**: the
  `:117-133` capture, `run_parallel(identity, (i for i in range(4)),
  total=4, show_progress=True)`, assert `seen["total"] == 4`. Kills
  **347 `return None`** (the bar would get `None` — a generator has no
  `__len__`).

Not written, marked equivalent in the CHANGELOG (§10 step 4): **37**
(`_worker_init`'s default is never read; the partial passes both
keywords) and **128** (`frozen=True`).

### 7.5 #379 — new `tests/test_frozen_surface.py`

No target module imported; no `TARGETS` entry (Q6).

- **T-F1 `test_the_frozen_session_surface_is_exactly_this`**: a literal
  `FROZEN_SESSION_METHODS: dict[str, list[str]]` transcribed from §5.3's
  table; assert `set(n for n in vars(DicomSession) if not
  n.startswith("_")) == set(FROZEN_SESSION_METHODS)` (both directions:
  a new public method must be added to the freeze or underscored, a
  removed one is a 2.0), and for each,
  `list(inspect.signature(getattr(DicomSession, name)).parameters)[1:]
  == params`. Killing mutations: any public method renamed, any
  parameter renamed or reordered, a new public method added without a
  row. Also `isocenter.__all__ == ["Session", "Builder", "Equipment",
  "RedactionError", "ExportError"]` and `isocenter.Session is
  DicomSession`.
- **T-F2 `test_the_frozen_shapes_have_these_fields`**: `dataclasses.fields`
  names for `IngestSummary`, `ExportSummary`, `PhiFinding`, `Equipment`,
  `Patient`, `Study`, `Series`, and the public non-underscore fields of
  `Instance`, against literals from §5.3; `RedactionError.__init__` and
  `ExportError.__init__` parameter names; both `issubclass(...,
  RuntimeError)`. Killing mutation: any field renamed.
- **T-F3 `test_the_api_reference_renders_every_frozen_session_method`**:
  parse `docs/api/session.md`'s `members:` block (the `- name` lines
  under `::: isocenter.session.DicomSession`) and assert it is a
  superset of `FROZEN_SESSION_METHODS`. **Red on `05d69e8`** on the
  twelve names in §5.7; green after §10 step 5. Killing mutation: a
  method removed from `members:`.
- **T-F4 `test_the_stability_page_names_every_tier_one_session_method`**:
  `docs/api/stability.md` mentions each name in `FROZEN_SESSION_METHODS`
  in backticks, and `mkdocs.yml`'s `nav` lists `api/stability.md`
  (`tests/test_doc_anchors.py` renders nav pages, so an unlisted page is
  unchecked). One direction only — the page may name more.
- **T-F5 `test_the_two_pass_behaviours_are_stated_as_contract`**: the
  docstrings of `DicomSession.compact`, `.redact` and `.ingest` each
  contain the word `RuntimeError` and, for `compact`, "pass"; a
  docstring pin of the #368 entry's "stated in the three docstrings as
  contract for #379". Weak by design (it pins that the promise is
  written where a user reads it; the behaviour itself is pinned by
  `tests/test_compact_refuses_during_a_pass.py`).

---

## 8. Earlier specs and CHANGELOG entries this touches

- **`2026-09-08-sidecar-concurrency-contract.md` §0.2 E** predicted #381
  (`__setstate__` → empty `:memory:` database; "either force threads or
  refuse"). Confirmed and narrowed to `redact()`; nothing to strike. Its
  measurement table row "`:memory:` + processes `redact()` →
  `RedactionError` / `no such table: instance_blobs`" is reproduced
  here on 3.12.13 and 3.14.7t.
- **`2026-09-08-export-fidelity-bunch-2.md` §10 amendment 1** records
  #390 as found during implementation. §2 here is its measurement;
  nothing to strike.
- **The `ISOCENTER_FORCE_THREADS` row's** "`export()` remains the one
  path you cannot debug, breakpoint or coverage-measure in the calling
  process" (`docs/environment.md:17`, written for #185) is falsified
  twice: `ingest()` is a second such path (§2.1), and the coverage half
  is answered by §4 — and was partly false already, since the export
  *worker* runs in-process on some path (§4.4). The row is rewritten in
  §2.3; the CHANGELOG does not carry that sentence (grepped), so nothing
  there is superseded.
- **`tests/test_api_coherence.py:577`'s docstring** ("this is the
  surface #26 will freeze") is a determination this spec reverses if
  Q5(a); the developer rewords it in the same PR.

---

## 9. What the reviewer should attack

1. **§1.2's fix under `ISOCENTER_FORCE_PROCESSES=1` on 3.14t.** The
   claim is that `force_threads=True` beats the variable
   (`parallel.py:327` before `:329`). §7.1's first test asserts it; run
   it on `venv314t` with the variable set and watch it pass *for the
   right reason* — the spy test's `True` is the mechanism, the
   front-door test's zeros are the outcome. If the developer wires the
   env var instead, the spy test is red and the front-door test is
   green; that combination is the wrong fix.
2. **§7.3's parametrization on a 1-CPU runner.** `(1, 1)` and
   `(None, 1)` are the arms that fail if `max(1, …)` goes; make sure
   the mutant list in the docstring matches what a run of the probe
   reports, not what this spec predicts.
3. **§3.2's two equivalent mutants.** Try to write a behavioural test
   for line 37 without disabling the test process's collector; if you
   can, it is a gap and the CHANGELOG line must not be written.
4. **§4.2's deadlock diagnosis** is from one stack sample and
   coverage's source. Attack it by running the full suite twice under
   the landed `.coveragerc` on 3.14.6; if it stalls, the fallback plugin
   in §4.2 item 2 is the path, and `sigterm = True` comes out.
5. **§4.5's `>=7.10`.** `coverage --version` of the oldest release with
   both `[run] core` and `[run] sigterm` is not something this spec
   verified below 7.15.4; if 7.10 lacks `core`, raise the floor.
6. **§5.3's `export()` option names.** Frozen with the method, but
   `**options` means the signature pin (T-F1) does not see them; T-F2
   should also pin `_ExportOptions`'s field names if that dataclass is
   what parses them, or the reviewer should say why not.
7. **§5.7's twelve unrendered methods** — check that rendering them
   does not break `tests/test_doc_anchors.py` (a docstring with a bad
   fragment link becomes a rendered heading) or
   `tests/test_documented_api_exists.py` (a Python fence in a docstring
   naming a method that does not exist).
8. **§3.3's hang.** Apply `parallel.py:259` `is None` → `is not None`
   by hand (CLAUDE.md's runbook) and run the five files in alphabetical
   order: before step 3's hardening it parks in
   `test_run_parallel_disable_gc_maxtasks` for as long as you let it;
   after, it must be a red assertion in under a second. If the developer
   hardens the test by *reordering* the files instead, refuse it — the
   probe passes `TARGETS`' list in list order, and the completeness
   test does not care about order, so a reorder is a silent dependency.
9. **§3.1's regex claim.** `test_redaction_worker_count.py` is *not*
   demanded because its text has a slash; if anyone later writes
   `isocenter.parallel` in its docstring the completeness test starts
   demanding it — which is fine, because it is already listed. The
   reverse risk is a new test file that spies on `isocenter.parallel`
   without being listed; the completeness test catches it, so this is a
   reminder, not a hole.

---

## 10. Implementation brief — one PR, five issues, in this order

Ordering: the #381 fix first because the #363 rows describe it; #365
next because its new tests are re-run by the probe whose entry it adds;
#380 and #379 are docs, config and tests and go last. One PR, per-issue
`Fixes` lines, one CHANGELOG entry per issue under `## [Unreleased]`.

### Step 1 — #381

1. `session.py:2876`: add `force_threads=self.store_backend.db_path ==
   ":memory:",` to the `run_parallel(` call in `_apply_redaction_rules`.
2. `session.py:2833`: ~~`print(f"Executing using {max_workers} workers...")`~~
   — superseded by #384 (2026-09-09); the parenthetical returns as
   `(threads)`/`(processes)`, read off the resolved strategy.
   Measured: `grep -rn "Process Isolation" docs/ README.md tests/` hits
   only the 2026-09-08 sidecar spec's prose (a historical record, left
   as is); no guide quotes the line and
   `tests/test_documented_output_matches.py` does not compare it, so
   nothing else moves.
3. If Q1(b): the `ValueError` refusal from §0.2 before the tasks are
   built, with the exact message in Q1(b).
4. If Q2(a): one sentence in `Session.__init__`'s docstring
   (`persistence_file=":memory:"` is accepted and is threads-only for
   `redact()`) and in the `ISOCENTER_DB_PATH` row.
5. `tests/test_memory_store_redaction_strategy.py` (§7.1). Run it on
   `venv312` and `venv314t`: red before step 1, green after, on both.
6. **CHANGELOG, `### Fixed`:**

   > - **`redact()` on a `:memory:` store runs in threads, on every
   > interpreter (#381).** On the processes path — the default on 3.12,
   > the `python_requires` floor, and `ISOCENTER_FORCE_PROCESSES=1`
   > elsewhere — `redact()` on `Session(":memory:")` raised
   > `RedactionError: Redaction failed for N of N instances ... First:
   > <uid>: no such table: instance_blobs`, because the redaction
   > worker is the only worker that *writes to the store* from inside
   > the child (`execute_redaction_task` → `persist_pixel_data`), and
   > `SqliteStore.__setstate__` hands a spawned child `_memory_conn =
   > None`, so `_get_connection` opened a fresh, empty in-memory
   > database. Measured on 3.12.13 and 3.14.7t; `ingest()`, `audit()`,
   > `save()` and `export()` on the same store succeed, because their
   > workers read from a file or return results the parent persists.
   > `_apply_redaction_rules` now passes `force_threads=True` to
   > `run_parallel()` when `store_backend.db_path == ":memory:"` — the
   > *argument*, per #390, not the environment variable, which is
   > process-global and does not reach every pool. `force_threads`
   > beats `ISOCENTER_FORCE_PROCESSES` by the documented order, so that
   > variable does not reopen the failure. **Exact exceptions:** a call
   > that raised `RedactionError` now returns the redacted count; no
   > call that returned now raises. **One corner stands, documented in
   > the `ISOCENTER_MAX_TASKS_PER_CHILD` row:** with that variable set,
   > worker recycling overrides the request for threads (#185's order,
   > with its warning) and the same `RedactionError` results; unset it
   > or use a file-backed store. The `(Process Isolation)` in
   > `redact()`'s progress line is gone — it was already false on
   > free-threaded builds. Tests in
   > `tests/test_memory_store_redaction_strategy.py`, run under
   > `ISOCENTER_FORCE_PROCESSES=1` so the processes path is selected on
   > 3.14t too; the spy test asserts the keyword is `True` for
   > `:memory:` and `False` for a file store, so wiring the environment
   > variable instead is red. Predicted by the 2026-09-08 sidecar spec
   > §0.2 E. Design record:
   > `docs/superpowers/specs/2026-09-08-frozen-surface-and-strategy-bunch-3.md` §1.

   (If Q1(b): add "A `:memory:` store with `ISOCENTER_MAX_TASKS_PER_CHILD`
   set is now refused up front with `ValueError("redact() on a :memory:
   store runs in threads, and ISOCENTER_MAX_TASKS_PER_CHILD asks for
   worker recycling, which only processes implement; unset it or use a
   file-backed store")` before any task is queued — `ValueError` rather
   than `RedactionError`, whose meaning is that something unsafe is
   still in the graph.")

### Step 2 — #363 and #390

1. `docs/environment.md`: the four row edits in §2.3, verbatim.
2. `parallel.py:322`: the warning's "elsewhere, unset
   ISOCENTER_MAX_TASKS_PER_CHILD to get threads" → "for audit(),
   scan_pixel_content() and redact(), unset ISOCENTER_MAX_TASKS_PER_CHILD
   to get threads; ingest() runs on the session's executor and takes no
   lever". `tests/test_parallel_contract.py::test_a_forced_thread_request_is_reported_when_worker_recycling_overrides_it`
   matches on the lever names, not this clause — check before editing.
3. `tests/test_redaction_worker_count.py`: §7.3's test.
4. `tests/test_shared_executor_lifecycle.py`: §7.2's test.
5. **CHANGELOG, `### Fixed`:**

   > - **`docs/environment.md` says which pool each lever reaches, and
   > states both worker-count defaults (#363, #390).** The
   > `ISOCENTER_MAX_WORKERS` row gave one default — one per CPU — and
   > called `_resolve_strategy` "the only place it is computed". True of
   > that expression; false of the default, because `redact()` has its
   > own: `max(1, min((os.cpu_count() or 1) // 2, 8))` in
   > `_redaction_worker_count`, half the CPUs capped at eight, a memory
   > ceiling (each worker holds a decoded frame; one-per-CPU exhausted
   > memory on large studies). The row now states both and names the
   > owner of each. Pinned by
   > `tests/test_redaction_worker_count.py::test_the_default_redaction_worker_count_is_half_the_cpus_capped_at_eight`
   > at **fixed** CPU counts — on a 14-CPU box the cap is never reached,
   > so a test computing from the live `cpu_count()` (as the file's
   > `_documented_default()` does, deliberately, for the override tests)
   > would let a `8 → 16` mutant survive by hardware. **The
   > `ISOCENTER_FORCE_THREADS` row said it does not apply to `export()`
   > and implied it applies everywhere else. Measured per verb: it
   > reaches `audit()`, `scan_pixel_content()` and `redact()`; it
   > reaches neither `export()` (recycling, #185, with a warning) nor
   > `ingest()`, where it is read and ignored without one** — `ingest()`
   > runs on the session's own `ProcessPoolExecutor`, handed to
   > `run_parallel()` as `executor=`, and a caller-supplied executor is
   > used as given (#390, found during #372's implementation, whose spec
   > amendment records it). `discover_redaction_zones()` is threads
   > regardless. The #185 warning's "elsewhere, unset
   > `ISOCENTER_MAX_TASKS_PER_CHILD` to get threads" named the three
   > verbs it is true of and now says `ingest()` takes no lever. No
   > behaviour changes; **no exception changes.**
   > `tests/test_shared_executor_lifecycle.py::test_force_threads_does_not_reach_ingest`
   > is a characterization pin — green on the code it was written
   > against, and the row is written from it (the #333 convention).
   > Design record: spec §2.

   *Note (#393, v0.9.6): that test now also asserts `ingest()`'s
   `WARNING` for the lever; see the front-matter supersession.*

### Step 3 — #365

1. `scripts/mutation_probe.py`: the `TARGETS` entry in §3.1 (five
   files, budget 80).
2. CLAUDE.md: the table row in §3.1, placed first, matching the
   `TARGETS` insertion.
3. The seven tests of §7.4, plus the hardening of
   `test_run_parallel_disable_gc_maxtasks` and
   `test_run_parallel_disable_gc_executor` in §3.3: each patches both
   `multiprocessing.get_context` and
   `isocenter.parallel.concurrent.futures.ProcessPoolExecutor`, and
   asserts the one it expects was called and the other was not, so a
   mutant that reroutes the dispatch is a red assertion rather than a
   900 s hang. Killing mutation: line 259 `is` → `is not` (today:
   killed by the three files in CLI order, hangs the five files in
   alphabetical order).
4. Run `tests/test_mutation_probe_targets.py` (green: completeness and
   the CLAUDE.md mapping), then
   `python -m scripts.mutation_probe 80 isocenter/parallel.py tests/test_logging.py tests/test_parallel_config.py tests/test_parallel_contract.py tests/test_redaction_worker_count.py tests/test_shared_executor_lifecycle.py`
   (the files in the order `TARGETS` lists them, which is alphabetical)
   **in a copy of the tree** (the probe writes mutations into
   `isocenter/parallel.py` and restores them; a crash mid-run leaves a
   mutant in the worktree — this bunch ran it in `cp -R` copies with
   `.venv` symlinked). Expected: `killed 66/68, SURVIVED 2/68`, lines 37
   and 128, **no `skipped`** — a `skipped ... TimeoutExpired` line means
   §3.3's hardening did not land. Report the line and the wall time in the PR.
5. **CHANGELOG, `### Added`:**

   > - **`isocenter/parallel.py` is a mutation-probe target (#365).**
   > 68 mutation sites; five test files (`test_logging.py`,
   > `test_parallel_config.py`, `test_parallel_contract.py`,
   > `test_redaction_worker_count.py`, `test_shared_executor_lifecycle.py`
   > — five rather than the issue's three, because
   > `tests/test_mutation_probe_targets.py` demands every file whose
   > text names `isocenter.parallel`, and two more do); budget 80, which
   > is stride 1 with headroom, affordable at 8 s a run where
   > `io_handlers.py`'s ~5× the sites is not. The first full pass killed
   > 56/68 against the issue's three files; **ten of the twelve
   > survivors were test gaps**, closed by seven tests: nothing ran the
   > worker initializer (`gc.disable()` could be deleted; the tests
   > asserted the partial's shape on purpose, since calling it would
   > disable the test process's collector — a spawned worker now reports
   > `gc.isenabled()`); nothing pinned that a GIL build defaults to
   > processes and a free-threaded build to threads (`_use_threads`'s
   > last line could be inverted, or its `and` made `or`, or its return
   > made `None`); `disable_gc` as an argument with no environment
   > variable was never tried (the `or` at both `_resolve_strategy` and
   > `resolve_worker_initializer` could become `and`, each rescued by
   > the other); `run_parallel`'s own defaults for `show_progress` and
   > `disable_gc`, and `resolve_worker_initializer`'s default as
   > `Session.__init__` calls it bare, were unexercised; and an explicit
   > `total=` never reached the progress bar in any test. **Two
   > survivors are equivalent mutants and are left standing on
   > purpose:** `_worker_init`'s `disable_gc=False` default (every
   > caller is the `functools.partial` `resolve_worker_initializer`
   > builds, which passes both keywords; the default is never read) and
   > `_Strategy`'s `frozen=True` (no path assigns to a strategy; the
   > flag guards a future one, and a test asserting
   > `FrozenInstanceError` would pin the implementation, not a
   > behaviour). The probe reports both as survived; that is not a test
   > gap and must not be closed by writing a test around it. After the
   > seven tests: 66/68 killed. No behaviour changes; **no exception
   > changes.** Design record: spec §3.

### Step 4 — #380

1. `.coveragerc` at the repo root: §4.1's file **without** `data_file`.
2. `setup.py` `dev` extra: `"coverage>=7.10"` after `"pylint>=3.0"`,
   with a comment: `# coverage: .coveragerc's spawned-worker measurement
   (#380); contributor tooling like pylint, never in tests or
   install_requires.`
3. CLAUDE.md Commands block: §4.1's line after the `pylint` line.
4. Run `tests/test_packaging_contract.py` (green), then the documented
   invocation once on 3.14.6 and once on `venv312` (where `ctrace` is
   the default anyway); record both totals in the PR and in the issue's
   closing comment with §4.3's table.
5. **CHANGELOG, `### Added`:**

   > - **A line-coverage run that counts the spawned workers (#380).**
   > `coverage run -m pytest tests/ && coverage combine && coverage
   > report`, configured by a new `.coveragerc` and documented in
   > CLAUDE.md's Commands; `coverage>=7.10` joins the `dev` extra
   > (contributor tooling, like pylint; it was in the venv only as a
   > dependency of `mutmut`). `concurrency = multiprocessing` reaches
   > both spawn paths — the session's `ProcessPoolExecutor` and export's
   > recycling `multiprocessing.Pool`, which `ISOCENTER_FORCE_THREADS`
   > cannot — and `sigterm = True` is what saves the recycling pool's
   > data, because `with ctx.Pool(...)` exits through `terminate()`.
   > **`core = ctrace` is the load-bearing line**: on 3.14 coverage
   > defaults to the `sys.monitoring` core, whose callback holds a
   > non-reentrant lock on the first sight of every code object, and a
   > SIGTERM handler that runs inside that region self-deadlocks the
   > worker while the parent waits in `Pool._terminate_pool → join()`
   > — measured once in ~460 spawned workers, at
   > `tests/test_export_contract.py::test_a_partial_export_returns_its_summary_and_does_not_raise`,
   > with the stack in the spec. The comment in `.coveragerc` says so
   > because that is the line a future reader will "clean up". No
   > `patch = subprocess` (double-instruments every child: 82 data files
   > where 22 are expected). First numbers, 3.14.6, `05d69e8`: **90% (6526 statements, 642 missed)**
   > overall; `_export_instance_worker` **98% (95 of 97 statements; the two missed are the `except OSError: pass` after a failed `save_as`)**,
   > `ingest_worker` **100% (61 of 61)** — both 0% without the
   > worker measurement, which is the number the issue was written
   > against. Not in CI, no threshold: a gate on a number nobody has yet
   > argued about is a gate on a guess. No behaviour changes; **no
   > exception changes.** Design record: spec §4.

### Step 5 — #379

1. New `docs/api/stability.md`: §5.2's paragraph, then §5.3–§5.5 as
   three lists (the `Session` table verbatim), then §5.6's two
   behaviours. Add `- 'API stability': api/stability.md` to `mkdocs.yml`
   nav under `API Reference`, first.
2. `docs/api/session.md`: `members:` extended to all 28 methods in the
   pipeline order CLAUDE.md gives (`ingest`, `save`, `examine`,
   `create_config`, `load_config`, `preview_config`, `audit`,
   `auto_remediate_config`, `anonymize`, `enable_reversible_anonymization`,
   `lock_identities`, `lock_identities_batch`, `recover_patient_identity`,
   `redact`, `redact_by_machine`, `scan_pixel_content`,
   `discover_redaction_zones`, `reconcile_private_tags`, `export`,
   `export_dataframe`, `get_cohort_report`, `phi_status_summary`,
   `generate_report`, `generate_manifest`, `save_analysis`, `compact`,
   `release_memory`, `close`) plus `configuration`. Run
   `tests/test_doc_anchors.py` and `tests/test_documented_api_exists.py`
   after: twelve newly rendered docstrings may carry links or fences
   nothing has graded before (§9 item 7).
3. One sentence at the top of `docs/api/persistence.md`,
   `docs/api/entities.md`, `docs/api/ocr.md`: "Everything on this page
   is *documented but internal* (see [API stability](stability.md))
   unless that page lists it as frozen."
4. Per Q3–Q5, Q7, Q9: the tier decisions written into `stability.md`;
   if Q5(a), reword `tests/test_api_coherence.py:577`'s docstring.
5. `tests/test_frozen_surface.py` (§7.5).
6. Close #26's blocking item with a comment linking `stability.md`.
7. **CHANGELOG, `### Added`:**

   > - **The 1.0 API surface is written down, in three tiers, and
   > pinned (#379, for #26).** `docs/api/stability.md`: **frozen** —
   > the `__all__` five and `__version__`; all 28 public `Session`
   > methods with their parameter names; `store`, `configuration`,
   > `persistence_file`; the shapes the frozen methods return
   > (`IngestSummary`, `ExportSummary`, `PhiReport`/`PhiFinding`,
   > `DiscoveryResult.filter/to_zones`, the entity graph's fields);
   > `RedactionError(failures, attempted)` and `ExportError(failures,
   > attempted, folder)` as `RuntimeError`s with those attributes; every
   > `ISOCENTER_*` variable and its documented semantics; the grade and
   > audit vocabularies; the store's forward compatibility and the
   > reversible-anonymization data promise; and two behaviours from
   > #368 — `compact()` raises `RuntimeError` and has done nothing while
   > a `redact()` or `ingest()` pass is open, and a pass waits, bounded,
   > behind a compaction and then proceeds. **Documented but internal**
   > — rendered on the site, safe to call, changeable in 1.x with a
   > CHANGELOG entry naming both spellings: `SqliteStore` and
   > `store_backend` (the owner's ruling on #26: "the facade is what
   > gets frozen, and the internal seams behind it are not"), the
   > `TrackedEntity` bookkeeping, `DicomExporter.write_tree`, the
   > exporter registry, the OCR page's classes, `DicomSession` as the
   > class's own name. **Private** — everything else. Pinned by
   > `tests/test_frozen_surface.py` (in its own file rather than
   > `tests/test_api_coherence.py`, which #379 named, for the reason
   > `tests/test_documented_api_exists.py` records for #234: that file
   > is an `io_handlers.py` probe target and a signature pin there buys
   > no kill signal): the public-method set of `Session` equals the
   > frozen list in both directions, every parameter name matches, the
   > shapes' fields match, and `docs/api/session.md` renders every
   > frozen method. **That last pin was red: the API reference rendered
   > 16 of the 28**, omitting `anonymize`, `lock_identities`,
   > `enable_reversible_anonymization`, `recover_patient_identity`,
   > `generate_report`, `export_dataframe` and six more — half the
   > pipeline the README teaches. All 28 are rendered now. No behaviour
   > changes; **no exception changes**; nothing renamed — `lock_identities`'
   > `_patient_obj` and `**kwargs` are frozen as they stand and their
   > cleanup is filed separately. Design record: spec §5.

### Step 6 — records

- This spec's §12 amendments log for anything implementation departs
  from.
- The #380 closing comment: §4.3's table and §4.4's two lines.
- The #365 closing comment: the probe's final line and wall time.

---

## 11. Measurement log and clean-tree record

| What | Where | Result |
| --- | --- | --- |
| `isocenter.__file__` under each interpreter | every probe's first line | `<worktree>/isocenter/__init__.py`, 3.12.13 / 3.14.6 / 3.14.7t |
| #381 verbs × interpreters × levers | `probe_381.py` | §1.1 table (5 rows) |
| #390 verbs × levers, dispatch path | `probe_390.py` | §2.1 table |
| The probe fixture's first version | `probe_381.py`, first run | `export()` failed CT IOD validation (missing `0008,0030`, `0018,0050`, `0018,0060`, `0020,0032`, `0020,0037`, `0028,0030`) — the fixture, not #381; fixed, re-run, `export(:memory:)` OK |
| 3.14.7t pyenv build | `venv314t-install.log` | needed `.[tests]` (`No module named pydicom` bare) |
| `Pool.terminate()` under coverage, five rcfile variants | `run_pool_variants.py` | none hung; no worker data without `sigterm` |
| First full coverage run | `cov/pytest.log` (superseded) | stalled at `test_a_partial_export_returns_its_summary_and_does_not_raise`; child stack `sample31647.txt`; 82 data files |
| `test_export_contract.py` alone, three variants, 300 s deadline | `export_variants.log` | none hung — timing-dependent |
| Second full coverage run (`core = ctrace`) | `cov/pytest.log` | 1657 passed, 1 skipped (`git`-dependent, in the `.git`-less copy) in 649 s; no stall; 1379 data files, 190 non-empty; TOTAL 90% |
| Mutation probe, 3 files, budget 68 | `probe_parallel.log` | killed 56/68, 12 survived, 7–8 s a mutant, contended |
| Mutation probe, 5 files, budget 68 | `probe_parallel5.log` | control 64 passed in 8.18 s (contended); killed 55/67, SURVIVED 12/67, `skipped line 259: Is -> IsNot: TimeoutExpired` (900 s); wall 19:01:22 → 19:20:40; same twelve survivors as the three-file run |
| Rendered-name enumeration | `site/`, `mkdocs-build.log` | 112 `doc-heading`s across five pages |
| `Session` signatures, `__all__`, shapes | `sigs.py`, `shapes.py` | §5.3 |
| Uninstrumented full suite on the worktree, 3.14.6 | `suite_worktree.log` | **1658 passed in 527.87 s (0:08:47)**, no skips, no stall, concurrent with the coverage run |
| `git diff -- isocenter/` in the worktree | — | empty; the probe ran in `probe_tree/` and `probe_tree2/`, copies with `.git` removed |

The worktree this spec was written in was removed once mid-bunch (an
API rate limit ended the session) and recreated from `05d69e8` on
`spec/bunch-3-frozen-surface`; every measurement above was re-run or
its log re-read against the recreated tree, and the two trees are the
same commit.

## 12. Amendments (made during implementation)

Appended by the developer, 2026-09-08, against the owner's decisions
recorded on #379 and #381 the same day (Q1 (a), Q2 (a), Q3 (a), Q4 (a),
Q5 (a), Q6 (a), **Q7 (b)**, Q8 sentence, Q9 (a)). Nothing above is
edited; each item names the clause it corrects.

1. **Q7 (b) was taken, and measurement decided its shape.** §0.2 Q7
   said the developer "would have to measure what passes through
   `**kwargs` to `lock_identities_batch` today". Measured on 3.12.13:
   two things. `tags_to_lock`, read on the single-patient path only; and
   `auto_persist_chunk_size`, forwarded to the batch method. The
   README's and quickstart's own call, `lock_identities(report,
   tags_to_lock=[...])`, raised `TypeError:
   DicomSession.lock_identities_batch() got an unexpected keyword
   argument 'tags_to_lock'` on 0.9.3 -- the documented reversible step
   did not run, and `tests/test_documented_api_exists.py` grades method
   names in fences, not keywords, so nothing saw it. A misspelled keyword
   on the single path was swallowed. The shape landed: `lock_identities
   (patient_id, persist=False, verbose=True, tags_to_lock=None)`;
   `lock_identities_batch(patient_ids, auto_persist_chunk_size=0,
   tags_to_lock=None)`, forwarding `tags_to_lock` per patient;
   `_patient_obj` became the argument of a private
   `_lock_patient_identity(patient, persist, verbose, tags_to_lock)`;
   `auto_persist_chunk_size` stays the batch method's alone because it
   does nothing for one patient (a dead argument on that path, CLAUDE.md).
   The two in-tree callers passing it through `lock_identities`
   (`tests/test_optimization.py`, `tests/benchmarks/run_stress_test.py`)
   now call `lock_identities_batch`. **Corrects** §5.3's table rows for
   `lock_identities` and `lock_identities_batch`, §7.5 T-F1's literal,
   and §10 step 5's CHANGELOG draft, whose closing clause ("frozen as
   they stand and their cleanup is filed separately") is false and was
   not used. Pinned by the new `tests/test_lock_identities_signature.py`.
   The default tag list moved to a module-level `_DEFAULT_TAGS_TO_LOCK`
   (private) rather than a class attribute, which would have been a
   29th public name on `DicomSession`. `verbose` and `tags_to_lock` are
   **keyword-only**: the third positional slot used to be
   `_patient_obj`, and without the `*` a caller still filling it would
   have a `Patient` silently read as `verbose`; with it the call is a
   `TypeError` naming the positional count.
2. **§7.2 named the wrong binding.** `ingest()` reaches `run_parallel`
   through `DicomImporter.import_files`, bound in `isocenter.io_handlers`;
   `isocenter.session`'s binding never sees an ingest, so a spy there
   records nothing. The test spies `isocenter.io_handlers.run_parallel`
   (the file already does, and is already listed under `io_handlers.py`)
   and additionally asserts `_use_threads(False, None) is True` under
   the variable, so "read and ignored" is two assertions, not one.
3. **§7.5 T-F2's "imports no target module" needed one detour.**
   `IngestSummary` is bound only in `io_handlers`, and naming that
   module's dotted spelling in the file would make
   `test_every_test_that_imports_a_target_module_is_listed` demand a
   `TARGETS` entry -- exactly what Q6 exists to avoid. The pin reaches
   the class through the facade: `ingest()` on an empty directory
   returns an `IngestSummary` before any pool starts. `ExportSummary`,
   `PhiFinding`, `PhiReport`, `LockingResult` and `IsocenterConfiguration`
   are read as attributes of `isocenter.session`, which binds them. Also,
   `_ExportOptions` is not a dataclass, so the export-option pin (§9 item
   6) is `_export_dicom`'s parameter list. The Q9 vocabularies are pinned
   as quoted literals somewhere under `isocenter/`, and T-F4 also
   requires `stability.md` to list each word.
4. **§7.1's fixture, two corrections.** The redaction index keys on
   `series.equipment.device_serial_number` (`RedactionIndex.index_store`),
   not on the `(0018,1000)` attribute, so the fixture sets
   `Series.equipment`. And the pixels are non-zero on a 16x16 frame with
   a pixel outside the zone asserted unchanged: a zero image redacted to
   zero is green with the fix reverted. The graph is built in memory and
   saved rather than written as files and ingested; the failing path
   (`persist_pixel_data` in the child) is the same. Red with the §1.1
   message on 3.12.13 and 3.14.7t; green after; the three §7.1
   mutations killed on 3.12.13.
5. **§3.3's hardening covers three tests, not two.**
   `test_run_parallel_maxtasksperchild` has the same shape as the two
   `disable_gc` tests (one constructor mocked, the other reachable under
   a reroute) and was hardened the same way.
6. **The `test_memory_store_redaction_strategy.py` docstring** could not
   say "not `isocenter.parallel`" in so many words -- the completeness
   test reads file text and demanded the file the moment it did (§9 item
   9, now observed rather than predicted). Reworded.
7. **Q10 (owner, 2026-09-08, after the PR's review): `persist` and
   `verbose` are forwarded on the batch path.** Amendment 1's shape
   forwarded `tags_to_lock` alone, and the batch loop kept its hardcoded
   `persist=False, verbose=False`, so the README's form with
   `persist=True` added -- `lock_identities(report, persist=True)` --
   wrote nothing in silence
   (reviewer's probe on 3.12.13: no token in the store, the single-ID
   path reached it). Landed: `lock_identities_batch(patient_ids,
   auto_persist_chunk_size=0, tags_to_lock=None, *, persist=False,
   verbose=True)`, both forwarded per patient; `lock_identities` forwards
   all three. **Corrects** §5.3's `lock_identities_batch` row a second
   time and amendment 1's batch signature. Three tests, three mutants
   killed; `docs/api/stability.md`'s Q7 paragraph carries the Q10
   sentence.
8. **§7.5 T-F1's pin missed the parameter kind.** The pin was
   `(name, default)` pairs with a special case for `**options`; the
   reviewer removed the `*` from `lock_identities` and the freeze stayed
   green (only `test_lock_identities_signature.py` caught it). The pin
   is now the signature spelled as §5.3's table spells it -- `*` before
   the first keyword-only parameter, `/` after positional-only,
   `**name` -- and T-F4 parses the page's table and compares it to the
   pins row for row, so the page and the test cannot disagree. Removing
   either `*` is a red freeze test (0.8 s on 3.12.13).
9. **§5.3's entity sentences used constructor notation, and it was
   false.** `Study(study_instance_uid, study_date, study_time,
   date_shifted, series)` is not the dataclass order (`study_instance_uid,
   study_date, series, date_shifted, study_time`), and `attributes`,
   `sequences`, `attribute_vrs`, ~~`date_shifted`~~ on `Instance` are
   `init=False`, not constructor arguments. The page lists fields in
   `dataclasses.fields` order with the `init=False` ones marked; T-F1
   pins which of the ~~nine~~ frozen `Instance` fields `__init__`
   accepts, and T-F4 pins the page's lists against `dataclasses.fields`.
   **Superseded in part by #510 (v0.9.6):** `Instance.date_shifted` was
   cut, so there are eight frozen `Instance` fields and it is not among
   the `init=False` ones. T-F1 now also pins the field as *absent*, in
   both directions.

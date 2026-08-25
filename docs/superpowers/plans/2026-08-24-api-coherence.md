# API Coherence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the duplicate and contradicting public methods that make "where does Isocenter put files?" and "how do I sanitize a name?" have more than one answer, before the API is frozen for 1.0.

**Architecture:** Three DICOM directory layouts collapse to one; two folder-name sanitizers collapse to one; the dead `version` parameter and the dead `generate_export_from_db` entry point are deleted; `DicomSession` gains the context manager its `close()` already implies.

**Tech Stack:** Python 3.12+, pytest, pydicom 3.x.

## Global Constraints

- **Pre-1.0, breaking changes are allowed and expected.** Remove outright; do not add deprecation shims. Every removal gets a CHANGELOG entry under `### Changed` marked **BREAKING** with the exact old and new call form.
- **TDD.** Write the test first, run it, and quote the actual failure output before implementing. A test that has not been observed failing has not been shown to test anything.
- **Every test must be able to fail.** Nine false-passing tests were found on the previous branch. For each test you write, state what it would look like if it silently passed against broken code. Guard negative assertions (`X not in Y`) with a positive precondition proving the thing exists at all.
- **Use `.venv/bin/python` for every python and pytest invocation** (Python 3.14.7). A bare `python` is a pyenv 3.12.13 shim missing `wfdb` and `jsonschema`, under which whole test files silently skip and still report green. Always pass `-rs`; always report skip counts.
- **Baseline: 373 passed, 1 skipped, 0 failed.** The single skip is `tests/test_discovery_integration.py:45` (undeclared `faker`, tracked as #44) — do not fix it here.
- **Do not touch `isocenter/exporters/wfdb.py`'s `_sanitize`, `_sanitize_description` or `_sanitize_units`.** They are purpose-specific WFDB header/record-name rules, not duplicates. See Task 4.
- **De-identification behaviour must not regress.** Several of these paths feed export directory names, which carry PHI. If a change alters what reaches a folder name, say so explicitly.

---

## Background: what is actually duplicated

Measured, not assumed (survey run against `main` @ `574dc87`):

| Sanitizer | Location | `'A^B^C'` | `''` | `0` | `'café'` |
|---|---|---|---|---|---|
| `ConfigLoader.clean_filename` | `config_manager.py:212` | `'ABC'` | `''` | `'0'` | `'café'` |
| `DicomExporter._sanitize` | `io_handlers.py:1359` | `'ABC'` | `'Unknown'` | `'Unknown'` | `'café'` |
| `wfdb._sanitize` | `exporters/wfdb.py:43` | `'A_B_C'` | `'record'` | `'0'` | `'caf'` |

The first two are two answers to the same question (make a folder name) and differ only in falsy handling. The third is a different question (make a bare WFDB record-name token) and is correctly different.

| Layout | Built by | Reachable by a library user? |
|---|---|---|
| Hybrid | `export_folder_names` (`io_handlers.py:807`) | Yes — the only path from `session.export()` |
| Legacy | `_legacy_generate_export_contexts_folder_names` (`io_handlers.py:866`) | **Yes** — via public `DicomExporter.save_patient`/`save_studies`, used by 3 `scripts/` files |
| Third | inline in `generate_export_from_db` (`io_handlers.py:927`) | **No** — only caller anywhere is `tests/test_export_sql.py:71` |

---

### Task 1: Delete the dead `version` parameter

`_export_dicom` accepts `version=None`, its own docstring calls it "Deprecated/Unused", and it is never read anywhere in the body or passed to any callee. No test passes it.

**Files:**
- Modify: `isocenter/session.py` (`_export_dicom` signature ~1651, docstring ~1660)
- Test: `tests/test_api_coherence.py` (create)

**Interfaces:**
- Consumes: nothing
- Produces: `_export_dicom` no longer accepts `version`

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_coherence.py`:

```python
"""The public API must offer one way to do each thing.

Pre-1.0 cleanup: duplicate export layouts, duplicate sanitizers, and dead
parameters are removed rather than deprecated.
"""
import inspect

from isocenter.session import DicomSession


def test_export_does_not_accept_a_dead_version_parameter():
    """`version` was accepted, documented as unused, and never read.

    A parameter that silently does nothing is worse than no parameter:
    a caller passing it reasonably believes it took effect.
    """
    signature = inspect.signature(DicomSession._export_dicom)
    assert "version" not in signature.parameters, (
        "`version` is still accepted by _export_dicom; it is never read, so "
        "any caller passing it is silently ignored")
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_api_coherence.py -q -rs`
Expected: FAIL, `assert 'version' not in ...`. Quote the actual output.

- [ ] **Step 3: Delete the parameter**

Remove `version=None` from the `_export_dicom` signature and the "Deprecated/Unused" line from its docstring. Change nothing else.

- [ ] **Step 4: Run the test and the full suite**

Run: `.venv/bin/python -m pytest tests/test_api_coherence.py -q -rs`
Expected: PASS.
Run: `.venv/bin/python -m pytest -q -rs 2>&1 | tail -5`
Expected: 374 passed, 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add isocenter/session.py tests/test_api_coherence.py
git commit -m "refactor: delete the dead version parameter from export

Accepted, documented as Deprecated/Unused, never read, and passed by no
test. A parameter that silently does nothing misleads any caller who
passes it."
```

---

### Task 2: Delete `generate_export_from_db`, the third layout

A public staticmethod with its own inline copy of the folder-format strings and no production caller — its only caller anywhere is `tests/test_export_sql.py:71`. It is a third answer to "where do files go", kept alive solely by the test that exercises it.

**Files:**
- Modify: `isocenter/io_handlers.py` (delete `generate_export_from_db`, lines ~927-1053)
- Delete: `tests/test_export_sql.py`
- Modify: `tests/test_shared_executor_lifecycle.py` (a `patch()` of the removed name at line ~85)
- Test: `tests/test_api_coherence.py`

**Interfaces:**
- Consumes: nothing
- Produces: `DicomExporter.generate_export_from_db` no longer exists

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_coherence.py`:

```python
def test_only_one_public_export_entry_point_builds_directory_trees():
    """`generate_export_from_db` was a third folder-naming scheme.

    It duplicated the format strings inline rather than sharing a helper,
    and nothing but a test ever called it.
    """
    from isocenter.io_handlers import DicomExporter

    assert not hasattr(DicomExporter, "generate_export_from_db"), (
        "generate_export_from_db still exists; it is a third, "
        "independently-maintained directory layout with no production caller")
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_api_coherence.py -q -rs`
Expected: FAIL on `hasattr`. Quote the output.

- [ ] **Step 3: Confirm the caller graph before deleting**

Run and paste the output into your report:

```bash
grep -rn "generate_export_from_db" isocenter/ scripts/ tests/ --include='*.py'
```

Expected: definitions in `isocenter/io_handlers.py`, a call in `tests/test_export_sql.py`, a `patch()` in `tests/test_shared_executor_lifecycle.py`. **If you find any other caller — especially under `scripts/` — stop and report BLOCKED**: the reachability premise of this task is wrong.

- [ ] **Step 4: Delete**

Delete the `generate_export_from_db` method. Delete `tests/test_export_sql.py`. In `tests/test_shared_executor_lifecycle.py`, remove the test that patches the removed name — read it first and say in your report what coverage is lost, if any.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q -rs 2>&1 | tail -5`
Report the exact counts and the delta from 374.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: delete generate_export_from_db, a third export layout

A public staticmethod with its own inline copy of the folder-format
strings and no production caller -- reached only by the test that
exercised it. Removing it leaves two layouts, unified in the next commit."
```

---

### Task 3: Unify the legacy export layout onto the shared helper

`DicomExporter.save_patient`/`save_studies` are public and used by three `scripts/` files, but produce a different tree than `session.export()`: no UID suffix, no modality, a different date format. This was attempted once during the WFDB work and reverted because `tests/test_structured_export.py` pins the legacy strings. Those assertions are the thing being changed, so update them deliberately.

**Files:**
- Modify: `isocenter/io_handlers.py` — `_generate_export_contexts` (~1055) to call `export_folder_names` (807); delete `_legacy_generate_export_contexts_folder_names` (866-902)
- Modify: `tests/test_structured_export.py` (hardcoded legacy assertions)
- Test: `tests/test_api_coherence.py`

**Interfaces:**
- Consumes: `export_folder_names(patient, study, series) -> (subj_name, study_folder, series_folder)` from `io_handlers.py:807`. Note it takes **three** arguments — no `instance`. The legacy helper it replaces took `(patient, s_date_str, series, instance)`, so the call shape changes as well as the output.
- Produces: `save_patient` and `session.export()` produce identical trees for the same data

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_coherence.py`:

```python
def test_both_public_export_paths_produce_the_same_tree(tmp_path):
    """`DicomExporter.save_patient` and `session.export()` must agree.

    Both are public and shipped. Two layouts means "where does Isocenter put
    files" has no single answer for a library user.

    Derives both trees from real exports rather than hardcoding names, so
    it cannot drift out of step with the naming logic it guards.
    """
    from isocenter.io_handlers import DicomExporter
    from isocenter.session import DicomSession
    from scripts.generate_waveform_test_data import write_fixture

    source = tmp_path / "src"
    source.mkdir()
    write_fixture(str(source / "ecg.dcm"), num_samples=50)

    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        session.ingest(str(source))
        patient = session.store.patients[0]

        via_session = tmp_path / "via_session"
        session.export(str(via_session), format="dicom")

        via_exporter = tmp_path / "via_exporter"
        DicomExporter.save_patient(patient, str(via_exporter))
    finally:
        session.close()

    def tree(root):
        return sorted(
            str(p.relative_to(root).parent)
            for p in root.rglob("*.dcm"))

    session_tree = tree(via_session)
    exporter_tree = tree(via_exporter)

    assert session_tree, "session.export() produced no .dcm files"
    assert exporter_tree, "save_patient produced no .dcm files"
    assert session_tree == exporter_tree, (
        f"the two public export paths disagree:\n"
        f"  session.export(): {session_tree}\n"
        f"  save_patient():   {exporter_tree}")
```

- [ ] **Step 2: Run it, confirm it fails, and record both trees**

Run: `.venv/bin/python -m pytest tests/test_api_coherence.py -q -rs`
Expected: FAIL showing two different directory strings. **Paste both trees into your report** — they are the before/after evidence and belong in the CHANGELOG.

- [ ] **Step 3: Point `_generate_export_contexts` at the shared helper**

In `isocenter/io_handlers.py` at ~line 1118, replace the call to `_legacy_generate_export_contexts_folder_names` with `export_folder_names(patient, st, se)`. The enclosing loops (`for st in studies:` at 1076, `for se in st.series:` at 1077) already bind the study and series objects, so no plumbing is needed; `s_date_str` and `inst` become unused at this call site — check whether `s_date_str` is still needed elsewhere in the loop before removing it. Then delete `_legacy_generate_export_contexts_folder_names` entirely, along with the docstring paragraph explaining why it was kept unshared — that reason no longer applies.

- [ ] **Step 4: Update the tests that pin the old strings**

`tests/test_structured_export.py` asserts the legacy names. Update each to the new scheme. For every assertion you change, record the old and new expected value in your report. **Do not weaken an assertion to make it pass** — if a test checked that a folder name contains the study description, it must still check that; only the expected string changes.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q -rs 2>&1 | tail -8`
Report exact counts. Every failure must be a test asserting the *old* layout; if anything else fails, investigate and report rather than adjusting the test.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: one DICOM export layout for both public entry points

DicomExporter.save_patient produced a different tree than
session.export() -- no UID suffix, no modality, a different date format
-- so a library user got a layout that depended on which public API they
happened to call. Both now use export_folder_names.

BREAKING: save_patient/save_studies output paths change."
```

---

### Task 4: One folder-name sanitizer

`ConfigLoader.clean_filename` and `DicomExporter._sanitize` are two answers to the same question. After Task 3, `_sanitize` should have no folder-naming callers left. It differs from `clean_filename` only in substituting `"Unknown"` for falsy input — including the integer `0`, which is a meaningful series number.

`isocenter/exporters/wfdb.py`'s `_sanitize` is **not** in scope: WFDB record names must be bare ASCII tokens, so it replaces `^`/`/`/space with `_` instead of deleting them, and `wfdb.py`'s own docstring warns against using it for folder names. Leave it, and leave `_sanitize_description`/`_sanitize_units` (WFDB header field rules).

**Files:**
- Modify: `isocenter/io_handlers.py` (delete `DicomExporter._sanitize`, ~1359; update remaining callers)
- Test: `tests/test_api_coherence.py`

**Interfaces:**
- Consumes: `ConfigLoader.clean_filename` from `isocenter/config_manager.py:212`
- Produces: `DicomExporter._sanitize` no longer exists

- [ ] **Step 1: Find the remaining callers**

Run and paste into your report:

```bash
grep -rn "_sanitize" isocenter/ scripts/ tests/ --include='*.py' | grep -v "exporters/wfdb.py"
```

For each remaining call site, decide whether `clean_filename` is a behaviour-preserving substitute. **The falsy case is the real difference**: `_sanitize(0)` is `"Unknown"`, `clean_filename(0)` is `"0"`. If any call site passes a series number or similar, `"0"` is the correct output and `"Unknown"` was a latent bug — say so in your report rather than preserving it.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_api_coherence.py`:

```python
def test_one_sanitizer_for_folder_names():
    """Two folder-name sanitizers is one too many.

    `wfdb._sanitize` is deliberately excluded -- WFDB record names are
    bare ASCII tokens with different rules, documented as such.
    """
    from isocenter.io_handlers import DicomExporter

    assert not hasattr(DicomExporter, "_sanitize"), (
        "DicomExporter._sanitize still exists alongside "
        "ConfigLoader.clean_filename; both sanitize folder names")


def test_a_zero_series_number_is_not_renamed_to_unknown():
    """`_sanitize(0)` returned 'Unknown' because `0` is falsy.

    A series number of 0 is a real value, not a missing one.
    """
    from isocenter.config_manager import ConfigLoader

    assert ConfigLoader.clean_filename(0) == "0"
```

- [ ] **Step 3: Run it, confirm the first test fails**

Run: `.venv/bin/python -m pytest tests/test_api_coherence.py -q -rs`
Expected: `test_one_sanitizer_for_folder_names` FAILS on `hasattr`. The zero test should already pass — it documents `clean_filename`'s existing correct behaviour and pins it against future change. Say so in your report rather than presenting it as a fix.

- [ ] **Step 4: Migrate callers and delete**

Replace each remaining `DicomExporter._sanitize(...)` with `ConfigLoader.clean_filename(...)`, then delete the method. Where the falsy behaviour genuinely mattered (a name that really can be absent), make it explicit at the call site — `clean_filename(value or "Unknown")` — rather than hiding it in the sanitizer.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q -rs 2>&1 | tail -8`
Report exact counts.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: one sanitizer for folder names

ConfigLoader.clean_filename and DicomExporter._sanitize both existed to
make a name safe for a directory, differing only in substituting
'Unknown' for falsy input -- including the integer 0, a real series
number. Callers that genuinely have an absent name now say so explicitly.

wfdb._sanitize is untouched: WFDB record names are bare ASCII tokens with
deliberately different rules."
```

---

### Task 5: Collapse the `compression` and `safe` aliases

`_export_dicom` carries a "Legacy Argument Mapping" block:

```python
if compression is not None:
    use_compression = compression
if safe:
    check_burned_in = True
```

Two names for one behaviour each. Unlike `version` these are live and tested, so this is a rename with test updates, not a deletion. `subset` is **not** in scope — it is a real feature (query/DataFrame/UID-list resolution), not an alias.

**Files:**
- Modify: `isocenter/session.py` (`_export_dicom` signature and the legacy-mapping block)
- Modify: the tests listed in Step 1
- Test: `tests/test_api_coherence.py`

**Interfaces:**
- Consumes: nothing
- Produces: `_export_dicom(folder, use_compression=True, check_burned_in=False, check_reversibility=True, patient_ids=None, show_progress=True, subset=None)`

- [ ] **Step 1: Inventory the callers**

Run and paste into your report:

```bash
grep -rn "safe=\|compression=" tests/ scripts/ --include='*.py'
```

Known: `safe=` in `test_safe_export.py`, `test_shared_executor_lifecycle.py`, `test_profile_end_to_end.py` (×2), `test_safe_export_jitter.py`, `test_safe_export_feedback.py`, `tests/profile_memory.py`, `tests/benchmarks/run_stress_test.py`. `compression=` in `test_feature_regression.py:75` and `tests/benchmarks/run_stress_test.py:136`. Note `use_compression=` is the canonical name and already used elsewhere — do not rewrite those.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_api_coherence.py`:

```python
def test_export_offers_one_name_per_behaviour():
    """`safe` and `compression` were aliases for parameters that already
    existed, so two spellings produced the same effect."""
    signature = inspect.signature(DicomSession._export_dicom)
    for alias, canonical in (("safe", "check_burned_in"),
                             ("compression", "use_compression")):
        assert alias not in signature.parameters, (
            f"`{alias}` is still accepted; it is an alias for `{canonical}`")
        assert canonical in signature.parameters, (
            f"`{canonical}` is missing -- the alias was removed but the "
            "canonical parameter did not survive")
```

- [ ] **Step 3: Run it, confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_api_coherence.py -q -rs`
Expected: FAIL on `safe`. Quote the output.

- [ ] **Step 4: Remove the aliases and update callers**

Delete `compression=None` and `safe=False` from the signature and delete the legacy-mapping block. Update every call site from Step 1 to the canonical name: `safe=True` becomes `check_burned_in=True`, `compression=X` becomes `use_compression=X`.

Watch for `safe=False` call sites — these become `check_burned_in=False`, which is already the default, so prefer deleting the argument entirely over writing a redundant one.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q -rs 2>&1 | tail -8`
Report exact counts.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: one parameter name per export behaviour

safe/check_burned_in and compression/use_compression were two spellings
each for one behaviour, mapped by a legacy block inside _export_dicom.

BREAKING: session.export(..., safe=True) is now check_burned_in=True, and
compression= is now use_compression=."
```

---

### Task 6: Make `DicomSession` a context manager

`close()` shuts down a `ProcessPoolExecutor`, the persistence-manager thread, and the audit thread that owns the sqlite connection. There is no `__enter__`/`__exit__`, so a caller who forgets `close()` leaks worker subprocesses. Every test in the suite already wraps sessions in `try/finally` by hand.

**Files:**
- Modify: `isocenter/session.py` (add `__enter__`/`__exit__` near `close()`, ~165)
- Test: `tests/test_api_coherence.py`

**Interfaces:**
- Consumes: `DicomSession.close()`
- Produces: `DicomSession.__enter__() -> DicomSession`, `DicomSession.__exit__(exc_type, exc, tb) -> None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_coherence.py`:

```python
def test_session_can_be_used_as_a_context_manager(tmp_path):
    """close() releases a process pool and two threads; forgetting it
    leaks worker subprocesses. `with` is how Python spells that."""
    from concurrent.futures import BrokenExecutor

    with DicomSession(persistence_file=str(tmp_path / "ctx.db")) as session:
        assert session.store is not None, "session unusable inside `with`"
        executor = session._executor

    try:
        executor.submit(int, "1")
        raised = None
    except (RuntimeError, BrokenExecutor) as exc:
        raised = exc

    assert raised is not None, (
        "the process pool still accepts work after the `with` block; "
        "__exit__ did not call close()")


def test_context_manager_closes_the_session_when_the_body_raises(tmp_path):
    """A leak on the error path is the one that matters -- that is
    precisely when a caller's own `close()` gets skipped."""
    session = DicomSession(persistence_file=str(tmp_path / "boom.db"))
    executor = session._executor

    class Boom(Exception):
        pass

    try:
        with session:
            raise Boom("failure inside the with-body")
    except Boom:
        pass
    else:
        raise AssertionError("__exit__ swallowed the exception; it must not")

    try:
        executor.submit(int, "1")
        raised = None
    except (RuntimeError, BrokenExecutor) as exc:
        raised = exc

    assert raised is not None, (
        "the process pool survived an exception in the with-body")
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_api_coherence.py -q -rs`
Expected: FAIL with `AttributeError: __enter__` (or `TypeError` about the context manager protocol). Quote the output.

- [ ] **Step 3: Implement**

In `isocenter/session.py`, beside `close()`:

```python
    def __enter__(self) -> "DicomSession":
        """Support `with DicomSession(...) as session:`.

        `close()` releases a ProcessPoolExecutor and two threads holding
        sqlite handles. Without this, forgetting it leaks worker
        subprocesses for the life of the process.
        """
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        """Always close, including when the body raised.

        Returns None so exceptions propagate -- a session manager that
        swallowed them would hide the caller's failure.
        """
        self.close()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_api_coherence.py -q -rs`
Expected: PASS.
Run: `.venv/bin/python -m pytest -q -rs 2>&1 | tail -5`
Report exact counts.

- [ ] **Step 5: Prove `__exit__` is load-bearing**

Mutate `__exit__` to `pass` (do not call `close()`), re-run
`tests/test_api_coherence.py`, and confirm both context-manager tests
FAIL. Restore, confirm green, and verify `git status --porcelain` is
clean apart from intended changes. Quote the failure output — a context
manager test that passes without `close()` is testing nothing.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: DicomSession supports the context manager protocol

close() releases a ProcessPoolExecutor and two threads holding sqlite
handles; without __enter__/__exit__ a caller who forgets it leaks worker
subprocesses. __exit__ returns None so exceptions still propagate."
```

---

### Task 7: Remove the stale comments and document everything

`active_rules`/`active_phi_tags` do not exist — they were fully migrated to `configuration.rules`/`configuration.phi_tags`. Only misleading prose survives.

**Files:**
- Modify: `isocenter/session.py:1581` (comment referencing `active_rules`)
- Modify: `tests/test_scaffold_features.py:112`, `tests/test_redact_error.py:9,102-103` (comments only)
- Modify: `CHANGELOG.md`
- Modify: `docs/` — any page documenting a removed parameter or the legacy export layout

- [ ] **Step 1: Fix the stale comments**

Update each to name `configuration.rules` / `configuration.phi_tags`. These are comment-only edits; change no logic.

- [ ] **Step 2: Find documentation that describes what we removed**

```bash
grep -rn "save_patient\|generate_export_from_db\|safe=\|compression=\|version=" docs/*.md README.md
```

Update each hit. Where docs show `session.export(...)`, prefer the `with` form now that it exists.

- [ ] **Step 3: Write the CHANGELOG entries**

Under `## [Unreleased]` → `### Changed`, one **BREAKING** bullet per removal, each giving the exact old and new call form. Use the real before/after directory trees recorded in Task 3 Step 2 for the layout change.

- [ ] **Step 4: Verify every documented example still runs**

Any code sample you touched must actually execute. Note that `session.ingest()` fails under multiprocessing spawn from `python -c` or a heredoc — write probes to a real `.py` file with an `if __name__ == "__main__":` guard.

- [ ] **Step 5: Run the full suite and commit**

```bash
.venv/bin/python -m pytest -q -rs 2>&1 | tail -5
git add -A
git commit -m "docs: record the API coherence removals

Corrects comments referencing active_rules/active_phi_tags, which were
migrated to configuration.rules/configuration.phi_tags and no longer
exist under those names."
```

---

## Out of scope

- `subset` — a real feature, not an alias.
- `wfdb._sanitize`, `_sanitize_description`, `_sanitize_units` — purpose-specific WFDB rules.
- Module decomposition (`session.py`, `persistence.py`, `io_handlers.py`) — a separate tranche.
- #44 (`faker` undeclared), #46 (codec priority ignored), #38/#39 (PHI gaps) — tracked separately.

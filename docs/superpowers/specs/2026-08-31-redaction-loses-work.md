# Redaction Loses Work Between Where It Happens And Where It Lands

**Date:** 2026-08-31
**Status:** Design approved, awaiting implementation
**Tracking:** #229 (a multi-zone rule applies only the last zone) and #228
(a redacted instance keeps its source SOP Instance UID on the processes
path). First cycle of the **v0.9.1 "Reports Success While Wrong"**
milestone.
**Base:** `main` at `4507d48`
**Measured on:** `3.12.13` and `3.14.7t` (free-threaded, `sys._is_gil_enabled()`
asserted `False` inside the process after imports), macOS, file-backed
`SqliteStore`. Every figure below was produced with `PYTHONPATH` pinned to
this worktree and `isocenter.__file__` asserted to resolve inside it;
without that pin a script run from outside the tree imports the main
repo's editable install and measures the wrong code.

---

## Context

Both issues are the same sentence with a different object: **the redaction
path loses work between where the work happens and where it lands.**

- #229 loses it *within one instance*. `_apply_roi_to_instance` copies a
  read-only array and rebinds a **local** name; the caller's loop hands it
  the pristine original again on the next zone. With N zones, N−1 are
  discarded.
- #228 loses it *across the process boundary*. `regenerate_uid()` runs in
  the worker; the parent never applies the result. On a GIL build the
  redacted instance keeps the source's identity.

Neither raises. #213's `RedactionError` and its `ERROR` audit row cannot
see either one, which is why both survive a run that reports success and a
report that grades `PASS`.

They are nevertheless **two PRs**. §0 rules on that and states the landing
order.

---

## 0. One fix or two — the ruling

**Two PRs, in this cycle, in this order: #229 first, #228 rebased on it.**
Clause numbering is contiguous per PR: **§A\*** is PR 1 (#229), **§B\*** is
PR 2 (#228).

The two do edit the same two function bodies (`execute_redaction_task`,
`redact_machine_instances`), which is the case for bundling. It loses to
three things:

1. **They differ in kind.** #229 is a data-loss bug: measured PHI on disk
   in an exported file, under a report that reads `**Validation Status** |
   **PASS**` (§1.3, measured here, not relayed). #228 is an *identity*
   defect whose fix requires a policy ruling (§B0) that a reviewer may
   legitimately want to argue. Bundling puts the PHI fix behind that
   argument.
2. **Only one of them is a documented behaviour change.** #228 changes what
   `session.export()` names its output files on 3.12 and what
   `instance.sop_instance_uid` holds after `redact()`. It needs a
   CHANGELOG entry with a migration note (§B5). #229 needs a CHANGELOG
   entry that says only "this was broken and is now not". One PR with both
   produces an entry where the breaking half and the bug-fix half are
   indistinguishable, which is the opposite of what CHANGELOG.md is for in
   this repo.
3. **The dependency runs one way and is weak.** PR 1 lands the
   reloaded-instance fixture (§A6); PR 2 *may* reuse it but does not need
   it — #228's tests need a threads/processes lever, not a read-only array,
   and an in-memory instance exhibits the divergence perfectly well
   (§B6/T8). So PR 2 rebasing on PR 1 costs a trivial rebase in
   `services.py` and buys clean bisection.

**Rebase surface, stated so the coder does not discover it:** PR 2 touches
`services.py:365` (`inst.regenerate_uid()`), `services.py:381-382` (the
mutation dict's two UID keys) and `services.py:625`. PR 1 touches
`services.py:358-361` and `services.py:618-621` (the zone loops) and
deletes `services.py:772-810`. The edits are adjacent but disjoint; the
`git rebase` is textual only.

---

## 1. Reproduction — #229

All figures on `4507d48`. Fixture: one patient / study / series / instance,
32×32 `uint8` filled with 200, one machine rule with **two disjoint zones**
`[[0,8,0,8],[16,24,16,24]]`. The graph is built in memory, `save()`d, the
session `close()`d and **reopened**, so the array arrives through
`SidecarPixelLoader`.

### 1.1 The mechanism, verified in the code

`SidecarPixelLoader.__call__` (`io_handlers.py:1461`) builds the array with
`np.frombuffer(raw, dtype=dt)`. A `bytes` buffer is immutable, so the
array and every reshape/transpose view of it is **read-only**. Measured:
`writeable at load: False`.

Both callers fetch once and loop:

```python
arr = inst.get_pixel_data()          # services.py:353 / :605
...
modified = False
for roi in rois:                     # services.py:358-361 / :618-621
    if self._apply_roi_to_instance(inst, arr, roi):
        modified = True
```

and the callee rebinds locally (`services.py:801-803`):

```python
if not arr.flags.writeable:
    arr = arr.copy()
    inst.set_pixel_data(arr)
```

The name `arr` inside `_apply_roi_to_instance` is a parameter. Rebinding it
cannot reach the caller's `arr`, which still refers to the read-only
original. Zone 2 therefore copies the **pristine** source a second time and
`set_pixel_data` replaces the instance's array with it, discarding
everything zone 1 wrote. Only the last zone survives.

### 1.2 Measured

```
writeable at load: False
redact returned: 1
zone1 sum: 12800    <- untouched, still 8*8*200
zone2 sum: 0        <- redacted
```

Identical on **3.12.13** (processes) and on **3.14.7t** (threads,
`_is_gil_enabled() == False`). The defect is not a concurrency artefact: it
is arithmetic on names, so both executors reproduce it.

### 1.3 It reaches disk, and it grades PASS

Same fixture as an SC Image Storage instance, `anonymize()` → two-zone
`redact()` → `save()` → rules cleared → `export()` → `generate_report()`.
Measured here on 3.12.13:

```
B exported:   ['1.2.3.b.dcm']
B ON DISK     zone1= 12800   zone2= 0
report line:  | **Validation Status** | **PASS** |
```

The exported DICOM file carries the burned-in identifier from zone 1, and
the compliance report grades the run `PASS`. The grade follows from the
mechanism rather than from luck: nothing raised, so no `ERROR` row exists,
and nothing was dropped, so no `DATA_LOSS` row exists.

The rules are cleared before `export()` on purpose, because
`_export_instance_worker` (`io_handlers.py:983`) re-applies
`ctx.redaction_zones` in a **single** `apply_redaction_to_array` call and
so silently repairs the damage on the way out whenever the export runs
under the same configuration. That repair is what has been hiding this.
It is unavailable on every other ordinary route: a second session that
loaded the store without the config, `DicomExporter.write_tree()` (#78,
applies no zones at all), a serial that no longer matches at export time,
or simply *reading the saved store* — `save()` persists the half-redacted
array to the sidecar, so the stored dataset carries the identifier whatever
a later export does.

### 1.4 Why the whole suite misses it

Every redaction fixture in `tests/` is either built in memory
(`inst.set_pixel_data(np.full(...))`) or read from a source file through
pydicom. **Both give a writeable array**, where `_apply_roi_to_instance`
takes its other arm, mutates in place, and is correct. There is no
redaction test anywhere in the suite that exercises a reloaded instance.
Closing that gap (§A6) is half of this PR.

`tests/test_redaction_failure_is_reported.py:400-405` contains a correct
*description* of this bug, written as a reason its fixture could not answer
a different question — "`_apply_roi_to_instance` copies it afresh for every
ROI and the instance ends holding a copy of the *pristine* original".
`CHANGELOG.md` on the #213 branch drew the opposite inference from the same
measurement ("there is no partial mutation to persist"). The measurement
was right both times; only the inference was wrong.

---

## 2. Reproduction — #228

Same shape, one zone, `redact()` then inspect the parent.

| build | `inst.sop_instance_uid` after `redact()` | store after close |
| --- | --- | --- |
| 3.12.13 (processes) | `1.2.3.phi` — **unchanged** | `instances`: 1 row under `1.2.3.phi`; `instance_blobs`: **2** rows — `1.2.3.phi` and an orphan under the worker's regenerated UID |
| 3.14.7t (threads) | `1.2.826.…` — regenerated | `instances`: 1 row, new UID; `instance_blobs`: 1 row, new UID |

`execute_redaction_task` calls `inst.regenerate_uid()` (`services.py:365`)
and returns the result as `mutation["sop_uid"]` (`services.py:382`). The
parent reads that key **only as a fallback lookup key**
(`session.py:2095`) and never assigns it. `mutation["attributes"]` carries
exactly `0008,0008`, `0028,0301`, `0008,2111` and
`_ISOCENTER_REDACTION_HASH`; **`0008,0018` is not among them.**

Two consequences beyond the issue's four:

- **The exported filename differs by gate interpreter.** Measured:
  `1.2.3.b.dcm` on 3.12 versus a generated UID on 3.14.7t, from the same
  input. `io_handlers.export_folder_names` and both write paths name DICOM
  files by SOP Instance UID (#78).
- **The processes path leaves an orphan `instance_blobs` row** and a second
  full copy of every redacted image's bytes in the sidecar. The worker
  regenerated *before* `persist_pixel_data`, so the blob went in under a
  UID no instance in the parent's graph carries. `compact()` reclaims it
  (`persistence.py:2378-2390` collects exactly these as `orphan_ids`), so
  it is dead space rather than a permanent leak — but it grows without
  bound for anyone who redacts and never compacts, and it is the same class
  as the defect fixed in CHANGELOG.md:786 (rows keyed by a UID whose
  instance is gone).

`tests/test_api_coherence.py` **never calls `redact()`** — verified by
grep; its only occurrence of the word is a docstring analogy at line 105 —
so its two-export-path comparison genuinely cannot see this.

---

## 3. Why no existing test catches #228

`tests/test_redaction_failure_is_reported.py::_instances` builds its
UID→instance map *before* `redact()`, with a docstring explaining that on
the threads path the worker mutates the parent's own object so the key is
gone. That is legitimate usage — and it means **no test in the suite
asserts what the parent's SOP Instance UID is after a redaction.**

`tests/test_redaction_consistency.py` looks like coverage and is not. Its
line 42 **replicates** `sop = mutation.get('original_sop_uid') or
mutation.get('sop_uid')` in the test body rather than calling production
code, so deleting that expression from `session.py` leaves it green. It is
listed in §B7 as *not* evidence.

---

# PR 1 — #229

## §A0. The shape

Make the loop stop existing. Both callers apply the **whole zone list in
one call**, which is the shape the export worker already uses
(`io_handlers.py:983`) — "one spelling per behaviour". The per-zone wrapper
is deleted and replaced by a per-**instance** one that owns the
writeability copy.

The alternative in #229's first bullet — hoist the copy into the two
callers and keep the per-zone wrapper — was rejected. It fixes the loss but
leaves a method named `_apply_roi_to_instance` that no longer manages the
instance, and it leaves the per-zone loop in place for the next person to
add a rebind to. It also keeps N−1 redundant full-array copies unless the
callers are changed anyway, which is the same edit.

## §A1. `RedactionService._redact_instance_pixels`

Delete `_apply_roi_to_instance` (`services.py:772-810`). Add, in its place:

```python
def _redact_instance_pixels(self, inst: Instance, arr, rois: List[tuple]) -> bool:
    if not arr.flags.writeable:
        arr = arr.copy()
        inst.set_pixel_data(arr)

    geometry = resolve_pixel_geometry(arr.shape, inst.attributes)
    return self.apply_redaction_to_array(arr, rois, geometry=geometry)
```

Name chosen to be **grep-distinct** from the deleted one. `_apply_rois_to_instance`
was rejected: a one-character difference from a name that meant something
else is exactly the "spelling that hides" CLAUDE.md warns about after #78.

Three properties the coder must not lose:

1. **One call, one `rois` list.** Not a loop with `[roi]`. The loop is the
   defect.
2. **`geometry` is resolved after the copy.** `set_pixel_data` can correct
   a descriptor, and the redaction has to address the array the way the
   instance now describes it. This is the existing ordering; keep it.
3. **`geometry` is passed by keyword** and is required (#217). Do not add a
   default here.

## §A2. Both callers

`services.py:358-361` (`execute_redaction_task`):

```python
modified = self._redact_instance_pixels(inst, arr, rois)
```

`services.py:618-621` (`redact_machine_instances`): the same line, one
indent level deeper. `inst._pixel_hash = None` at `:616` stays exactly
where it is (see §A9).

**Neither caller may read `arr` after the call.** Verified on `4507d48`
that neither does today. §A3's comment states the invariant that makes that
safe.

## §A3. The docstring moves, it does not disappear

`_apply_roi_to_instance`'s long docstring documents a **two-arm dirtying
asymmetry that still exists after this change**:

```
writeable=False  returned=True  dirty=True   zone_zeroed=True
writeable=True   returned=True  dirty=False  zone_zeroed=True
```

The read-only arm copies and calls `set_pixel_data`, which ends in an
unconditional `mark_modified()`. The writeable arm mutates in place, never
calls the setter, and leaves `has_unsaved_changes` False — the callers'
`inst.mark_modified()` under `if modified:` is what closes it. **Move that
text to `_redact_instance_pixels` unchanged in substance**, updating only
the method name and the line references.

Add to it the invariant that replaces the deleted loop:

> Call this **at most once per instance per pass, with the full zone
> list**. It rebinds `arr` locally on the not-writeable arm, so a caller
> that called it once per zone would hand it the pristine original again
> every time and keep only the last zone's work — with `modified` True, a
> redaction hash written, and a report grading PASS. That was #229. There
> is deliberately no per-zone entry point to call in a loop.

## §A4. Cross-references to the deleted name

Deleting the method orphans five references. All are **required edits**;
leaving a comment pointing at a method that no longer exists is the class
#234 is open about.

| file:line | what it says | required edit |
| --- | --- | --- |
| `isocenter/entities.py:761` | "callers mutate arrays in place (`RedactionService._apply_roi_to_instance`)" | rename to `_redact_instance_pixels`; the claim stays true |
| `isocenter/services.py:721` | "Both callers (\_apply_roi_to_instance and the export worker) copy first" | rename; still exactly two callers |
| `tests/test_pixel_geometry_pipeline.py:566, 590, 599, 849` | four docstring references | rename; the described behaviour is unchanged (§A3) |
| `tests/test_redaction_failure_is_reported.py:402` | "copies it afresh for every ROI … the instance ends holding a copy of the *pristine* original" | **rewrite** — this becomes false (§A8) |
| `CHANGELOG.md:127` | historical entry | **leave alone.** A changelog records what was true at the time. |

## §A5. Call sites in tests

`grep -rn "_apply_roi_to_instance" --include="*.py" .` finds exactly three
executable call sites, all passing a single ROI:

| file:line | array | required edit |
| --- | --- | --- |
| `tests/test_redaction_roi.py:30` | writeable, 100×100 | `service._redact_instance_pixels(inst, arr, [roi])` |
| `tests/test_redaction_roi.py:55` | writeable, 50×50×3 | same |
| `tests/test_pixel_geometry_pipeline.py:639` | **read-only** 8×8 | `service._redact_instance_pixels(inst, arr, [(0, 4, 0, 4)])` |

Every assertion in all three survives verbatim. The third is the pin on the
copy arm's dirtying and is the reason the copy stays **inside** the new
method rather than moving to the callers: move it out and that test's
subject ceases to exist.

## §A6. The fixture gap — `reloaded_redaction_session`

Add to `tests/conftest.py` a factory fixture. It is the asset this PR
leaves behind and the reason §A7's tests are possible at all.

```python
@pytest.fixture
def reloaded_redaction_session(tmp_path):
    """A saved-and-reopened session whose pixels arrive read-only.

    Build -> save() -> close() -> reopen, so `get_pixel_data()` comes back
    through `SidecarPixelLoader`, which builds its array with
    `np.frombuffer` over an immutable `bytes` buffer and is therefore
    **not writeable**. That is the ordinary shape for any instance loaded
    from a saved store -- the documented ingest -> save -> reopen ->
    redact workflow -- and until #229 no redaction test in the suite used
    it. Every other fixture builds the graph in memory or reads a source
    file, and both give a *writeable* array, where the redaction path
    mutates in place and is correct.

    The `flags.writeable is False` assertion is the fixture's guard on
    itself: give this instance a `file_path` and pydicom hands back a
    writeable array, at which point every test built on it goes vacuous
    rather than red.
    """
```

Contract:

- Returns a callable `make(zones, *, serial="SN_RELOAD", uid="1.2.3.reload",
  shape=(32, 32), fill=200, sop_class=SC_STORAGE, name="reload")` →
  `(session, instance)`.
- Builds `Patient → Study → Series(Equipment(serial)) → Instance`, sets
  `instance.file_path = None`, `set_pixel_data(np.full(shape, fill, uint8))`.
- **The instance must be exportable, and this is not optional.** Set
  `sop_class = "1.2.840.10008.5.1.4.1.1.7"` (SC Image Storage) and, on the
  instance, `0008,0016` = that class, `0008,0030` = `"120000"`,
  `0008,0060` = `"OT"`, `0028,0004` = `"MONOCHROME2"`. Measured on
  `4507d48`: a CT-class instance without these fails export validation
  with `['[Type 1 Error] Missing 0008,0030 in Common', '[Type 2 Error]
  Missing 0018,0050 in CTImage', … 'Missing 0028,0030 in CTImage']`,
  `export()` **does not raise**, and the output tree is empty. Under SC
  the CTImage module does not apply, which is why only the Common-module
  `0008,0030` has to be supplied. Any test that walks the exported tree
  from a non-exportable fixture iterates an empty list and passes on
  `4507d48` — the vacuity §6 forbids. T3 and T9 therefore assert
  `len(files) == 1` **before** any pixel or filename assertion.
- `save()`, `close()`, reopen the same `.db`.
- **Asserts** `instance.get_pixel_data().flags.writeable is False`, then
  calls `instance.unload_pixel_data()` so the redaction re-reads through
  the loader rather than a cached array.
- Sets `session.configuration.rules = [{"serial_number": serial,
  "redaction_zones": zones}]`.
- Teardown closes the session.

`tests/test_redaction_failure_is_reported.py::_hydrated` is the same idea
one file down. **Do not delete or rewire it in this PR** — it carries
#213's reasoning in its docstring and rewiring it would put a #213
regression risk inside a #229 PR. §9 lists the consolidation as future
work.

## §A7. Tests, with polarity

Every clause below names the test that goes **red if the clause is
reverted**. Polarity is stated for each; a guard that cannot fail under the
mutation is fine but is not evidence.

New file `tests/test_redaction_multizone.py` unless noted.

### T1 — two zones, reloaded instance, parallel path. **Detection.**
`reloaded_redaction_session([[0, 8, 0, 8], [16, 24, 16, 24]])`, `redact()`,
assert **both** zone sums are 0.
- On `4507d48`: **red**, `zone1 sum == 12800`. Measured, both interpreters.
- Reverting §A1 or §A2: red.
- **3.12.13**: red before, green after (processes). **3.14.7t**: red before,
  green after (threads). The mechanism is name rebinding, so both legs
  behave identically — state that in the docstring so nobody assumes the
  threads leg is redundant.

### T2 — the same through `redact_machine_instances`. **Detection.**
The serial path is public API (`process_machine_rules` calls it, #213 tests
it directly) and has its own copy of the loop at `services.py:618-621`. A
coder who fixes only `execute_redaction_task` passes T1.
- On `4507d48`: **red**, same figures.
- Both interpreters: identical (this path never leaves the calling thread).

### T3 — it reaches disk. **Detection, end-to-end.**
Two zones, `redact()`, `save()`, **clear the rules**, `export()`, assert
`len(files) == 1`, then `dcmread` the exported file and assert both zone
regions are 0.
- On `4507d48`: **red**, `zone1 == 12800` on disk. Measured (§1.3).
- The `len(files) == 1` assertion comes first and is not decoration:
  `export()` does not raise on a validation failure, so a fixture that
  cannot be exported (§A6) leaves an empty tree and every pixel assertion
  below it is skipped rather than run.
- Clearing the rules is load-bearing: leave them set and
  `_export_instance_worker` repairs the file and the test passes on
  `4507d48`. Say so in the docstring, or someone will "simplify" it into a
  vacuous test.
- **3.12.13 and 3.14.7t**: identical. `export()` uses processes on both
  (#185), so only the `redact()` half differs, and it does not.

### T4 — exactly one copy per instance. **Detection (structural).**
Wrap `Instance.set_pixel_data` with a counter, run a **three**-zone rule
through `redact_machine_instances` on a reloaded instance, assert the
counter is `1`.
- On `4507d48`: **red**, counter is `3`.
- Use the serial path deliberately: it runs in the calling thread on every
  interpreter, so a parent-side wrap is visible. A wrap around `redact()`
  would be invisible in a spawned child on 3.12.
- This is the clause that fails if someone fixes the aliasing but keeps a
  per-zone loop. T1 alone does not.
- Both interpreters: identical.

### T5 — the writeable arm is unchanged. **Selectivity guard — not evidence.**
`tests/test_pixel_geometry_pipeline.py::test_redaction_dirties_the_instance_it_redacted`
and `tests/test_redaction_roi.py`'s two, adapted per §A5.
- On `4507d48`: **green**. They are green after too. They cannot fail under
  this mutation and must not be presented in the PR body as evidence that
  anything was fixed. Their job is to catch a fix that breaks the arm it
  was not about.

### T6 — a failed instance is still left as it was found, on the reloaded shape. **Detection of §A8's new risk, not of #229.**
New case in `tests/test_redaction_failure_is_reported.py`. One rule, two
zones in **this order**: `GOOD = (0, 8, 0, 8)` then `BAD = (1, "x", 0, 8)`.
Assert `RedactionError` is raised, an `ERROR` row exists, and — after
`save()`, `close()` and reopen — the stored pixels are **pristine**:
`zone1 sum == 12800`.

Two mechanical notes the coder needs:

- `prepare_redaction_tasks` computes its hash from `sorted(valid_rois)` but
  hands the task `valid_rois` in **config order** (`services.py:297` vs
  `:305`). So the apply order is the config order, and the `sorted()` call
  must not raise: `(1, "x", 0, 8)` sorts against `(0, 8, 0, 8)` on element
  0 (`0 < 1`) and never compares `8` with `"x"`. The `[0, "abc", 0, 8]`
  spelling used elsewhere in that file **raises `TypeError` out of
  `sorted` before any worker runs** when it shares a rule with an int zone
  — a different, loud defect that would mask this one.
- `int("x")` raises `ValueError` inside `apply_redaction_to_array`, which
  re-raises it (#66).

Polarity, stated precisely: on `4507d48` this **passes**, because the
partial mutation it guards against cannot form there (§A8). It is a
detection guard for a *different* mutation — an implementation that also
moves or weakens the `finally` persist gate — and it must be labelled that
way in the PR body, not counted as reproducing #229.
- **3.12.13 and 3.14.7t**: must both be run and both recorded. This is the
  clause where the two paths' `finally` blocks diverged before #213.

## §A8. Deliberate behaviour change — the failure path's intermediate state

**Own this; it is the item most likely to bounce the PR in review.**

Today, on the read-only path, a mid-loop raise leaves the instance holding
a copy of the *pristine* original: zone k made its own fresh copy, so
zones 1..k−1 were never in the array the instance ended up with. No partial
mutation forms. After §A1, the copy is made **once**, so zones 1..k−1 **are**
zeroed in the instance's array when zone k raises.

The end state is unchanged, and here is the chain that makes that true:

1. `failed = True`, so the `finally` block's persist gate
   (`services.py:439-445` / `:651-663`) does not run. Nothing partial
   reaches the sidecar. This gate is the whole of #213's "a failed instance
   is left as it was found".
2. `inst.unload_pixel_data()` then runs unconditionally. It succeeds
   because `set_pixel_data` **does not clear `_pixel_loader`** — verified
   in `entities.py:656-700`, which touches `pixel_array` and descriptors
   only. The partially-zeroed copy is dropped.
3. The next `get_pixel_data()` reloads the original through the loader.

What genuinely changes: the instance is marked modified (it already was —
`set_pixel_data`'s unconditional `mark_modified()` fired on the first zone
under both the old and new code) while holding no resident array, so a
subsequent `save()` writes attributes and the unchanged blob reference and
no pixel bytes. Same as today.

The one instance this cannot reach is the same one #213 already names: a
graph built in memory with neither loader nor `file_path`, where
`unload_pixel_data()` correctly refuses. There, zones 1..k−1 stay applied
in memory. That was already accepted in `services.py:429-437` on the
grounds that zeroing is monotone — a partial redaction has removed *more*
PHI than none — and that the instance carries no hash, so the next run
retries it. **This change widens that window from "the last zone only" to
"every zone before the failure", in the direction of removing more PHI, and
the accepted reasoning covers it unchanged.** Say so in the PR body.

Two documentation consequences, both required:

- `tests/test_redaction_failure_is_reported.py:400-405` (`_write_source`'s
  docstring) asserts "a sidecar-loaded array is read-only, so the partial
  mutation the persist gate exists to keep out never forms". **That becomes
  false.** Rewrite it to say the partial mutation now *does* form and is
  discarded by the unload, citing §A8's chain.
- `_hydrated`'s docstring (`:53-72`) makes the same claim in its last
  paragraph. Check and correct it.

## §A9. Explicitly out of scope for PR 1

- `inst._pixel_hash = None` exists in `redact_machine_instances`
  (`services.py:616`) and **not** in `execute_redaction_task`. A real
  asymmetry; do not fold it into `_redact_instance_pixels`, and do not
  "harmonise" it here. Listed in §9.
- The `applied` overcount and the unconditional mutation dict (§9 item 1).
- `apply_redaction_to_array`'s signature, bool return, and its #66 raise.
  Untouched.
- The export worker's call at `io_handlers.py:983`. It is already the
  single-call shape this PR converges on.

---

# PR 2 — #228

## §B0. The ruling on the UID question

**A redacted instance gets a new SOP Instance UID, on every interpreter,
and the parent applies it.** The identity is generated in the worker as
today and *applied* in `_apply_redaction_outcomes`.

### Why regenerate at all

1. **It is what the code already intends and already does on one gate
   interpreter.** `regenerate_uid()`'s docstring (`entities.py:364-375`)
   states the reason it exists: "ensure the instance is treated as a new
   distinct entity, preventing collisions with the original data". This
   ruling delivers a stated contract rather than inventing one.
2. **Conformance.** A SOP Instance UID identifies exactly one SOP Instance
   (PS3.5 §9.1). The redacted instance already announces itself as a
   different one — `_apply_redaction_flags` writes `ImageType` starting
   `DERIVED`, `BurnedInAnnotation = NO`, a Derivation Description and a
   Derivation Code Sequence (`113062`, Pixel Data modification). Reusing
   the source UID while carrying those is self-contradicting: two different
   pixel sets under one identity. PS3.15's Basic Application Level
   Confidentiality Profile likewise assigns SOP Instance UID action **U**
   — replace with a new, internally consistent UID — for de-identified
   data, and de-identification is what this library is.
3. **#197 is about duplicate SOP Instance UIDs breaking counts.** Keeping
   the source UID *manufactures* duplicates by design: the source file and
   the redacted export become indistinguishable to any index that keys on
   UID, which is also the linkage the "clean copies" premise exists to
   break.
4. **The store is measurably healthier.** §2's table: keeping the source
   UID on the processes path is what strands the orphan `instance_blobs`
   row and the duplicate sidecar bytes. Applying the identity in the parent
   removes both — measured in §B6.

### Why not "never regenerate"

The real cost of regenerating is that Isocenter does **not** remap
references: `grep -rn "0008,1155\|0008,1150\|ReferencedSOP" isocenter/`
returns **nothing**. An RTSTRUCT, SR or presentation state pointing at the
redacted image keeps pointing at a UID that no longer exists in the export.
That is a genuine harm and it is why the option was weighed rather than
assumed.

It loses because the harm is **already shipped** on 3.14t, where the whole
suite is green, and because the alternative harm — a de-identification tool
emitting a modified image under the identity of the file it was derived
from — is worse and is unfixable downstream. The reference-remapping gap is
real, orthogonal, and belongs in its own issue (§9 item 3), not in a fix
for an interpreter divergence.

### Why the worker keeps generating it

#228's own preferred shape is to move `regenerate_uid()` into the parent's
apply step, on the principle that identity assignment belongs on the same
side of the boundary as every other graph mutation. **Both shapes were
measured; both leave a consistent store** (the parent-regenerates variant
was simulated on 3.14.7t with `Instance.regenerate_uid` neutered and the
rename done in the apply step: `instances` and `instance_blobs` both end
with exactly one row under the new UID, reloaded pixels correct). So the
predicted "the blob was persisted under the old UID and `_delete_instances`
removes it" hazard **does not materialise** — `save()` rewrites the blob
row from the loader under the instance's current UID. The issue's preferred
shape is viable.

It is still not the one chosen, for one reason:

**The mutation dict is built unconditionally, so "a mutation came back" does
not mean "the pixels changed."** `execute_redaction_task` builds and returns
`mutation` outside the `if modified:` block (`services.py:380-392`). An
instance whose zones all start past the edge of the image takes the
`continue` at `services.py:730` for every zone, ends with `modified = False`,
and still returns a mutation. Measured on 3.12.13, one rule with a single
`[100, 108, 100, 108]` zone against a 32×32 image:

```
A applied: 1                      <- reported as updated
A hash:    None                   <- no redaction hash written
A 0028,0301: None  has_key: True  <- a null BurnedInAnnotation created
```

A parent that regenerates on "a mutation arrived" therefore hands a new
identity to an instance whose pixels were never touched. Keeping
`regenerate_uid()` in the worker gives the parent a gate that cannot get
this wrong: **`sop_uid != original_sop_uid` is true exactly when the worker
took the `if modified:` branch**, because that branch is the only caller of
`regenerate_uid()`. The gate is the issue's own suggested `if new_uid and
new_uid != sop`, and it is correct for a reason worth writing down.

That coupling is a real cost and §B3 requires it be stated as a comment.
§9 item 1 records the interaction: a future fix that adds `"modified":
modified` to the mutation dict creates a *second* gate that could disagree
with this one, and whoever adds it must make one of them authoritative
rather than leaving both.

## §B1. `_apply_redaction_outcomes` applies the identity

`session.py`, in the per-mutation block, after the attribute/sequence
updates and before `instance.mark_modified()`:

```python
new_uid = mutation.get('sop_uid')
if new_uid and new_uid != sop:
    # The worker regenerated. It does that only inside `if modified:`,
    # which makes "the UID differs" the parent's only honest signal that
    # pixels actually changed -- the mutation dict itself is built
    # unconditionally and comes back for an instance nothing was applied
    # to (#228, and see the note in `execute_redaction_task`).
    instance.sop_instance_uid = new_uid
    instance.attributes["0008,0018"] = new_uid
    # `regenerate_uid()` ends the same way, deliberately: the instance no
    # longer matches the file it was read from.
    instance.file_path = None
```

Assign all three or none. `file_path` is not optional: leaving the parent
pointed at the unredacted source file after a successful redaction is half
of what #228 is.

## §B2. Delete the `sop_uid` lookup fallback

`session.py:2095`:

```python
sop = mutation.get('original_sop_uid') or mutation.get('sop_uid')
```

becomes

```python
sop = mutation.get('original_sop_uid')
```

`instances` is keyed on **pre-redaction** UIDs (`session.py:1999`,
deliberately, with a comment). `sop_uid` is the **post**-redaction UID. The
fallback can therefore only ever miss, and after §B1 it would be actively
misleading — the same name would mean the lookup key on one line and the
new identity three lines down. Pre-1.0: delete, do not alias.

Behaviour for a mutation missing `original_sop_uid`: `instances.get(None)`
→ `None` → the existing "does not match any targeted instance" error log
and discard. Identical to today, since the fallback found nothing either.

## §B3. Comments the fix depends on

- `services.py:382` (`"sop_uid": inst.sop_instance_uid`): state that the
  parent **assigns** this now, that it equals `original_sop_uid` whenever
  nothing was modified, and that the inequality is the parent's gate.
  Moving `regenerate_uid()` out of `if modified:` silently gives every
  skipped instance a new identity.
- `_apply_redaction_outcomes`'s docstring: add the identity assignment to
  the list of what the parent does, alongside the existing note that the
  audit write must stay in the parent (#126).

## §B4. Explicitly out of scope for PR 2

- Reference remapping (`0008,1155` and friends). §9 item 3.
- The `applied` overcount and the null-valued attribute writes. §9 item 1.
- `regenerate_uid()` itself (`entities.py:364-391`). Unchanged;
  `tests/test_uid_regeneration.py` must stay green with an empty diff.
- Retroactive renaming of instances redacted under the old behaviour. §B5.

## §B5. CHANGELOG entry and migration

No previously-working call raises. The entry must therefore state the
behaviour change with the same precision CLAUDE.md demands of an exception,
and must cover:

1. **What changes.** On a GIL build (3.12, 3.13, 3.14 non-`t`),
   `session.redact()` now assigns the redacted instance a new SOP Instance
   UID and sets `file_path = None`, as it has always done on a
   free-threaded build. `instance.sop_instance_uid` after `redact()` is a
   generated UID, not the source's.
2. **What a caller sees.** Code that holds a **reference** to the instance
   is unaffected — that is why `tests/test_redaction_wildcard.py` and
   `tests/test_session.py::test_execute_config_integration` are untouched.
   Code that re-looks-up an instance by its **pre-redaction UID** after
   `redact()` now finds nothing, on every interpreter rather than on one.
   `Session` and `DicomStore` expose no lookup-by-UID method
   (`grep -n "def find\|def get_instance\|def query\|sop_instance_uid =="
   isocenter/session.py isocenter/store.py` — no hits), so on those two
   objects this reaches only callers walking the graph themselves. The
   **backend** is a different matter and the entry must say so rather than
   claim a blanket absence: `SqliteStore.load_vertical_attributes(
   instance_uid)`, `load_vertical_attributes_bulk()` (called public API in
   CHANGELOG.md:270), `get_blob_ref(instance_uid, kind)` and
   `log_audit(entity_uid, …)` all take a UID, and `docs/analytics.md:93`
   documents `sop_instance_uid` as an emitted DataFrame column. None of
   them changes behaviour here — they read whatever the store holds, and
   after `save()` the store holds the new UID — but a caller who cached a
   pre-redaction UID and later passes it to one of them gets an empty
   result rather than an error, on every interpreter rather than on one.
3. **Exported filenames change on GIL builds.** Files are named by SOP
   Instance UID (#78). A pipeline that redacts and then matches exported
   filenames against source filenames stops matching on 3.12 — and starts
   agreeing with 3.14t, which is the point.
4. **Migration for stores written under the old behaviour.** They hold
   redacted instances under their **source** UIDs, plus one orphan
   `instance_blobs` row per redacted instance. Neither is repaired
   retroactively, and that is deliberate:
   - `execute_redaction_task` skips an instance whose
     `_ISOCENTER_REDACTION_HASH` already matches the configuration, so
     re-running `redact()` with the same rules is a no-op and does **not**
     rename anything. Already-redacted instances keep the identity the old
     run gave them.
   - Redacting such a store under a *different* rule set does regenerate,
     at that point.
   - The orphan blob rows are reclaimed by `session.compact()`, which
     already collects exactly them (`persistence.py:2378-2390`). Recommend
     one `compact()` for anyone who redacted on a GIL build.
   - No schema change, no data loss, no downgrade hazard: a store written
     after the fix opens unchanged on an older Isocenter.

## §B6. Measured behaviour of the proposed fix

The §B1 shape was simulated on both interpreters by wrapping
`DicomSession._apply_redaction_outcomes` in a script — **no production file
was modified to produce these numbers.**

| build | parent UID after `redact()` | `instances` after close | `instance_blobs` after close | reloaded zone sum |
| --- | --- | --- | --- | --- |
| 3.12.13 | `1.2.826.…` (new) | 1 row, new UID | 1 row, new UID | 0 |
| 3.14.7t | `1.2.826.…` (new) | 1 row, new UID | 1 row, new UID | 0 |

Compare §2's table. The processes path becomes byte-for-byte the shape the
threads path already produces, and the orphan blob row is gone.

## §B7. Tests, with polarity

New file `tests/test_redaction_identity.py` unless noted.

### T8 — the parent's identity changed. **Detection.**
Parametrised over the executor lever — `ISOCENTER_FORCE_PROCESSES=1` and
`ISOCENTER_FORCE_THREADS=1` — capture the instance **reference** before
`redact()`, then assert:
`inst.sop_instance_uid != source_uid`,
`inst.attributes["0008,0018"] == inst.sop_instance_uid`,
`inst.file_path is None`.
- On `4507d48`: **red on the processes leg**, green on the threads leg, on
  **both** interpreters. That asymmetry *is* #228 and the test must assert
  it as a parametrised pair, not as one case per interpreter.
- The lever is the same one `test_a_failed_instance_is_left_as_it_was_found`
  already uses, and legitimate for the same reason.
- Reverting §B1: red on the processes leg.

### T9 — the two executors export the same filename. **Detection.**
Redact under each lever, `export()`, assert `len(files) == 1`, then assert
the exported basename is `f"{inst.sop_instance_uid}.dcm"` **and** is not
`f"{source_uid}.dcm"`.
- On `4507d48`: **red on the processes leg** — measured, `1.2.3.b.dcm`.
- `export()` uses processes on every interpreter (#185), so the lever acts
  only on `redact()`. Say so, or a reader will think the lever is being
  tested twice.
- Needs the §A6 attribute set (SC Image Storage plus `0008,0016`,
  `0008,0030`, `0008,0060`, `0028,0004`). A bare CT instance fails
  validation, `export()` does not raise, and the tree is empty — a
  filename assertion over an empty list is not an assertion. The
  `len(files) == 1` check comes first for that reason.

### T10 — an off-edge-only rule does **not** change the identity. **Selectivity guard w.r.t. #228; detection w.r.t. §B1's gate.**
One zone entirely past the edge (`[[100, 108, 100, 108]]` on 32×32),
`redact()`, assert `inst.sop_instance_uid == source_uid` and
`inst.file_path` is unchanged.
- On `4507d48`: **green**, both legs, both interpreters. It cannot
  reproduce #228 and must not be listed as doing so.
- It goes red the moment §B1's gate is written as `if mutation:` or
  `if new_uid:` instead of `if new_uid and new_uid != sop`, which is the
  most likely way to implement this clause wrongly.

### T11 — the store holds one instance row and one pixels blob row. **Detection.**
Redact, `save()`, `close()`, then read `instances` and `instance_blobs`
straight out of sqlite (after close — the persistence thread is
asynchronous and a read before `close()` is stale; §1 of the audit spec is
the precedent). Assert one row in each, both under the new UID.
- On `4507d48`, processes leg: **red** — `instances` holds the source UID
  and `instance_blobs` holds **two** rows. Measured.
- Threads leg: green before and after.

### Must keep passing — checked by grep, both questions asked

Tests (`grep -ln redact tests/*.py` intersected with `grep -ln
'\.dcm\|listdir\|os\.walk\|glob\|iterdir' tests/*.py`, then filtered to
files that actually *call* a redaction entry point with
`grep -n '\.redact(\|redact_by_machine\|process_machine_rules\|redact_machine_instances'`):

| file | why it survives |
| --- | --- |
| `tests/test_redaction_wildcard.py:103` | walks the store by object reference after `redact()` and `save(sync=True)`; asserts pixels and the hash attribute, never a UID. The `save(sync=True)` after a UID change on 3.12 is new — §B6 measures that path clean. |
| `tests/test_session.py:44, :73` | `test_load_empty_config` redacts with no rules. `test_execute_config_integration` is **file-backed** and reads `inst.get_pixel_data()` after `redact()`; §B1 sets `file_path = None`, so it depends on the worker's `persist_pixel_data` having created a loader. It does, and this test is **already green on 3.14t**, where `file_path = None` already happens in the parent. Verified: 55 passed on both interpreters across the seven redaction-touching files. |
| `tests/test_pixel_geometry_pipeline.py:318` | calls `redact_machine_instances` in-process and reads `inst.pixel_array` by reference. The serial path has no boundary; unaffected by §B1. |
| `tests/test_redaction_failure_is_reported.py` | its `_instances()` map is built before `redact()` precisely because the UID moves. That workaround becomes *necessary on both interpreters* rather than one, which is a strengthening. Its `bad.sop_instance_uid == "1.2.3.bad"` assertion at `:695` is on the **failed** instance, which never regenerates. |
| `tests/test_api_coherence.py` | **never calls `redact()`** — verified by grep, its only occurrence of the word is a docstring analogy at line 105. |
| `tests/test_uid_regeneration.py` | a unit test on `regenerate_uid()`, which is untouched. Empty diff. |

**`tests/test_redaction_consistency.py` is not on this list and is not
coverage.** Its line 42 replicates the `original_sop_uid or sop_uid`
expression in the test body rather than calling `_apply_redaction_outcomes`,
so §B2's deletion leaves it green. It is named here so nobody cites it as
evidence the fallback is exercised.

Non-test consumers, asked as a separate question and by grep:

| consumer | command | result |
| --- | --- | --- |
| `scripts/` fixture generators | `grep -rn "regenerate_uid\|_apply_roi_to_instance\|\.redact(" scripts/` | **no hits.** `generate_ocr_test_data.py`, `generate_redaction_example.py` and `generate_test_dataset.py` set `0008,0018` when building a graph and call `DicomExporter.write_tree()`; none redacts. |
| `docs/` and `README.md` | `grep -rn "\.dcm" README.md docs/*.md` | one hit, `docs/waveforms.md:30`, generic prose with no example filename. |
| `docs/` UID prose | `grep -rni "regenerate\|new SOP Instance UID" docs/*.md CHANGELOG.md README.md` | no statement of redaction's UID behaviour anywhere. Nothing to correct; §B5's entry is the first time it is documented. |

Commands recorded verbatim so a reviewer can re-run them:

```
grep -rn "_apply_roi_to_instance" --include="*.py" --include="*.md" .
grep -rn "regenerate_uid\|sop_uid" --include="*.py" --include="*.md" .
grep -rn "0008,1155\|0008,1150\|ReferencedSOP" isocenter/
grep -ln "redact" tests/*.py | sort > a
grep -ln "\.dcm\|listdir\|os\.walk\|glob\|rglob\|iterdir" tests/*.py | sort > b
comm -12 a b
grep -n "\.redact(\|redact_by_machine\|process_machine_rules\|redact_machine_instances" <that list>
grep -rn "regenerate_uid\|_apply_roi_to_instance\|\.redact(" scripts/
grep -rn "\.dcm" README.md docs/*.md
```

---

## 4. Interpreter coverage, per clause

The gate is **3.12 and 3.14t only**. `redact()` takes threads on a
free-threaded build (`_use_threads(False, None)` is `True` there) and
processes everywhere else, which is exactly why #228 diverges and #229 does
not.

| clause | 3.12.13 (processes) | 3.14.7t (threads) |
| --- | --- | --- |
| §A1/§A2 (#229) | defect present; fixed | defect present, identically; fixed |
| §A8 (failure path) | partial mutation now forms and is unloaded | same |
| T1–T4 | red → green | red → green |
| T5 | green → green | green → green |
| T6 | green → green (guard) | green → green (guard) |
| §B1 (#228) | **defect present**; fixed | already correct; unchanged |
| T8, T9, T11 | red on the processes leg → green | green on the threads leg throughout |
| T10 | green throughout | green throughout |

Both builds are installed (`pyenv versions`: `3.12.13`, `3.14.7t`). Any
measurement on 3.14.7t must assert `sys._is_gil_enabled() is False`
**inside the process after imports** — a free-threaded interpreter silently
re-enables the GIL on importing an extension without free-threaded support,
and a measurement taken under a re-enabled GIL is a measurement of the
other build. Every figure in this spec was taken that way.

Before any suite run: `find . -name .DS_Store -delete`. Otherwise two
`tests/test_packaging_contract.py` tests fail spuriously and the failure
message recommends the wrong remedy (#234).

---

## 5. Files touched

**PR 1 (#229)**

| file | change |
| --- | --- |
| `isocenter/services.py` | delete `_apply_roi_to_instance` (`:772-810`); add `_redact_instance_pixels`; replace the two zone loops (`:358-361`, `:618-621`); update the comment at `:721` |
| `isocenter/entities.py` | rename the reference at `:761` |
| `tests/conftest.py` | add `reloaded_redaction_session` |
| `tests/test_redaction_multizone.py` | new — T1, T2, T3, T4 |
| `tests/test_redaction_roi.py` | two call sites (§A5) |
| `tests/test_pixel_geometry_pipeline.py` | one call site, four docstring references |
| `tests/test_redaction_failure_is_reported.py` | T6; rewrite the `_write_source` and `_hydrated` docstring claims (§A8) |
| `CHANGELOG.md` | one entry |

**PR 2 (#228)**

| file | change |
| --- | --- |
| `isocenter/session.py` | §B1 identity apply; §B2 fallback deletion; docstring |
| `isocenter/services.py` | comment at `:382` (§B3) |
| `tests/test_redaction_identity.py` | new — T8, T9, T10, T11 |
| `CHANGELOG.md` | one entry, with §B5's migration note |

No production file outside `services.py`, `session.py` and `entities.py` is
touched by either PR. No schema change. No public signature change.

---

## 6. What the PR bodies must record

For each PR, run every listed test against `4507d48` and record the
observed failure mode. A test in a "detection" row that passes on
`4507d48` is testing something else and must be rewritten or reclassified
before merge — several such tests were found and deleted earlier in this
milestone.

Additionally, for PR 1: verify the fix is not vacuous by reverting only
§A1's single-call line to a `for roi in rois: ... [roi]` loop and
confirming T1, T2, T3 and T4 all go red.

---

## 7. Explicitly out of scope for both PRs

- `apply_redaction_to_array`'s signature, bool return, and #66 raise (#217).
- The export worker (`io_handlers.py:983`).
- `regenerate_uid()` itself.
- #213's `RedactionError`, the `ERROR` audit row, and the persist gate —
  except for §A8's *documentation* of a changed intermediate state.
- #197's duplicate-UID accounting. §B0 argues this fix helps it; it does
  not implement it.
- The compliance grade. Both defects are invisible to it because they
  produce no `ERROR` and no `DATA_LOSS` row, and fixing them removes the
  condition rather than the blind spot. Whether a silently-partial
  redaction *could* be graded is a separate question and belongs with #218's
  successors.

---

## 8. The strongest objection

**"§B0 changes what a de-identification tool emits, on the basis that one
of two interpreters already does it."**

The honest form of the objection is that the free-threaded build's
behaviour is not evidence of correctness — it is evidence of an accident,
since nobody chose it. Ratifying an accident because the suite is green
under it is how a bug becomes a contract.

The answer is that §B0 does not rest on 3.14t. It rests on
`regenerate_uid()`'s docstring (the intent was written down before either
path existed), on PS3.5 §9.1 and PS3.15's action **U** for `0008,0018`, and
on the fact that the redacted instance already writes `DERIVED` and a
Derivation Code Sequence — it is *already* asserting it is a different SOP
Instance, in three tags, on both interpreters. 3.14t is used only to bound
the *risk*: the behaviour being generalised is one the whole suite already
runs against on a gate interpreter, so the blast radius is measured rather
than estimated.

What the objection does land is the reference-remapping gap (§B0), which
this spec does not fix and §9 files. A reviewer who wants §B0 reversed
should argue that gap, not the conformance question — and the reversal
would then have to explain why the same run writes `DERIVED` on an instance
it insists is the original.

---

## 9. Found here, to be filed separately

Not folded in; listed for the maintainer to file or discard. Each was
measured on `4507d48` while writing this spec.

1. **A rule whose zones all miss reports success and writes null
   attributes.** `execute_redaction_task` builds its mutation dict
   *outside* `if modified:` (`services.py:380-392`), so an instance where
   `apply_redaction_to_array` returned `False` — every zone starting past
   the edge of the image — still returns a mutation, is counted in
   `applied`, and has four keys written onto it by
   `_apply_redaction_outcomes`. Measured, one `[100, 108, 100, 108]` zone
   on a 32×32 image, 3.12.13:
   ```
   applied: 1                        <- "1 of 1 images updated"
   _ISOCENTER_REDACTION_HASH: None
   0028,0301: None   has_key: True   <- BurnedInAnnotation created, null
   ```
   Two defects in one: the count is wrong (squarely the v0.9.1 theme), and
   a null-valued DICOM attribute is created on an instance nothing was
   applied to and carried toward the store and the export.
   **Interaction with §B0:** the obvious fix is to add `"modified":
   modified` to the mutation dict, which creates a **second** gate on the
   same question as §B1's `new_uid != sop`. Whoever implements it must make
   one authoritative — two gates that can disagree about whether an
   instance was redacted is how #228 happened in the first place.

2. **`inst._pixel_hash = None` is set on the serial redaction path only.**
   `services.py:616`, with a comment explaining why ("if persist/save fails
   later, we don't want to match the Old Hash"). `execute_redaction_task`
   has no equivalent. Either the reason applies to both paths or it applies
   to neither. Low severity — `persist_pixel_data` rewrites the hash on
   both paths — but it is one behaviour with two spellings.

3. **Nothing remaps `ReferencedSOPInstanceUID`.**
   `grep -rn "0008,1155\|0008,1150\|ReferencedSOP" isocenter/` returns
   nothing. Any instance that regenerates its UID — on redaction today on
   3.14t, and on every interpreter after §B1 — leaves references from
   RTSTRUCT, SR, presentation states and Source Image Sequences pointing at
   a UID that is not in the export. Pre-existing, orthogonal to both issues
   here, and the strongest argument against §B0; it deserves its own issue
   rather than a footnote.

4. **The orphan `instance_blobs` row is fixed as a side effect, and that is
   worth a test even after PR 2.** T11 pins it, but only for the redaction
   path. Anything else that changes an instance's UID after its blob was
   persisted would strand a row the same way, and `compact()` is the only
   thing that notices.

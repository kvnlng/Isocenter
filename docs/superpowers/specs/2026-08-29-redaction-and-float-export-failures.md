# Success Reported On A Failure: The Redaction Swallow And The Float Export Gap

**Date:** 2026-08-29
**Status:** Design, not yet implemented
**Tracking:** #213 (the redaction swallow), #216 (the float export gap)
**Base:** `main` at `258331c`
**Recommended landing:** two PRs, #216 first. See §0.3.

---

## 0. Summary

### 0.1 The shared theme, and where it stops being shared

Both defects let a file reach the output tree while the pipeline reports
that it went fine. That is the whole of the shared theme. The two fixes
touch disjoint files (`services.py` + `session.py` versus
`io_handlers.py`), disjoint tests, and carry very different risk: #213
changes the failure contract of a public method on a library heading to
1.0; #216 is a bounded restructure of one worker function with no API
change.

### 0.2 What each one is

**#213.** `RedactionService.apply_redaction_to_array` re-raises
`(ValueError, IndexError, TypeError)` deliberately, under a comment
saying a silent skip ships the unredacted image. One frame up, the
enclosing loop catches bare `Exception` and only logs. The raise travels
exactly one stack frame. `modified` stays `False`,
`_apply_redaction_flags` never runs, `_ISOCENTER_REDACTION_HASH` is never
set, no audit row is written, `session.redact()` returns a number, and
the instance exports with its burned-in identifier intact and grades
`PASS`.

**#216.** `_export_instance_worker` refuses to write a `GUESSED`
geometry, and that refusal lives inside the **integer** Pixel Data
branch. The float branch sits above it and ends with `arr = None`, so a
float array never reaches the refusal and never reaches
`ds.Rows`/`ds.Columns`/`ds.SamplesPerPixel`.

### 0.3 Land #216 first, as its own PR

#216 does not touch the redaction contract, so a revert of #213 does not
drag it. #213 carries a `CHANGELOG.md` breaking entry naming an exception
a previously-working call now raises; a reviewer has to weigh that on its
own, next to nothing else. The one thing that genuinely couples them —
`resolve_pixel_geometry`'s `ValueError` is a new way into #213's swallow
(§2.3) — is a fact about #213, not a dependency of #216.

Nothing in this spec requires the two to land together. §9 gives an
implementation order that works either way.

---

> **Amended 2026-08-31.** #217 was decided after this spec was written
> and lands in the same method as #213. **§12** carries it, and marks
> the five clauses here that it contradicts. #216 is unaffected.

## 1. Corrections to the issues and to the brief

Verified against `258331c`. Every one of these matters to the design.

1. **`RedactionService.redact()` does not exist.** Both issue #213 and
   the brief name it. The method is
   `RedactionService.redact_machine_instances` (`services.py:368`), it
   returns `None`, and `services.py:452-453` is its `except Exception as
   e:` handler. It is documented in its own docstring as the
   "Legacy/Single-threaded entry point (mostly replaced by parallel
   approach)". **It is not the method `session.redact()` runs.**

2. **`_process_redaction_task` does not exist.** The parallel worker is
   `RedactionService.execute_redaction_task` (`services.py:208-295`).
   Its `except Exception` is at `services.py:280`, not 283.

3. **"`redact()`'s return type today cannot express 'tried and failed'
   (the bool already means 'no zones matched')" conflates three
   functions.** `Session.redact()` returns an **`int`** — how many
   instances were updated. `redact_machine_instances` returns `None`. The
   bool belongs to `apply_redaction_to_array`. This is load-bearing: the
   `int` return is exactly what lets §3 keep the signature unchanged and
   add a raise instead of inventing a summary object.

4. **The `int` return is already documented as capable of raising.**
   `session.redact()`'s docstring says:

   > `Raises: Exception: Whatever the redaction backend raised, after
   > logging it. Redaction is the step that removes burned-in PHI, so a
   > failure here must reach the caller.`

   Today only *setup* failures honour that (an exploding
   `prepare_redaction_tasks`, pinned by
   `test_redact_reports_outcome.py::test_a_failing_redaction_raises_instead_of_reporting_completion`).
   A per-instance failure does not. §3 is therefore **the existing
   documented contract being honoured**, not a new contract.

5. **The comment's premise at `services.py:546-553` is half-true, as the
   brief says, and the half that is true is worth keeping.** The export
   worker's own call to `apply_redaction_to_array`
   (`io_handlers.py:882`) has no handler between it and the worker's
   outermost `except`, so it does propagate and does become
   `ExportOutcome(ok=False)` — audited, counted, no file written. That
   path is correct today and this design does not change it. It is the
   `redact_machine_instances` call the comment sits above, and the
   `execute_redaction_task` call it does not mention, that swallow.

6. **`CHANGELOG.md` already anticipates #213.** The #186/#205 entry says:
   "Through `session.redact()` this new `ValueError` is a second path
   into #213 -- that method's `except Exception` swallows the re-raise
   #66 added and documents as never to be swallowed -- which is filed,
   not fixed here." The #213 entry should read as the closing of a
   known-open item.

7. **#216 understates its own defect.** The issue measures the case where
   the geometry descriptors are *absent*. §2.5 measures the case where
   they are *present and wrong*, which is worse and which the issue's
   suggested fix ("hoist the refusal") does not address. See §4.1.

---

## 2. Reproduction — measured, not read

All measurements on `258331c` with
`/Users/kevin/Developer/Isocenter/.venv/bin/python` (CPython 3.14.6,
GIL enabled), pydicom 3.0.2, numpy 2.5.2.

### 2.1 #213 through `session.redact()`, malformed ROI

One instance, `(10,10) uint8` all 200, one rule with one zone
`[0, "abc", 0, 4]`. This is the original #66 trigger and depends on
nothing #215 added.

```
ERROR:   Failed I1: invalid literal for int() with base 10: 'abc'
redact returned 0    pixel[0,0] = 200    hash = None    audit errors = 0
```

The zone did not apply, the pixels are untouched, no `ERROR` row exists,
and `redact()` returned a number.

### 2.2 The same, one frame further out

`redact()` prints its own summary and warns about the shortfall:

```
WARNING: Redaction updated 0 of 1 targeted images. The remainder returned
no change: already redacted under this configuration, pixel data that
would not load, or a worker that failed -- see the entries above for which.
Redaction complete: 0 of 1 images updated.
```

That warning is the entire current report, and it cannot distinguish its
three cases: it says to go and read log lines that a batch run has
already scrolled past, and it writes nothing to the audit log, which is
what the compliance report reads.

### 2.3 #213 through the geometry contradiction (#215's new entry)

Same fixture, array `(5,8,4) uint8`, `0028,0002 = 3` declared:

```
ERROR:   Failed I1: Pixel array shape (5, 8, 4) cannot be reconciled with
the instance's declared geometry (SamplesPerPixel=3, ...).
redact returned 0    pixel[0,0,0] = 200    audit errors = 0
```

Same sink, different source. Both are in scope; §7 uses the malformed
ROI as the primary fixture and this as a second arm (§7.2, test 16).

### 2.4 What a failed redaction leaves behind — and it is not the same on
both interpreters

**This is the finding neither issue names, and it is why §3.7 exists.**

`apply_redaction_to_array` applies zones in a loop and raises *mid-loop*,
so zones 1..k-1 are already zeroed when zone k fails.
`execute_redaction_task`'s `finally` then calls
`persist_pixel_data(inst)` unconditionally.

Fixture: one saved instance, `(32,32) uint8` all 200, zones
`[[0,8,0,8], [1,"abc",0,8]]` — the first elements differ so
`prepare_redaction_tasks`' `sorted(valid_rois)` never compares the string
to an int (§2.6). Zone 1 applies, zone 2 raises. Measured, save, close,
reopen:

| `run_parallel` mode | zone 1 in memory | `has_unsaved_changes` | zone 1 after reload |
| --- | --- | --- | --- |
| threads (`ISOCENTER_FORCE_THREADS=1`; this is 3.14t's default) | **zeroed** | True | **zeroed** |
| processes (`ISOCENTER_FORCE_PROCESSES=1`; this is 3.12's default) | untouched (12800) | False | untouched (12800) |

The same failed redaction leaves two different graphs, and two different
sidecars, depending on which interpreter ran it. Under threads the worker
mutates the parent's own array and then persists it; under processes it
mutates a copy, and the copy's bytes are appended to the shared sidecar
as orphan data the parent never points at. Neither instance is flagged,
neither carries a hash, and `redact()` returns normally in both.

The `run_parallel` mode is decided by `_use_threads` and is *not* fixed
for the redaction path: `_apply_redaction_rules` passes no
`maxtasksperchild`, so it is `ProcessPoolExecutor` on 3.12 and
`ThreadPoolExecutor` on 3.14t. Both CI gate versions are exercised, and
they disagree.

### 2.5 #216 — three shapes, only one of which the issue measures

Driven through `_export_instance_worker` directly (the harness
`tests/test_float_pixel_data_export.py::_export_one` already uses), array
assigned to `inst.pixel_array` rather than through `set_pixel_data`,
because the setter writes the descriptors back and destroys the case.

| case | array | declared | result |
| --- | --- | --- | --- |
| a | `(5,6,3) float32` | nothing | **written**: `Rows=None Columns=None SamplesPerPixel=None NumberOfFrames=None BitsAllocated=32 FloatPixelData=True` |
| a′ | `(5,6,3) uint8` | nothing | refused, no file, `ok=False` |
| b | `(4,4) float32` | `Rows=10 Columns=10 SamplesPerPixel=1` | **written**: `Rows=10 Columns=10 SamplesPerPixel=1`, `len(FloatPixelData) == 64` |
| c | `(2,4,8) float32` | `SamplesPerPixel=1 NumberOfFrames=2 Rows=99 Columns=99` | **written**: `Rows=99 Columns=99 NumberOfFrames=2`, `len(FloatPixelData) == 256` |
| d | `(5,8,4) float32` | `SamplesPerPixel=3` | refused (`ValueError` from the resolver, caught by the worker's outer handler) — already correct |
| e | `(4,4) float32` | `7fe0,0010` present in `attributes` | written; `(7fe0,0010)` correctly deleted |
| f | `(4,4) uint8` | `7fe0,0008` present in `attributes` | **written carrying both `(7fe0,0010)` and `(7fe0,0008)`** |

Case **a** is the issue as filed: descriptors absent, Type 1 in the
Floating Point Image Pixel Module (PS3.3 C.7.6.24), nothing can decode
it. Loud.

Cases **b** and **c** are worse and are not in the issue. The file
declares 100 pixels while carrying 16 floats, or 9801 while carrying 64.
It is not undecodable — it is a file describing a different image, and
this project's own changelog says at length that a relabelling is worse
than a drop because nothing invites the reader to go back. The
descriptors come from `_merge`, which writes whatever `attributes` holds;
the integer path corrects them with `ds.Rows = geom.rows`, and the float
path does not.

**A fix that only hoists the refusal leaves b and c standing.** That is
why §4 states the clause as "the float path writes its descriptors from
the resolved geometry" and treats the hoist as one consequence of it.

Case **f** is the PS3.5 Section 8.2 gap the brief asked about, confirmed
reachable: the float branch deletes `(7fe0,0010)`, the integer branch
does not delete `(7fe0,0008)`/`(7fe0,0009)`. `populate_attrs` skips the
whole `7fe0` group at ingest, so this arrives only from a hand-built
graph or a `set_attr` call — the same reachability class as the float16
arm `_export_one`'s docstring already exists to serve.

Also measured on every float case: `PhotometricInterpretation` is
**absent** from the written file unless the instance declared one. It is
Type 1 with Enumerated Value `MONOCHROME2` in both C.7.6.24 (Floating
Point Image Pixel) and C.7.6.25 (Double Floating Point Image Pixel),
verified against the published standard rather than recalled.

### 2.6 A separate defect found while building the fixtures, not fixed here

`prepare_redaction_tasks` computes its config hash with
`rois_stable = sorted(valid_rois)` (`services.py:192`). A configuration
holding two zones whose first differing element is a string against an
int raises `TypeError: '<' not supported between instances of 'str' and
'int'` **before any worker runs** — out of `redact()`, uncaught, with a
traceback naming `sorted`. That is a loud failure and not a swallow, so
it is not #213; but it means a malformed-ROI fixture must choose zone
values whose sort never compares the bad element (§7.4). Recorded in §10
to be filed.

### 2.7 Can the compliance report see any of this today?

**#213: no, in the general case.** No `ERROR` row is written, so
`get_audit_errors()` is empty and `validation_status` is `PASS`
(`session.py:1583`). There is one partial and unreliable mitigation:
`check_unsafe_attributes()` scans `instances.attributes_json` for
`"0028,0301": "YES"` and appends to `exceptions`, which would grade
`REVIEW_REQUIRED`. It fires only when the instance both declares
BurnedInAnnotation = YES *and* has been saved. Burned-in identifiers on
instances that declare nothing — which is why redaction zones exist at
all — are invisible to it.

**#216: no.** Nothing was dropped, so there is no `DATA_LOSS` row; the
worker returns `ok=True`, so there is no `ERROR` row. `PASS`.

---

## 3. #213 — the design

### 3.1 The rule

> **A zone that raised is a failure. It is reported to the audit log by
> the parent, it is counted, it does not change the instance, and it
> reaches the caller as an exception after every other instance has been
> attempted.**

Three things are deliberately *not* failures and must stay silent skips:
an instance whose `_ISOCENTER_REDACTION_HASH` already matches the config,
an instance whose `get_pixel_data()` returns `None`, and a rule with no
zones or no matching instances.

### 3.2 `RedactionOutcome` — the worker's one return shape

`execute_redaction_task` returns `None` for three different situations
today: "already redacted", "no pixels", and "it blew up". The parent
reads all three as "nothing to apply". Split them with a dataclass at
module scope in `services.py`:

```python
@dataclass
class RedactionOutcome:
    """What one worker has to tell the parent about one instance (#213).

    `None` used to mean three things -- already redacted under this
    configuration, no pixel data to redact, and an exception -- and the
    parent read all three as "nothing to apply". Only the third is a
    failure, and it is the one that leaves burned-in PHI in an instance
    the pipeline then reports as fine.
    """
    ok: bool
    sop_instance_uid: str
    mutation: Optional[dict] = None
    error: Optional[str] = None
```

- **already redacted / no pixels:** `RedactionOutcome(ok=True, uid,
  mutation=None)`
- **redacted:** `RedactionOutcome(ok=True, uid, mutation={...})` — the
  same dict the method builds today, unchanged.
- **raised:** `RedactionOutcome(ok=False, uid, error="<Type>: <message>")`

`sop_instance_uid` is the **pre-redaction** UID (`original_uid`, captured
before mutation), for the same reason the mutation dict already carries
`original_sop_uid`: a redacted image gets a new UID and the parent's map
is keyed on the old one.

`services.py` imports `hashlib`, `json`, `traceback`, `gc`, `typing`'s
`Dict`/`List`/`Optional`, `tqdm`, `numpy` and four `isocenter` modules —
**`dataclasses` is not among them**. Add `from dataclasses import
dataclass`. It is stdlib, so `tests/test_packaging_contract.py`'s
module-scope-import check has nothing to say about it, but the decorator
is not in scope today and the snippet above assumes it is.

### 3.3 `error` is a string, and that is a deliberate divergence from
`ExportOutcome`

`ExportOutcome.error` holds a `BaseException` and crosses the process
boundary as a pickled exception object. Every consumer of it stringifies
it (`_report_export_failures` interpolates it into `detail`; `write_tree`
interpolates it into a `RuntimeError` message) — there is no reader that
uses it as an object. Carrying prose instead removes a failure mode that
turns a reportable failure into an unreportable one: an exception whose
`__init__` signature does not round-trip through `pickle` fails to
serialise, and what the parent then receives is a pickling error about
the *result*, not the failure the worker was trying to report.

The worker formats `f"{type(exc).__name__}: {exc}"`. The type name is
part of the record because "invalid literal for int()" without
`ValueError` in front of it reads like prose rather than an exception.

This is knowingly a second spelling of the same idea, and "one spelling
per behaviour" applies. Unifying them means changing `ExportOutcome`,
which is on the export path and out of scope here. Filed in §10.

### 3.4 The parent applies, counts, and audits

`Session._apply_redaction_mutations` becomes
`Session._apply_redaction_outcomes(outcomes, instances, store_backend)`
returning `(applied: int, failures: List[Tuple[str, str]])`.

Three result shapes must survive it, mirroring
`_report_export_failures`:

1. `RedactionOutcome` — handled per §3.2.
2. `Exception` — `run_parallel` handing back a worker that died. One
   failure row, `entity_uid = "UNKNOWN"`.
3. **anything else, including `None`** — one failure row reading that the
   worker returned an unrecognised result. **Not silently skipped.**
   Tolerating a bare `None` here would re-create exactly the conflation
   this change removes, and it would let a stubbed test go on passing
   against a contract it no longer implements (§6, row for
   `test_a_partially_applied_redaction_is_reported`).

Each failure writes **one** audit row, **in the parent**:

```python
store_backend.log_audit(action_type="ERROR", entity_uid=sop, details=detail)
```

The detail is flattened to one line and its pipes escaped —
`" ".join(str(detail).split()).replace("|", "\\|")` — for the reason
`_report_export_failures` gives: it is rendered straight into a markdown
table row in the compliance report. It is **not** truncated.

**The audit write is in the parent and must stay there.** Measured:
`SqliteStore.__getstate__` drops the queue, the stop event and the audit
thread, and `__setstate__` starts a *new* audit thread in the child. That
child is torn down at pool shutdown without `stop()`, so a queued row can
be lost, and for a `:memory:` database the child writes nowhere at all.
This is the same reason `_report_export_failures` runs in the parent
(#126).

### 3.5 `ERROR`, not `DATA_LOSS`, and what that grades

`ERROR` is right and `DATA_LOSS` is wrong, for two independent reasons.

*Vocabulary.* `DATA_LOSS` means an element that was in the source is not
in the exported data. Here nothing was dropped — the burned-in
identifier is *present*, and being present is the problem. A failed
redaction is a failed *operation*, which is precisely what
`_report_export_failures` writes `ERROR` for.

*Grading.* A `DATA_LOSS` row is graded by `loss_scope`
(`session.py:1565`): `STANDARD` leaves the run at `PASS`, which is the
opposite of what is needed, and `PRIVATE` would be a lie about the tag,
since there is no tag. An `ERROR` row lands in `get_audit_errors()`,
which selects `action_type IN ('ERROR','WARNING')`, which populates
`exceptions`, which takes `validation_status` to `REVIEW_REQUIRED`:

```
log_audit("ERROR") -> get_audit_errors() -> report.exceptions non-empty
                   -> validation_status = "REVIEW_REQUIRED"
```

**Dependency, not a design element.** #218 is open on
`get_audit_errors()` missing a row that is still in flight, and it is
being fixed concurrently in `persistence.py`. This design does not touch
the audit reader or writer; it calls `log_audit()`. It does mean §7.6's
grading test must read the rows through a path that has drained the
queue — see §7.10.

### 3.6 `session.redact()` keeps its `int` return and raises at the end

**The signature does not change.** `redact(show_progress=True) -> int`.
The `int` continues to mean "how many instances were updated in memory",
and it is now returned only on a run where nothing that was tried failed.

If any instance failed, `redact()` raises `RedactionError` — **after**
every task has been dispatched, every successful mutation applied to the
graph, every failure audited, `scan_burned_in_annotations()` run, and the
console summary printed. Ordering matters: the `RISK` rows that scan
writes and the summary a user reads must be in place whether or not the
caller catches.

```python
class RedactionError(RuntimeError):
    """Redaction did not remove what it was asked to remove (#213).

    Raised after the whole pass, not at the first failure: the
    instances that could be redacted are redacted, and the failures are
    already in the audit log, so a caller that catches this still gets a
    compliance report that grades REVIEW_REQUIRED.
    """
    def __init__(self, failures, attempted):
        self.failures = list(failures)   # [(entity_uid, details)]
        self.attempted = attempted
        ...
```

Message shape: `"Redaction failed for N of M instances; their pixel data
still carries whatever the configured zones were meant to remove. First:
<uid>: <detail>. See the audit log for the rest."`

Defined in `services.py` next to `RedactionService`, and re-exported from
`isocenter/__init__.py` alongside `Session`, `Builder` and `Equipment`:
an exception a caller is expected to catch needs a stable import path,
and `isocenter.services` is not one this package advertises.

**Why `RuntimeError` and not `Exception`, recorded so it is not
"unified" later.** The package already raises bare `RuntimeError` from
two places on this same pipeline: `write_tree` wraps export failures in
one, and `_export_instance_worker` raises one for the `GUESSED` geometry
refusal (§4.3). So `except RuntimeError` around a full run cannot tell
the three apart, and someone will eventually notice that and propose
collapsing them. Subclassing is still the right call: a redaction that
did not redact *is* a runtime failure, so the base class is honest, and
inheriting keeps every existing `except RuntimeError` catching it rather
than turning a caught error into an escaping one — which is what
subclassing `Exception` directly would do to any caller that already
guards this pipeline. The reverse move is the one to resist: do not
demote `RedactionError` back to a bare `RuntimeError` for symmetry with
the export raises. The asymmetry is the point — the two export raises
mean "nothing was written", this one means "something unsafe is still in
the graph", and a caller has to be able to separate them. Giving the
export raises their own subclasses later is a fine follow-up (§10); it
does not block this.

**Why raise rather than return a summary.** Four reasons, in order of
weight:

1. The docstring already promises it (§1.4). Returning a summary object
   would leave that promise still unkept while changing the return type.
2. A failed *export* writes no file, so #181 could reasonably audit and
   continue — nothing unsafe leaves. A failed *redaction* is not
   contained: the instance stays in the graph and `export()` will write
   it. The stronger response is justified by the asymmetry.
3. `int` already carries a well-understood meaning and five test sites
   plus `README.md`, `docs/quickstart.md` and `GettingStarted.ipynb`
   depend on the happy path. Keeping it means CLAUDE.md's
   delete-don't-deprecate rule never has to engage.
4. A caller who genuinely wants to continue writes `except
   RedactionError` and reads `.failures`. A caller who wants to continue
   *by accident* is the case this exists to stop.

**Why the worker must not raise.** `_apply_redaction_rules` calls
`run_parallel(..., return_generator=True)` and consumes the results
incrementally. An exception escaping a worker terminates that generator
mid-iteration, and every mutation still queued behind it is lost — the
instances that *were* successfully redacted would silently not be applied
to the graph. Returning an outcome is what makes "all successful
mutations are applied before the raise" true rather than aspirational.

### 3.7 A failed instance is left exactly as it was found

Per §2.4, today a failure leaves a partially-zeroed, *persisted* array on
the threads path and an untouched instance on the processes path.

**Rule: on a failed task, do not persist.** `execute_redaction_task`'s
`finally` currently calls `store_backend.persist_pixel_data(inst)`
unconditionally; gate that call on the task not having failed. Keep
`inst.unload_pixel_data()` in the `finally`, unconditional.

This makes the two modes agree, because it is the *persist* that makes
the partial mutation durable: with no persist, `unload_pixel_data()`
drops the mutated array, and the next `get_pixel_data()` reloads the
original from the sidecar loader. The processes path is unaffected
(nothing crossed back to the parent to begin with).

**The one case this cannot reach, stated rather than discovered.** An
instance with neither a `_pixel_loader` nor a `file_path` — a hand-built
graph that has never been saved — cannot be unloaded:
`unload_pixel_data()` returns `False` and logs at DEBUG, deliberately,
because clearing would be a silent discard. Such an instance keeps
whatever zones were applied before the failure. That is acceptable and
must not be "fixed" with a pre-image copy: copying every array before
every redaction is exactly the resident-memory cost the whole lazy-pixel
design exists to avoid, and zeroing is monotone — a partial redaction has
removed *more* PHI than none, never less. What matters is that it is not
reported as a success, carries no `DERIVED` flag and no
`_ISOCENTER_REDACTION_HASH`, and so is retried on the next run (§7.9).

### 3.8 The serial path must do the same thing

`redact_machine_instances` is a public method, is what
`process_machine_rules` calls, and is directly exercised by five test
files. It gets the same treatment, in-process:

- Only an exception raised while applying zones is a failure. `None`
  pixels, a hash match and an empty ROI list stay silent skips —
  `test_redaction_robustness.py::test_redaction_crash_prevention` asserts
  the exact warning string on the no-pixels path and must keep passing
  verbatim.
- Each failure writes one `ERROR` audit row through `self.store_backend`
  when there is one (there may not be; `RedactionService(store)` is a
  supported construction and five tests use it). No backend means the
  failure is logged only, exactly as `_report_export_losses` handles the
  same asymmetry.
- Gate its `finally`'s `persist_pixel_data` on the instance not having
  failed, per §3.7.
- At the end of the pass, raise `RedactionError` if any instance failed.
- **The signature does not change.** It still returns `None`.
  `test_redaction_optimization.py` mocks it and asserts
  `assert_called_once()`; a changed return type would not break that, but
  a changed parameter list would.

### 3.9 What is deliberately not changed

- **`apply_redaction_to_array`'s bool return and its #66 raise.**
  **Amended 2026-08-31 (#217) — see §12.** The *signature* is no longer
  unchanged: #217 makes `geometry` required, so the sentence that stood
  here — "Signature, bool return, and the raise all stay. Its four tests
  in `test_redaction_robustness.py` must pass unmodified" — is now true
  of the return and the raise and false of the signature and of the four
  tests. The bool return and the raise do stay; the signature and the
  four tests change, per §12.1 and §12.3.
  Its comment at `services.py:546-553` is **corrected in
  place** to say which caller propagates and which swallowed, per
  CLAUDE.md's "comments explain the trap": the export worker's call
  propagates and always did; `redact_machine_instances` and
  `execute_redaction_task` are the ones that did not, and now do.
- **`_apply_roi_to_instance`.** Untouched, including the long docstring
  about the two arms and their dirtying.
- **The export worker's redaction call** (`io_handlers.py:882`). Already
  correct: it propagates to `ExportOutcome(ok=False)`, audited and
  counted by #181's machinery, with no file written.
- **Anything in `persistence.py`.** #218 is concurrent. This design calls
  `log_audit()` and reads nothing.
- **`_redaction_worker_count`, `run_parallel`, the `int` return, the
  console output.**

---

## 4. #216 — the design

### 4.1 The rule

> **Both pixel branches write their geometry descriptors from the one
> resolved `PixelGeometry`, and neither writes a geometry that had to be
> guessed.**

Stated this way rather than as "hoist the refusal" because the hoist
alone leaves §2.5's cases b and c — a written file whose `Rows` and
`Columns` describe an image the bytes do not contain.

### 4.2 One helper, called from both arms

Add a module-level function to `io_handlers.py`:

```python
def _write_pixel_geometry(ds, geom, attributes):
    """Write the descriptors that describe the pixel element just written.

    Both pixel branches call this, so `Rows`, `Columns`,
    `SamplesPerPixel` and `NumberOfFrames` agree with the bytes by
    construction rather than by review. They are Type 1 in the Image
    Pixel Module and Type 1 again in the Floating Point and Double
    Floating Point Image Pixel Modules (PS3.3 C.7.6.24, C.7.6.25), so
    "the float element does not need them" was never true -- and worse,
    `_merge` had already written whatever `attributes` declared, so an
    instance whose descriptors were stale exported a file describing a
    different image (#216).
    """
```

Body, moved verbatim from the integer branch's existing lines so the
integer path's behaviour is unchanged:

```python
ds.Rows = geom.rows
ds.Columns = geom.cols
ds.SamplesPerPixel = geom.samples
if geom.frames > 1 or "0028,0008" in attributes:
    ds.NumberOfFrames = geom.frames

photometric = resolve_photometric_interpretation(attributes, geom.samples)
if photometric is None:
    photometric = attributes.get("0028,0004")
if photometric:
    ds.PhotometricInterpretation = photometric

if planar_configuration_default(attributes, geom.samples):
    ds.PlanarConfiguration = 0
```

Notes the coder must not "simplify" away:

- The `photometric is None` arm is **not** an `or`. `None` means "the
  declared value is coherent, leave it alone", and that arm is what lets
  `YBR_FULL`, `YBR_ICT` and `MONOCHROME1` survive a round trip.
- `planar_configuration_default` returns `False` for `samples < 3`, so
  including it costs the float path nothing on any conformant instance
  and keeps one spelling of the block.
- **The `NumberOfFrames` clause is a no-op on every existing float
  fixture, and that was measured, not assumed.** `Instance.set_pixel_data`
  writes `0028,0008` into `attributes` only for a rank-3 array — measured:
  `(4,4)` and `(8,8)` produce `{0028,0002, 0028,0004, 0028,0010, 0028,0011,
  0028,0100}` and no frame count, `(2,4,8)` produces `0028,0008: 2`. Every
  float fixture in the suite is rank 2, so both halves of the condition are
  false and the float path writes no `NumberOfFrames` element where it
  writes none today. Keep the clause anyway: it is what makes a rank-3
  float with a declared `SamplesPerPixel` (the §2.3 shape) write a frame
  count instead of dropping one, and it keeps the helper a single spelling
  shared with the integer branch.
- Use the **literal** `"0028,0008"`. `pixel_geometry` defines
  `TAG_NUMBER_OF_FRAMES`, but `io_handlers.py`'s import at line 40 pulls
  only `GeometryEvidence`, `planar_configuration_default`,
  `resolve_photometric_interpretation` and `resolve_pixel_geometry` — no tag
  constants — and the existing integer branch spells it `"0028,0008"` at
  line 993. Adding the constant to that import would be harmless, but it
  would be a second spelling of a tag the file already writes one way.
  Tags are lowercase-hex strings.
- **`BitsAllocated` is not in here.** Each branch keeps its own: the
  float arms set 32 or 64 because those are the Enumerated Values the two
  modules require next to the tag they chose, and the integer arm derives
  `arr.itemsize * 8` for the reason spelled out in its own comment
  (spec §3.10 of the pixel-geometry design). They happen to agree
  numerically; they are not the same statement.

### 4.3 The restructured worker

Only the ordering changes; every existing block keeps its body.

```
arr = <pixel array, or None>
geom = resolve_pixel_geometry(arr.shape, inst.attributes) if arr is not None else None

if arr is not None:
    <redaction block, unchanged>

# (A) The refusal, hoisted out of the integer branch.
if arr is not None and geom.evidence is GeometryEvidence.GUESSED:
    raise RuntimeError(<the existing message, unchanged>)

if arr is not None and arr.dtype.kind == 'f':
    if arr.itemsize == 4:
        ds.FloatPixelData = arr.tobytes(); ds.BitsAllocated = 32
    elif arr.itemsize == 8:
        ds.DoubleFloatPixelData = arr.tobytes(); ds.BitsAllocated = 64
    else:
        <float16 arm, unchanged: RuntimeError on an image modality, else a loss row>

    if "PixelData" in ds:
        del ds.PixelData

    # (B) New. Only on the arms that actually wrote a pixel element --
    # the float16 arm writes none, so it gets no descriptors, exactly as
    # BitsAllocated is not written there either.
    if arr.itemsize in (4, 8):
        _write_pixel_geometry(ds, geom, inst.attributes)

    arr = None

if arr is not None:
    if not ctx.compression:
        ds.PixelData = arr.tobytes()

    # (C) New: the other half of PS3.5 Section 8.2's mutual exclusion.
    for kw in ("FloatPixelData", "DoubleFloatPixelData"):
        if kw in ds:
            del ds[kw]

    _write_pixel_geometry(ds, geom, inst.attributes)
    ds.BitsAllocated = arr.itemsize * 8
    ds.BitsStored = ...
    ds.HighBit = ...
    ds.PixelRepresentation = ...
```

**(A) — the hoist.** The refusal moves above the float branch. Nothing
between the resolution and its old site can change `geom`: the redaction
block copies the array when it is not writeable, which does not change
its shape, and reassigns nothing else. So the integer path's behaviour is
byte-identical.

Placing it *after* the redaction block rather than before costs one
redaction pass on an instance that is about to be refused, and buys the
property that the block order in the source still reads as
resolve → redact → write. Either placement is correct; this one is
specified so the reviewer and the coder do not have to negotiate it.

**(B) — descriptors accompany the element.** The float16 arm writes no
pixel element (it reports a `STANDARD` loss on a non-image modality and
raises on an image one), so it writes no descriptors either. This is the
same rule the existing `BitsAllocated` placement already follows.

**(C) — mutual exclusion, the other direction.** Measured reachable
(§2.5 case f). The float branch has deleted `PixelData` since #170 for
exactly this reason; the integer branch never deleted its counterpart.

### 4.4 What is deliberately not changed

- **`pixel_geometry.py`.** No change, and no new import. It is pure
  stdlib on purpose, `_export_instance_worker` imports it at module scope
  in a bare child process, and
  `test_pixel_geometry.py::test_module_imports_nothing_heavy` enforces it
  with an AST check. `resolve_pixel_geometry` already takes a shape tuple
  and does not care about dtype.
- **`GUESSED` accepted by `set_pixel_data`.** The asymmetry between the
  setter (accepts, warns) and the export (refuses) is deliberate and
  pinned by
  `test_pixel_geometry_pipeline.py::test_set_pixel_data_accepts_a_guessed_geometry_but_warns`.
- **Refusing `samples > 1` on a float element.** Both C.7.6.24 and
  C.7.6.25 enumerate `PhotometricInterpretation = MONOCHROME2`, which
  implies a conformant float instance is single-sample, so a float array
  resolving to `samples == 3` is nonconformant whatever this code does.
  It is *not* refused here. This design's job is to stop the exporter
  from inventing or mis-stating geometry; adding a new conformance
  refusal is a separate decision with its own blast radius (a rank-3
  float array with a *declared* `SamplesPerPixel = 3` resolves `DECLARED`
  and would newly fail). Decided against, not overlooked. Filed in §10.
- **`_merge`.** It goes on writing whatever `attributes` holds; the
  geometry block overwrites it afterwards, which is exactly how the
  integer path already works.
- **The float16 loss row and its scope.**
- **`_compress_j2k`.** Compression is not offered for float pixel data
  and this design does not add it.

---

## 5. Compatibility survey — #216

Method note, taken from the pixel-geometry spec's §6, which got its
survey wrong twice by asking which *tests* asserted on a behaviour and
never which *non-test consumer* depended on it. Both questions are asked
below.

### Non-test consumers

| Consumer | Reaches the float branch? | Effect |
| --- | --- | --- |
| `DicomExporter.export_batch` ← `Session._run_export_batch` ← `session.export()` | yes | A float instance with a `GUESSED` geometry now returns `ok=False`, is audited `ERROR`, counted in `ExportSummary.failed`, and grades `REVIEW_REQUIRED` instead of writing an undecodable file. A float instance with stale `Rows`/`Columns` now writes correct ones. |
| `DicomExporter.write_tree` (the serializer; `scripts/` generators, and tests that need DICOM with no session) | yes | Newly raises `RuntimeError` on a `GUESSED` float geometry, via its existing "Export incomplete" wrapper. |
| `scripts/generate_test_dataset.py`, `generate_ocr_test_data.py`, `generate_redaction_example.py` | **no** | Verified by grep: `float32`, `float64`, `np.float` and `dtype=float` have **zero** occurrences anywhere under `scripts/`. Every generated array is integer, so the hoisted refusal cannot newly fire on them through the float path, and the integer path is byte-identical. No generated fixture changes. |
| `scripts/generate_waveform_test_data.py` | no | Waveforms, no pixel branch. |
| `isocenter/exporters/wfdb.py` | no | Only names `_export_instance_worker` in a comment. It writes PhysioNet format 16 through its own path. |
| `isocenter/exporters/dicom.py` | via `session.export()` only | No direct call. |
| `isocenter/murmur.py` | no | Annotation JSON. |
| `SidecarPixelLoader` | n/a | Never produces a float array — it derives dtype as `uint16 if bits > 8 else uint8`. Float arrays reach the worker only from `pydicom` re-reading the source file (the ingest path, #170) or from a direct `pixel_array`/`set_pixel_data` assignment. |
| `_merge` | reader of nothing new | Continues to write declared descriptors; the geometry block now overwrites them on both branches instead of one. |

### Tests

| Test | Effect |
| --- | --- |
| `tests/test_float_pixel_data_export.py`, all 11 test functions (16 cases with parametrisation) | **Unchanged, must keep passing.** Every fixture is rank 2 (4×4 or 8×8) with `Rows`/`Columns` derived from `arr.shape[-2:]` and `SamplesPerPixel = 1` declared, so the resolver returns `STRUCTURAL` and `_write_pixel_geometry` writes the values `_merge` already wrote. `PhotometricInterpretation` is declared `MONOCHROME2` in `_export_one` and in `_write_float_src`, and `resolve_photometric_interpretation` returns `None` for a coherent monochrome pair, so the declared value survives. **Measured, not read**: for `(4,4)`/`(8,8)` float32 and float64, for the three attrs-after-`set_pixel_data` variants (`7fe0,0010` bytes, `0028,0100 = 16`, `0028,0100 = 64`), and for the ingest arm (`_write_float_src` written with pydicom, ingested, `get_pixel_data()` back as `(4,4) float32`), the file written today and the resolved geometry agree on every field: `Rows`, `Columns`, `SamplesPerPixel`, `PhotometricInterpretation = MONOCHROME2`, no `NumberOfFrames`, `planar_configuration_default` false, evidence `STRUCTURAL`. The helper writes what `_merge` already wrote. |
| `tests/test_float_pixel_data_export.py::test_a_float16_array_is_refused_and_reported` and `::test_an_unwritable_float_on_an_image_modality_fails_the_export` | Unchanged: 4×4 float16, rank 2, `STRUCTURAL`, never `GUESSED`; the float16 arm writes no descriptors before and none after. |
| `tests/test_pixel_geometry_pipeline.py::test_export_worker_refuses_to_write_a_guessed_geometry` | Must keep passing. Same `(100,200,3) uint8` fixture, same `RuntimeError`, raised one block earlier. |
| `tests/test_pixel_geometry_pipeline.py` (the rest) | Integer path, unchanged. |
| `tests/test_api_coherence.py` | Must keep passing: `write_tree` and `session.export()` still produce identical trees. Both route through the same worker. |
| `tests/test_pixel_export.py`, `test_pixel_integrity.py`, `test_ingestion_normalization.py`, `test_export_pixels.py`, `test_redaction_export.py` | Integer path, unchanged. |
| `tests/test_export_failure_audit.py` | Unchanged; the new refusal produces the same `ExportOutcome(ok=False)` shape it already exercises. |
| `tests/test_private_binary_ingest.py:368,379,412,646` | Ingest-side float fixtures, `4` elements, rank 1 or rank 2. It asserts on `DATA_LOSS` rows at ingest, not on export descriptors. Unchanged. |

### Costs to accept, stated rather than discovered

1. **`session.export()` can newly fail a float instance** whose geometry
   is a guess. It is audited and reported, which is the point.
2. **`write_tree` can newly raise** on the same input. No `scripts/`
   generator produces one.
3. **A float instance's `Rows`/`Columns`/`SamplesPerPixel` can change**
   between `258331c` and this fix — from the declared value to the
   resolved one — whenever the two disagree. That is §2.5 cases b and c,
   and changing them is the fix.
4. **A float instance that declared no `PhotometricInterpretation` now
   carries `MONOCHROME2`.** Type 1 in both float modules.

---

## 6. Compatibility survey — #213

### Non-test consumers

| Consumer | Effect |
| --- | --- |
| `Session.redact_by_machine` (`session.py:2061`) | Calls `self.redact()` inside `try/finally`. The `RedactionError` propagates; the `finally` restores the original rules first. Correct as written, no change. |
| `Session._apply_redaction_rules` | Rewritten (§3.4, §3.6). Internal. |
| `RedactionService.process_machine_rules` | Calls `redact_machine_instances`, so it newly propagates `RedactionError`. Public, and the only in-tree caller is `test_redact_error.py` / `test_redaction_optimization.py`. |
| `tests/benchmarks/run_stress_test.py:126` | `sess.redact(show_progress=False)` on generated data with well-formed zones. Unaffected on the happy path; would now fail loudly rather than under-report if a benchmark fixture ever produced a bad zone, which is an improvement to the benchmark. |
| `README.md:213`, `docs/quickstart.md:74`, `GettingStarted.ipynb` | All three show `session.redact()` on the happy path with no return value used. Unaffected. **The docstring of `redact()` must be updated** — it is the source of the generated API reference, and its `Raises:` clause should now name `RedactionError` and say what state a failed run leaves. That is the whole of the docs change; `docs.yml` redeploys on any `isocenter/**.py` push. `test_doc_anchors.py` is unaffected (no new headings, no new fragment links). |
| `isocenter/__init__.py` | Gains `RedactionError` in the export block, next to `Session`, `Builder` and `Equipment`. |
| `scripts/mutation_probe.py` `TARGETS` and CLAUDE.md's module→test table | **No change.** Neither `services.py` nor `session.py` is a probe target, and `test_mutation_probe_targets.py` pins the table against `TARGETS`, not against the set of modules that exist. Adding one would require a CLAUDE.md edit and is out of scope. |
| `automation.py`, `profiles.py`, `verification.py` | No redaction calls (grepped). |
| Third-party callers of `RedactionService.apply_redaction_to_array` | **Amended 2026-08-31 (#217) — see §12.** **Broken, deliberately.** `geometry` becomes required; a call omitting it raises `TypeError`. Bool return and raise unchanged. §12.1, §12.6. |

### Tests

| Test | Effect |
| --- | --- |
| `tests/test_redact_reports_outcome.py::test_a_partially_applied_redaction_is_reported` | **Must be edited, and this is the one edit the change forces.** It monkeypatches `execute_redaction_task` to return `None` for tasks 2 and 3 and asserts `redacted == 1` plus a "1 of 3" warning. Under §3.4 a bare `None` is a *failure*, so the un-edited stub would raise. Change the stub's fallback from `None` to `RedactionOutcome(ok=True, sop_instance_uid=task["instance"].sop_instance_uid, mutation=None)`. **Both assertions survive verbatim**, because the benign no-mutation arm still counts toward the shortfall warning. The test's meaning — a task that produced no mutation must not vanish — is exactly preserved, and it is now stated in the vocabulary that distinguishes it from a failure. |
| `tests/test_redact_reports_outcome.py`, the other six | Unchanged, must keep passing: `== 3`, `== 0`, the setup-failure `pytest.raises(RuntimeError)`, the two console tests, and the `ISOCENTER_MAX_WORKERS=banana` test. All are happy-path or setup-path. |
| `tests/test_redaction_robustness.py::test_redaction_crash_prevention` | Must keep passing **verbatim**, including its exact warning-string assertion. A `None` pixel array is a silent skip, not a failure (§3.1). |
| `tests/test_redaction_robustness.py`, the four `apply_redaction_to_array` tests | **Amended 2026-08-31 (#217) — see §12.** **All four break** — they omit `geometry`. Each gains one keyword argument; every assertion survives verbatim. §12.2, §12.3. |
| `tests/test_redaction_robustness.py::test_log_throttling` | Unchanged. |
| `tests/test_redaction_optimization.py` | Mocks `redact_machine_instances` and asserts `assert_called_once()`; also calls it for real at `:63` with a valid ROI. The signature does not change and the valid path does not raise. |
| `tests/test_redact_error.py::test_execute_config_crash_repro` | `process_machine_rules` with a list-form zone `[10,50,10,50]`, which is valid. No failure, no raise. |
| `tests/test_redact_error.py::test_execute_config_session_level_interruption` | `AttributeError` from a malformed rule still propagates out of `redact()` unchanged — it is raised inside `prepare_redaction_tasks`, above the outcome machinery. |
| `tests/test_redact_error.py::test_burned_in_safety_check` | `redact()` with no rules returns 0 early. Unchanged. |
| `tests/test_services.py:30`, `tests/test_memory_redaction.py:70`, `tests/test_redaction_rgb.py:40`, `tests/test_pixel_geometry_pipeline.py:318` | All call `redact_machine_instances` with valid ROIs on well-formed instances. No failures, no raise, return value still `None`. |
| `tests/test_redaction_parallel.py:118,168`, `tests/test_redaction_wildcard.py:103`, `tests/test_session.py:44,73`, `tests/test_full_logging.py:47` | `session.redact()` on valid configurations. Unchanged. |
| `tests/test_zone_validation.py`, `tests/test_redaction_roi.py` | Zone parsing, above the worker. Unchanged. |
| `tests/test_reporting*.py` | A run with no redaction failures writes no new `ERROR` rows, so no grade changes. Verified by the requirement that the §7.6 control arm grades `PASS`. |

### Costs to accept

1. **`session.redact()` raises where it previously returned an int.** The
   `CHANGELOG.md` breaking entry must name it exactly:
   `isocenter.RedactionError`, raised at the end of a pass in which any
   instance's zone could not be applied, where the call previously
   returned the count of the instances that *did* work. §11.
2. **One test stub must be updated** (the row above). No production
   consumer changes.
3. **A partially-redacted array on the threads path is no longer
   persisted.** That is §3.7 and it is the fix, but it is a behaviour
   change on 3.14t that a reader of a sidecar might notice.
4. **`RedactionOutcome.error` is a second spelling of
   `ExportOutcome.error`.** Stated in §3.3, filed in §10.

---

## 7. Tests

For every clause, the test that **fails if that clause is reverted**. The
recurring failure on this work has been reasoning about whether code is
correct instead of about what would catch it changing; a test that passes
both before and after is worth nothing here.

Each test is labelled with one of the four polarities the pixel-geometry
spec's §7 introduced, and the PR body must repeat the labels:

- **(fix)** — reproduces a defect this change fixes; fails on `258331c`.
- **(guard)** — pins correct existing behaviour; passes on `258331c`.
- **(new)** — pins a deliberate behaviour change; fails on `258331c`
  because the behaviour is new, not because it was broken.
- **(regression-guard)** — passes on `258331c`, would fail on a
  mis-implementation of this change.

### 7.1 #216 — new tests, added to `tests/test_float_pixel_data_export.py`

That file is the home of the float branch and already has the direct
`_export_instance_worker` harness these need.

1. **`test_a_guessed_geometry_is_refused_on_the_float_path`** *(fix)*
   `(5,6,3) float32`, and **none** of `0028,0002`, `0028,0008`,
   `0028,0010`, `0028,0011` declared. Assert `outcome.ok is False`, that
   the output path does not exist, and that the message names
   `(5, 6, 3)` and `SamplesPerPixel`. Kills clause §4.3(A).
   On `258331c`: a file is written with no Rows, Columns or
   SamplesPerPixel (§2.5 case a).

   **Fixture trap, and it is the mirror of the pixel-geometry spec's
   trap 1.** The array must be assigned with `inst.pixel_array = arr`,
   *not* through `set_pixel_data`. The setter writes
   `SamplesPerPixel = 3` back into `attributes`, which makes the
   resolution `DECLARED` and the test passes with the clause deleted. Add
   a same-file assertion that `"0028,0002" not in inst.attributes`
   immediately before the call, so a future edit to the fixture fails
   loudly rather than silently going vacuous.

2. **`test_the_float_path_writes_the_geometry_it_resolved_not_the_one_declared`**
   *(fix)* `(4,4) float32`, with `{"0028,0010": 10, "0028,0011": 10,
   "0028,0002": 1}` applied **after** the pixel assignment. Assert
   `ds.Rows == 4`, `ds.Columns == 4`, `ds.SamplesPerPixel == 1`, and
   `ds.Rows * ds.Columns * ds.SamplesPerPixel * 4 ==
   len(ds.FloatPixelData)`. Kills clause §4.2/§4.3(B).
   On `258331c`: `Rows=10 Columns=10` beside 64 bytes (§2.5 case b).

   **This is the test the issue's own suggested fix does not produce.**
   A fixture whose declared descriptors *agree* with the array passes
   with the clause deleted, because `_merge` wrote the right values by
   luck.

3. **`test_a_multiframe_float_carries_the_frame_count_it_resolved`**
   *(fix)* `(2,4,8) float32` with `{"0028,0002": 1, "0028,0008": 2,
   "0028,0010": 99, "0028,0011": 99}` applied after. Assert
   `Rows == 4`, `Columns == 8`, `NumberOfFrames == 2`,
   `SamplesPerPixel == 1`, and the byte-length identity above times
   `NumberOfFrames`. On `258331c`: `Rows=99 Columns=99` (§2.5 case c).

4. **`test_photometric_interpretation_is_written_on_the_float_path`**
   *(fix)*, two arms.
   (a) `(4,4) float32` with **no** `0028,0004` → assert
   `ds.PhotometricInterpretation == "MONOCHROME2"` (Type 1, Enumerated
   Value, PS3.3 C.7.6.24). On `258331c` the element is absent.
   (b) `(4,4) float32` with `0028,0004 = "MONOCHROME1"` declared → assert
   it is **preserved**. Guards against "the module enumerates
   MONOCHROME2, so force it", which would put a second, disagreeing
   answer next to `resolve_photometric_interpretation`.

5. **`test_the_float_elements_are_deleted_when_pixel_data_is_written`**
   *(fix)* `(4,4) uint8` with `attrs={"7fe0,0008": b"\x00" * 64}` applied
   after. Assert `(0x7FE0, 0x0008) not in ds` and `"PixelData" in ds`.
   Kills clause §4.3(C). On `258331c` the file carries both, violating
   PS3.5 Section 8.2 (§2.5 case f). Mirrors the existing
   `test_the_integer_tag_is_deleted_when_a_float_element_is_written`,
   which is why it belongs beside it.

6. **`test_a_guessed_float_geometry_stops_write_tree_too`** *(fix)*
   Build a one-instance `Patient` graph by hand with the case-a float
   array, call `DicomExporter.write_tree`, assert `RuntimeError` and that
   no `.dcm` exists under the output root. Pins that the refusal reaches
   the serializer path the `scripts/` generators use, which is the path
   #205 measured and the one `session.export()` cannot exercise.

7. **Must keep passing, unmodified** *(guard)*: all 11 existing test functions in
   `tests/test_float_pixel_data_export.py`;
   `tests/test_pixel_geometry_pipeline.py::test_export_worker_refuses_to_write_a_guessed_geometry`;
   `tests/test_api_coherence.py`.

### 7.2 #213 — new file `tests/test_redaction_failure_is_reported.py`

Fixture: one session, two series on two machines.
`SN_OK` carries two instances of `(32,32) uint8` all 200 and a rule with
one valid zone `[0, 8, 0, 8]`. `SN_BAD` carries one instance and a rule
with one zone `[0, "abc", 0, 8]`. One rule per machine keeps
`sorted(valid_rois)` out of §2.6's way. The session is `save()`d before
`redact()`, so the failed instance has a loader and §3.7 is exercisable.

8. **`test_a_zone_that_cannot_be_applied_raises_instead_of_returning_a_count`**
   *(new)* `pytest.raises(RedactionError)`; assert the message names the
   failing instance's UID and that `exc.failures` has length 1.
   Kills clause §3.6. On `258331c`: returns `2`.

9. **`test_the_instances_that_could_be_redacted_still_were`** *(new)*
   Inside the `pytest.raises` block, then afterwards: the two `SN_OK`
   instances have `_ISOCENTER_REDACTION_HASH` set, `0008,0008`
   containing `"DERIVED"`, and `arr[0:8, 0:8].sum() == 0`; the `SN_BAD`
   instance has no hash and no `DERIVED`. Kills the ordering half of
   §3.6 — an implementation that raises from the worker, or at the first
   failure, leaves the successful mutations unapplied and fails this.

10. **`test_a_failed_redaction_writes_one_error_audit_row`** *(fix)*
    Catch the `RedactionError`, then read the rows (§7.10). Assert
    exactly one `ERROR` row, that `entity_uid` is the failing instance's
    pre-redaction SOP UID, and that `details` contains `ValueError` and
    the ROI or the reason. Kills clause §3.4/§3.5.
    On `258331c`: zero rows (§2.1).

11. **`test_a_failed_redaction_grades_the_report_review_required`**
    *(fix)*, **two arms, and the control arm is not optional.**
    Control: the same fixture with `SN_BAD`'s zone corrected → `redact()`
    returns 3, `generate_report()` renders `PASS`.
    Failure: as filed → `REVIEW_REQUIRED`.
    Both arms must `anonymize()` first so `audit_summary` is non-empty;
    an empty summary grades `REVIEW_REQUIRED` on its own
    (`session.py:1583`) and a test without the control passes with the
    whole fix deleted. This is the trap
    `tests/test_export_failure_audit.py::_run` already documents.

12. **`test_the_serial_path_reports_the_same_failure_as_the_parallel_one`**
    *(new)* Drive the same malformed rule through
    `RedactionService(store, backend).process_machine_rules(rule)` and
    assert `RedactionError` and one `ERROR` row — the same assertions as
    tests 8 and 10. Kills clause §3.8. On `258331c` neither raises.

13. **`test_a_task_with_nothing_to_do_is_not_a_failure`**
    *(regression-guard)* Run `redact()` twice on a fixture with only
    valid zones. The second call returns 0, does not raise, and writes no
    `ERROR` row. Passes on `258331c`. This is the guard against
    implementing §3.2 as "no mutation means it failed", which would
    invert the same conflation the change exists to remove.

    Second arm: an instance whose `get_pixel_data()` returns `None`
    (patch `Instance.get_pixel_data`, as
    `test_redaction_robustness.py` does) is skipped, not failed.

14. **`test_a_failed_instance_is_left_as_it_was_found`** *(fix on
    threads, guard on processes — say both)*
    Parametrised over `ISOCENTER_FORCE_THREADS=1` and
    `ISOCENTER_FORCE_PROCESSES=1` via `monkeypatch.setenv`. One saved
    instance, zones `[[0,8,0,8], [1,"abc",0,8]]` (first elements differ,
    per §2.6). After the `RedactionError`: `save()`, `close()`, reopen,
    and assert the reloaded array is **entirely 200** — including
    `arr[0:8, 0:8]` — and that the instance has no
    `_ISOCENTER_REDACTION_HASH` and no `DERIVED`.
    Kills clause §3.7.
    On `258331c`: **fails under threads** (the reloaded zone 1 is zeroed)
    and **passes under processes** (§2.4). The PR body must say so; a
    reviewer running only the local 3.14 default sees the passing arm.

    **The `monkeypatch.setenv` is legitimate here and would not be on the
    export path.** `_resolve_strategy` reads `ISOCENTER_FORCE_THREADS` /
    `ISOCENTER_FORCE_PROCESSES` **in the parent** when it picks the
    executor, and `_apply_redaction_rules` passes no `maxtasksperchild`,
    so the parent's environment decides. `session.export()`'s workers are
    always separate processes and a parent monkeypatch is invisible to
    them — which is why tests 1–6 drive `_export_instance_worker`
    directly instead.

15. **`test_a_failed_zone_can_be_retried`** *(regression-guard)*
    After the failure, correct the rule and call `redact()` again: it
    returns the full count, does not raise, and the previously-failing
    instance now carries the hash and the zeroed zone. Passes on
    `258331c` for the "not sticky" half; pins that reporting the failure
    did not make it permanent.

16. **`test_the_geometry_contradiction_reaches_the_same_report`**
    *(fix)* The §2.3 fixture: `(5,8,4) uint8` with `0028,0002 = 3`
    declared, one valid zone. Assert `RedactionError` and one `ERROR`
    row. This is the second, independent entry into the same sink, and
    it is the one `CHANGELOG.md` already promised would be closed here.
    Kept as a *second* arm rather than the primary fixture because it
    depends on `resolve_pixel_geometry`'s behaviour, and a test built on
    it could go green on a resolver change rather than on the clause
    under test.

17. **Must keep passing** *(guard)*: everything in §6's test table,
    including `tests/test_redaction_robustness.py::test_redaction_crash_prevention`
    verbatim, and `tests/test_redact_reports_outcome.py` with the single
    stub edit §6 specifies.

### 7.3 Reading audit rows without racing #218

**Every test in §7.2 that reads audit rows must use one of these two
patterns, and the coder must not invent a third.** Measured on
`258331c`: `store_backend.get_audit_errors()` called immediately after an
export that had just written an `ERROR` row returned **zero rows**. That
is #218, it is being fixed concurrently, and this design must not depend
on it.

- **Preferred:** direct sqlite after `session.close()`, exactly as
  `tests/test_export_failure_audit.py::_audit` does —
  `SELECT entity_uid, details FROM audit_log WHERE action_type='ERROR'`.
- **Also safe:** call `session.generate_report(path)` first. Its
  `get_audit_summary()` calls `stop()`, which joins the audit thread and
  flushes the queue, so the subsequent read is drained. This is the
  pattern test 11 needs anyway.

### 7.4 Non-vacuity

- Tests 1–6, 10, 11, 12, 16 must **fail on `258331c`**. Tests 1, 2, 3, 5
  and 10 are verified failing by the measurements in §2.
- Tests 8 and 9 fail on `258331c` because the behaviour is new.
- Tests 13 and 15 **pass on `258331c`**. Say so in the PR body rather
  than listing them as regressions.
- Test 14 fails on `258331c` under threads and passes under processes.
  Both must be stated.
- **Test 18** (§12.5, #217) fails on `258331c` with `DID NOT RAISE`:
  measured, the call with `geometry` omitted currently succeeds and
  returns `True`. The four #66 tests beside it pass both before and
  after, which is why 18 exists.
- Each of tests 1, 2, 3, 5, 10, 12 must also be checked against an
  implementation with **only its own clause** reverted, not only against
  `258331c`. Test 1 in particular passes with §4.2 reverted (the file is
  refused either way), and test 2 passes with §4.3(A) reverted, so
  neither alone guards the other.

---

## 8. Out of scope

- **Anything inside `persistence.py`.** #218 is concurrent. This design
  calls `log_audit()` and states §3.5's grading chain as a dependency.
- **`ExportOutcome.error`'s exception-object shape.** §3.3, filed in §10.
- **A `DATA_LOSS` scope for redaction.** Nothing is dropped; §3.5.
- **Refusing a float element with `samples > 1`.** §4.4, filed in §10.
- **Any change to `verify()`.** It is an OCR pass comparing detected text
  boxes against configured zones and has no view of either defect.
- **`apply_redaction_to_array`'s bool return or raise.** **Amended 2026-08-31 (#217) — see §12.** Its *signature* is now in scope: §12.1.
- **The `int` return of `session.redact()`.**
- **Compressing float pixel data.**
- **A pre-image copy of every pixel array**, so that a partially-applied
  redaction could be rolled back. §3.7 argues against it on resident
  memory, which is the guarantee the whole lazy-pixel design rests on.

---

## 9. Implementation order

Two PRs. Each numbered step is independently revertible.

**PR 1 — #216** (`fix: write the float pixel path's geometry, and refuse a
guessed one (#216)`)

1. `_write_pixel_geometry` in `io_handlers.py`, called from the integer
   branch only, replacing the lines it was moved from. Suite stays green
   and nothing changes.
2. Hoist the `GUESSED` refusal (§4.3 A). Tests 1, 6.
3. Call `_write_pixel_geometry` from the float arms (§4.3 B). Tests 2, 3, 4.
4. Delete the float elements in the integer branch (§4.3 C). Test 5.
5. `CHANGELOG.md`.

**PR 2 — #213** (`fix: report a redaction zone that could not be applied
(#213)`)

6. `RedactionOutcome` and `RedactionError` in `services.py`;
   `RedactionError` re-exported from `isocenter/__init__.py`. No call site
   changed. Suite stays green.
7. `execute_redaction_task` returns outcomes (§3.2) and stops persisting
   a failed instance (§3.7). Update the one test stub (§6). Tests 13, 14.
8. `Session._apply_redaction_outcomes` and the raise in
   `_apply_redaction_rules` (§3.4, §3.6). Tests 8, 9, 10, 11, 16.
9. `redact_machine_instances` (§3.8). Test 12.
10. Correct the comment at `services.py:546-553` (§3.9), and the
    `redact()` docstring's `Raises:` clause (§6).
11. `CHANGELOG.md`.

**PR 2 gains steps 12-14 for #217 — see §12.7.**

Full suite on 3.12 and 3.14t for both. The local venv is 3.14.6, so a
local pass exercises **one** gate version and — for test 14 — the arm
that already passes.

---

## 10. Found here, to be filed separately

1. **`prepare_redaction_tasks` sorts heterogeneous ROI tuples.**
   `rois_stable = sorted(valid_rois)` (`services.py:192`) raises
   `TypeError: '<' not supported between instances of 'str' and 'int'`
   out of `redact()` for a configuration holding two zones whose first
   differing element is a string against an int (§2.6). Loud, so not
   #213, but the hash exists to make re-runs idempotent and a
   configuration that cannot be hashed should be rejected by
   `zone_validation` with a message naming the zone, not by `sorted`.

2. **`ExportOutcome.error` and `RedactionOutcome.error` are two spellings
   of one idea** (§3.3). Unifying them means moving `ExportOutcome` to
   prose, which touches the export path.

3. **A float pixel element with `SamplesPerPixel > 1` is nonconformant
   and is not refused** (§4.4). Both C.7.6.24 and C.7.6.25 enumerate
   `PhotometricInterpretation = MONOCHROME2`. A refusal is a new
   conformance gate with its own blast radius.

4. **`check_unsafe_attributes()` matches the JSON text
   `'%"0028,0301": "YES"%'`** (`persistence.py`). It is the only thing
   that grades a burned-in instance today, and it depends on
   `json.dumps`' default separator and on the value being the exact
   string `"YES"`. An instance storing `0028,0301` as a list, or a store
   written with a different separator, is invisible to it. Adjacent to
   #213 (§2.7) but a different defect.

5. **The two export raises are bare `RuntimeError`s.** `write_tree`'s
   "Export incomplete" wrapper and `_export_instance_worker`'s `GUESSED`
   geometry refusal both raise the base class, so once `RedactionError`
   exists, `except RuntimeError` around a pipeline catches three
   different conditions and can distinguish only one of them. Giving
   those two their own subclasses is the symmetric follow-up; it is a
   separate breaking-ish change to the export path and does not belong
   in either PR here.

6. **`execute_redaction_task` calls `traceback.print_exc()`** on every
   failure, writing an uncaptured traceback to the worker's stderr. With
   the failure now reported structurally, the raw print is noise on a
   cohort run. Left in this design because removing it is a console
   change, not a correctness one.

---

## 11. `CHANGELOG.md`

Two entries, one per PR, under `### Fixed`. Both must match the depth of
the surrounding entries — CLAUDE.md calls the changelog the project's
primary design record.

**#213's entry must contain, explicitly:**

- The exact exception a previously-working call now raises:
  `isocenter.RedactionError`, from `session.redact()` and from
  `RedactionService.redact_machine_instances` /
  `process_machine_rules`, at the end of a pass in which any instance's
  zone could not be applied — where the call previously returned the
  count of the instances that did work, or `None`.
- That the raise comes **after** the successful mutations are applied and
  the failures are audited, so a caller that catches it still has a
  correct graph and a compliance report that grades `REVIEW_REQUIRED`.
- The measured divergence in §2.4: the same failure left a
  partially-redacted, *persisted* array on 3.14t and an untouched
  instance on 3.12, and now leaves an untouched instance on both.
- That this closes the item the #186/#205 entry already filed as "a
  second path into #213 ... filed, not fixed here".
- That `apply_redaction_to_array`'s #66 raise is unchanged, and that its
  comment's premise was half-true: the export worker's call always did
  propagate; the other two callers did not. **Amended 2026-08-31 (#217) — see §12.** Its
  *signature* is not unchanged — say so here and cross-reference the
  #217 entry (§12.6).

**#216's entry must contain, explicitly:**

- That the defect had two shapes, and the filed one is the milder:
  descriptors *absent* (undecodable, loud) and descriptors *present and
  stale* (decodable, describing a different image). With the measured
  numbers: `Rows=10 Columns=10` beside 16 floats.
- That `Rows`, `Columns`, `SamplesPerPixel` and
  `PhotometricInterpretation` are Type 1 in PS3.3 C.7.6.24 and C.7.6.25,
  and that `PhotometricInterpretation` is enumerated `MONOCHROME2`.
- That `session.export()` and `write_tree` can newly fail a float
  instance whose geometry is a guess, and that no `scripts/` generator
  produces one (verified: zero float arrays under `scripts/`).
- That a file carrying both `(7fe0,0010)` and `(7fe0,0008)` was
  reachable, that PS3.5 Section 8.2 forbids it, and that the deletion now runs in
  both directions rather than one.

---

## 12. Amendment, 2026-08-31 — #217 folds into the #213 pass

Kevin decided #217 on 2026-08-31 (option 1, comment on the issue):
`RedactionService.apply_redaction_to_array`'s `geometry` parameter
becomes **required** and the `None` arm — the `arr.shape[-1] in [3, 4]`
heuristic that put 32 of 32 identifier cells into an exported file before
#215 — is **deleted, not defaulted**. It lands in the same method §3
rewrites the error handling around, so it is one pass over `services.py`
and one PR with #213.

The clauses below are additive. **§4 and everything else about #216 is
unchanged** — that branch (`code/216-float-export-geometry`) is live and
this amendment does not touch `io_handlers.py`.

Five earlier clauses are contradicted by it and are marked in place with
`**Amended 2026-08-31 (#217) — see §12.**`: §3.9's first bullet, §6's
non-test row for third-party callers, §6's test row for the four
`apply_redaction_to_array` tests, §8's "signature, bool return, or raise"
bullet, and §11's "#66 raise is unchanged" bullet.

### 12.1 The signature

```python
@staticmethod
def apply_redaction_to_array(arr: np.ndarray, rois: List[tuple],
                             geometry: PixelGeometry) -> bool:
```

Three deletions, all in `services.py`:

1. The default. `geometry: Optional[PixelGeometry] = None` becomes
   `geometry: PixelGeometry`, positional-or-keyword, third.
2. **The annotation, not only the default.** Leaving `Optional` while
   removing the default is a self-contradicting signature and is exactly
   the "one spelling per behaviour" concern #217 invokes: it would tell a
   reader `None` is still a legal value while the interpreter says the
   argument is mandatory. `Optional` stays imported — `RedactionOutcome`
   (§3.2) uses it.
3. The docstring paragraph documenting the fallback, from "Without it
   this falls back to the last-axis heuristic" through "The fallback
   exists only for third-party callers of this public static method."
   What replaces it is one line stating the invariant the surviving
   comment at `services.py:496-503` already depends on: **the geometry
   must have been resolved from the shape of *this* array.**

And one deletion in the body — the `else` arm and the `if geometry is not
None:` that selects it:

```python
interleaved = geometry.samples > 1
```

The comment above it survives, because the invariant it states is now the
only thing standing between a borrowed geometry and `row_dim=-1`.

### 12.2 Callers — verified, and the issue's note is half right

`grep -rn "apply_redaction_to_array" --include="*.py" --include="*.md"`
returns **eleven** hits. Every one classified:

| Site | Passes `geometry` today? | Effect |
| --- | --- | --- |
| `isocenter/services.py:598` (`_apply_roi_to_instance`) | yes, keyword | No change. |
| `isocenter/io_handlers.py:882` (`_export_instance_worker`) | yes, keyword | No change. **Not edited by this PR** — #216's branch owns that file. |
| `tests/test_redaction_robustness.py:100` | **no** | Breaks. §12.3. |
| `tests/test_redaction_robustness.py:110` | **no** | Breaks. §12.3. |
| `tests/test_redaction_robustness.py:122` | **no** | Breaks. §12.3. |
| `tests/test_redaction_robustness.py:129` | **no** | Breaks. §12.3. |
| `tests/test_pixel_geometry_pipeline.py:843` | docstring prose only | No change. |
| `tests/test_float_pixel_data_export.py:364` | docstring prose only | No change. |
| `CHANGELOG.md:44`, `:60`, `:739` | prose | `:60` becomes wrong-in-the-present; §12.6 supersedes it. |

**`scripts/` is clean, and this was checked as a separate question rather
than inferred from the test sweep.** `grep -rn "redact\|RedactionService"
scripts/` returns nothing: no fixture generator constructs a redaction, so
none calls this method, directly or through the session. That is the
survey question the pixel-geometry spec got wrong twice — which *non-test
consumer* depends on the behaviour — and here the answer is genuinely
none.

**But the issue's implementation note is only half right.** It says "Both
in-tree callers already pass `geometry`, so no in-tree call changes."
That is true of the two *production* callers and **false of the test
suite**: four tests call the method with the argument omitted and all
four break with `TypeError`. This is the same survey inversion in the
other direction — the issue asked which non-test consumers depended on
the default and never asked which tests did. The coder must expect a red
suite after step 12 of §12.7 until step 13 lands.

`_apply_roi_to_instance` was checked for a default of its own, because a
deleted default that reappears one frame up is not deleted. It has none:
its signature is `(self, inst, arr, roi)`, and it resolves the geometry
itself at `services.py:596` from `arr.shape` and `inst.attributes`, after
the writeability copy. §3.9's "`_apply_roi_to_instance`. Untouched"
bullet stands.

### 12.3 The four `test_redaction_robustness.py` edits

All four are the #66 regression guards — the tests that pin "a zone that
cannot be applied raises instead of reporting nothing". **They are
edited, not rewritten and not deleted.** The edit is *adding one keyword
argument*; every assertion, every fixture, every docstring survives
verbatim:

```python
geometry=resolve_pixel_geometry(arr.shape, {})
```

with `from isocenter.pixel_geometry import resolve_pixel_geometry` added
to the file's imports.

Resolve it, do not construct the `NamedTuple` literal. Two reasons: it is
what both production callers do, so the test exercises the invariant
`services.py:496-503` states rather than asserting around it; and a
literal pins field order, which a `NamedTuple` makes silently wrong to
reorder.

Measured, so the coder does not have to guess what it returns:

```
resolve_pixel_geometry((64, 64), {})
  -> PixelGeometry(frames=1, rows=64, cols=64, samples=1,
                   evidence=GeometryEvidence.STRUCTURAL)
```

`samples == 1`, so `interleaved` is `False`, so `row_dim, col_dim = -2,
-1` — identical to what the deleted heuristic computed for a rank-2
array. Measured for each of the four:

- `:100` read-only `(64,64)` — still `ValueError: assignment destination
  is read-only`. `pytest.raises(ValueError)` passes.
- `:110` same array, same raise, `caplog` still gets `(0, 10, 0, 10)` and
  `(64, 64)`.
- `:122` empty zone list — still returns `False`, array still sums to
  `64*64`.
- `:129` `(0,10,0,10)` on a writeable array — still returns `True`,
  `arr[0:10,0:10].sum() == 0`, `arr[10:,10:].sum() == 54*54`.

Because all four outcomes are unchanged, none of them is the test that
guards #217. That test is §12.5.

### 12.4 The `TypeError` collision — caught and audited, deliberately

**The question.** After #217, `apply_redaction_to_array` can raise
`TypeError` for a missing `geometry` in addition to the `TypeError` it
already re-raises from inside its per-ROI loop. §3's design catches
broadly in `execute_redaction_task` and `redact_machine_instances` and
turns what it catches into an audited `ERROR` row. Should the
missing-argument `TypeError` be caught and reported as a failed
redaction, or propagate as the programming error it is?

**The answer: caught and audited, and the catch stays broad.** Not a
compromise — the alternative is worse, for four reasons that had to be
measured rather than reasoned about.

**1. A `TypeError` from a malformed zone is reachable from data, so
`TypeError` cannot be excluded from the audited set.** Measured, all
three through the real method with a valid geometry:

```
zone (0, None, 0, 8) -> TypeError: int() argument must be a string,
                        a bytes-like object or a real number, not 'NoneType'
zone (0, [1],  0, 8) -> TypeError: ... not 'list'
zone (0, 'abc',0, 8) -> ValueError: invalid literal for int() with base 10
```

A configuration holding a `null` or a nested list in a zone — a JSON
config is the normal way these arrive — produces a `TypeError` from
inside the loop, and that is precisely a failed redaction that #213
exists to report. Narrowing the catch to make room for the
missing-argument case would drop these on the floor and re-open the hole.

**2. The two populations are structurally distinct but not
`except`-distinguishable, and the introspection that would separate them
is worse than the ambiguity.** A missing-argument `TypeError` is raised
by the interpreter at *binding* time; the body never executes. Measured
on a minimal reproduction: the traceback of such a `TypeError` contains
**no frame for the callee**, only the caller's. So it is separable by
walking `exc.__traceback__` and checking whether
`apply_redaction_to_array` appears in it. Do **not** do this. It is a
frame-name string comparison in the middle of a PHI-safety path, it
breaks under any rename or decorator, and CLAUDE.md's "one spelling"
convention is not served by two ways of asking what went wrong. The
inner `except (ValueError, IndexError, TypeError)` inside
`apply_redaction_to_array` is not part of this collision at all — it is
inside the loop, and a binding error never reaches the body.

**3. The failure a missing argument produces is total and loud, which is
not the #213 defect class.** #213 is *silence*: one instance fails, the
count says it succeeded, the report grades `PASS`, the file ships. A
deleted `geometry=` argument fails **every** instance in the pass. Under
§3.4 that is an `ERROR` row per instance, `report.exceptions` non-empty,
`validation_status = REVIEW_REQUIRED`, and then §3.6's `RedactionError`
raised from the public entry point. Nothing is exported as clean and
nothing reports success. A caller's bug becoming a very loud audit *plus*
an exception is an acceptable presentation of a caller's bug; it is the
silent single row that would not be.

The audit row is also self-identifying, because §3.2's detail is
`f"{type(exc).__name__}: {exc}"`. It reads `TypeError:
apply_redaction_to_array() missing 1 required positional argument:
'geometry'` — which names the defect precisely enough that a reviewer
reads it as a bug in the caller, not as a compliance finding about the
data.

**4. Every public entry point that can reach the method terminates in a
raise — verified, not assumed.** This is what claim 3 rests on, so all
five were read:

| Entry point | Ends in a raise on total failure? |
| --- | --- |
| `Session.redact()` | Yes — §3.6, `RedactionError` after the pass. |
| `Session.redact_by_machine()` (`session.py:2061`) | Yes. It calls `self.redact()` inside `try/finally` with **no `except`**; the `finally` restores `configuration.rules` and the exception propagates through it. |
| `RedactionService.redact_machine_instances()` | Yes — §3.8. |
| `RedactionService.process_machine_rules()` | Yes — it calls `redact_machine_instances`; §6 already records that it newly propagates. |
| `_export_instance_worker`'s own call | Yes, and unchanged: it propagates to `ExportOutcome(ok=False)` and no file is written. |

There is no audits-and-returns path. If one is ever added, this clause is
the one that breaks, and it should be re-read before it is.

**The residue, stated rather than discovered.** #217 stops the *omission*
of `geometry`; it does not stop an explicit `geometry=None`, which
remains a legal call and dies at `geometry.samples` with an
`AttributeError` — *before* the loop, so outside the inner handler, and
caught by the broad catch as a failed redaction. That is accepted. The
annotation change in §12.1 clause 2 is the mitigation that matters, and
the important property is the direction of the failure: the deleted
heuristic failed **open** — it zeroed the wrong region and reported
success — whereas `geometry=None` after this change fails **closed** —
nothing is zeroed, the instance is not persisted (§3.7), no
`_ISOCENTER_REDACTION_HASH` is written, the row is audited, and the pass
raises. Trading a fail-open for a fail-closed is the whole of #217.

### 12.5 Test 18 — the default coming back

Added to `tests/test_redaction_robustness.py`, beside the four #66
guards. **Numbered 18** to continue §7's list; §7.4's non-vacuity list
gains a bullet for it.

```python
def test_the_geometry_argument_has_no_default():
    """#217: the default arm was the heuristic that shipped 32 of 32
    identifier cells. A default here is not a convenience -- it is the
    leak, reachable by any caller who does not know to pass a keyword."""
    arr = np.ones((64, 64), dtype=np.uint16)

    with pytest.raises(TypeError, match="geometry"):
        RedactionService.apply_redaction_to_array(arr, [(0, 10, 0, 10)])
```

Three things about it that are the point, not decoration:

- **It asserts the `TypeError`, not correct behaviour when the argument
  is passed.** The four tests in §12.3 all pass both before and after
  #217 — they assert the passing case. Per §7.4's discipline a test that
  passes on both sides of the change is worth nothing as a guard, and
  those four are guards for #66, not for #217. This is the only test in
  the suite that fails if the default is restored.
- **`match="geometry"` is required.** Bare `pytest.raises(TypeError)`
  passes on *any* `TypeError`, including one from a malformed zone
  (§12.4 clause 1, measured: two of three zone shapes produce one), so it
  would keep passing against a restored default that then raised for an
  unrelated reason. The interpreter's message is
  `apply_redaction_to_array() missing 1 required positional argument:
  'geometry'`, which contains the parameter name.
- **It calls the static method directly**, not through
  `execute_redaction_task`, so it is independent of §12.4's decision. If
  someone later narrows the catch, this test is unaffected; if someone
  restores the default, it fails regardless of what the catch does.

**Non-vacuity, to run before the fix**: with the current signature the
call succeeds and returns `True` (measured), so the test fails on `DID
NOT RAISE`. Confirm that, then make the change.

### 12.6 `CHANGELOG.md` — two entries, one PR

One PR now carries #213 and #217, and it gets **two** entries under
`### Fixed`. Not one: the audiences are disjoint. #213's reader is
someone who calls `session.redact()` and got a number back; #217's is
someone who calls a public static method directly, and is the only one of
the two who has a call that stops working.

§11's requirements for #213's entry stand, with **one correction**: its
last bullet said "`apply_redaction_to_array`'s #66 raise is unchanged".
The *raise* is still unchanged; the *signature* is not. Say so, and
cross-reference the #217 entry.

**#217's entry must contain, explicitly:**

- The exact exception a previously-working call now raises, quoted:
  `TypeError: apply_redaction_to_array() missing 1 required positional
  argument: 'geometry'`, from
  `RedactionService.apply_redaction_to_array(arr, rois)` — a call that
  worked in 0.9.0 and silently used the heuristic.
- **Why the old behaviour was wrong**, in the terms CLAUDE.md asks for:
  the default arm was `arr.shape[-1] in [3, 4]`, which cannot tell
  `(frames, rows, cols)` from `(rows, cols, samples)`; on a 4-frame 8×4
  volume with an identifier at rows 6–7 of every frame, zone `(6,8,0,4)`
  addressed frames `6:8` of a 4-frame array — an empty slice — so all 32
  identifier cells reached the exported file while `redact()` reported
  "1 of 1 images updated". Reproduced during #215's review: 32/32
  surviving on `692218c`, 0/32 on `cf9dcd6`.
- That this **supersedes** the sentence in 0.9.0's `#186, #205` entry
  reading "A caller of the public static
  `RedactionService.apply_redaction_to_array` outside this tree keeps the
  old behaviour" (`CHANGELOG.md:60`). That promise is withdrawn on
  purpose, one release later, under the pre-1.0 convention — and the
  entry should say that rather than leaving two entries disagreeing.
- The migration, in one line: resolve it —
  `from isocenter.pixel_geometry import resolve_pixel_geometry;
  resolve_pixel_geometry(arr.shape, instance.attributes)` — which is what
  both in-tree callers do.
- That there was no way for a third-party caller to get the *correct*
  behaviour from the default, so nothing usable is being taken away.

### 12.7 Implementation order — §9's PR 2 becomes PR 2 + #217

§9 steps 6–11 are unchanged and still run first: #213's machinery lands
before the signature changes, so a failure in either is attributable.
Three steps are appended.

12. Make `geometry` required in `services.py` (§12.1): default deleted,
    annotation narrowed, docstring paragraph deleted, `else` arm and its
    `if` deleted. **The suite goes red here** — four tests in
    `test_redaction_robustness.py`. That is expected and is the survey's
    finding, not a mistake.
13. Add `geometry=resolve_pixel_geometry(arr.shape, {})` to those four
    calls and the import (§12.3). Suite green again, all four assertions
    untouched.
14. Test 18 (§12.5), then the second `CHANGELOG.md` entry (§12.6).

Steps 12 and 13 are one commit, not two: a deliberately red intermediate
commit in `main`'s history is worse than a slightly larger one, and a
`git bisect` that lands on a known-red commit is the cost.

Full suite on **3.12 and 3.14t**. Nothing in #217 is
interpreter-dependent — the signature change is bytecode-level and the
four edited tests never enter `run_parallel` — but §2.4's divergence is
in the same PR and test 14 still needs both gate versions, so the matrix
requirement is unchanged from §9.

### 12.8 What #217 does *not* change in the earlier design

- **§3.1 through §3.8 stand as written.** The outcome shape, the parent
  audit, `ERROR` over `DATA_LOSS`, the end-of-pass raise, the
  not-persisting of a failed instance, and the serial path all read the
  same with a required `geometry` as with a defaulted one.
- **§2.4's interpreter divergence is untouched and still needs pinning.**
  The measurement stands: with zones `[[0,8,0,8],[1,"abc",0,8]]` on a
  saved instance, the threads path (3.14t default) leaves zone 1 zeroed
  *and persisted through save/close/reopen* while the processes path
  (3.12 default) leaves the instance untouched. Test 14 pins it, and it
  must run on both gate versions — a local 3.14.6 pass exercises the arm
  that already behaves, so a green local run is not evidence for the 3.12
  arm. The parent-monkeypatch-invisible-in-the-child rule applies
  unchanged.
- **§4, all of #216.** Different file, different branch, different PR.

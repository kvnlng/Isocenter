# The redaction attestation: what it means and when it is earned (#235, #237)

Design spec. Base: `84113ab` on `main`, which already carries #228
(`7b1fe13`), #223 (`234b3d1`) and #226 (`84113ab`).

`docs/superpowers/specs/2026-08-31-redaction-loses-work.md` §9.1 deferred
#235 with an explicit reason: the obvious fix adds a **second** gate on
the same question as #228's `new_uid != original_sop_uid`, and two gates
that can disagree about whether an instance was redacted is how #228
happened. #228 has landed. This spec decides the one gate.

Everything labelled *measured* below was run during this spec. Every
mutation cited was executed, including the design itself: B0, B1 and B9
were applied to a throwaway prototype tree and the full suite run against
it on both gate interpreters (§C.2). Anything not run is labelled
**UNMEASURED**, **REASONED** or **read, not measured** -- there are
exactly four such statements (§B3's already-zero zone, §B4's DICOM
element type, §B5.2's pixel-hash reasoning, §C.4's `force=` signature)
and none of them is load-bearing for a decision.

---

## 0. Environment for every measurement in this document

| | |
| --- | --- |
| Tree | `84113ab`, worktree `.claude/worktrees/agent-a9af04c0fa14db288` |
| Interpreters | `3.12.13` (pyenv), `3.14.7t` (pyenv, `sys._is_gil_enabled()` asserted `False` **in-process**, printed by each script) |
| Isolation | `PYTHONPATH` pinned to the worktree; every script prints `isocenter.__file__` and it is asserted to be the worktree's `isocenter/__init__.py` |
| numpy / pydicom / pytest | `2.5.2` / `3.0.2` / `9.1.1`, identical on both venvs |
| Housekeeping | `.DS_Store` swept from the worktree before any run (#234) |
| Old-version replay | `git archive v0.9.0 | tar -x` into a scratch tree, `PYTHONPATH` pointed at it, `isocenter.__version__` printed and asserted `0.9.0` |
| Prototype | `git archive HEAD | tar -x` into a second scratch tree, B0/B1/B9 applied there by a script with asserted string anchors. **No production file in this worktree was modified**; the prototype exists only to measure §C.2 and is not the implementation |

Every reproduction script uses an `if __name__ == "__main__":` guard.
Without one the first `redact()` dies with `BrokenProcessPool` on macOS
spawn, which reads as a defect and is not one.

---

## 1. The three artifacts that disagree, measured on `84113ab`

One 32x32 `uint8` `MONOCHROME2` SC instance filled with `200`, built,
`save()`, `close()`, reopened -- so `get_pixel_data()` arrives through
`SidecarPixelLoader` and is **not writeable** (`writeable: False`
printed). One rule, one zone `[100, 108, 100, 108]`, entirely past the
edge. `redact(show_progress=False)`.

**Identical on 3.12.13 and 3.14.7t:**

```
tasks: 1
APPLIED: 1
uid unchanged: True
HASH present: True  value: None
0028,0301  present: True  value: None
0008,0008  present: True  value: None
0008,2111  present: True  value: None
0008,9215 seq present: False
pixel sum: 204800          <- 32*32*200, nothing was redacted
```

§9.1 recorded this as "**no** `_ISOCENTER_REDACTION_HASH` is written".
That is one word off and the word matters: the key **is created**, with
the value `None`. The skip check is `current_hash == config_hash`, and
`None != <hex>`, so the instance is still retried on the next run -- the
null hash is noise rather than a second seal. Corrected here.

### 1.1 The nulls reach the store and the exported file

Same fixture, then `save()` and `export()`:

```
STORE instances.attributes_json:
  ..., "0008,0008": null, "0028,0301": null, "0008,2111": null,
  "_ISOCENTER_REDACTION_HASH": null

exported 1.2.3.offedge.bare.dcm:
  BurnedInAnnotation     present=True value='' VR=CS
  ImageType              present=True value='' VR=CS
  DerivationDescription  present=True value='' VR=ST
```

So the end state is not an in-memory oddity. `(0028,0301)` reaches a
DICOM file as a **zero-length CS element**. Its enumerated values are
`YES` and `NO` (PS3.3 C.7.6.1.1.6); zero length is neither, and the
question it answers -- are identifiers burned into these pixels -- is one
where "absent" and "present but unanswered" are not the same claim to a
compliance reader.

### 1.2 A hypothesis this spec had, and falsified

The obvious worry is that the null write **downgrades a positive flag**:
a source declaring `(0028,0301) = YES` losing it to `None`, after which
`scan_burned_in_annotations` -- whose read is
`inst.attributes.get("0028,0301", "NO")` behind an `isinstance(val, str)`
guard -- silently stops counting it. Measured, and it does **not**
happen:

```
BEFORE 0028,0301: 'YES'   0008,0008: ['ORIGINAL', 'PRIMARY']
APPLIED: 1
AFTER  0028,0301: 'YES'   0008,0008: ['ORIGINAL', 'PRIMARY']
RISK rows: 2 (one before redact(), one after)
exported BurnedInAnnotation: 'YES'
```

The mutation dict carries `inst.attributes.get("0028,0301")` read from
the **worker's** instance, which still holds whatever the source had. The
`None` appears only where the attribute was **absent**. The defect is
therefore *creation of a null element*, not *destruction of a value*,
and §4 below is written to that narrower claim. Recording the falsified
hypothesis because the wider claim is the one a reader would assume.

### 1.3 A sidecar orphan, not previously recorded

Same fixture, `save()` / `close()` / reopen / off-image `redact()` /
`save()` / `close()`, reading `instance_blobs` and the sidecar size after
the close. **Both levers, 3.12.13:**

| | sidecar after first `save()` | after off-image `redact()` + `save()` | `instance_blobs` |
| --- | --- | --- | --- |
| `ISOCENTER_FORCE_THREADS` | 17 | **34** | one row, `(uid, 'pixels', offset 0, length 17)` |
| `ISOCENTER_FORCE_PROCESSES` | 17 | **34** | one row, `(uid, 'pixels', offset 0, length 17)` |
| control: no `redact()`, `mark_modified()` + `save()` | 17 | 17 | same one row |

17 bytes are appended that nothing references. `execute_redaction_task`'s
`finally` calls `store_backend.persist_pixel_data(inst)` whenever the
task did not fail, and `persist_pixel_data` (`persistence.py:1532`) has
**no deduplication** -- it hashes, writes a frame, and re-points the
loader, every time it is called with a resident array. On the off-image
path there is nothing to persist and it writes a copy anyway.

Measured for the in-bounds case too, for scope: zone `[0, 8, 0, 8]`,
processes lever, sidecar `17 -> 63`, blob row `(new_uid, 'pixels', 17,
23)`. That is 46 bytes appended, 23 referenced -- **the in-bounds path
writes an orphan as well**, because `execute_redaction_task` persists
twice: once inside `if modified:` (which is the call that supplies
`mutation["pixel_loader"]`, and so cannot move) and again in the
`finally`. §B9 fixes the off-image half, which is this issue's; §D files
the in-bounds half, which is not.

---

## 2. #237, replayed against the released `v0.9.0` rather than against `4507d48`

#237's measurement is against `4507d48`, an unreleased commit. The
question that decides how much repair machinery is proportionate is
whether the damaged population can exist outside this repository, so the
replay here is against the **released** version.

`git show v0.9.0:isocenter/services.py | grep -c "for roi in rois"` ->
`3`. The same count on `v0.8.2` and `v0.7.0`. The per-zone loop that #229
removed -- each zone taking a fresh copy of the pristine original when
the array is read-only, so only the last applicable zone survives --
**shipped in every release up to and including `0.9.0`**. The damaged
population is real: anyone who ran `0.9.0` or earlier with a **multi-zone**
rule against a **saved-and-reopened** store.

Two phases, one database carried between them, same rule
`[[0,8,0,8],[16,24,16,24]]` on a 32x32 fill-200 image:

**Phase 1, `PYTHONPATH` -> extracted `v0.9.0` (`__version__` printed `0.9.0`):**

| interpreter | result |
| --- | --- |
| 3.12.13 | `applied 1`, `zone1 12800`, `zone2 0`, `total 192000`, `HASH d106238cc57d1b0931aef639e5367dc9`, `0028,0301 'NO'`, uid **unchanged** (`1.2.3.multizone`) |
| 3.14.7t | `applied 1`, `zone1 12800`, `zone2 0`, `total 192000`, same hash, `0028,0301 'NO'`, uid **regenerated** (the #228 divergence) |

**Phase 2, same db, `PYTHONPATH` -> `84113ab`:**

| interpreter | result |
| --- | --- |
| 3.12.13 | hash carried `d106238c...`, `0028,0301 'NO'`, **`redact()` returns 0**, `zone1` still `12800`, `total` still `192000` |
| 3.14.7t | identical |

The fixed code declines to look. The burned-in identifier stays in the
pixels under an attestation and a `BurnedInAnnotation = NO` that both say
it was removed. #237 is reproduced on both gate interpreters, from a
released version, on current `main`.

### 2.1 The `sorted(rois)` collision #237 names is **stale**, and this changes the design

#237's second argument is that the hash is computed over `sorted(rois)`
while zones are applied in config order, citing
`[[0,8,0,8],[100,200,100,200]] -> total 204800` against the reverse
order `-> 192000` under one hash. Re-measured both orderings on both
trees:

| tree | `[[0,8,0,8],[100,200,100,200]]` | `[[100,200,100,200],[0,8,0,8]]` | same pixels? | same hash? |
| --- | --- | --- | --- | --- |
| `v0.9.0` | total **204800** | total **192000** | **no** | yes (`fa0a9591...`) |
| `84113ab` | total **192000** | total **192000** | **yes** | yes (`fa0a9591...`) |
| `84113ab`, overlapping `[[0,8,0,8],[4,12,4,12]]` vs reverse | total **182400** | total **182400** | **yes** | yes (`661d82ad...`) |

The divergence #237 measured was the #229 bug, not the sort. Redaction
zeroes, and zeroing is commutative and idempotent, so on `main` the
applied pixels cannot depend on zone order -- which makes `sorted()`
**correct** rather than a collision. §B7 keeps it, and records why
changing it would be worse than leaving it.

---

## 3. What the four signals mean today

| signal | written where | gated on |
| --- | --- | --- |
| `applied` (the `redact()` return) | `session.py:2160`, `_apply_redaction_outcomes` | a mutation came back and its instance was found |
| `_ISOCENTER_REDACTION_HASH` | `services.py:368` (parallel), `:636` (serial) | `if modified:` |
| `(0028,0301)`, `(0008,0008)`, `(0008,2111)`, `(0008,9215)` | `_apply_redaction_flags`, `services.py:838` | `if modified:` |
| the new SOP Instance UID | `session.py:2138`, `_apply_redaction_outcomes` | `new_uid != sop` |
| the *copy* of all of the above onto the parent | `session.py:2122-2135` | a mutation came back |

`modified` is `apply_redaction_to_array`'s return: **true when at least
one configured zone was applied to this array**. The four `if modified:`
gates agree with each other because they are the same `if`. The two that
do not agree with them are the last two rows, and both are in the parent,
and both exist only because `execute_redaction_task` builds its mutation
dict **outside** `if modified:` (`services.py:380-403`).

**`redact_machine_instances` -- the serial path -- has no such
divergence.** It builds no mutation; when `modified` is false it simply
does nothing, and #235 does not reproduce through it. Confirmed by
`tests/test_redaction_multizone.py::test_a_rule_that_applied_nothing_returns_False_and_attests_nothing`,
which drives the serial path with an all-off-image rule and is green on
`main`.

That reframes the whole fix, and it is the argument this spec leads
with: **the parallel path is not missing a gate, it has a divergence from
the serial path.** The repair is to delete the divergence, not to invent
a second gate.

---

## B. The change

### B0. The single gate

> **The gate is `modified`: at least one configured zone was applied to
> this instance's pixel array. It is evaluated once per execution path,
> at the point the redaction outcome is decided, and nothing downstream
> re-derives it.**

Concretely, in `execute_redaction_task` the construction of the mutation
dict moves **inside** `if modified:`. When the gate is false the function
returns `RedactionOutcome(ok=True, sop_instance_uid=original_uid)` --
`mutation=None`, the shape it already returns for the two other
legitimate skips ("already redacted under this configuration", "no pixel
data"). "No zone landed inside the image" is a legitimate skip of exactly
that kind and now says so in the same vocabulary.

**`redact_machine_instances` is not touched by the gate change** -- it is
already this shape. (B5.4 does add a `force` keyword to its signature;
that is the only edit it takes, and §C.4 surveys the one test that
mocks it.) That is the point: after this change the two paths answer the
question the same way, and the parallel one is the one that moved.

**Reachability, because it bears on how much B0 actually repairs.**
`session.redact()` does **not** call `redact_machine_instances`;
`_apply_redaction_rules` dispatches `execute_redaction_task` through
`run_parallel`. The serial method is reached only from
`process_machine_rules` (`services.py:530`) and from tests calling it
directly (`grep -rn "process_machine_rules\|redact_machine_instances"
isocenter/ tests/`). So the two are parallel *public* APIs for one
behaviour, not two halves of one call path -- which is why they could
drift for this long, and why "one spelling per behaviour" is the right
frame for the fix.

### B1. What the parent then does, and what of #228 this supersedes

`_apply_redaction_outcomes` already has the branch that absorbs the new
case:

```python
mutation = outcome.mutation
if not mutation:
    # A legitimate skip ...
    continue
```

so an all-off-image instance takes it, is not counted, and has nothing
written onto it. The comment on that branch gains "no zone landed inside
the image" as a third reason.

`session.py:2137-2138` becomes:

```python
new_uid = mutation.get('sop_uid')
if new_uid:
```

**`and new_uid != sop` is deleted.** After B0 a mutation exists only for
an instance the worker regenerated, so the inequality is provably always
true where it is evaluated -- a condition that reads as a gate and
decides nothing. Keeping it would be the second answer to one question
that #235 was deferred to avoid; the remaining `if new_uid:` is a
`None`-safety guard on a `dict.get`, not a semantic gate.

**#228 clauses this supersedes, named exactly** -- all *mechanism*, no
*property*:

1. `services.py:383-390`, the comment on `"sop_uid"` reading "*that
   inequality is the parent's gate*". Rewritten: the parent's gate is
   the existence of the mutation, and this key is the new identity.
2. `session.py:2139-2146`, the comment reading "*The gate is `sop_uid !=
   original_sop_uid`, not the presence of a mutation ... Do not widen
   this to `if new_uid:` or `if mutation:`*", and the same sentence in
   `_apply_redaction_outcomes`'s docstring (`session.py:2052-2056`).
   Rewritten: the mutation dict is no longer built unconditionally, so
   its presence **is** the honest signal; the instruction inverts.
3. The CHANGELOG paragraph beginning "**The identity is applied in the
   parent, gated on the UID having actually changed.**" It must be
   amended in the same release, not left standing, because it states a
   mechanism this change removes. CLAUDE.md's rule is that the CHANGELOG
   carries the real reasoning; a superseded mechanism left in it is the
   documentation drift the project already paid for once.
4. `tests/test_redaction_identity.py::test_an_instance_nothing_was_applied_to_keeps_its_identity`'s
   **docstring**, which asserts the gate is UID inequality and that the
   test "goes red the moment that condition is written `if new_uid:` or
   `if mutation:`". After B0 that is false -- see B2.

What #228 **established and this preserves unchanged**: a redacted
instance takes a new SOP Instance UID on every interpreter; an instance
nothing was applied to keeps its identity, its `0008,0018` and its
`file_path`; the identity is assigned in the parent; one `instances` row
and one `instance_blobs` row after a successful redaction. Every #228
test stays green.

### B2. Where the selectivity moves, stated because it does move

`test_an_instance_nothing_was_applied_to_keeps_its_identity` stays green
and stays worth having, but it stops being the guard on the gate. Under
B0, re-widening the mutation construction back outside `if modified:`
would give an off-image instance a mutation whose `sop_uid` equals its
`original_sop_uid`; `if new_uid:` then assigns the instance the identity
it already has, and that test stays **green**. Its docstring must say so
and must name what does go red: T1 (`applied == 0`) and T2 (no attributes
created). The selectivity is not lost, it is carried by the new tests --
which is a strictly better place for it, since it is now detected by the
two artifacts #235 is actually about.

### B3. What `applied` counts

> **`applied` counts instances whose pixels a rule actually changed --
> the gate. Not instances a rule matched.**

`session.redact()` returns an `int` and it is public. This is a
**behaviour change to a public return value**, pre-1.0, deleted-not-
deprecated per CLAUDE.md: there is no second count and no
`applied_including_skipped`. For a configuration every one of whose zones
lands, nothing changes. For a rule that matched an instance and applied
nothing to it, the return drops by one per such instance, and the
console line `"Redaction complete: N of M images updated"` drops with it
-- which is the sentence #235 says is wrong, so it is the sentence that
had to move.

`_apply_redaction_rules`'s shortfall warning currently enumerates three
reasons: "already redacted under this configuration, pixel data that
would not load, or a worker that failed". It gains a fourth: **"or no
configured zone landed inside the image"**. Same clause, same commit.

`redact_by_machine` discards `redact()`'s return, so it is unaffected
(`session.py:2185`).

**The honest boundary of this gate, measured.** The gate is "a zone was
*applied*", not "the bytes changed", and those differ in two reachable
shapes:

- A zone that is in bounds but selects zero pixels. Measured on
  `84113ab` with `[[0, 0, 20, 20]]` on a 32x32 image: `APPLIED: 1`, and
  a **full attestation** -- `0028,0301 'NO'`, `0008,0008 ['DERIVED',
  'SECONDARY']`, `0008,2111` set, a real hash -- with no pixel touched.
  This is **unchanged** by this spec.
- A zone whose pixels are already zero. Same reasoning; **UNMEASURED**,
  and asserted only as following from `apply_redaction_to_array`'s
  source, which sets `modified = True` after the assignment without
  comparing.

The alternative -- redefining `modified` as "the bytes changed" -- was
considered and is **rejected**. It would kill the `current_hash ==
config_hash` skip for every already-redacted instance, since a correct
re-run changes nothing and so would never re-earn the attestation; on a
100GB store that turns every later `redact()` into a full pixel read.
It would also require changing `apply_redaction_to_array`'s contract,
which is the most heavily commented function in `services.py` and whose
bool return is load-bearing for #66, #186, #205 and #217. The zero-area
zone stays a known, documented, non-repaired case, and
`tests/test_redact_reports_outcome.py`'s fixture uses exactly such a zone
(`[[0, 0, 20, 20]]`, `session` fixture, `:44`), which is why
`test_redact_returns_how_many_instances_it_changed` asserting `== 3`
stays green -- see §C.

### B4. `(0028,0301)`: `YES`, `NO`, or absent -- never null

> **`(0028,0301)` is written by exactly one place, `_apply_redaction_flags`,
> inside the gate, with the value `"NO"`. When the gate is false the
> element is left exactly as it was found: absent stays absent, `"YES"`
> stays `"YES"`.**

Nothing new is written to achieve this; it falls out of B0, because the
only writer of a null value is
`instance.attributes.update(mutation['attributes'])` in the parent, and
after B0 there is no mutation to update from. The same sentence covers
`(0008,0008)`, `(0008,2111)`, `(0008,9215)` and
`_ISOCENTER_REDACTION_HASH`: five signals, one gate, no nulls.

**When each value is correct.** `"NO"` is earned when zones were applied
-- it is the library's claim about what it just did. Absent is correct
when the gate is false and the source carried nothing: whatever the
element's type is in the enclosing IOD, that is the state the source
file was in, and a redaction that did nothing has no basis to change it.
Inventing a value would be Isocenter answering a question about pixels it
did not touch. (#235 calls it Type 1C; it is Type 3 in the General Image
Module and 1C in others. The argument here does not rest on which,
because "leave it as found" is conformant under both -- **UNMEASURED**,
and not load-bearing.) `"YES"` surviving untouched when the gate is false is the
safety-relevant half: it is the scanner's own claim that identifiers are
drawn into the pixels, nothing was removed, and it must keep reaching
`scan_burned_in_annotations`. Measured green on `main` already (§1.2) --
so the test for it is a **selectivity guard**, not evidence of a fix.

**Survey of everything that touches the tag**
(`grep -rn "0028,0301" isocenter tests`):

| site | direction | effect of this change |
| --- | --- | --- |
| `services.py:860`, `_apply_redaction_flags` | writes `"NO"` | unchanged; still the only writer |
| `services.py:396`, the mutation dict | copies whatever the worker holds | now only built inside the gate |
| `services.py:203`, `scan_burned_in_annotations` | reads, `.get(tag, "NO")` behind `isinstance(val, str)` | unchanged. The `isinstance` guard is why today's `None` never crashed anything -- it reads as "NO", silently |
| `session.py:1074`, `_burned_in_warning` | reads, same shape | unchanged |
| `persistence.py:832` | SQL `LIKE '%"0028,0301": "YES"%'` over `attributes_json` | unchanged; a `null` never matched it either |
| `session.scan_pixel_content` | **does not touch it** | `grep -n "0028,0301"` over `session.py:1239-1315` returns nothing. It filters by rule coverage and equipment serial, and its findings are OCR regions. Named because the brief asks; it is not a consumer |
| `tests/test_services.py:49`, `test_create_config_output.py:44`, `test_scaffold_features.py:189`, `test_redaction_robustness.py:58`, `test_redact_error.py:80,85`, `test_reporting.py:119` | set or assert `"YES"`/`"NO"` | none uses an all-off-image rule; all unchanged. See §C |

### B5. #237: not fixed. Made repairable, and documented.

**Stated plainly, in the words the brief asks for: this design does not
repair a damaged store. A user who never calls `redact(force=True)`
keeps the damaged store, and keeps it silently.** What changes is that
the repair exists in the API instead of requiring the user to know an
internal private-tag key name.

**B5.1 -- Rejected: an epoch or version stamp on the attestation.**
Making `_ISOCENTER_REDACTION_HASH` carry a format version, so `0.9.1`
declines to honour any attestation written by a predecessor it knows was
wrong, is the option that would actually repair #237 automatically, and
it is #237's own open call 2. It is rejected on a constraint that landed
three commits ago. The #228 CHANGELOG entry, merged at `7b1fe13`, states
verbatim:

> re-running `redact()` with the same rules is a no-op and renames
> nothing -- already-redacted instances keep the identity the old run
> gave them.

An epoch bump falsifies that sentence for every store in existence.
Because `modified` is true whenever a zone is in bounds (B3), a healthy
already-redacted instance re-redacts on the next call: `regenerate_uid()`
fires, the SOP Instance UID changes, the exported filename changes (#78),
and `file_path` is cleared -- which is #238's mechanism, reaching a
population that had no defect. For a beneficiary population of "ran
`<= 0.9.0`, **and** used a multi-zone rule, **and** against a
saved-and-reopened store", that is disproportionate, and it would mean
amending a shipped promise in the release that made it.

**B5.2 -- Rejected: folding a pixel-derived component into the hash.**
#237's open call 1. The attestation would become
`{config, pixel_digest_at_attestation}`. It does not repair #237: the
damaged instance's pixels are exactly the pixels the damaged run left
behind, so the recorded digest still matches what is in the store and the
skip still fires. It buys a different, real property -- an attestation
invalidated when something *else* changes the pixels -- which is not this
issue and costs a hash of every array on the skip path. **REASONED, not
measured**; the reasoning is that the digest is written by the same run
that wrote the wrong pixels, so it cannot disagree with them.

**B5.3 -- Rejected: changing `sorted(rois)` to config order.** §2.1
measured that the collision is gone on `main`. Worse, changing the hash
*input* is a **silent partial epoch bump**: every store whose rule's
config order differs from its sorted order would re-redact and rename on
the next call, and every other store would not -- B5.1's cost, delivered
to an arbitrary subset, with no entry explaining it. `sorted()` stays,
and gains a comment saying it is correct because zeroing is commutative,
with the measurement.

**B5.4 -- Accepted: `force: bool = False` on `Session.redact()`.**
Threaded to both call sites and suppressing **only** the
`current_hash == config_hash` early return -- nothing else. Six lines and
one parameter.

```python
def redact(self, show_progress=True, force=False):
```

carried into `prepare_redaction_tasks`' task dict as `task["force"]` for
the parallel path and as a keyword on `redact_machine_instances` for the
serial one, so the two paths stay symmetrical.

This is a **new parameter, not an alias**. CLAUDE.md's rule deletes
duplicate spellings of an existing parameter; it does not forbid new
surface, and the surface here is the difference between "repairable" and
"not repairable". **The repair is measured, not assumed.** Deleting
`_ISOCENTER_REDACTION_HASH` from the instance's `attributes` is exactly
what `force=True` does -- it suppresses the same early return and nothing
else -- so the premise is measurable on unmodified `84113ab`. Run against
the two `v0.9.0`-damaged databases from §2, one per interpreter:

| | before | `redact()` after clearing the attestation |
| --- | --- | --- |
| 3.12.13 | `zone1 12800`, `total 192000` | `applied 1`, `zone1 **0**`, `zone2 0`, `total 179200`, new UID `1.2.826...386847`, hash rewritten |
| 3.14.7t | `zone1 12800`, `total 192000` | `applied 1`, `zone1 **0**`, `zone2 0`, `total 179200`, new UID `1.2.826...777780`, hash rewritten |

It works because the damaged instance's PHI is still *in* the store's
pixels, so re-applying the rule zeroes it from the store's own bytes with
no source file required. The new SOP Instance UID in both rows is the
documented cost, arriving exactly where the paragraph below says it does.

`force=True`'s cost is documented **on the parameter**, which is the
right place for it, since the user is choosing it: every instance the
rule matches is re-redacted, and every one of them takes a **new SOP
Instance UID**, a new exported filename, and `file_path = None`. That is
B5.1's cost made opt-in and explained, rather than imposed and unexplained.

`redact_by_machine` does **not** get `force`. It is an interactive
one-ROI helper that discards the count; a caller wanting the repair sets
rules and calls `redact(force=True)`. Minimal surface pre-1.0.

**B5.5 -- Accepted: the remediation is documented where a damaged user
looks.** A CHANGELOG entry under the #235/#237 heading naming the exact
population ("redacted with a rule carrying two or more zones, against a
store that had been saved and reopened, on `0.9.0` or earlier"), the
symptom ("`redact()` returns 0 and the pixels do not change"), and the
one-line repair (`session.redact(force=True)` followed by `save()`), plus
its cost. `docs/quickstart.md`'s redaction section gains a sentence
pointing at it. This is the half that reaches a user who does not read
issue trackers.

**B5.6 -- What remains unfixed, named.** The general shape #237
describes survives: `_ISOCENTER_REDACTION_HASH` is still an attestation
independent of what it attests to, so any *future* redaction defect is
equally self-sealing -- the first run writes the hash and every later run
declines to look. `force=` gives a lever; it does not give detection.
That is the version-stamped attestation of B5.1, which is the same
migration lever #172 and #168 want, and it should be decided for all
three together rather than invented here for one. **File it**, referencing
this section, and close #237 as "repairable + documented" only if the
maintainer agrees that framing; otherwise it stays open on B5.1.

### B6. #238, and whether this contradicts its reproduction

It does not, and the check is measured. #238 reproduces with an
**in-bounds** zone: the instance is redacted, `regenerate_uid()` fires,
`file_path` is cleared, and the source file stops being in
`get_known_files()`. This spec changes nothing on the in-bounds path --
the gate is already `if modified:` there. On the **off-image** path
`file_path` is already preserved today, measured in §1 (`uid unchanged:
True`) and pinned by
`test_an_instance_nothing_was_applied_to_keeps_its_identity`'s
`assert inst.file_path == str(stale)`. So this fix neither widens nor
narrows #238's reproduction.

The one interaction to name: **`force=True` widens #238's exposure**, by
clearing `file_path` on instances that had already been redacted once.
B5.4's parameter docstring says so.

### B7. `sorted(rois)` stays, with the reason written down

Per §2.1. The comment at `services.py:584-586` currently reads "Sort to
ensure stability if zones are re-ordered", which is the *what*. It gains
the *why it is safe*: redaction zeroes, zeroing is commutative and
idempotent, so two orderings of one zone list cannot produce different
pixels -- measured on `84113ab`, both a disjoint pair and an overlapping
pair, identical totals both ways. And a warning: changing this input
silently re-redacts and renames the subset of stores whose config order
differs from sorted order, which is B5.1's cost delivered to an arbitrary
population.

### B8. Docstrings that state the count

`Session.redact()`'s `Returns:` currently reads "How many instances were
updated in memory. Zero means nothing was redacted -- no rules loaded, or
no image matched one." It becomes "How many instances had at least one
configured zone applied to their pixels", with the two boundaries B3
names: an instance a rule matched but whose every zone fell outside the
image is **not** counted, and a zone that is in bounds but selects zero
pixels **is**. `execute_redaction_task`'s `Returns:` gains the third
skip reason. `docs.yml` redeploys on any `isocenter/**.py` push, so the
generated API reference follows; no new headings and no new fragment
links, so `tests/test_doc_anchors.py` is unaffected.

### B9. The off-image sidecar orphan

`execute_redaction_task`'s `finally` persist gains the gate:

```python
if (modified and not failed and self.store_backend
        and hasattr(self.store_backend, 'persist_pixel_data')):
```

with `modified` initialised `False` beside `failed` so the early returns
and the exception path see a bound name. This removes the 17 unreferenced
bytes measured in §1.3 on the off-image path, and is the same gate --
which is the whole argument for including it here rather than filing it:
persisting a swap that did not happen is the same "attested without being
earned" defect in the sidecar instead of in the attributes.

`unload_pixel_data()` stays unconditional. #213's argument for the
`not failed` half is untouched and its comment stays; this narrows the
condition, it does not replace it.

The in-bounds double-persist measured in §1.3 (46 bytes appended, 23
referenced) is **not** fixed here -- see §D.

---

## C. Compatibility survey

Commands run, verbatim, from the worktree root:

```
grep -rn "_ISOCENTER_REDACTION_HASH" isocenter tests scripts docs
grep -rn "0028,0301" isocenter tests
grep -rn "\.redact(" isocenter/ docs/ scripts/ README.md GettingStarted.ipynb
grep -rn "redact(" tests/ | grep -E "==|assert|applied|result|count"
grep -rn "_apply_redaction_outcomes\|execute_redaction_task\|redact_machine_instances" isocenter tests
grep -rn "_pixel_hash" isocenter/
grep -rn "scan_pixel_content" isocenter/ tests/
grep -n "instance_blobs" -A 12 isocenter/persistence.py
```

### C.1 Non-test consumers

| site | reads | effect |
| --- | --- | --- |
| `session.py:2185`, `redact_by_machine` | calls `self.redact()`, **discards** the return | none |
| `session.py:2016-2021`, the shortfall warning | `applied < len(tasks)` | still fires; string gains a fourth reason (B3) |
| `session.py:2025`, the console line | `applied` | number drops for an all-off-image rule; that is the fix |
| `io_handlers.py:1363-1364` | `if "_ISOCENTER_REDACTION_HASH" in ds: del ds[...]` | unchanged, and **it is dead code** -- see §D.6. The hash does not reach an exported file either way, because a key that is not a `gggg,eeee` tag never becomes an element: the degenerate-zone export above, whose graph carried a real hash, wrote exactly `(0008,0008) (0008,0016) (0008,0018) (0008,0020) (0008,0030) (0008,0060) (0008,2111) (0008,9215) (0010,0010) (0010,0020) (0020,000D) (0020,000E) (0020,0011) (0020,0013) (0028,0002) (0028,0004) (0028,0010) (0028,0011) (0028,0100) (0028,0101) (0028,0102) (0028,0103) (0028,0301) (7FE0,0010)` -- measured. That is why the null hash never surfaced in a file while the other three nulls did |
| `persistence.py:832` | the `"0028,0301": "YES"` SQL `LIKE` | unchanged |
| `docs/quickstart.md:74`, `README.md`, `GettingStarted.ipynb` | `session.redact()` with the return unused | unchanged; quickstart gains B5.5's sentence |
| `tests/benchmarks/run_stress_test.py:126` | `sess.redact(show_progress=False)` | generated zones are in-bounds; count unchanged |
| `scripts/` | `grep -rn "\.redact(" scripts/` -> **no hits** | none |

### C.2 Tests that assert a count

Every `redact()` count assertion in the suite, and whether its zones land:

| test | zones | verdict |
| --- | --- | --- |
| `test_redact_reports_outcome.py:58` `== 3` | `[[0, 0, 20, 20]]` -- in bounds, **selects zero pixels** | **green**, by B3's documented boundary. This is the single most important row in this table: the gate is "applied", not "changed", and this test is why |
| `test_redact_reports_outcome.py:86` `== 0` | no rules | green |
| `test_redact_reports_outcome.py:156` `redacted == 1` **plus `any("1 of 3" in record.message ...)`** | same fixture; stubs `run_parallel` serial and returns `mutation=None` for tasks 2-3 | green -- **and it is the one test that reads the shortfall warning's text**. B3 appends a fourth reason to that string; the `"Redaction updated {applied} of {len(tasks)} targeted images."` prefix must not move, or this goes red for the wrong reason. Note also that this test already exercises the `mutation=None` path B0 routes the off-image case through |
| `test_redact_reports_outcome.py:174` `== 3` | as above | green |
| `test_redaction_failure_is_reported.py:234,338` `== 3` | in-bounds | green |
| `test_redaction_failure_is_reported.py:339,355` `== 0` | second call, hash matches | green |
| `test_redaction_failure_is_reported.py:551` `== 1` | in-bounds | green |
| `test_redaction_multizone.py:136` `== 1` | in-bounds | green |
| `test_redaction_multizone.py:267` `== 1` | `[[0,8,0,8],[100,200,100,200]]` **and** the reverse -- one zone lands, one does not | **green**, and it is the existing partial guard on the gate's polarity being "any", not "all". T4 below makes that explicit and asserts the attestation, which this one does not |
| `test_redaction_multizone.py:59` | assigns `updated`, does not assert on it | green |

**No count assertion in the suite regresses, and this is measured, not
reasoned.** A throwaway prototype was built to answer it -- `git archive
HEAD | tar -x` into scratch, then B0, B1 and B9 applied there by a script
with asserted anchors. No production file in the worktree was modified;
the prototype is not part of this branch and is not the implementation.

| tree | interpreter | result |
| --- | --- | --- |
| `84113ab` (baseline) | 3.12.13 | **988 passed** in 231s |
| prototype (B0+B1+B9) | 3.12.13 | **988 passed** in 230s |
| prototype (B0+B1+B9) | 3.14.7t | **988 passed** in 193s |

Zero failures, zero errors, identical totals -- so every row in the two
tables above is confirmed, including the one that matters most
(`test_redact_reports_outcome.py:58`'s zero-area zone still counting) and
all four of #228's tests on both levers.

The same prototype confirms the fix, on 3.12.13:

```
APPLIED: 0                                    (was 1)
graph 0028,0301:                present=False (was True, None)
graph 0008,0008:                present=False
graph 0008,2111:                present=False
graph _ISOCENTER_REDACTION_HASH: present=False
exported BurnedInAnnotation:    present=False (was present, zero-length CS)
exported ImageType:             present=False
exported DerivationDescription: present=False
sidecar after off-image redact: 17            (was 34)
```

**What the prototype does not settle**: it carries no `force=` (B5.4),
no docstring or CHANGELOG work, and none of the new tests in §T. The
implementer still writes those and reruns the suite; what is settled is
that the gate change alone breaks nothing.

### C.3 Tests that touch the attestation attributes

| test | shape | verdict |
| --- | --- | --- |
| `test_redaction_multizone.py:347` `assert not inst.attributes.get("_ISOCENTER_REDACTION_HASH")` | serial path, all-off-image | green, and **note the assertion is `not ... .get(...)`, which passes on both absent and `None`**. It does not pin absence; T2 must use `not in` |
| `test_redaction_failure_is_reported.py:187,321,531,797` `"_ISOCENTER_REDACTION_HASH" not in inst.attributes` | failed instances | green. These already use `not in`, and after B0 they are joined by the off-image case |
| `test_redaction_failure_is_reported.py:178,553` `assert inst.attributes.get(...)` truthy | successfully redacted | green |
| `test_redaction_wildcard.py:121` `assertIsNotNone(...get(...))` | in-bounds | green |
| `test_services.py:49` `assert inst.attributes.get("0028,0301") == "NO"` | calls `_apply_redaction_flags` directly | green, unchanged |
| `test_redact_error.py:80,85`, `test_redaction_robustness.py:58`, `test_scaffold_features.py:189`, `test_create_config_output.py:44`, `test_reporting.py:119` | set `"YES"` as an input | green; none uses an all-off-image rule |

### C.4 `test_redaction_optimization.py`, the one test B5.4's signature can break

`services.py:560-566` names this file in a comment -- "*The signature is
unchanged; `test_redaction_optimization.py` mocks this method and asserts
on the call, not the result*" -- so a spec that adds a parameter to
`redact_machine_instances` has to answer it. Read:

| test | what it pins | verdict for `force=` |
| --- | --- | --- |
| `test_process_valid_zones:52` | `service.redact_machine_instances = MagicMock()`, then `assert_called_once()` | **safe.** No arguments are pinned -- not `assert_called_once_with`, no `call_args` read |
| `test_redact_feedback_tqdm:63` | calls `service.redact_machine_instances("M1", [(0,10,0,10)])` **positionally, two arguments**, then asserts on `tqdm`'s kwargs | **safe** provided `force` is added with a default and *after* the existing parameters. This is a constraint on the implementation, and it is the only one |
| `test_skip_empty_zones` | `process_machine_rules` early return | untouched |

Also calling `redact_machine_instances` positionally with two arguments:
`test_redaction_rgb.py:40`, `test_services.py:30`,
`test_pixel_geometry_pipeline.py:318`. Same constraint, same conclusion.
All green in the prototype run, which did not add `force` -- so this row
is **read, not measured**; the implementer measures it when the parameter
exists.

### C.5 #228's own tests

All four in `tests/test_redaction_identity.py`, both levers, stay green.
`test_an_instance_nothing_was_applied_to_keeps_its_identity` keeps its
assertions and **loses its docstring's claim about the gate** (B1.4,
B2).

---

## T. Tests

Polarity is stated for every one. "Detection" = red on `84113ab`,
green after. "Selectivity guard" = green on `84113ab` and green after;
it is not evidence of a fix and must not be presented as one. All new
tests go in a new `tests/test_redaction_attestation.py` and build on
`reloaded_redaction_session`, whose geometry monoculture is noted in T8.

Executor coverage: `redact()` picks threads on 3.14t and processes
elsewhere, so every test whose subject is the parallel path is
parametrised over `ISOCENTER_FORCE_THREADS` / `ISOCENTER_FORCE_PROCESSES`
exactly as `tests/test_redaction_identity.py` does, and for the same
reason -- the property is about the executor, not the build.

| # | test | polarity | subject |
| --- | --- | --- | --- |
| T1 | `test_a_rule_whose_zones_all_miss_reports_nothing_applied` -- all-off-image rule, `assert session.redact(...) == 0`. Non-vacuity: `prepare_redaction_tasks` returns exactly 1 task first, so the rule really did match | **detection**; `applied == 1` measured on `84113ab`, both interpreters | B3 |
| T2 | `test_a_rule_whose_zones_all_miss_creates_no_attributes` -- after the same run, `"0028,0301" not in inst.attributes` and the same for `0008,0008`, `0008,2111`, `_ISOCENTER_REDACTION_HASH`; `"0008,9215" not in inst.sequences`. **`not in`, never `not ...get()`** | **detection**; all four measured present-with-`None` on `84113ab` | B4 |
| T3 | `test_a_rule_whose_zones_all_miss_writes_no_null_element_to_the_exported_file` -- `save()`, `export()`, `dcmread`, assert the three tags are **absent** from the dataset | **detection**; measured on `84113ab` as three zero-length elements (`CS`, `CS`, `ST`) | B4, and it is the clause a graph-only test cannot state |
| T4 | `test_one_zone_landing_is_enough` -- `[[0,8,0,8],[100,108,100,108]]`; `applied == 1`, `0028,0301 == "NO"`, a real hash, UID **changed**, `arr[0:8,0:8].sum() == 0` | **selectivity guard**, green on `84113ab` | The only guard against over-correcting B0's polarity to "all zones landed". Nothing else asserts the attestation for a mixed rule -- `test_redaction_multizone.py:267` asserts only pixels and the count |
| T5 | `test_an_existing_burned_in_flag_survives_a_rule_that_applied_nothing` -- source `(0028,0301) = "YES"`, off-image rule, assert still `"YES"` after `redact()` and that `scan_burned_in_annotations` still files a `RISK` row | **selectivity guard**, green on `84113ab` -- measured in §1.2, and the hypothesis it guards was falsified there | B4's safety half. Marked a guard *because* it was measured green; writing it as detection is the failure mode this spec's brief names |
| T6 | `test_the_serial_path_attests_nothing_when_no_zone_lands` -- `redact_machine_instances` with an all-off-image rule, same four `not in` assertions | **selectivity guard**, green on `84113ab` -- the serial path never had the defect (§3) | Pins the property B0 makes the parallel path match. `test_redaction_multizone.py:347` covers the hash only, and with an assertion that does not distinguish absent from `None` |
| T7 | `test_an_off_image_redaction_appends_nothing_to_the_sidecar` -- `save()`/`close()`/reopen/off-image `redact()`/`save()`/`close()`, then `os.path.getsize(<db>_pixels.bin)` unchanged and one `instance_blobs` row. Read **after `close()`** -- the persistence manager drains on a background thread | **detection**; measured `17 -> 34` on `84113ab`, both levers | B9 |
| T8 | `test_a_damaged_store_is_repaired_by_force` -- redact a two-zone rule, then reach into the store and restore the first zone's pixels to simulate a `0.9.0`-damaged store, reopen, `redact()` -> `0` and PHI intact, `redact(force=True)` -> `1` and `zone1 == 0` | **detection** for `force=`, which does not exist on `84113ab` | B5.4. **It simulates the damage rather than replaying `0.9.0`**, and the docstring must say so and must cite §2's real two-version replay as the evidence that the simulated state is the state `0.9.0` leaves, plus §B5.4's table as the evidence that the repair lands. A test that shells out to an old version is not a test |
| T9 | `test_force_is_off_by_default` -- second `redact()` with the same rule returns `0` and the UID does not change | **selectivity guard**, green on `84113ab` (`test_redaction_failure_is_reported.py:339` already asserts the count half) | Pins the #228 CHANGELOG promise B5.1 refused to break. Its docstring should quote that sentence |

**T8's fixture caveat, and every other test's.**
`reloaded_redaction_session` is a geometry monoculture: 32x32, `uint8`,
`MONOCHROME2`, `SamplesPerPixel 1`, single-frame, SC Image Storage. Every
test above inherits it. That is adequate for #235 and #237 -- both are
about control flow around `apply_redaction_to_array`, not about axis
selection, which is #186/#205/#217's territory and separately covered --
and the limitation is stated rather than left for a reader to discover.

**Interpreter coverage.** The gate is 3.12 + 3.14t
(`.github/workflows/tests.yml`). `redact()` takes threads on 3.14t and
processes elsewhere, so T1-T7 and T9 are parametrised over both levers
and therefore state their property on both executors on both builds --
four combinations per test, as #228 established. T3 additionally crosses
`export()`, which **always uses processes** regardless of interpreter
(#185), so its assertion is about the export worker's view of a graph the
redaction left alone; that is stated in its docstring so a future reader
does not add a lever to it expecting the export to follow.

---

## D. Found here, to be filed separately

1. **The in-bounds redaction writes an orphan sidecar frame, on both
   levers.** Measured on `84113ab`: zone `[0,8,0,8]`, sidecar `17 -> 63`,
   `instance_blobs` referencing 23 bytes at offset 17. 23 of the 46
   appended bytes are unreachable. `execute_redaction_task` persists
   twice -- once inside `if modified:` (which supplies
   `mutation["pixel_loader"]` and so cannot move) and once in the
   `finally` -- and `persist_pixel_data` (`persistence.py:1532`) has no
   deduplication, unlike `_persist_pixels` (`:1949`), which does. B9
   fixes only the off-image half. `compact()` reclaims these, so it is
   dead space rather than a leak, and it grows with every redaction for
   anyone who never compacts. Same family as #228's orphan row.

2. **A zone that is in bounds but selects zero pixels earns a full
   attestation.** Measured: `[[0, 0, 20, 20]]` on a 32x32 image ->
   `applied 1`, `0028,0301 'NO'`, `ImageType ['DERIVED','SECONDARY']`, a
   real hash, zero pixels touched. B3 declines to fix it because the fix
   is redefining `modified` as "bytes changed", which costs the skip
   optimisation. It deserves an issue of its own, not a footnote --
   partly because `tests/test_redact_reports_outcome.py`'s main fixture
   uses such a zone, which suggests the shape was not intended.

3. **`inst._pixel_hash = None` is still set on the serial path only**
   (`services.py:625`), with no equivalent in
   `execute_redaction_task`. Carried forward unchanged from
   `2026-08-31-redaction-loses-work.md` §9.2; still one behaviour with
   two spellings, still low severity.

4. **The version-stamped attestation.** B5.6. The same migration lever
   #172 and #168 want; should be decided once for all three.

5. **`io_handlers.py:1363`'s redaction-hash strip is dead code and emits
   a pydicom warning on every exported instance.**
   `if "_ISOCENTER_REDACTION_HASH" in ds` asks a `Dataset` about a string
   that is neither a tag nor a keyword; pydicom answers `False` and warns
   `UserWarning: Invalid value '_ISOCENTER_REDACTION_HASH' used with the
   'in' operator: must be an element tag as a 2-tuple or int, or an
   element keyword`. Measured once per exported instance on `84113ab`, on
   both an instance carrying a real hash and one carrying a null. The
   `del` therefore never runs -- harmlessly, since the key cannot become
   an element anyway (the element dump in §C.1) -- but the warning is now
   user-visible, because #144 stopped silencing pydicom's warnings for the
   host application. Not fixed here: it is the export path, not the
   redaction gate, and "delete the check" versus "make the check work"
   is a small decision that deserves its own issue rather than riding
   along.

6. **#237 carries no milestone.** `gh issue view 237 --json milestone`
   returns `null`, and no labels. The brief and this spec both treat it
   as v0.9.1. Needs setting, or the framing needs correcting.

---

## E. Shape of the work

**One PR.** The deferral's premise was that #235's gate and #228's must
be decided together as one gate; splitting the count from the attributes
from the identity comment would ship the divergence B0 removes in two
halves, and #237's `force=` lands on the same two call sites and belongs
in the same CHANGELOG entry. The CHANGELOG entry amends #228's
(B1.3) and must therefore be written beside it.

Commit message trailer: `(#235, #237)`.

# The Last Silences, bunch E: a call that accepts what it will not honour

**Date:** 2026-09-10
**Milestone:** v0.9.5 -- The Last Silences
**Issues:** #410 (`export(format="wfdb")` silently ignores an unknown
option), #399 (`embed_identity_token` appends a token item per call while
recovery reads item 0)
**Base:** `main` at `df0feea` (bunch D: #407, #406)
**Status:** design brief for a TDD developer. Three architect decisions are
recorded in §2, §3 and §4; #410's fix is designed **twice** in §6, once for
each side of the ruling the owner has not yet made. Owner questions are in
§9. §12 is the Amendments log, empty until implementation fills it.
**Superseded in part:** #790 (2026-09-23). §4's "a file written by 0.9.4
carrying three items stays readable at item 0 ... The promise holds in both
directions" is no longer true: every release through 0.9.8 wrote the token
into `(0400,0510)`, the transfer syntax UID's element, and the owner ruled
that 1.x reads only the PS3.6 layout (token in `(0400,0520)`), so a 0.9.4
file of any item count is refused by name, not read. Recovery still reads
item 0.

Both issues are the same sentence with different nouns: **a call accepts an
argument it will not honour, and says nothing.**

- #410: `session.export(folder, format="wfdb", patient_id=[...])` -- one
  character off the frozen `patient_ids` -- exports **every patient**. The
  same typo on `format="dicom"` raises `TypeError` immediately. In a
  de-identification library the silent one is the expensive one: the caller
  asked for one patient and received the cohort, with nothing in the return
  value, the log, the audit table or the report saying the filter was
  dropped.
- #399: `lock_identities()` called a second time appends a second token to
  `(0400,0500)` while `recover_original_data` reads item **0**, so the first
  capture wins forever. The re-lock is accepted, reported as success,
  persisted, exported -- and ignored on recovery. Every stale token ships in
  the file.

Neither fix is large. Both sit one wrong line away from replacing the
silence they close with a new one, and §5.4 and §6.5 are mostly about those
two lines.

---

## 1. How this was measured

Nothing below is inherited from the issue text. Where an issue disagrees
with the code, the code is what is reported and the correction is called out
in that issue's own section.

- **Floor:** `/Users/kevin/Developer/Isocenter/.venv/bin/python` --
  3.12.14 (`main`, Sep 9 2026, Clang 23.1.0).
- **Free-threaded gate:** `/Users/kevin/Developer/Isocenter/.venv314t/bin/python`
  -- 3.14.7t. Neither issue touches `parallel.py`, `run_parallel()` or any
  worker function, so nothing here is expected to differ between the two;
  the gate still runs both.
- **Worktree:**
  `/Users/kevin/Developer/Isocenter/.claude/worktrees/agent-abac44101e48f6401`.
- **Third parties:** `pydicom 3.0.2`, `numpy 2.5.3`, `cryptography` (Fernet)
  as installed.

Every invocation carried `PYTHONDONTWRITEBYTECODE=1` (#174) and an explicit
`PYTHONPATH=<worktree>`. The resolved import was checked first and read:

```
/Users/kevin/Developer/Isocenter/.claude/worktrees/agent-abac44101e48f6401/isocenter/__init__.py
```

so the editable install did not serve the main checkout. Every candidate fix
below was applied **in the worktree itself** and reverted with
`git checkout`; a scratchpad copy is not a mutation sandbox, because
`PYTHONPATH` loses to the process cwd and the copy measures the unmutated
tree.

**The suite was run whole with the #410 `raise` branch in place**, because
"who is relying on the shrug?" is the question that decides that ruling and
a `grep` cannot answer it -- a helper that builds a kwargs dict and forwards
`**opts` is invisible to a text search:

| tree | result |
| --- | --- |
| `df0feea` + #410 branch R (raise) | **`1 failed, 1822 passed, 2 skipped in 283.89s`** |
| `df0feea` + #410 branch W (warn) | `119 passed` across the eight wfdb-touching files; nothing red |
| `df0feea` + the #399 fix | **`1 failed, 1822 passed, 2 skipped in 286.52s`** |

The unmodified tree was not run whole here, deliberately: the branch-R run
accounts for all 1823 collected tests and names its one failure, which is
the number the ruling in §6.6 turns on. Bunch D's `1813 passed` at `124e9e5`
is not quoted as this tree's baseline, because it is not one.

The single failure under branch R is
`tests/test_wfdb_writer.py::test_a_waveform_with_no_samples_does_not_cost_the_run_its_pass`
(`tests/test_wfdb_writer.py:688`), and §6.3 says exactly what it is and what
to do about it. The single failure under the #399 fix is
`tests/test_reversibility_coverage.py::test_embed_token_exception`, and it
is a **mock** failure, not a behaviour failure; §5.7 says what to do with it
and with its two green neighbours, which go quietly vacuous on the same
change.

Both suites were run whole for the same reason. A grep-selected subset found
neither failure: `tests/test_wfdb_writer.py:688` builds its kwargs inline
and only a run sees it, and `tests/test_reversibility_coverage.py` never
mentions `lock_identities`, `reversibility_service`, `0400,0500` or
`recover_patient_identity` -- it drives `ReversibilityService` directly
through a `MagicMock`. Both were invisible to the ten- and eight-file runs
that preceded the whole-suite ones. **No behavioural test in this repository
can see either defect** (§5.7, §6.3); the only thing the #399 fix reddens is
a mock bound to the implementation's spelling.

Four probes are committed beside this brief in
`docs/superpowers/specs/2026-09-10-the-last-silences-bunch-e/`, for bunch A,
B, C and D's reason -- a document whose whole claim is measured evidence
cannot point at a directory that exists for nobody but its author. They are
kept off the docs site by `mkdocs.yml`'s `exclude_docs` and cannot be
collected by pytest (`pytest.ini` sets `testpaths = tests`). Each derives
the repo root from its own `__file__`, so they run from any checkout:

- `probe_410_unknown_option_both_formats.py` -- the typo and a nonsense
  option against both formats, two patients (§6.1).
- `probe_399_relock_token_items.py` -- one, two and three locks; what
  recovery answers; the save/reload round trip; the exported file (§5.1).
- `probe_399_relock_dirty_and_persisted.py` -- the re-lock with **no other
  mutation between the two locks**, which is the only shape that exposes the
  `mark_modified()` trap in §5.4.
- `probe_399_foreign_sequence_at_ingest.py` -- a source file carrying its
  own Encrypted Attributes Sequence, which is the case §3 turns on (§5.3).

**Line-number drift in the issues, recorded rather than corrected silently.**
#399 cites `isocenter/reversibility.py:48-70` for `embed_identity_token`; at
`df0feea` the method is **48--81**, the `TODO` that predicts this exact
defect is **70--74**, and the append itself is **line 75**. `recover_original_data`
is 108--147 and its `items[0]` read is **line 131**. The window named in the
issue stops four lines above the line the issue is about. Neither
miscitation changes the argument. #410 cites no line numbers and is exact in
prose.

---

## 2. Architect decision 1: bunch membership

**Ruling: one PR, both issues. Sequence #399 first.**

Bunch D's architect declined to fold #410 into bunch D and recommended
"#410 and #411 as a bunch E", on three reasons (`2026-09-09-the-last-silences-bunch-d.md`
§2). Two of them have since been overtaken and the third was reversed by the
task that commissioned this brief:

1. *"It is blocked on an owner ruling."* Still true, and no longer a reason
   to separate it: §6 designs **both** branches to the level of the probe,
   the fix, the mutation and the CHANGELOG entry, so the developer proceeds
   on either answer without a second design pass. The ruling picks a branch;
   it does not gate the work.
2. *"It carries a breaking CHANGELOG entry of its own... Two unrelated
   breaking entries in one release note is how a reader loses the thread of
   either."* This was the load-bearing objection, and it is measured false
   for this pairing. The owner's ruling on #399 (issue comment, 2026-09-09)
   says in terms: *"Not a breaking change under `docs/api/stability.md` --
   no frozen name or parameter moves, and the previously-observable
   behaviour (first token wins, stale tokens exported) was a defect, not a
   promise."* §4 confirms that against the page. So this bunch carries **at
   most one** breaking entry -- #410's, and only on branch R. The reader
   loses no thread.
3. *"It pairs naturally with #411."* Superseded by the commissioning task:
   #411 is a v1.0 freeze ruling about five user-visible audit strings, not
   v0.9.5 code. Pairing a code fix with a freeze decision is the same
   category error §2.1 of bunch D was avoiding.

What is left is the positive case, and it is the milestone's own sentence.
"The Last Silences" is about **a call that claims a fact is checked and
checks nothing**. #410 is a filter argument accepted and discarded; #399 is
an identity capture accepted, stored, exported, and discarded at the one
moment it is read. A developer holding *"what did this call promise, and
what did it actually honour?"* does both without switching models, and the
PR body is one paragraph.

They share no machinery, and that is fine -- a bunch is a theme, not a
module. It does mean the two halves must not be interleaved in the branch
history. **Sequence #399 first, complete, committed; then #410.** Two
reasons: #399 needs no ruling and therefore ships whatever happens to the
#410 question; and if the ruling arrives late enough to be uncomfortable,
the PR can be cut at the #399 commit without unpicking anything.

---

## 3. Architect decision 2: which reading of the #399 ruling

**Ruling: `sequence.items[:] = [item]` -- the sequence holds exactly one
item after any `embed_identity_token`, whatever it held before. Not
`items[0] = item`.**

The owner's comment contains two sentences that are the same instruction on
a fresh graph and different instructions on any other:

> `embed_identity_token` **replaces item 0** of `0400,0500` rather than
> appending, so **the sequence holds exactly one token** however many times
> it is called.

"Replaces item 0" reads as `items[0] = item`, which preserves items 1..n.
"Holds exactly one token however many times it is called" reads as
`items[:] = [item]`, which does not. They diverge on exactly two starting
states, and both are real:

- **A graph carrying 0.9.4's appended tail.** A store written before this
  fix holds two or three token items (measured: `probe_399_relock_token_items.py`
  reports `after reload: items = 3`). Under `items[0] = item` a re-lock
  leaves the new token at 0 and the stale ones behind it, and the exported
  file still carries every one of them -- which is half the defect #399
  names, left standing. Under `items[:] = [item]` the re-lock cleans them
  up.
- **A source file that already carried an Encrypted Attributes Sequence.**
  Measured in `probe_399_foreign_sequence_at_ingest.py` (§5.3): it reaches
  the graph, and **both** readings overwrite its item 0, because item 0 is
  where the token goes. So neither reading preserves the foreign sequence.
  `items[0] = item` merely preserves its *tail* -- leaving a sequence that
  is neither the source's nor ours, whose item 0 decrypts and whose item 1
  does not.

Given that both readings destroy the foreign head anyway, half-preserving
the foreign tail is the worst of the three available behaviours: it keeps
the storage and the export cost of a blob nobody can read, and it keeps the
ambiguity `recover_original_data` was written not to have. `items[:] = [item]`
satisfies the owner's second sentence literally and for every starting
state; `items[0] = item` satisfies it only when item 0 was already ours.

**This is a decision the owner should be able to see and reverse**, which is
why it is here rather than inside §5.4. It has a user-visible consequence
(§8): an instance whose source file carried a foreign `(0400,0500)` loses
that element on `lock_identities()`. Today that same instance is worse off
-- recovery returns `None` for it (§5.3) -- so the fix is a strict
improvement for the identity, and a documented loss for the foreign blob.
The fidelity question that remains ("should a de-identification library ever
silently overwrite a source element it did not write?") is **filed, not
decided here**: §5.8, owner question Q2.

---

## 4. Architect decision 3: what "frozen" obliges here

**Ruling: neither fix moves a frozen name, a frozen parameter or a frozen
return shape. #410 changes frozen *documented behaviour* and must therefore
change `docs/api/stability.md` in the same commit; #399 changes nothing the
page promises and must be checked against the page's data promise, which it
keeps.**

`docs/api/stability.md` is pinned by `tests/test_frozen_surface.py`, which
parses the Session table row for row. Neither fix touches that table.

**#399.** `reversibility.py` is named in the **Private** section, wholesale,
so `embed_identity_token` and `recover_original_data` are tier 3 and their
behaviour is not frozen. `session.reversibility_service` is tier 2 -- the
attribute is documented, the service's API is not. What *is* frozen, and
what the fix is obliged to keep, is the **data promise** at
`docs/api/stability.md:152-157`:

> A DICOM file exported with reversible anonymization by 1.0 is recoverable
> by every 1.x with its key: the tags `(0400,0500)`, `(0400,0510)`,
> `(0400,0520)` and the key file's format (raw Fernet key bytes).

The fix writes the same three tags with the same payload encoding and
`recover_original_data` **still reads item 0**. ~~So a file written by 0.9.4
carrying three items stays readable at item 0 -- its old first-wins token,
exactly as the owner's comment says -- and a file written after the fix
carries one item at index 0. The promise holds in both directions.~~
**Superseded by #790 (2026-09-23):** a 0.9.4 file carries its tokens in
`(0400,0510)`, a layout 1.x refuses by name rather than reads. §5.5's
test 5 is the pin that stops a later "tidy-up" moving recovery to
`items[-1]`, which would be indistinguishable from correct on every
post-fix file and would break every pre-fix one.

`lock_identities`, `lock_identities_batch` and `recover_patient_identity`
are tier 1; none of their signatures moves.

**#410.** `export` is tier 1 with its parameters `folder, format='dicom',
**options`, and the page's prose freezes the option *names*:

> the `wfdb` options are `patient_ids` and `include_annotation_text`. Those
> option names are frozen with the method.

Tier 1 freezes "their documented behaviour ... for every 1.x release; a
change is a 2.0". **What an unrecognised option does is not documented
anywhere** -- not on this page, not in `docs/waveforms.md`, not in
`export`'s docstring. So today's shrug is undocumented behaviour, not a
promise, and 0.9.5 is the last moment it can be chosen: after the 1.0 tag,
tightening it is a 2.0. That asymmetry is the strongest single argument in
§6.6.

Two concrete obligations follow, on either branch:

1. **The page must say what an unrecognised option does**, in the same
   commit. Prose only -- the table is untouched, and
   `tests/test_frozen_surface.py:305` builds its expectation by parsing that
   table alone, so prose outside it cannot move a pin. That is a reading of
   the parser, not a measurement: **no edit to `docs/api/stability.md` was
   made or run here**, and the developer should run
   `tests/test_frozen_surface.py` after making it rather than trusting this
   paragraph.
2. **`WfdbExporter.export`'s docstring and `Session.export`'s `Raises:`
   block** must match. `Session.export` currently documents `ValueError` for
   an unknown *format* and `ExportError` from the DICOM exporter; branch R
   adds a `TypeError`.

One trap the freeze does *not* cover, and the new test must
(`tests/test_wfdb_privacy.py:794`,
`test_the_wfdb_export_options_are_the_two_the_page_freezes`): that test
collects literal keys touched on the `options` dict by five syntactic forms.
An allow-list constant is none of them. **Measured:** with
`_WFDB_OPTIONS = frozenset({"patient_ids", "include_annotation_text", "zzz_third"})`
in the module, that test still passes. So the existing pin covers *what is
read* and nothing covers *what is admitted*; §6.4 requires a second pin on
the constant, and the two go red against each other if they ever disagree.

---

## 5. #399 -- the re-lock nobody honours

### 5.1 The defect, measured

`isocenter/reversibility.py:75`:

```python
instance.add_sequence_item(self.TAG_ENCRYPTED_ATTRS_SEQ, item)
```

and `isocenter/reversibility.py:131`:

```python
item = seq.items[0]
```

`add_sequence_item` (`isocenter/entities.py:363`) appends. The two lines
have disagreed since the feature was written, and the `TODO` at
`reversibility.py:70-74` predicts the consequence and then gets it wrong:

> Recovery uses the first item, so it's safe, but we should probably clear
> existing items or warn.

It is not safe. "The first item wins" is the defect, not the mitigation.

`probe_399_relock_token_items.py`, on `df0feea`. The graph is built by hand
the way `tests/test_reversibility.py` does; the lock is the thing under
test, not the ingest. Between lock #1 and lock #2 the visible
`PatientName` is changed and a **different** `tags_to_lock` is passed, so a
re-lock that was honoured would be visible in the answer:

```
lock #1: items = 1
  recover -> {'0010,0010': 'Original^Name'}
lock #2: items = 2
  recover -> {'0010,0010': 'Original^Name'}
  item 1 (skipped by recovery) -> b'{"0010,0010": "CHANGED^Value", "0010,0020": "REV_399"}'
lock #3: items = 3
  revision/persisted/dirty: 24 19 True
after reload: items = 3
  recover -> {'0010,0010': 'Original^Name'}
exported files: 1
exported sequence items: 3
```

Every claim in the issue is confirmed and two are extended:

- one item per call: 1, 2, 3;
- recovery answers with the **first** capture, and the second capture is
  sitting one index away, decryptable, unread;
- the tail **survives the store**: `save(sync=True)`, a fresh `DicomSession`
  on the same database, and recovery still answers with capture #1;
- the tail **reaches the exported DICOM file**: `pydicom.dcmread(...)` finds
  three items in `(0400,0500)`. Every stale identity a caller ever captured
  ships to the recipient, encrypted with the same key as the live one. That
  is the half of the defect that is a disclosure question rather than a
  correctness one.

`session.lock_identities()` returns a `LockingResult` naming every instance
it "modified" on each of the three calls. Nothing anywhere reports that two
of the three captures will never be read.

### 5.2 What it costs, and what it does not

The documented pipeline locks once, which is why this is not a 0.9.4 item
and why #395's persist test locks each patient once (its docstring says so).
The sequences that reach it:

- **lock, remediate, lock again** -- the owner's own example, and the
  sequence the pipeline does not forbid. The second capture describes the
  identity as it stands; recovery returns the first.
- **lock with the wrong `tags_to_lock`, notice, lock again.** The correction
  is accepted and discarded. There is no way to clear it.
- **`lock_identities(report)` where a patient appears in the report twice**
  -- the batch path runs `_lock_patient_identity` per entry
  (`isocenter/session.py:2648` is the embed inside it), so a duplicated
  patient id locks twice in one call.

What it does not cost: a single-lock session is bit-identical before and
after the fix. The token payload, the tags, the transfer-syntax UID and the
key format are unchanged.

### 5.3 A case neither the issue nor the ruling names: a foreign sequence

`probe_399_foreign_sequence_at_ingest.py` writes a source file carrying its
own `EncryptedAttributesSequence` -- one item, `EncryptedContent =
b"NOT-OUR-TOKEN"` -- ingests it, and locks:

```
source items: 1
after ingest, graph items: 1
  item 0 attrs: ['0400,0510', '0400,0520']
  recover before any lock -> None
after lock, graph items: 2
ERROR: Failed to recover data from 1.2.826...399.1:
  recover -> None
```

So on `df0feea`, an instance whose source already carried that sequence is
**not recoverable at all** after `lock_identities()`: the foreign blob sits
at item 0, `engine.decrypt` refuses it, `recover_original_data` logs an
error and returns `None`, and `recover_patient_identity` prints "No
encrypted identity token found or decryption failed" over a token that is
sitting at item 1 in perfect condition. This is the same defect with the
volume turned up, and the fix in §5.4 closes it as a side effect.

It is also the case §3's ruling turns on, and the one place this bunch takes
something away: the foreign item is overwritten. §5.8 files the fidelity
question rather than deciding it.

### 5.4 The fix

`isocenter/reversibility.py`, in `embed_identity_token`, replacing line 75
and the `TODO` at 70--74:

```python
# `add_sequence()` + a slice assignment rather than
# `add_sequence_item()`, which appends: this sequence holds exactly
# one item, and the item is the token this call was handed. See
# `recover_original_data` below -- it reads items[0], and until #399
# the two disagreed, so a second lock was accepted, persisted and
# exported while recovery kept answering with the first capture.
#
# `mark_modified()` is NOT redundant and must not be tidied away.
# `add_sequence()` marks the instance modified **only when it
# creates** (entities.py:331, #186's rule), and this path reaches
# into `items` in place rather than through `add_sequence_item()`,
# which marks on every call. Without the line below, the second and
# later locks advance no revision, `has_unsaved_changes` stays
# False, the next `save()` skips the instance, and the new token
# never reaches the store -- measured: memory answers with capture
# #2 and a reopened session answers with capture #1. That is #173's
# shape one module over: the graph changes and the store is never
# told.
sequence = instance.add_sequence(self.TAG_ENCRYPTED_ATTRS_SEQ)
sequence.items[:] = [item]
instance.mark_modified()
```

`recover_original_data` is **unchanged**. It reads item 0 because item 0 is
now the item it means to read, and because every file 0.9.4 wrote is
readable there (§4).

Measured, `probe_399_relock_token_items.py` with the fix in the worktree:

```
lock #1: items = 1
  recover -> {'0010,0010': 'Original^Name'}
lock #2: items = 1
  recover -> {'0010,0010': 'CHANGED^Value', '0010,0020': 'REV_399'}
lock #3: items = 1
after reload: items = 1
  recover -> {'0010,0010': 'CHANGED^Value'}
exported files: 1
exported sequence items: 1
```

**The `mark_modified()` line is the whole reason this section is long.**
`probe_399_relock_dirty_and_persisted.py` locks, saves, and re-locks with
**no other mutation in between** -- the only shape that isolates it, because
any `set_attr` between the two locks dirties the instance for its own
reasons and hides the hole. Three trees, same probe:

| tree | dirty after re-lock | items | in-memory recover | **stored** recover |
| --- | --- | --- | --- | --- |
| `df0feea` | True | 2 | `{'0010,0010': 'Original^Name'}` | `{'0010,0010': 'Original^Name'}` |
| fix **without** `mark_modified()` | **False** | 1 | `{'0010,0020': 'REV_399D'}` | **`{'0010,0010': 'Original^Name'}`** |
| fix **with** `mark_modified()` | True | 1 | `{'0010,0020': 'REV_399D'}` | `{'0010,0020': 'REV_399D'}` |

The middle row is a *new* silence, strictly worse than the one being fixed:
memory and the store disagree about the identity, and nothing says so.

**Use the `save()` path in the tests, not `persist=True`.**
`SqliteStore.update_attributes` (`isocenter/persistence.py:3791`) serializes
and writes every instance it is handed with no `has_unsaved_changes` check
at all, so `lock_identities(pid, persist=True)` writes the new token whether
or not the revision moved -- and would mask the middle row completely. The
default is `persist=False`, the documented pipeline saves separately, and
that is the path the trap lives on.

### 5.5 The probe: one new test file, five tests

`tests/test_relock_identity_token.py`. Every one of these is red on
`df0feea` except where stated.

1. **`test_a_second_lock_leaves_one_token_item`** -- lock twice, assert
   `len(instance.sequences["0400,0500"].items) == 1`. Red today at 2. The
   count assertion is the one that survives a fix that replaces the wrong
   index (mutation M3).
2. **`test_a_re_lock_is_what_recovery_answers_with`** -- lock with
   `tags_to_lock=["0010,0010"]`, change `0010,0010`, lock with
   `tags_to_lock=["0010,0010", "0010,0020"]`, assert
   `recover_original_data(...) == {"0010,0010": "CHANGED^Value",
   "0010,0020": "<pid>"}`. **Full dict equality, not `in`** -- the first
   capture is a *subset* of the second's keys, so `"0010,0010" in recovered`
   passes on both captures and `recovered["0010,0010"]` is the only field
   that distinguishes them. The differing value is what carries the
   assertion; assert it directly.
3. **`test_a_re_lock_reaches_the_store`** -- the §5.4 middle row, as a test:
   lock, `save(sync=True)`, re-lock with a different `tags_to_lock`, **no
   other mutation**, assert `instance.has_unsaved_changes` is True, then
   `save(sync=True)`, close, reopen the same database, and assert the
   *stored* recovery answers with the second capture. Red today (it gets one
   item too many and the first capture); red under M2. This is the only test
   that sees the `mark_modified()` line.
4. **`test_locking_over_a_foreign_encrypted_attributes_sequence_recovers`**
   -- build the §5.3 fixture (an instance whose `(0400,0500)` holds one item
   with `0400,0510 = b"NOT-OUR-TOKEN"`), lock once, assert recovery returns
   the locked identity, and assert the sequence holds one item. Red today,
   where recovery returns `None`. Build the sequence by hand on an
   `Instance` -- no file needed; the probe uses a real file only to prove
   ingest carries it, which §5.3 has already established.

   **The fixture must carry the tag it locks, or this test is red on both
   trees and the developer will conclude the fix does not work.**
   `embed_identity_token` returns immediately on `if not token`
   (`reversibility.py:59-60`), and `_lock_patient_identity` builds the token
   from `first_instance.attributes.get(tag)` for each entry in
   `tags_to_lock` (`session.py:2628-2632`). An instance carrying only the
   foreign `(0400,0500)` and none of the five default tags therefore yields
   `original_attrs == {}`, an empty token, and an embed that does nothing at
   all -- the foreign item survives, recovery still returns `None`, and the
   arm under test was never entered. Set `0010,0010` on the instance (or
   pass a `tags_to_lock` naming a tag it does have), and assert
   `recover_original_data(...) is not None` **as the positive precondition**
   before asserting what it contains.
5. **`test_a_legacy_three_item_sequence_is_still_read_at_item_zero`** --
   hand-build the 0.9.4 artefact: three token items, each encrypting a
   different identity, and assert `recover_original_data` returns **item
   0's**. **Green today, green after the fix, and it is not decoration.**
   Post-fix every sequence this library writes has exactly one item, so
   `items[0]`, `items[-1]` and `items[len(items)//2]` are the same
   expression on every new file, and a later reader "simplifying" recovery
   to the most recent item would be green on the whole suite while breaking
   every file 0.9.4 shipped. This test is the freeze in §4 made executable.
   Its docstring must say that in those words.

Two shapes to keep out, both of which this repo has produced before:

- **`assert len(items) >= 1`** -- trivially true on 1, 2 and 3. Assert `== 1`.
- **A test that locks twice with the *same* `tags_to_lock`.** Both captures
  are then byte-identical after decryption and "recovery returns the second"
  is unfalsifiable. Change a value or change the tag set between the locks;
  test 2 does both.

### 5.6 The mutations that must kill it

The reviewer runs these. Each is applied to the fixed tree, alone.

- **M1 -- revert to the append.** `sequence.items[:] = [item]` becomes
  `sequence.items.append(item)` (with the `add_sequence` call kept).
  Expected: tests 1, 2, 3 and 4 red. Test 5 green (its fixture is
  hand-built and never enters `embed_identity_token`).
- **M2 -- delete `instance.mark_modified()`.** Expected: **test 3 red and
  nothing else**. That single-test kill is the point: it is the measured
  middle row of §5.4, and if any other test goes red the suite is seeing the
  revision counter through something incidental. If test 3 stays *green*
  under M2, the test is using `persist=True` somewhere and is not measuring
  the save path.
- **M3 -- replace the wrong index.** `sequence.items[:] = [item]` becomes
  `sequence.items.insert(0, item)`. Expected: test 1 red (count 2), tests 2,
  3 and 5 green (item 0 is still the new token), test 4 red (the foreign
  item survives at index 1, but recovery works, so this one is red on the
  count assertion only). This mutation is why test 1 exists as a separate
  test from test 2: without it, "the sequence holds one item" is never
  asserted independently of "recovery returns the right thing".
- **M4 -- recovery drifts to the newest item.** `item = seq.items[0]`
  becomes `item = seq.items[-1]`. Expected: **test 5 red and nothing else.**
  If test 5 is green under M4, it is not built from three distinct
  identities and is asserting `0 == 0`.

None of the three rewritten mocks in `tests/test_reversibility_coverage.py`
(§5.7) kills any of M1--M4: they watch which method is called, not what ends
up in the sequence. That is fine and expected -- they cover the
empty-token early return and the exception re-raise -- but it means they
must not be counted as coverage of this fix.

### 5.7 Collateral: three mocks bound to the spelling, and no behavioural test at all

Whole suite with the fix in the worktree: `1 failed, 1822 passed, 2 skipped
in 286.52s`. Every behavioural test passes unchanged -- all ten
reversibility-touching files (`59 passed in 8.95s` across
`test_reversibility.py`, `test_check_reversibility.py`,
`test_lock_identities_signature.py`, `test_crypto.py`,
`test_mutation_gaps.py`, `test_analysis.py`,
`test_export_delivery_counters.py`, `test_optimization.py`,
`test_feature_regression.py`, `test_frozen_surface.py`) and everything else.
Not one of them locks the same patient twice. `tests/test_reversibility.py`
asserts `len(seq.items) > 0` and reads `seq.items[0]` -- the exact shape that
cannot tell one item from three. The developer should **not** tighten that
assertion to `== 1` in passing: the new file owns this behaviour, and a
second home for it is a second answer that can drift. Leave it.

**The one failure is a mock, and two of its neighbours go vacuous silently.**
`tests/test_reversibility_coverage.py` drives `ReversibilityService` against
a `MagicMock(spec=Instance)` and asserts on `add_sequence_item` **by name**.
Three of its tests are affected, and only one of them says so:

| test | line | on the fix | what to do |
| --- | --- | --- | --- |
| `test_embed_token_exception` | 37--43 | **red** -- `DID NOT RAISE Exception`: it puts the `side_effect` on `add_sequence_item`, which the fixed code no longer calls | move the `side_effect` to `add_sequence` |
| `test_embed_token_empty` | 31--35 | green | `add_sequence_item.assert_not_called()` becomes `add_sequence.assert_not_called()` -- otherwise it asserts the absence of a call the code could not make either way |
| `test_embed_original_data_empty` | 45--48 | green | the same substitution, for the same reason |

The first is honest work the fix creates. The second and third are the
danger: they stay green while asserting nothing, and a developer who fixes
only the red one leaves two tests that read as coverage of the empty-token
early return and are no longer attached to it. Fix all three in the same
commit, and say in the commit message that the change is a rename of the
call the mock watches, not a change in what the empty-token path does.

Note also what this failure says about method: it was invisible to the
ten-file grep-selected run, because `test_reversibility_coverage.py`
contains none of the strings the grep looked for. The whole-suite run is
what found it.

### 5.8 Scope boundaries, and two to file

**In scope:** `embed_identity_token`'s three lines and their comment; the
new test file; the CHANGELOG entry; the `TODO` at `reversibility.py:70-74`,
which must be **deleted**, not updated -- it is a note predicting a defect
that no longer exists, and leaving it re-opens the question for the next
reader.

**Out of scope, and do not drift into them:**

- `recover_original_data` itself. It reads item 0 and continues to.
- `recover_patient_identity` (`session.py:2766`). Its `first_inst` loop
  (`session.py:2788-2792`) has an asymmetry of its own: the inner `break`
  leaves the outer `for st` loop running, and unlike
  `_lock_patient_identity` there is no `if first_instance: break` after it,
  so `first_inst` ends up as the first instance of the **last** study that
  has any instances rather than the first study's. It is harmless -- every
  instance of the patient carries the identical token, so which one is read
  does not change the answer -- and it is not "the same instance", which is
  why it is written out here rather than waved at. Leave it; it is
  unrelated to #399.
- `SqliteStore.update_attributes` writing without a dirty check, and without
  calling `mark_persisted()` afterwards. It is why `persist=True` masks the
  trap. **File it** (owner question Q3): "`update_attributes()` writes
  unconditionally and never marks the instances persisted, so a
  `lock_identities(persist=True)` leaves every instance it just wrote
  claiming unsaved changes."
- The foreign-sequence overwrite. **File it** (owner question Q2): "should
  `lock_identities()` refuse, or report, when it overwrites an Encrypted
  Attributes Sequence this library did not write?" It is a fidelity question
  in the same family as #367, it needs an owner ruling, and this bunch's job
  is to make the identity recoverable -- which it does.
- #412, #414, #415, #416, #417, #418, #419 are filed and untouched.

---

## 6. #410 -- the option that is accepted and dropped

### 6.1 The defect, measured

`probe_410_unknown_option_both_formats.py`, on `df0feea`, two patients:

```
patients in store: ['WFPAT-A', 'WFPAT-B']
wfdb patient_ids=['WFPAT-A'] -> ['WFPAT-A_1_0.hea']
wfdb patient_id=['WFPAT-A']  -> ['WFPAT-A_1_0.hea', 'WFPAT-B_1_0.hea']
wfdb zzz_not_an_option=True  -> ['WFPAT-A_1_0.hea', 'WFPAT-B_1_0.hea']
dicom patient_id=[...] -> TypeError: DicomSession._export_dicom() got an unexpected keyword argument 'patient_id'
```

The issue is exact. `WfdbExporter.export` (`isocenter/exporters/wfdb.py:302`)
reads `options.get("patient_ids")` at line 322 and
`options.get("include_annotation_text", False)` at line 327 and never looks
at the rest of the dict. `DicomFormatExporter.export`
(`isocenter/exporters/dicom.py:28`) forwards `**options` into
`_export_dicom`'s real signature, so Python raises for it -- the strictness
is accidental, and the message names a private method through a frozen
public surface (§6.7).

Nothing else records the drop. There is no audit row, no `DATA_LOSS`
entry, no log line, and the returned `List[str]` is longer than the caller
expected but carries nothing that says why.

### 6.2 What the two branches have in common

Whichever way the ruling goes, three things are identical and the developer
can write them first:

1. **A module-level allow-list** in `isocenter/exporters/wfdb.py`, beside
   `WFDB_FORMAT`:

   ```python
   # The options `export()` below honours, and the set every other name
   # is measured against. `docs/api/stability.md` freezes both names with
   # the method; `tests/test_wfdb_option_strictness.py` pins this constant
   # against that page, because the AST pin in test_wfdb_privacy.py
   # collects only the keys the body *reads* and is blind to a name
   # admitted here and never used (measured, #410).
   _WFDB_OPTIONS = frozenset({"patient_ids", "include_annotation_text"})
   ```

2. **The check, first thing in `export()`**, before `patient_ids` is read:
   `unknown = sorted(set(options) - _WFDB_OPTIONS)`. Sorted, so the message
   is deterministic and a test can assert on it.
3. **`docs/api/stability.md` and the two docstrings** say what happens
   (§4).

### 6.3 Branch R -- raise

```python
unknown = sorted(set(options) - _WFDB_OPTIONS)
if unknown:
    raise TypeError(
        f"export(format='wfdb') got unexpected keyword argument(s) "
        f"{', '.join(repr(n) for n in unknown)}; the wfdb options are "
        f"{', '.join(repr(n) for n in sorted(_WFDB_OPTIONS))}.")
```

`TypeError`, not `ValueError`: it is what Python raises for an unexpected
keyword, it is what the `dicom` path already raises for the same mistake
(measured above), and matching it is the whole point of the issue.

**The one existing test that breaks, and it is real.** Whole suite with this
applied: `1 failed, 1822 passed, 2 skipped in 283.89s`. The failure is
`tests/test_wfdb_writer.py::test_a_waveform_with_no_samples_does_not_cost_the_run_its_pass`
(defined at line 624), which at **line 688** calls:

```python
written = session.export(str(tmp_path / "out"), format="wfdb",
                         show_progress=False)
```

`show_progress` is a `dicom` option. This is precisely the case #410's text
asks about -- *"whether any caller is relying on passing a superset of
options to both formats"* -- and the answer, measured across the whole
suite, is: **exactly one site, and it is the repo's own test convenience,
not a user contract.** The fix is to delete the argument; the test does not
depend on it in any way. Do that in the same commit and say so in the commit
message, because a reviewer seeing a test edited alongside a strictness
change should be told which direction the causation runs.

**Tests (branch R), `tests/test_wfdb_option_strictness.py`:**

- **R1 `test_a_mistyped_subset_option_raises_instead_of_exporting_everyone`**
  -- two patients; `pytest.raises(TypeError)` on
  `export(..., format="wfdb", patient_id=[first])`; assert
  `re.search(r"\bpatient_id\b", str(excinfo.value))` -- word-boundary, not
  `"patient_id" in msg`, which is a substring of `patient_ids` and would
  pass on a message naming only the accepted options. Then assert the output
  directory is empty or absent, so "it raised" also means "it wrote
  nothing".
- **R2 `test_the_two_frozen_options_are_still_accepted`** -- `patient_ids`
  and `include_annotation_text` together, in one call, asserting on the
  files written. Paired with R1 in the same file so neither can pass
  vacuously: a check that rejects everything fails R2, one that rejects
  nothing fails R1.
- **R3 `test_both_formats_refuse_the_same_typo`** -- the coherence
  assertion the issue is actually about: `pytest.raises(TypeError)` on
  `format="dicom"` *and* on `format="wfdb"` for the same keyword, in one
  test, so the two can never drift apart again without something going red.
- **R4 `test_the_admitted_options_are_the_two_the_page_freezes`** -- set
  equality on `wfdb._WFDB_OPTIONS`, with a docstring naming the measured
  gap: the AST pin at `tests/test_wfdb_privacy.py:794` collects the keys the
  body *reads* and passes with a third name in this constant. One pin on
  what is read, one on what is admitted; they go red against each other.

**Mutations (branch R).** Each applied alone to the fixed tree:

- **RM1 -- delete the `if unknown: raise`.** Expected: R1 and R3's wfdb half
  red. If R1 stays green, it is asserting on the wrong thing.
- **RM2 -- invert to `if not unknown: raise`.** Expected: R2 red (the good
  call now raises), R1 red (the bad call now does not), R3's wfdb half red
  for the same reason as R1. Kills any test that only ever checks one
  direction.
- **RM3 -- `set(options) - _WFDB_OPTIONS` becomes `set(options) - set()`.**
  Expected: R2 red. This is the "the allow-list stopped being consulted"
  mutation.
- **RM4 -- add `"show_progress"` to `_WFDB_OPTIONS`.** Expected: **R4 red
  and nothing else** -- and this is the mutation the existing AST pin cannot
  kill, measured. If R4 is green here it was written against the docstring
  rather than the constant.

### 6.4 Branch W -- warn

```python
unknown = sorted(set(options) - _WFDB_OPTIONS)
if unknown:
    logger.warning(
        "export(format='wfdb') ignored unrecognised option(s) %s; "
        "the wfdb options are %s. The export continues with the "
        "options it recognises.",
        ", ".join(repr(n) for n in unknown),
        ", ".join(repr(n) for n in sorted(_WFDB_OPTIONS)))
```

`logger.warning`, not `warnings.warn`: this project routes user-facing
notices through `get_logger()`, and `pytest.ini`'s `filterwarnings` has no
`error` entry, so a `warnings.warn` would be invisible to anyone not looking
for it. Measured: with this applied, all 119 tests across the eight
wfdb-touching files pass and nothing needs editing.

**Tests (branch W), same file name.** W1 replaces R1, W2/W4 are R2/R4
unchanged, and R3 becomes W3 asserting the *asymmetry* is deliberate
(dicom raises, wfdb warns) so that a later change to either is visible.

- **W1 `test_a_mistyped_subset_option_is_reported_and_the_export_continues`**
  -- two patients; `caplog.at_level(logging.WARNING, logger="isocenter")`;
  call with `patient_id=[first]`; then **three** assertions, all of which
  the correct-by-accident list demands:
  1. a record exists with `record.levelno == logging.WARNING` -- not
     `"WARNING" in caplog.text`;
  2. `re.search(r"\bpatient_id\b", record.getMessage())` -- word-boundary
     again, and on that record's own message, not on `caplog.text`, which
     also carries `"WFDB export complete"` and every other line the export
     logs;
  3. **both** patients' records were written. Without this the test cannot
     tell branch W from branch R and would pass on either.
- **W3 `test_the_dicom_path_still_raises_where_the_wfdb_path_warns`** --
  the same keyword, both formats, asserting the two different outcomes in
  one place. If the ruling is W, this asymmetry is a decision and needs a
  test that names it; otherwise the next reader closes it as a bug.

**Mutations (branch W):** WM1 delete the `if unknown:` block -> W1 red.
WM2 downgrade `logger.warning` to `logger.debug` -> W1 red on `levelno`
(and green if the test asserted on `caplog.text`, which is the point).
WM3 as RM3. WM4 as RM4.

### 6.5 The one line that would replace this silence with another

On **either** branch, the check must sit **before** `patient_ids =
options.get("patient_ids")` and before any file is written. Branch W in
particular is one placement away from being useless: a warning emitted after
the walk, or from inside `_write_instance`, arrives after the cohort is on
disk. Put it at the top of `export()`, where the measurement above put it.

And on either branch, `_WFDB_OPTIONS` must be the *only* place the two names
are written in the module body. If the developer spells the allow-list
inline in the check *and* keeps the two `options.get(...)` calls, there are
two lists to keep in step and R4/W4 pins only one of them.

### 6.6 Recommendation: raise

**Raise.** Four measured reasons and one that is about time. The dangerous
direction is not symmetric: a dropped `patient_ids` on a de-identification
library means the caller asked for one patient and shipped the cohort, and
the caller most likely to make that typo is a script that nobody is watching
-- which is exactly the reader a log line does not reach, since the report's
"Exceptions & Errors" section reads audit rows and not `isocenter.log`. The
cost of strictness is measured rather than feared: the whole suite says
**one** call site in the entire repository relies on the shrug
(`tests/test_wfdb_writer.py:688`, `show_progress=False`), it is a test's own
convenience, and it is a one-line deletion. The `dicom` path already raises
`TypeError` for the same mistake, so raising makes `export()` one rule
instead of two, and #410's whole complaint is that the two formats disagree
-- a warning leaves them disagreeing, just more audibly. And the timing is
one-way: `docs/api/stability.md` freezes `export`'s documented behaviour for
every 1.x, so 0.9.5 is the last release in which "unknown option" can be
tightened at all; loosening a raise later is a patch, tightening a warning
later is a 2.0. The honest counter-argument is the superset-forwarding
pattern -- `for fmt in ("dicom", "wfdb"): session.export(out / fmt,
format=fmt, **opts)` -- which raising breaks; the repo contains exactly one
instance of it, in a test, and the fix at a real call site is to split the
dict, which is work a caller should be doing anyway since the two formats do
not honour the same options.

### 6.7 Scope boundaries, and one to file

**In scope:** `_WFDB_OPTIONS`, the check, the two docstrings
(`WfdbExporter.export`'s `**options` block and `Session.export`'s `Raises:`
on branch R), the `docs/api/stability.md` prose, the new test file, the
CHANGELOG entry, and -- branch R only -- deleting `show_progress=False` at
`tests/test_wfdb_writer.py:688`.

**Out of scope:**

- **The `dicom` path's message.** It reads `DicomSession._export_dicom() got
  an unexpected keyword argument 'patient_id'`, naming a tier-3 private
  method through a tier-1 public call. Exception *text* is not frozen
  (`stability.md` puts log messages and print lines in tier 2 and says
  nothing about exception strings), and rewriting it means adding a wrapper
  signature to `DicomFormatExporter.export` -- a change to how the `dicom`
  path dispatches, in a PR about the `wfdb` path. **File it** (owner
  question Q4). It is #411-adjacent and belongs with whatever ruling #411
  gets.
- **The registry.** A third-party exporter registered through
  `register()` still shrugs at unknown options, and `Exporter.export`'s base
  docstring says nothing about them. Making the base class enforce an
  allow-list is a plugin-contract change; not now.
- **`docs/waveforms.md`** mentions `include_annotation_text` four times and
  needs no edit on either branch -- it documents the option, not the
  handling of unknown ones. Check it, do not rewrite it.
- **#411.** The five user-visible audit strings outside the freeze are a
  v1.0 freeze ruling. This bunch does not touch them.

---

## 7. Change list, file by file

| File | Change | Issue |
| --- | --- | --- |
| `isocenter/reversibility.py` | `embed_identity_token`: `add_sequence` + `items[:] = [item]` + `mark_modified()`; delete the `TODO` at 70--74; add the trap comment | #399 |
| `tests/test_relock_identity_token.py` | new; five tests (§5.5) | #399 |
| `tests/test_reversibility_coverage.py` | three mocks move from `add_sequence_item` to `add_sequence` (§5.7); one is red without it, two go vacuous with it | #399 |
| `isocenter/exporters/wfdb.py` | `_WFDB_OPTIONS` constant; the unknown-option check at the top of `export()`; docstring | #410 |
| `isocenter/session.py` | `export`'s `Raises:` gains `TypeError` (**branch R only**) | #410 |
| `tests/test_wfdb_option_strictness.py` | new; four tests (§6.3 or §6.4) | #410 |
| `tests/test_wfdb_writer.py` | delete `show_progress=False` at line 688 (**branch R only**) | #410 |
| `docs/api/stability.md` | prose: what an unrecognised `wfdb` option does | #410 |
| `CHANGELOG.md` | one `### Fixed` entry, plus one `### Breaking` on branch R | both |

Nothing in `setup.py` changes: no new import.

---

## 8. CHANGELOG

### #399 -- `### Fixed`, under `## [Unreleased]`

> **A second `lock_identities()` on the same patient is now the one recovery
> answers with, and the sequence holds one token instead of a growing pile
> of them (#399).** `ReversibilityService.embed_identity_token` appended a
> new item to the Encrypted Attributes Sequence `(0400,0500)` on every call
> while `recover_original_data` read item 0, so the *first* capture won
> forever: locking, remediating, and locking again -- a sequence the
> pipeline does not forbid -- left recovery returning the identity as it
> stood before the remediation, with the correct capture sitting one index
> away, decryptable and unread. Measured: three locks produced three items,
> the tail survived `save()` and a reopened session, and `pydicom` found all
> three in the exported file, so every stale identity a caller ever captured
> shipped to the recipient under the same key as the live one. The embed now
> replaces the sequence's contents rather than appending, and marks the
> instance modified explicitly -- `add_sequence()` marks only when it
> creates, so without that the new token would have stayed in memory and
> never reached the store. **Not breaking:** no frozen name, parameter or
> return shape moves, and `recover_original_data` still reads item 0, so a
> file written by 0.9.4 carrying several items remains recoverable at item 0
> -- its old first-wins token. Nothing is migrated. **One behaviour is
> genuinely new:** an instance whose *source* file already carried an
> Encrypted Attributes Sequence had that foreign item at index 0, so
> recovery returned `None` for it after a lock -- the token was at index 1.
> That instance is now recoverable, and the foreign item is replaced rather
> than kept. A single-lock session is bit-identical before and after.

### #410 -- **branch R**, `### Breaking`

> **BREAKING: `session.export(folder, format="wfdb", ...)` now raises
> `TypeError` for an option name it does not recognise, where it previously
> ignored it (#410).** The wfdb exporter read `options.get("patient_ids")`
> and `options.get("include_annotation_text")` and never looked at the rest
> of the dict, so `export(folder, format="wfdb", patient_id=["P1"])` -- one
> character off the frozen name -- **exported every patient**, with nothing
> in the returned list of paths, the log, the audit table or the compliance
> report saying the subset filter had been dropped. The same typo on
> `format="dicom"` has always raised, because `_export_dicom` has a real
> signature; a de-identification library that is loud about a typo on one
> format and ships the whole cohort on the other has the strictness exactly
> backwards. The exact exception: `TypeError: export(format='wfdb') got
> unexpected keyword argument(s) 'patient_id'; the wfdb options are
> 'include_annotation_text', 'patient_ids'.` **What breaks:** a caller
> forwarding one options dict to both formats -- `session.export(out,
> format=fmt, **opts)` in a loop -- now raises on the wfdb pass for any
> `dicom`-only option (`use_compression`, `check_burned_in`,
> `check_reversibility`, `show_progress`, `subset`, `verify_readback`).
> Split the dict per format. `patient_ids` and `include_annotation_text`
> remain the two wfdb options and are unchanged.

### #410 -- **branch W**, `### Changed`

> **`session.export(folder, format="wfdb", ...)` now reports an option name
> it does not recognise instead of dropping it in silence (#410).** The wfdb
> exporter read only `patient_ids` and `include_annotation_text`, so
> `export(folder, format="wfdb", patient_id=["P1"])` -- one character off
> the frozen name -- exported every patient with nothing anywhere saying the
> subset filter had been dropped. It now logs a `WARNING` naming the
> unrecognised options and the two it accepts, and continues with the
> options it recognises. **Not breaking:** every call that worked before
> still works and writes the same files. The `dicom` path continues to raise
> `TypeError` for the same mistake, because `_export_dicom` has a real
> signature; the two formats therefore still differ in what they refuse, and
> `tests/test_wfdb_option_strictness.py` pins that difference as a decision
> rather than leaving it as an accident.

---

## 9. Owner questions

- **Q1 (blocking one branch of §6): raise or warn on an unrecognised wfdb
  option?** §6.6 recommends **raise**. The measured input to the decision:
  exactly one site in the whole repository relies on the current shrug
  (`tests/test_wfdb_writer.py:688`, passing `show_progress=False`), it is a
  test's convenience, and the whole suite is otherwise green under the
  raise. `docs/api/stability.md` freezes `export`'s documented behaviour for
  every 1.x, so this is the last release in which it can be tightened.
- **Q2 (file, do not decide here): should `lock_identities()` refuse or
  report when it overwrites an Encrypted Attributes Sequence this library
  did not write?** §3 and §5.3. Today such an instance is unrecoverable
  after a lock; after this bunch it is recoverable and the foreign item is
  gone. That is a source element silently replaced, which is the family
  #367 belongs to.
- **Q3 (file):** `SqliteStore.update_attributes()`
  (`isocenter/persistence.py:3791`) writes every instance handed to it with
  no `has_unsaved_changes` check and never calls `mark_persisted()`, so
  `lock_identities(persist=True)` leaves every instance it just wrote still
  claiming unsaved changes. Harmless today; it is also why `persist=True`
  masks the §5.4 trap.
- **Q4 (file, #411-adjacent):** the `dicom` path's `TypeError` names
  `DicomSession._export_dicom()` -- a tier-3 private method surfaced through
  a tier-1 public call. Exception text is not frozen, and fixing it means
  giving `DicomFormatExporter.export` a real signature.

---

## 10. What the reviewer should attack

1. **Run the four mutations in §5.6 and the four in §6.3 (or §6.4).** The
   two single-test kills are the ones that matter: M2 must redden test 3
   *and nothing else*, and RM4/WM4 must redden R4/W4 *and nothing else*.
   Anything wider means a test is seeing the change through something
   incidental.
2. **Check test 3 really uses the `save()` path.** If it locks with
   `persist=True` anywhere, `update_attributes` writes unconditionally and
   the test is green under M2 while measuring nothing (§5.4).
3. **Check test 5 is built from three *distinct* identities.** If the three
   hand-built items encrypt the same payload, it asserts `0 == 0` and M4
   walks past it.
4. **Check test 2 asserts the differing value, not the key.** The first
   capture's keys are a subset of the second's; `"0010,0010" in recovered`
   is true of both.
5. **Check R1/W1 use a word boundary on `patient_id`.** `"patient_id" in
   msg` is satisfied by a message that names only `patient_ids`.
6. **Check W1 asserts `record.levelno`, not `"WARNING" in caplog.text`,**
   and that it also asserts both patients were written -- without that it
   cannot tell branch W from branch R.
7. **Check the #410 check sits above `options.get("patient_ids")`** and not
   inside the walk (§6.5).
8. **Confirm the `TODO` at `reversibility.py:70-74` is deleted, not
   edited.**
9. **Confirm all three mocks in `tests/test_reversibility_coverage.py`
   moved, not just the one that was red** (§5.7). Two of them stay green
   while asserting the absence of a call the code can no longer make; a
   developer who fixes only `test_embed_token_exception` leaves them
   reading as coverage of the empty-token path and attached to nothing.
10. **Confirm `docs/api/stability.md` gained the sentence** and that its
   Session table is untouched.

---

## 11. Sequencing

1. **#399 first, complete.** New test file red, fix, green, mutations, the
   `TODO` deleted, the three mocks in `tests/test_reversibility_coverage.py`
   moved (§5.7), CHANGELOG. Commit. It needs no ruling and ships whatever
   happens to Q1.
2. **#410 second**, on whichever branch the ruling names. The three items in
   §6.2 are common to both and can be written before the answer arrives;
   only the seven lines of the check and the tests' assertions differ.
3. **The full suite twice** -- `.venv` (3.12.14) and `.venv314t` (3.14.7t)
   -- before the PR. Neither fix touches `parallel.py`, so a divergence
   between them would be a finding, not a flake, and should stop the PR.
4. **One PR**, `fix:` with both issue numbers and a `Fixes` line each.

---

## 12. Amendments

Corrections made **during** implementation, in the register the project
reserves for them: what the brief predicted, what the tree actually did, and
the measurement. These are not `**Superseded in part:**` entries -- that
marker is for a *later* change falsifying a clause, and belongs in the front
matter. Predictions above are left standing so the two can be compared.

Every brief this milestone has needed this section, and each one has been
wrong somewhere specific: bunch D's log runs to seven entries, six of them
found by running exactly what the brief asked for. The likeliest candidates
here, named in advance so the developer knows what to watch:

- **§5.6 M1's blast radius.** The prediction is "tests 1, 2, 3, 4 red, test
  5 green". Test 4's fixture is hand-built and *does* enter
  `embed_identity_token`, so it should be red -- but if the developer builds
  it through `lock_identities()` on a real ingest instead, the arm it enters
  may differ.
- **The two `1 failed, 1822 passed` rows.** Both were measured on `df0feea`
  with one candidate fix in place and no new tests. The counts move once the
  new files exist; the *identity* of each single failure --
  `test_a_waveform_with_no_samples_does_not_cost_the_run_its_pass` under
  #410-R, `test_embed_token_exception` under #399 -- is the claim, not the
  count. The two fixes were never applied together; if the combined tree
  fails somewhere neither of them did alone, that is a finding and belongs
  here.
- **§5.5 test 3's `has_unsaved_changes` assertion.** Measured on a
  hand-built graph with one instance. On an ingested graph the instance may
  be dirty for reasons of its own between the two locks, which would make
  the assertion pass without the `mark_modified()` line. If so, the fixture
  is wrong, not the assertion.

### A1 -- M3 reddens test 3 as well, on an assertion §5.5 does not name

**Predicted (§5.6, M3):** `sequence.items[:] = [item]` becomes
`sequence.items.insert(0, item)` -> "test 1 red (count 2), tests 2, 3
and 5 green, test 4 red on the count assertion only."

**Measured, on the fixed tree with M3 alone
(`tests/test_relock_identity_token.py`, `test_reversibility_coverage.py`
and `test_reversibility.py` in one run):** `3 failed, 14 passed`. Tests
1, **3** and 4 red; tests 2 and 5 green. Test 3 fails at
`assert len(_items(stored)) == 1` -- not at either of its
`has_unsaved_changes` assertions, both of which pass under M3.

**Why:** test 3 as written pins the item count on the *reloaded* graph
as well as what the stored recovery answers with. §5.5's sketch for it
names only "assert `has_unsaved_changes` is True ... and assert the
stored recovery answers with the second capture", and under M3 the
stored recovery does answer with the second capture, because the new
token is at index 0. The count assertion was added because the count is
the half of #399 that reaches the exported file: a store still holding
two items still ships two, and test 1 measures only memory. It is kept.

**What this does not change:** the two single-test kills the brief calls
the point of the exercise both hold. Every count below is over the same
**17-test set** -- `tests/test_relock_identity_token.py` (5),
`tests/test_reversibility_coverage.py` (9) and
`tests/test_reversibility.py` (3) in one run -- named because a count
whose file set is not stated cannot be reproduced from this log (see
A5, S2). **M2 (delete `mark_modified()`) reddens test 3 and nothing
else** -- `1 failed, 16 passed`, failing at
`assert inst.has_unsaved_changes` with
`_revision=14, _persisted_revision=14`, which is the §5.4 middle row
exactly. **M4 (`items[-1]`) reddens test 5 and nothing else** --
`1 failed, 16 passed`. M1 (revert to the append) reddens tests 1, 2, 3
and 4 with test 5 green, exactly as predicted. The reviewer re-ran M2
and M4 against the **whole suite** and got `1 failed, 1831 passed` for
each, with the same single red in both cases -- so "and nothing else"
survives widening here, which is exactly what it did not do for RM3.

### A2 -- the two §12 candidates that did not materialise

Recorded because "the brief was right here" is worth as much to the
next reader as "the brief was wrong there".

- **§5.6 M1's blast radius** was predicted correct: test 4's hand-built
  fixture does enter `embed_identity_token`, and M1 reddens it.
- **§5.4's post-fix probe output is reproduced verbatim.**
  `probe_399_relock_token_items.py` on the fixed tree: `lock #1/#2/#3:
  items = 1`, `recover -> {'0010,0010': 'CHANGED^Value', '0010,0020':
  'REV_399'}` after the second lock, `after reload: items = 1`,
  **`exported sequence items: 1`**. That last line is the disclosure
  half of #399 and no test in `tests/test_relock_identity_token.py`
  reaches it -- the tests stop at the store round trip -- so the probe
  is what carries it.
- **`probe_399_foreign_sequence_at_ingest.py` on the fixed tree**
  confirms the §5.3 case on the real ingest path, which test 4
  deliberately skips: `after ingest, graph items: 1`, `recover before
  any lock -> None`, then `after lock, graph items: 1` and
  `recover -> {'0010,0010': 'Foreign^Source', '0010,0020':
  'FOREIGN_399'}`. Before the fix this line read `after lock, graph
  items: 2` and `recover -> None`.
- **§5.5 test 3's `has_unsaved_changes` assertion** holds on the
  hand-built graph as designed. On `df0feea` the instance is 16/16
  after the first `save(sync=True)` and 17/16 after the second lock
  (`probe_399_relock_dirty_and_persisted.py`, re-run here and matching
  the brief's row exactly), so the "not dirty" precondition before the
  second lock is real rather than accidental, and the test would have
  caught a fixture that was dirty for its own reasons.

### A4 -- #410 branch R: every prediction held, including the one that mattered most

The ruling arrived as **raise** (owner comment on #410, 2026-09-10),
so §6.3 is the branch implemented. Recorded here because the brief has
been wrong somewhere in every bunch this milestone and this half of it
was not.

- **§6.1's probe output is reproduced verbatim** on this branch's tip
  (`b60ad4f`, which carries #399 and no #410 work):
  `wfdb patient_ids=['WFPAT-A'] -> ['WFPAT-A_1_0.hea']`,
  `wfdb patient_id=['WFPAT-A'] -> ['WFPAT-A_1_0.hea', 'WFPAT-B_1_0.hea']`,
  the same for a name that is not an option at all, and
  `dicom patient_id=[...] -> TypeError: DicomSession._export_dicom() got
  an unexpected keyword argument 'patient_id'`.
- **§6.3's "one existing test breaks" is exact.** The eight
  wfdb-touching files under the fix: `1 failed, 123 passed`, and the
  failure is
  `tests/test_wfdb_writer.py::test_a_waveform_with_no_samples_does_not_cost_the_run_its_pass`.
  `show_progress` appears **once** in that file, and the whole-suite
  runs below confirm it is the only site in the repository.
- **The four mutations land where §6.3 says**, with one qualification
  A5 corrects: the counts first recorded here came from three different
  unnamed file sets and RM3's "and nothing else" was true only of a run
  confined to the strictness file. Every mutation is re-measured in A5
  over one named set.
- **§4's measured claim about the existing AST pin is confirmed
  independently.** Under RM4 --- a third name admitted by the constant
  and read nowhere ---
  `tests/test_wfdb_privacy.py::test_the_wfdb_export_options_are_the_two_the_page_freezes`
  stays **green**. That is the whole case for the second pin, and it is
  now measured twice by two different people.
- **§4's reading of `tests/test_frozen_surface.py` holds.** The
  `docs/api/stability.md` edit is prose outside the Session table, and
  the frozen-surface pin is green after it. The brief flagged that as a
  reading of the parser rather than a measurement; it is a measurement
  now.

One citation drifts by a line and nothing turns on it: §6.3 and §7 cite
`tests/test_wfdb_writer.py:688` for the `show_progress=False` argument.
Line 688 holds `written = session.export(str(tmp_path / "out"),
format="wfdb",` and the argument itself is on 689. The citation names
the call, which is the useful thing to name.

### A5 -- what the review found, and the mutation table redone with its file set named

Three findings from the review of PR #421. None blocking; the first is a
hole in this PR's own machinery and is the milestone's theme recursing
one level.

**S1 -- `R4` pinned the constant and nothing pinned the check.**
`_WFDB_OPTIONS` exists, by its own comment, so the check and the
refusal message cannot drift apart. Nothing said so. The reviewer left
the constant untouched and inlined a three-name set literal in the
check --

```python
unknown = sorted(set(options) - {"patient_ids",
                                 "include_annotation_text",
                                 "show_progress"})
```

-- and the **whole suite stayed green at `1832 passed, 2 skipped`**,
byte-identical to the unmutated tree: R1-R4 green (R4 reads the
constant, which was untouched), the AST pin green (a set literal inside
a `BinOp` is none of its five collected forms), frozen surface green.
On that tree `show_progress=False` exported both patients while the
refusal message still recited `_WFDB_OPTIONS` -- so the exception text
was the thing lying about which options were really accepted, and that
text is what the CHANGELOG quotes as *the exact exception*.

Compounding it: after this PR deleted the one line in the repository
that passed a `dicom` name to the wfdb path (`show_progress=False` in
`tests/test_wfdb_writer.py`), **no test anywhere passed one**, while the
CHANGELOG's "what breaks" paragraph promises the refusal for six names.
The documented breaking change was unmeasured by the suite shipping it.

Closed with the behavioural option (a):
`test_every_dicom_only_option_is_refused_by_the_wfdb_path`, parametrized
over `use_compression`, `check_burned_in`, `check_reversibility`,
`show_progress`, `subset` and `verify_readback`, asserting `TypeError`
*and* that the message names the option the caller passed -- the second
half being what catches the message/check drift rather than merely the
missing refusal. The AST option (b) was not also taken: one spelling per
behaviour, and (a) additionally discharges the CHANGELOG promise, which
(b) would not.

**S2 -- RM3's "and nothing else" was not reproducible, and no count
named its file set.** Confirmed here: over the four-file set below, RM3
gives **`3 failed, 57 passed`** -- R2 plus
`test_wfdb_privacy.py::test_annotation_text_is_present_when_opted_in_end_to_end`
and `::test_the_wfdb_export_patient_ids_option_limits_the_export`, both
of which pass a *valid* option that `- set()` now rejects. The original
claim was true only of a run confined to
`tests/test_wfdb_option_strictness.py`. The error was in the safe
direction and the headline above it read "every prediction held", which
is the bunch D failure mode; it is corrected rather than softened.

**The table redone.** One file set for every row --
`tests/test_wfdb_option_strictness.py`,
`tests/test_wfdb_privacy.py`, `tests/test_wfdb_writer.py` and
`tests/test_frozen_surface.py`, **60 passed** unmutated -- so each count
below is reproducible from this line alone.

| mutation | measured over the 60-test set |
| --- | --- |
| RM1 delete the `if unknown: raise` | `8 failed, 52 passed` -- R1, R3 and all six R5 cases |
| RM2 invert to `if not unknown:` | `26 failed, 34 passed` -- every valid wfdb export in the set now raises |
| RM3 `- _WFDB_OPTIONS` becomes `- set()` | `3 failed, 57 passed` -- R2 and the two `test_wfdb_privacy.py` tests that pass a valid option |
| RM4 `"show_progress"` added to `_WFDB_OPTIONS` | `2 failed, 58 passed` -- R4 and `R5[show_progress]` |
| **RM5** the reviewer's: check inlined, constant untouched | **`1 failed, 59 passed` -- `R5[show_progress]` alone** |

RM5 is the row that did not exist before this review. It was green on
the whole suite; it is now a single-test kill.

**S3 -- the AST pin's docstring said #410 was unfixed.**
`tests/test_wfdb_privacy.py`'s
`test_the_wfdb_export_options_are_the_two_the_page_freezes` read
"...#410 -- that the wfdb path shrugs at an unknown option where the
`dicom` path raises -- is the real fix and is filed rather than done
here." False on this tree, and it is the docstring making the case for
the second pin, so a reader arriving there was told the wfdb path still
shrugs. Corrected in place: the residual it describes is real, but it no
longer reaches a caller as a silently dropped option.

Two cosmetic notes taken at the same time: a missing blank line before
`` `generate_report(format=)` `` in `docs/api/stability.md`, which the
#410 prose block had glued to an unrelated paragraph, and a trailing
blank line at the end of this file.

**Confirmed by the review and deliberately not changed:** A1's extra
count assertion in test 3 is load-bearing rather than a duplicate --
test 1 counts in memory, test 3 counts on the reloaded store; brief
item 4 came back clean end to end (a source file carrying a foreign
`(0400,0500)`, ingested, locked, exported, `dcmread` -> one item, and
re-ingesting recovers the original); and the case §3's ruling turns on
that no probe covered was measured too -- a stored 0.9.4-style
three-item sequence, reloaded, re-locked, saved and reloaded, comes back
at `items = 1`, so `items[:] = [item]` cleans up the stored tail across
the save boundary.

### A3 -- an environment note, not a correction to the brief

The bash sandbox in this worktree refuses `PYTHONPATH=... python ...`
written as a plain command; the same command prefixed with `env` runs.
The recipe in CLAUDE.md is otherwise unchanged and
`isocenter.__file__` resolved to this worktree on every run.

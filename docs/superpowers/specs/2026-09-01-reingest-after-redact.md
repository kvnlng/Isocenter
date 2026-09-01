# Re-ingesting a folder after `redact()` re-adds the un-redacted source (#238)

**Status:** design, ready to implement
**Base:** `origin/main` @ `65dcb2e` ("one gate decides the redaction attestation…", #235/#237 via #246)
**Milestone:** v0.9.1 — *Reports Success While Wrong*
**Issue:** #238. Adjacent: #228/#246 (the fix that made this uniform), #197 (duplicate SOP UID accounting), #167 (`SCAN_GAP`, in flight), #191 (export return value, decided/unbuilt), #78 (filenames are the SOP Instance UID).

Every behavioural claim below is followed by the command that produced it and
the output it produced. Scripts live in the session scratchpad
(`repro238.py`, `repro238b.py`, `repro238c.py`, `repro238f.py`, `repro238g.py`);
each pins `PYTHONPATH` at the worktree and asserts `isocenter.__file__` is
inside it before doing anything.

Interpreters:

```
$SCRATCH/venv312/bin/python   -> 3.12.13,  pydicom 3.0.2,  GIL on   (redact() runs in processes)
$SCRATCH/venv314t/bin/python  -> 3.14.7t,  pydicom 3.0.2,  sys._is_gil_enabled() False (redact() runs in threads)
```

---

## 1. The problem, measured

### 1.1 The fixture

One 32×32 `MONOCHROME2` SC instance written to `src/a.dcm`, `SOPInstanceUID =
1.2.3.phi`, `DeviceSerialNumber = SN1`, pixel value 200 burned into rows/cols
0–7, one redaction zone `[0, 8, 0, 8]`. `ingest(src)` → `redact()` →
`ingest(src)`.

### 1.2 It reproduces identically on both gate interpreters

```
$ PYTHONPATH=<worktree> $SCRATCH/venv312/bin/python $SCRATCH/repro238.py
python=3.12.13 gil_enabled=True isocenter=<worktree>/isocenter/__init__.py
after 1st ingest: instances = ['1.2.3.phi']
  known_files = {'…/repro238-uhyf6knk/src/a.dcm'}
redact applied = 1
after redact: instances = ['1.2.826.0.1.3680043.8.498.674733…']
  file_path = [None]
  known_files = set()
after 2nd ingest: instances = ['1.2.826.0.1.3680043.8.498.674733…', '1.2.3.phi']
exported: ['…/1.2.3.phi.dcm', '…/1.2.826.0.1.3680043.8.498.674733….dcm']
  zone sum …/1.2.3.phi.dcm 12800
  zone sum …/1.2.826….dcm 0
validation: REVIEW_REQUIRED
audit summary: {}
exceptions: []
```

```
$ PYTHONPATH=<worktree> $SCRATCH/venv314t/bin/python $SCRATCH/repro238.py
(python=3.14.7 gil_enabled=False)
after redact: … file_path = [None] … known_files = set()
after 2nd ingest: instances = ['1.2.826.0.1.3680043.8.498.320893…', '1.2.3.phi']
  zone sum …/1.2.3.phi.dcm 12800
  zone sum …/1.2.826….dcm 0
validation: REVIEW_REQUIRED
audit summary: {}
exceptions: []
```

Post-#246 the two interpreters agree, which is what #246 was for. The issue's
table (processes green on `dddb659`) is history; **this is now the behaviour on
every build**.

The `REVIEW_REQUIRED` above is not detection. `audit summary: {}` — the whole
run wrote no audit row at all, so the grade came from the `not audit_summary`
arm of `session.py:1584-1587`. Any session that had written one audit row
would have graded `PASS` over an export containing 12 800 units of burned-in
identifier.

### 1.3 The pre-redaction row does **not** linger in the database

This matters because it decides whether `source_path` has a job to do:

```
$ PYTHONPATH=<worktree> $SCRATCH/venv312/bin/python $SCRATCH/repro238f.py   # no second ingest
graph after redact: [('1.2.826.0.1.3680043.8.498.961098…', None)]
DB rows:            [('1.2.826.0.1.3680043.8.498.961098…', None)]
blob rows:          [('1.2.826.0.1.3680043.8.498.961098…', 'pixels', 23, 17)]
graph after reload: [('1.2.826.0.1.3680043.8.498.961098…', None)]
known_files after reload: set()
```

`save_all` prunes rows for instances that left the graph (`_delete_instances`,
`persistence.py:1823`), so there is exactly one row and no orphan of the
original. **But `known_files` is empty after a reload too** — so a *reopened*
redacted store re-imports its own source folder just as a live one does, and
whatever we record has to survive the SQLite round-trip to fix that leg.

### 1.4 With rules still loaded, the export "repairs" the pixels and lies about them

```
$ PYTHONPATH=<worktree> $SCRATCH/venv312/bin/python $SCRATCH/repro238b.py
=== LEG B: rules still loaded at export ===
instances: ['1.2.826.0.1.3680043.8.498.771583…', '1.2.3.phi']
  1.2.3.phi.dcm                 zone_sum=0 ImageType=None            BurnedIn=None
  1.2.826….dcm                  zone_sum=0 ImageType=['DERIVED','SECONDARY'] BurnedIn=NO
  validation: REVIEW_REQUIRED   audit: {}   exceptions: []
```

The export worker re-applies the series' zones, so the duplicate's *pixels*
come out clean — and the file is still a second copy of the same image, named
for the source's SOP Instance UID (#78), carrying **no `DERIVED` Image Type and
no Burned In Annotation**: an un-attested duplicate claiming to be an original.
The un-redacted frame also stays in the store and in the sidecar. So the harm
is not confined to the rules-cleared case; only its worst form is.

### 1.5 What already reaches the report, and what does not

```
=== LEG C: BurnedInAnnotation=YES, no rule in force ===
  validation: REVIEW_REQUIRED
  unsafe: [('1.2.3.burned', '…/src/a.dcm', 'BurnedInAnnotation FLAGGED as YES')]
  section4: Exceptions & Errors … | COMPLIANCE_CHECK | BurnedInAnnotation FLA…
```

An instance whose *file* declares `(0028,0301) = YES` and that no rule covers
already reaches section 4 and already grades `REVIEW_REQUIRED`, through
`SqliteStore.check_unsafe_attributes()` → `exceptions` in `generate_report`.
What is invisible is an identifier burned into pixels with no tag declaring it
— the issue's own fixture — and the only thing that could see that is OCR.

### 1.6 The mechanism, restated

* `Instance.regenerate_uid()` (`entities.py:364-390`) ends `self.file_path =
  None`, deliberately: the in-memory instance no longer matches the disk bytes,
  and `get_pixel_data()`'s fallback chain (`entities.py:467`) would re-read the
  un-redacted file if it did. **This stays.**
* `DicomStore.get_known_files()` (`store.py:49-63`) builds the incremental
  de-dup set out of `file_path`.
* Its one consumer, `io_handlers.py:522-523`, filters `all_files` with it.
* Since #246, `file_path = None` happens on every interpreter for every
  genuinely-redacted instance — `_apply_redaction_outcomes`
  (`session.py:2210`) mirrors what `regenerate_uid()` does in the worker.

So the de-dup key is a field redaction is *required* to clear. Provenance and
"where the current bytes are" are two facts wearing one name.

---

## 2. The three open calls, decided

### 2.1 Call 1 — a provenance field: **yes**

`Instance.source_path`: *the file this instance's bytes were first read from.*
Set once, at construction, never cleared. `file_path` keeps its exact present
meaning — *the file whose bytes match this instance right now* — and keeps
being cleared by `regenerate_uid()`. The de-dup index moves to `source_path`;
nothing that loads pixels ever looks at it.

Persisted as a **column** on `instances`, not as an `_ISOCENTER_*` attribute,
for three reasons: `file_path` is already a column and provenance is the same
kind of fact; the column is queryable, which is how the store already answers
questions of this shape (`check_unsafe_attributes`); and a filesystem path can
contain PHI (a folder named for a patient), so it must not enter
`Instance.attributes`, which flows into `get_cohort_report(expand_metadata=
True)` and into the export merge. The column exposes exactly what `file_path`
already exposes and nothing more.

**Rejected — keep `file_path` and teach `get_pixel_data()` not to trust it.**
That is the load-bearing comment in `regenerate_uid()` inverted, and it moves
the guard from one place (the field is absent) to every reader of the field.
The issue rejects it and so does this spec.

**Rejected — key de-dup on file content (hash) instead of path.** Correct, and
it costs a read of every candidate file on every ingest, which is the cost
incremental ingest exists to avoid.

**Rejected — a second `_ISOCENTER_SOURCE_PATH` attribute** (cheapest: rides
`attributes_json`, no schema change). Puts a possibly-PHI-bearing path into the
attribute dict; see above.

### 2.2 Call 2 — a second `ingest()` landing a recorded pre-redaction identity: **decline the file, say so, and grade it**

`source_path` fixes "the same folder, again". It cannot fix "the same file,
reached by another path" — a copy, a move, or a symlinked mount (§D.1 measures
the last one producing two instances with the *identical* SOP Instance UID
today). So the fix carries a second, content-keyed gate:

* `regenerate_uid()` records the identity it retires, once, under
  `_ISOCENTER_SOURCE_SOP_UID`.
* At ingest, a parsed instance whose SOP Instance UID is some live instance's
  recorded pre-redaction identity is **not linked into the graph**, and a
  `WARNING` audit row is written naming the file, the superseded UID and the
  redacted instance that supersedes it.

**Decline, not warn-and-add.** Adding it back is what puts the un-redacted
frame in the store, in the sidecar and in the export; a report row on top of
that is a louder version of the same defect. Declining cannot lose data the
session does not already hold: a SOP Instance UID is globally unique, so a file
carrying UID *X* *is* the image already indexed under *X*. (A file that carries
*X* and is not that image is a duplicate-UID store, which is #197 and not this
fix's problem — see §8.)

**Decline, not raise.** `ingest()` is a bulk operation over a folder. Raising
aborts the import of every other file in it, which is a large penalty for the
one condition where the tool already knows the right answer.

**The channel is a `WARNING` audit row, and that is not a new channel.**
Measured:

```
$ PYTHONPATH=<worktree> $SCRATCH/venv312/bin/python $SCRATCH/repro238c.py
=== LEG D: WARNING audit row ===
  before: validation = REVIEW_REQUIRED audit = {}
  after:  validation = REVIEW_REQUIRED
  audit summary: {'ANONYMIZE': 1, 'WARNING': 1}
  section4: Exceptions & Errors … | WARNING | probe: supe…
  section2: Processing Audit … | ANONYMIZE | 1 | | WARNING | 1 |
```

`SqliteStore.get_audit_errors()` selects `action_type IN ('ERROR','WARNING')`
(`persistence.py:762-780`), so a `WARNING` row lands in section 4 *and* in
section 2's counts, and `validation_status` is `REVIEW_REQUIRED` even with
other audit rows present. `WARNING` is declared there and currently emitted by
nothing. **No renderer change, no new `ComplianceReport` field, no section
renumbering** — which is also why this cannot collide with #167 (§8).

**Rejected — `RISK`.** `RISK` is what `scan_burned_in_annotations` writes; it
is counted in section 2 and never graded. A declined re-import that the
operator never sees is the failure mode this issue is filed under.

**Rejected — a new graded action type + getter + report subsection**, the
`SCAN_GAP` shape. Justified there because a scan gap is a claim no existing
section makes ("content is in the output and was not read"). Here the claim —
"something happened during processing that you should look at" — is exactly
what section 4 says on its own heading. A second answer to that question is the
thing this codebase deletes.

**Rejected — silent skip.** It is the correct data outcome with the wrong
report, i.e. the milestone's defect with the polarity reversed: the operator's
source folder still holds un-redacted PHI that their pipeline keeps re-feeding,
and nothing tells them.

### 2.3 Call 3 — `_scan_before_export` flagging an uncovered burned-in identifier: **no**

No change to `_scan_before_export` (`session.py:2479`). Three measured
reasons:

1. **What can be checked is already checked.** Leg C: an instance declaring
   `(0028,0301) = YES` that no rule covers already reaches section 4 as a
   `COMPLIANCE_CHECK` exception and already grades `REVIEW_REQUIRED`. A second
   emitter for the same fact is a duplicate row, not a new signal.
2. **What is not already checked needs OCR.** The only thing distinguishing
   the issue's fixture from a clean image is the pixel content;
   `pixel_analysis.HAS_OCR` is an optional extra that must degrade gracefully,
   so a grade that depended on it would differ between two installs of the same
   version — the same class of divergence as #228, arrived at deliberately.
3. **A rule-coverage test is silent exactly when it is needed.** "No rule in
   force covers it" is only computable when rules are in force, and the
   measured worst case (§1.2) is a session that cleared them. Leg B shows what
   happens when they *are* in force: the export worker re-applies the zones, so
   a pixel-level check at export would fire on an instance whose exported
   pixels are clean, while the real defect there is a metadata one — a
   duplicate file with `ImageType=None, BurnedIn=None` claiming to be an
   original.

Given the §2.2 gate, the un-redacted original does not reach the graph, so
there is nothing for an export-time net to catch on this path. Declining also
keeps this fix clear of #191: no new export-time signal, nothing that wants an
`ExportSummary` to live in.

---

## 3. Design

Two records and two gates.

| | record | set where | persisted as | read by |
|---|---|---|---|---|
| path provenance | `Instance.source_path` | construction (`__post_init__` derives it from `file_path`) | `instances.source_path` column | `DicomStore.get_ingested_paths()` |
| identity provenance | `attributes["_ISOCENTER_SOURCE_SOP_UID"]` | `regenerate_uid()`, mirrored in `_apply_redaction_outcomes` | `attributes_json` (existing) | `DicomStore.get_superseded_uids()` |

Gate 1 (path) filters candidate files before any file is parsed, exactly where
`known_files` filters today. Gate 2 (identity) filters parsed instances in the
aggregation loop, **before the sidecar write**, so a declined file leaves no
bytes behind.

Three invariants the coder must not trade away:

* **`file_path` is never assigned from `source_path`.** The two exist because
  they answer different questions.
* **`_ISOCENTER_SOURCE_SOP_UID` is written once** — the *first* identity, the
  one a file on disk can carry. A later `redact(force=True)` must not overwrite
  it with an intermediate generated UID that exists in no file.
* **The identity record is assigned in two places on purpose.**
  `regenerate_uid()` covers the serial path and the free-threaded path (where
  the worker *is* the parent's object); `_apply_redaction_outcomes` covers the
  process path (where the worker mutated a copy). This is the same shape, for
  the same reason, as the `file_path = None` pair the #228 comment already
  describes at `session.py:2208-2210`.

---

## 4. File-by-file changes

### 4.1 `isocenter/entities.py`

**(a)** Module-scope constant, beside `_canonical_tag` (after line 27):

```python
#: Attribute key under which an instance records the SOP Instance UID it
#: carried before `regenerate_uid()` first replaced it.
#:
#: Assigned into `attributes` **directly, never through `set_attr`** --
#: `set_attr` runs the key through `_canonical_tag`, which lowercases it,
#: and every reader spells it upper-case. `_ISOCENTER_REDACTION_HASH` is
#: written the same way in `services.py` for the same reason; it stays a
#: bare literal at its five sites there because renaming them is not this
#: fix.
SOURCE_SOP_UID_ATTR = "_ISOCENTER_SOURCE_SOP_UID"
```

**(b)** New field on `Instance`, immediately after `file_path`
(`entities.py:331`):

```python
    # Persistence: the file this instance was *read from*, which stays
    # true after redaction detaches `file_path`.
    #
    # `file_path` answers "where are bytes that match this instance now",
    # and `regenerate_uid()` must clear it -- `get_pixel_data()` falls
    # back to it, and a redacted instance that still pointed at its
    # source would silently reload the un-redacted frame. `source_path`
    # answers "which file did this come from", which redaction does not
    # change. Ingest de-duplication keys on this one (#238).
    #
    # Never read to load pixels. Nothing may assign `file_path` from it.
    source_path: Optional[str] = None
```

**(c)** `Instance.__post_init__` (line 355) — derive it, before the `set_attr`
calls:

```python
        # An instance constructed from a file records that file as its
        # origin, structurally rather than by convention: every
        # construction site that knows a path passes `file_path`, and a
        # site that had to remember a second argument is a site that can
        # forget one. `and not self.source_path` is what lets an
        # explicit value win -- passed here, or assigned straight
        # afterwards, which is how the store's load path restores the
        # origin of an instance whose `file_path` redaction cleared.
        if self.file_path and not self.source_path:
            self.source_path = self.file_path
```

**(d)** `regenerate_uid()` (line 364) — record the identity being retired.
Capture *before* step 1, write *after* step 3, and only if absent:

```python
        previous_uid = self.sop_instance_uid
        ...
        # 4. Record the identity this instance is leaving behind, once.
        #
        # Only the first one. The UIDs generated here exist in no file,
        # so recording a later one would replace the single value a
        # re-ingested source file could actually carry -- which is what
        # the ingest gate matches on (#238). A second redaction
        # (`force=True`, #237) must therefore leave this alone.
        #
        # Direct assignment, not `set_attr`: `set_attr` lowercases the
        # key. The revision already moved on the `set_attr` above, so
        # the store still sees this instance as unsaved.
        if previous_uid and SOURCE_SOP_UID_ATTR not in self.attributes:
            self.attributes[SOURCE_SOP_UID_ATTR] = previous_uid

        # 5. Detach from physical file  (existing step 4, unchanged)
        self.file_path = None
```

`source_path` is **not** touched here. That is the point of it.

### 4.2 `isocenter/store.py`

**(a)** Rename `get_known_files()` → `get_ingested_paths()` and re-key it.
Delete the old name; do not alias it (pre-1.0 convention, `tests/
test_api_coherence.py`). One production consumer, `io_handlers.py:522`.

```python
    def get_ingested_paths(self) -> Set[str]:
        """Every file path this store has imported, for ingest de-duplication.

        Keyed on `Instance.source_path`, not `file_path`. It was
        `file_path` until #238, and `regenerate_uid()` clears that, so a
        redacted instance stopped contributing its source path and the
        next `ingest()` of the same folder re-added the un-redacted
        original as a second instance.

        **A path in this set does not mean the file matches the
        instance.** For a redacted instance it means the opposite: the
        file still holds the burned-in identifier. This set answers
        "have I imported this file before" and nothing else -- do not
        reuse it to decide what can be read back off disk. That is what
        `file_path` is for, and it is absent precisely where it would be
        wrong.

        Returns:
            Set[str]: Absolute paths, one per instance that came from a file.
        """
        files = set()
        for p in self.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        if inst.source_path:
                            files.add(os.path.abspath(inst.source_path))
        return files
```

`file_path` is deliberately not consulted as a fallback: no production site
assigns `file_path` after construction (the only two assignments set it to
`None`), so `__post_init__` has already mirrored it into `source_path`, and a
fallback here would be dead code re-asserting the reading this method's
docstring denies.

Tests *do* assign `file_path` after construction — `grep -n "\.file_path = "
tests/` finds 20 sites, among them `test_redaction_identity.py:113,225`,
`test_session.py:59`, `test_entities.py:44`, `test_export_pixels.py:36`,
`test_float_pixel_data_export.py:1437` and six in
`test_redaction_failure_is_reported.py`. Every one of them assigns a path so
the *lazy loader* can find a file, and none of those tests calls `ingest()`
afterwards or reads the de-duplication set; no test calls `get_known_files()`
at all. So those instances now contribute nothing to `get_ingested_paths()`,
invisibly, and are expected to stay green. If one goes red, it is reading the
de-dup set for something that is not de-duplication — read it before
"fixing" it with a fallback.

**(b)** New method beside it:

```python
    def get_superseded_uids(self) -> Dict[str, str]:
        """Pre-redaction identities, mapped to the instance that holds them now.

        `regenerate_uid()` records the SOP Instance UID an instance
        carried before redaction gave it a new one. A file offered to
        `ingest()` under one of these UIDs is the un-redacted original of
        an image this store already holds, reached by a path
        de-duplication did not recognise -- a copy, a move, or a
        symlinked mount. `DicomImporter.import_files` declines it (#238).

        Deliberately narrow. This is *not* "every UID in the store": a
        map that answered that would make the ingest gate refuse every
        re-offered file, including files this store has never seen, and
        would be a second, worse answer to #197.

        Returns:
            Dict[str, str]: pre-redaction UID -> the current SOP Instance
            UID of the instance that recorded it.
        """
        superseded = {}
        for p in self.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        original = inst.attributes.get(SOURCE_SOP_UID_ATTR)
                        if original and original != inst.sop_instance_uid:
                            superseded[original] = inst.sop_instance_uid
        return superseded
```

Import `SOURCE_SOP_UID_ATTR` from `.entities` (the module already imports
`Patient, Equipment` from there) and `Dict` from `typing`.

### 4.3 `isocenter/io_handlers.py`

**(a)** Line 522-523 — the path gate:

```python
        known_paths = store.get_ingested_paths()
        new_files = [fp for fp in all_files
                     if os.path.abspath(fp) not in known_paths]
```

**(b)** Line 394 (`ingest_worker`) — **no change**. `Instance(meta['sop'],
meta['sop_class'], 0, file_path=fp)` gets `source_path` from `__post_init__`,
and the field crosses the process boundary with the rest of the slots (§4.7).

**(c)** The identity gate, as the **first** statement inside the `try:` at
line 569, above the sidecar write. `superseded` and `declined` are bound beside
`count = 0` (line 561), i.e. once, before the aggregation loop: nothing this
loop appends to the graph carries a retired identity, so a snapshot taken
before it cannot go stale during it.

```python
        superseded = store.get_superseded_uids()
        declined = 0
        count = 0
        for meta, inst, … in results:
            …
            if inst:
                try:
                    # Above the sidecar write, deliberately. This file is
                    # the un-redacted original of an image the store
                    # already holds in redacted form -- it kept its SOP
                    # Instance UID while the redacted copy took a
                    # generated one (#228), so nothing else in the graph
                    # can tell they are the same image. Linking it back
                    # in puts the burned-in identifier into the store,
                    # the sidecar and the export (#238), and
                    # `persist_pixel_data` does not de-duplicate, so a
                    # write here would also strand the frame (#235).
                    supersedes = superseded.get(inst.sop_instance_uid)
                    if supersedes:
                        detail = (
                            f"Not importing {inst.file_path}: SOP Instance "
                            f"UID {inst.sop_instance_uid} is the "
                            f"pre-redaction identity of {supersedes}, which "
                            f"this session already holds. The file still "
                            f"carries the un-redacted original.")
                        declined += 1
                        # First five individually, as
                        # `scan_burned_in_annotations` does: a re-run over
                        # a large redacted cohort would otherwise print a
                        # line per file. The audit row is per file
                        # regardless -- it is the compliance trail, and
                        # DATA_LOSS rows are per instance for the same
                        # reason.
                        if declined <= 5:
                            logger.warning(detail)
                        elif declined == 6:
                            logger.warning(
                                "... (suppressing further per-file messages "
                                "for superseded sources) ...")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="WARNING",
                                entity_uid=inst.sop_instance_uid,
                                details=detail)
                        continue
```

`WARNING` is chosen because `get_audit_errors()` selects it, which puts the row
in section 4 and takes the grade to `REVIEW_REQUIRED` with no renderer change
(measured, §2.2). It is written **in the parent**: `import_files` runs there,
so this is not the worker-audit hazard of #126.

After the loop, beside the existing `Successfully ingested` line:

```python
        if declined:
            logger.warning(
                f"Declined {declined} file(s) whose SOP Instance UID is the "
                "pre-redaction identity of an instance already in this "
                "session; see the compliance report.")
```

### 4.4 `isocenter/persistence.py`

**(a)** `SCHEMA`, `instances` table (line ~329), immediately after `file_path
TEXT`:

```sql
        source_path TEXT,
```

**(b)** `_UPSERT_INSTANCE_SQL` (line 47): add `source_path` to the column list
**immediately after `file_path`**, one more `?` in `VALUES`, and in the
conflict clause:

```sql
        source_path=COALESCE(excluded.source_path, instances.source_path),
```

`COALESCE`, matching the pixel columns above it, and for the same reason: a
later write that does not know an instance's origin (a graph built by
`DicomBuilder`, re-saved over an ingested row) must not erase one that was
recorded.

**(c)** `_add_missing_columns` (line 526): a new guarded ALTER after the
`phi_status` loop. `CREATE TABLE IF NOT EXISTS` leaves an existing store's
table alone, and both load sites `SELECT *`, so without this a store created by
0.9.0 raises `IndexError: No item with that key` on `r['source_path']`.

```python
        # `source_path` on instances (#238). Rows predating the column
        # read NULL; for an un-redacted instance `Instance.__post_init__`
        # re-derives it from `file_path` on load, so only instances
        # already redacted in an older release stay without provenance
        # -- their `file_path` was cleared before anything recorded it,
        # and nothing here can recover it.
        instance_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(instances)").fetchall()}
        if "source_path" not in instance_columns:
            conn.execute("ALTER TABLE instances ADD COLUMN source_path TEXT")
```

**(d)** `_build_instance_writes` (line 1928): add `inst.source_path` to the row
tuple **immediately after `inst.file_path`**, matching (b)'s column position.
The tuple and the column list are positional and nothing checks them against
each other, so an insertion in one place and not the other writes the sidecar
offset into the path column without erroring.

**(e)** Both hydration sites — `load_all` (line 946) and `load_patient`
(line 1074) — after the `Instance(...)` construction:

```python
                    # After construction, so a stored value wins over the
                    # `file_path` derivation in `__post_init__`. For a
                    # redacted instance `file_path` is NULL and this is
                    # the only thing that brings its origin back; without
                    # it the field is memory-only, ingest de-duplication
                    # is correct in the session that redacted and wrong
                    # in every session that reopens the store (#238).
                    if r['source_path']:
                        inst.source_path = r['source_path']
```

`r['source_path']` on a `sqlite3.Row` raises `IndexError` if the column is
absent, so the ALTER in (c) has to have run first. It always has:
`SqliteStore.__init__` calls `_init_db()` unconditionally
(`persistence.py:427`), before the audit thread and before anything can hold
the store. `__setstate__` (line 459) deliberately does **not** re-run it in a
child process — and does not need to, because no worker hydrates: `load_all()`
and `load_patient()` have exactly two callers, `session.py:552` and
`session.py:62`, both in the parent, and the parent constructed the store
through `__init__`. Do not add a `r.keys()` guard; it would be a second, silent
answer to "does this store have the column", and the one place that answers it
is `_add_missing_columns`.

**(f)** `get_flattened_instances` (line ~2096): **no change.** Its select list
is a published row shape (#164) and nothing in the flattened row consumes
provenance. Adding a column there is a separate decision with its own
consumers.

### 4.5 `isocenter/session.py`

**(a)** `_apply_redaction_outcomes`, inside the `if new_uid:` block
(line 2178-2210), with the other identity statements — after the two UID
assignments, before `instance.file_path = None`:

```python
                # Recorded here as well as in `regenerate_uid()`, and
                # both are needed. Under threads the worker *is* this
                # object and has already written it; under processes it
                # wrote it on a copy that is discarded, and `sop` --
                # `mutation['original_sop_uid']` -- is this process's own
                # authority for the same fact. `setdefault` makes the
                # two paths agree and keeps a `force=True` re-redaction
                # from replacing the original identity with a generated
                # one (#237, #238). Same shape and same reason as the
                # `file_path = None` below it (#228).
                instance.attributes.setdefault(SOURCE_SOP_UID_ATTR, sop)
```

Import `SOURCE_SOP_UID_ATTR` from `.entities` at the top of `session.py`.

**(b)** `_make_lightweight_copy` (line 2824): **no assignment**, one comment
after the `Instance(...)` construction:

```python
                    # `source_path` is not carried across deliberately.
                    # `__post_init__` derives it from the `file_path`
                    # above, which is what a scan worker would see
                    # anyway; the clone is read by `scan_worker` and
                    # discarded, and no finding carries provenance back.
                    # If a clone is ever written to the store, this is
                    # the line that has to change first (#238).
```

The redaction path is unaffected: `prepare_redaction_tasks` puts the **live**
`Instance` into the task dict, and slots pickle whole (§4.7).

### 4.6 Tests and comments elsewhere

* `tests/test_shared_executor_lifecycle.py:96` — the comment names
  `DicomStore.get_known_files`. Update to `get_ingested_paths`. (The store
  there is real, not a mock; only the comment is stale.)
* `CLAUDE.md` does not name either method; `tests/test_claude_md_api_names.py`
  pins only the "Object graph and dirty tracking" section. No change.
* `docs/superpowers/specs/2026-08-31-redaction-attestation.md:553` mentions
  `get_known_files()` — a historical spec. Leave it.
* No page under `docs/` states the de-duplication rule, so none becomes false.
  Do not add one.
* `DicomImporter.import_files` has one production caller (`session.py:860`)
  and two direct test callers — `tests/test_io.py:22` and
  `tests/test_recursive_import.py:46` — which pass a bare `DicomStore` and
  **neither** a `sidecar_manager` nor a `store_backend`. That is why the audit
  write is guarded by `if store_backend is not None:` and why gate 2 must not
  assume either exists.

### 4.7 Things measured to need **no** change

Instance slots round-trip through pickle unaided, so a new slot needs no
`__getstate__` work:

```
$ PYTHONPATH=<worktree> $SCRATCH/venv312/bin/python $SCRATCH/repro238c.py
=== LEG E: Instance pickling ===
  slots: ['sop_instance_uid', 'sop_class_uid', 'instance_number', 'file_path',
          'pixel_array', '_pixel_loader', '_pixel_hash', 'waveform_array',
          '_waveform_loader', '_waveform_hash', 'date_shifted', 'attributes',
          'sequences', '_revision', '_persisted_revision', '_phi_status',
          '_phi_status_revision']
  file_path round-trip: /tmp/x.dcm date_shifted: True revision: 6 A^B
```

(no `MISMATCH` line printed — every slot compared equal after
`pickle.loads(pickle.dumps(inst))`).

The new attribute survives the privacy sweep by construction:
`PhiInspector._scan_instance`'s private-tag sweep does
`group_str, _ = tag.split(',')` inside `try: … except ValueError: pass`
(`privacy.py:369-390`), so a key with no comma is skipped; the configured-tag
pass matches exact tag keys. It stays out of exported files through
`DicomExporter._merge`'s `if t.startswith("_") or "," not in t: continue`
(`io_handlers.py:2250`) — the same route `_ISOCENTER_REDACTION_HASH` takes.
`_split_core_and_private` (`persistence.py:229`) keeps unparseable keys in the
core JSON, so it persists in `attributes_json` and reloads through
`_deserialize_into`.

---

## 5. Test plan

New file `tests/test_reingest_after_redact.py`. Every test builds its source
file on disk and ingests it, because the values under test — `source_path`,
`_ISOCENTER_SOURCE_SOP_UID` — must be produced by the code, never handed in by
a fixture. Where the executor can change the answer, parametrise over
`LEVERS = ["ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES"]`, as
`tests/test_redaction_identity.py` does.

### Detection — red on `65dcb2e`

| # | Test | Catches | Red on main because |
|---|---|---|---|
| T1 | `test_reingesting_a_redacted_folder_adds_nothing` (× LEVERS) — ingest → redact → ingest, assert exactly one instance and that its UID is not the source's | the whole defect, through public API only | **behaviourally**: two instances, measured §1.2 on both interpreters |
| T2 | `test_redaction_keeps_the_source_path_and_still_drops_the_file_path` — after redact, `inst.source_path == str(src_file)` **and** `inst.file_path is None` | the field exists, redaction does not clear it, and `file_path` was not resurrected | `AttributeError: 'Instance' object has no attribute 'source_path'` |
| T3 | `test_provenance_survives_a_save_and_reload` — ingest → redact → `save()` → `close()` → reopen; assert `store.get_ingested_paths() == {abspath(src)}` and the instance's `source_path` | the persistence round-trip: schema, upsert row, ALTER, both load sites | method absent; and `get_known_files()` returns `set()` after reload, measured §1.3 |
| T4 | `test_reingesting_after_a_reload_adds_nothing` — same as T3 then `ingest(src)`; assert one instance | that the field is not memory-only — the pre-#84 shape the brief warns about. T1 passes with a memory-only field; this does not | behaviourally: two instances |
| T5 | `test_a_copy_of_the_source_at_another_path_is_declined` — ingest → redact → copy `a.dcm` into a **second directory** → `ingest(dir2)`; assert one instance and a `WARNING` audit row whose details name both UIDs | the identity gate. The copy must be in another directory: with it in the same one the path gate answers first and the test passes for the wrong reason | behaviourally: two instances |
| T6 | `test_the_declined_import_reaches_the_compliance_report` — T5's flow, then `generate_report`; assert the superseded UID appears in section 4 and `validation_status` is `REVIEW_REQUIRED` | the channel: `WARNING`, not a row the report only counts | no such row exists |
| T7 | `test_a_declined_import_writes_nothing_to_the_sidecar` — record `os.path.getsize(session.store_backend.sidecar.filepath)` **and** the `instance_blobs` row count before the second ingest; assert both unchanged after | gate placement. Moving the `continue` below the pixel write leaves this red while T5 stays green | behaviourally: the sidecar grows and a second blob row appears |
| T8 | `test_the_pre_redaction_identity_is_recorded_under_either_executor` (× LEVERS) — assert `inst.attributes[SOURCE_SOP_UID_ATTR] == source_uid` after redact | the two-site assignment. Deleting the line in `_apply_redaction_outcomes` reddens the processes leg only; deleting it in `regenerate_uid()` reddens the threads leg only — the #228 shape | `KeyError` |
| T9 | `test_a_forced_second_redaction_keeps_the_first_identity` — redact, then `redact(force=True)`, assert the recorded UID is still the source file's | `setdefault`/`not in` semantics. An unconditional assignment records a generated UID no file carries, and gate 2 stops matching the real source | `KeyError` |

T7's size assertion depends on the session supplying a live
`sidecar_manager`: the write it is guarding is
`if p_bytes and sidecar_manager:` (`io_handlers.py:570`), and
`session.ingest()` passes `self.store_backend.sidecar`. Say that in the test's
docstring. Rewriting the fixture to call `DicomImporter.import_files` directly
without a manager — as several other tests do — makes T7 vacuous rather than
red, and the blob-row half is what says *why* the sidecar grew.

T1, T4, T5, T7 are the behavioural ones — red without touching any new API.
T2, T3, T8, T9 are red by attribute/key absence, which is weaker evidence on
its own; they are there to pin clauses T1 cannot distinguish.

### Selectivity guards — green before and after

| # | Test | Guards against |
|---|---|---|
| S1 | `test_a_genuinely_new_file_is_still_ingested_after_a_redaction` — ingest → redact → write a *different* SOP Instance UID into a new directory → ingest; assert two instances | `get_superseded_uids()` or `get_ingested_paths()` matching too broadly. Without this, a map returning every UID in the store passes the entire detection set |
| S2 | `test_an_instance_no_zone_touched_records_no_provenance_change` — off-image zone (`[100,108,100,108]`, as `test_redaction_identity.py` uses); assert `SOURCE_SOP_UID_ATTR not in inst.attributes`, `inst.file_path` still set, `source_path == file_path` | recording a retired identity for an instance that was never re-identified, which would make that instance's own source file permanently un-re-ingestable — a false refusal. Vacuously green on main (the key never exists there) |
| S3 | `test_the_recorded_identity_does_not_reach_the_exported_file` — export after redaction; assert `b"_ISOCENTER_SOURCE_SOP_UID"` is not in the written file's bytes and `dcmread` still parses it | the `_`-prefix filter in `DicomExporter._merge`. Vacuously green on main |
| S4 | `test_ingesting_an_unredacted_folder_twice_is_still_silent` — ingest → ingest, assert one instance and `get_audit_errors() == []` | gate 2 firing on ordinary incremental ingest, which would stamp `REVIEW_REQUIRED` on every session that re-scans its inbox |

### Suite-level

* Baseline on `65dcb2e`, measured in this worktree (`find . -name .DS_Store
  -delete` first; clean tree, #245):

  ```
  $ PYTHONPATH=<worktree> $SCRATCH/venv312/bin/python -m pytest -q
  1010 passed in 251.45s (0:04:11)
  ```

  The full suite must pass on **3.12.13 and 3.14.7t** after the change.
  (`pytest --timeout=` is not available here — `pytest-timeout` is not
  installed; `-q` alone.)
* No existing test does `ingest()` → `redact()` → `ingest()` on one session:
  of the 14 files calling `.ingest()` more than once, only four mention
  redaction (`test_api_coherence.py`, `test_float_pixel_data_export.py`,
  `test_pixel_geometry_pipeline.py`, `test_profile_end_to_end.py`) and in each
  the calls are in separate test functions over separate directories. Expect
  no fallout there — confirm rather than assume.
* `tests/test_pixel_geometry_pipeline.py` asserts `inst.attributes == before`
  in five places. All five are instances redaction did **not** modify, and
  `regenerate_uid()` is the only writer of the new key, so they stay green.
  If one goes red, the gate has widened and that is the bug, not the test.
* `pylint isocenter` ≥ 8.5.

---

## 6. CHANGELOG

Under `### Fixed`, house depth — the exact behaviour a previously-working call
now produces:

* **Re-ingesting a folder after `redact()` no longer re-adds the un-redacted
  source (#238).** `Instance.regenerate_uid()` clears `file_path` — it must, or
  `get_pixel_data()` would reload the un-redacted frame — and
  `DicomStore.get_known_files()` built the incremental-ingest de-duplication
  set out of that same field. A redacted instance therefore stopped
  contributing its source path, and a second `session.ingest(folder)` re-added
  the original as a distinct instance: it keeps its SOP Instance UID while the
  redacted copy carries a generated one, so no key in the store relates them.
  Measured on 3.12.13 and 3.14.7t, both exporting two files, the source's
  carrying its burned-in identifier intact, under a compliance report naming
  nothing. De-duplication now keys on `Instance.source_path`, a new field
  recording the file an instance was read from, which redaction does not
  clear and which is persisted in a new `instances.source_path` column (added
  to existing databases by `ALTER TABLE` on open).
  * `DicomStore.get_known_files()` is **renamed** `get_ingested_paths()`.
    `store.get_known_files()` now raises `AttributeError: 'DicomStore' object
    has no attribute 'get_known_files'`. The rename is the behaviour change: a
    path in the returned set no longer implies the file matches the instance —
    for a redacted instance it means the opposite.
  * A second `ingest()` that offers the **pre-redaction original of an image
    the session already holds** — recognised by SOP Instance UID, so a copy at
    another path or through a symlinked mount is caught too — no longer adds
    it. The file is skipped, a `WARNING` is logged, and a `WARNING` audit row
    per file names it in section 4 of the compliance report, taking the run to
    `REVIEW_REQUIRED`. `ingest()` still returns normally; nothing raises.
  * Instances redacted by 0.9.0 or earlier and saved have no recorded origin —
    their `file_path` was cleared before any release recorded it — so a store
    reopened from that state still re-imports its source folder once. There is
    nothing in the database to recover it from. Re-running `redact()` on such
    a store does not repair it either; the redacted pixels are already the
    store's own.
  * This fix does not clean up a store that was already polluted. An
    un-redacted original that a previous run re-added is a live instance, and
    removing it is a data decision this release does not make for you.

---

## 7. Verification the coder must run and paste

1. `find . -name .DS_Store -delete`
2. `$SCRATCH/venv312/bin/python -m pytest -q` → 1010 + the new tests, 0 failed.
3. `$SCRATCH/venv314t/bin/python -m pytest -q` → same.
4. Both interpreters on the issue fixture: `PYTHONPATH=<worktree>
   <python> $SCRATCH/repro238.py` → `after 2nd ingest: instances = [<one
   generated UID>]`, `exported:` one file, its zone sum `0`.
5. `repro238f.py` after the change → `known_files after reload:
   {'…/src/a.dcm'}` (it prints `set()` on main; rename the call in the script
   when you rename the method).
6. Legacy-store check, by hand: create a store on `65dcb2e`, ingest, save,
   close; open it with the changed code and confirm `_add_missing_columns` adds
   the column and both load paths hydrate without raising.
7. `pylint isocenter`.

---

## 8. Interaction with adjacent work

* **#167 (`SCAN_GAP`, branch `code/167-private-sequence-implicit-vr`)** —
  disjoint by construction. That branch adds an action type, a
  `get_audit_scan_gaps()` reader, a `ComplianceReport.scan_gaps` field, a grade
  term and report subsections 3.1/3.2. This fix adds **no** report field, no
  getter and no renderer line; it reuses `WARNING`, which `get_audit_errors()`
  already selects into section 4. If #167 lands first, the only shared file is
  `session.py`'s `generate_report` — and this fix does not touch it. Merge in
  either order.
* **#197 (duplicate SOP Instance UID accounting)** — not built here, and this
  fix is deliberately narrower than it. Gate 2 keys on *recorded pre-redaction
  identities only*, never on "a UID already in the store". A store can hold two
  instances with the same SOP Instance UID by other routes (§D.1 measures one),
  and answering that is #197's job. If #197 later introduces a general
  duplicate-UID index, `get_superseded_uids()` should be re-expressed in terms
  of it rather than duplicated — the note belongs in #197's issue thread.
* **#191 (`export()` returning `None` vs an `ExportSummary`)** — untouched.
  Deciding call 3 as "no" is partly what keeps it untouched: there is no new
  export-time signal wanting a return value to live in.
* **#228/#246** — this fix extends the identity block those built, in the same
  place and with the same "assign in the parent" reasoning. The new `setdefault`
  sits inside the same `if new_uid:` gate; do not add a second condition beside
  it.
* **#237 (`force=True`)** — `redact(force=True)` re-redacts and takes another
  new UID. §3's write-once rule is what keeps that from destroying the record
  of the original identity; T9 pins it.

---

## 9. Non-goals and deferrals

1. **Repairing an already-polluted store.** Removing an un-redacted original a
   previous run re-added is a data decision (which of the two instances is the
   cohort?) and it belongs to the operator.
2. **Legacy stores redacted before this release.** Unrecoverable, by
   construction — documented in the CHANGELOG rather than guessed at.
3. **`os.path.abspath` → `os.path.realpath` in the path gate.** One line, and a
   real de-duplication hole (§D.1), but it changes matching semantics for every
   store with symlinked mounts and deserves its own issue and its own tests.
   Gate 2 already catches the *dangerous* half of it — the post-redaction case.
4. **An escape hatch for a deliberate re-import** (`ingest(..., force=True)`).
   No caller has asked; adding a parameter that re-admits un-redacted PHI ahead
   of a use case is the wrong default to invent.
5. **`get_flattened_instances`, `generate_manifest` and the cohort report**
   exposing `source_path`. Each is a published shape with its own consumers.
6. **Report verbosity.** One section-4 row per declined file means a large
   re-run produces a large section, exactly as `DATA_LOSS` already does. Any
   aggregation should be decided for both at once, not for this row alone.
7. **OCR-backed burned-in detection at export.** §2.3.
8. **Changing `check_burned_in`'s default.** Out of scope; it is a separate
   argument about export safety defaults.

---

## §D. Unfiled findings

Found while measuring this; **none is fixed by this spec** and none should be
folded into the coder's branch. File them.

**D.1 — the same file reached through a symlink is ingested twice, producing
two instances with the identical SOP Instance UID.** `get_known_files()` /
`get_ingested_paths()` key on `os.path.abspath`, which does not resolve
symlinks:

```
$ PYTHONPATH=<worktree> $SCRATCH/venv312/bin/python $SCRATCH/repro238g.py
=== LEG G: same file under two paths (symlinked dir) ===
  after ingest(real):    ['1.2.3.phi']
  after ingest(symlink): ['1.2.3.phi', '1.2.3.phi'] -> count = 2
  known_files: {'…/real/a.dcm', '…/link/a.dcm'}
```

Two graph instances under one UID; both export to the same filename (#78), so
one overwrites the other. This is #197's territory reached by a route #197 does
not describe. `realpath` would close it (see §9.3).

**D.2 — `session.redact()` writes no audit row on success.** Measured
repeatedly above: `audit summary: {}` after a run that redacted an image. The
serial path (`RedactionService.redact_machine_instances`) writes one
`REDACTION` row per machine; the parallel path — the public `redact()` — writes
rows only for failures and for `scan_burned_in_annotations`' `RISK` findings.
Consequences: section 2 reads "*No audit logs found*" for a session that
redacted, and the run grades `REVIEW_REQUIRED` through the `not audit_summary`
arm — the right grade for the wrong reason, which is exactly the failure mode
this milestone is named after. A run that only redacts and then writes one row
of any other kind would grade `PASS`.

**D.3 — every exported instance emits a pydicom `UserWarning` into the host's
warning stream.** `io_handlers.py:1363` tests `if "_ISOCENTER_REDACTION_HASH"
in ds:`; `Dataset.__contains__` rejects a non-tag key and warns:

```
pydicom/dataset.py:589: UserWarning: Invalid value '_ISOCENTER_REDACTION_HASH'
used with the 'in' operator: must be an element tag as a 2-tuple or int, or an
element keyword
```

(observed on every run of `repro238.py`, once per exported instance). The test
is also unreachable-by-construction: `_merge` skips `_`-prefixed keys, and a
string that is neither a tag nor a DICOM keyword cannot be a `Dataset` member
at all, so the `del` beneath it can never run. Deleting both lines is very
likely a no-op beyond silencing the warning — confirm that when the issue is
worked, rather than on this reading. Under #144 — this package deliberately installs no global
warning filter — this noise lands in the host application.

**D.4 — the manifest prints the string `"None"` as a redacted instance's
file.** `session.py:1615` reads `getattr(inst, 'file_path', "N/A")`; the
attribute exists and is `None`, so the `"N/A"` default never applies and
`str(None)` is stored:

```
$ PYTHONPATH=<worktree> $SCRATCH/venv312/bin/python $SCRATCH/repro238g.py
=== LEG H: manifest File column for a redacted instance ===
  manifest rows: … "sop_instance_uid": "1.2.826…", "file_path": "None", …
```

A manifest is a document handed to someone else. `source_path` would be the
honest thing to show once it exists — under a column name that does not claim
the bytes match — but that is a manifest-schema decision, not this fix.

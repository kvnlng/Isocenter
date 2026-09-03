# Pixel Geometry Authority

**Date:** 2026-08-29
**Status:** Design approved, in implementation
**Amended:** 2026-08-29 (four passes) — see §11. The most consequential:
the original §3.11 was wrong and dropped redacted pixels; §7 never asked
for a test on the line §4.3 calls "the single most important"; and §4.3
left `BitsAllocated` read from `attributes`, contradicting §3.10 and
shipping a silently wrong image.
**Superseded in part:** #217. §9's compatibility table promises
`RedactionService.apply_redaction_to_array` a "new keyword argument,
defaulted; old behaviour when omitted". #217 deleted that default:
`services.py` now reads
`def apply_redaction_to_array(arr, rois, geometry: PixelGeometry) -> bool`
— a required positional. A third-party caller that omits it raises
`TypeError`, not the old behaviour. The clause is marked in place at §6;
this line is here because the clause is nine hundred lines below the
point a reader enters the document.
**Tracking:** #186, #205 (same defect, two entry points). Folds in the same
expression at `services.py:489` and `pixel_analysis.py:209`.
**Base:** `main` at `692218c`

---

## Context

Four places in the codebase decide what an image looks like by reading a
numpy array's `.shape`, and all four break the `(frames, rows, cols)` vs.
`(rows, cols, samples)` ambiguity with the same line:

```python
if shape[-1] in [3, 4]:
```

Both readings are rank 3. The test is a guess, and the information that
settles it — `SamplesPerPixel` (0028,0002) and `NumberOfFrames`
(0028,0008) — is sitting unread in `Instance.attributes` at every one of
the four sites.

This spec replaces the guess with one shared resolver that consults the
attributes, states what happens when the attributes are absent, partial,
or contradicted, and says which of the current behaviours are kept
deliberately.

---

## 1. Reproduction — what actually happens today

All measurements below were taken on `main` at `692218c` with
`/Users/kevin/Developer/Isocenter/.venv/bin/python`, `pydicom 3.0.2`,
`numpy 2.5.2`, against a `MONOCHROME2` Secondary Capture written by hand
and put through the real pipeline. Both filed issues reproduce. Neither
is wrong; they measured **different entry points**, and the difference
matters to the design.

### 1.1 `get_pixel_data()` corrupts the graph (#186)

`ingest → release_memory() → get_pixel_data()`, attributes before and
after that one call:

| source | changed by the read |
| --- | --- |
| 3 frames, 4×4 | SamplesPerPixel 1→4, PhotometricInterpretation MONOCHROME2→RGB, PlanarConfiguration None→0, Rows 4→3 |
| 3 frames, 8×3 | SamplesPerPixel 1→3, PhotometricInterpretation MONOCHROME2→RGB, PlanarConfiguration None→0, Rows 8→3, Columns 3→8 |
| 3 frames, 8×8 | *(nothing — `shape[-1] == 8`, the frames arm is taken)* |

The returned **array is correct in every case** (`np.array_equal` with
the source is `True`). `SidecarPixelLoader.__call__` reshaped it from the
attributes, correctly. `get_pixel_data()` then calls `set_pixel_data(arr)`
"to ensure attributes (rows, cols) are synced", and that sync overwrites
the very attributes the reshape just used. The only thing it can do is
disagree.

**New finding, broader than the filed trigger.** The same line relabels
colour spaces. A single-frame 8×8 `YBR_FULL` instance — no unusual
dimensions at all — comes back from `get_pixel_data()` with
`PhotometricInterpretation` rewritten `YBR_FULL → RGB`, and exports that
way. Every non-RGB colour instance is affected, not only the 3-or-4-column
ones. It is the same line (`if samples >= 3: set_attr("0028,0004", "RGB")`)
and the same rule fixes it, so it is in scope here rather than filed
separately.

### 1.2 The corruption is durable

`ingest → release_memory → get_pixel_data → save → close → reopen`:

```
reloaded from DB: SamplesPerPixel=4, PhotometricInterpretation=RGB,
                  PlanarConfiguration=0, NumberOfFrames=3, Rows=3, Columns=4
```

`set_pixel_data` ends with `mark_modified()`, so a **read** dirties the
entity and the next `save()` writes the wrong geometry to SQLite. The
source file is untouched, but the index that the rest of the pipeline
reads is not.

### 1.3 What reaches disk through `session.export()`

`ingest → export(use_compression=False)`, then `pydicom.dcmread`:

| source | exported header | `ds.pixel_array` |
| --- | --- | --- |
| 3 frames, 4×4 | NoF=3 R=3 C=4 SPP=4 PI=RGB | `ValueError: 'Samples per Pixel' value of '4' is invalid` |
| 2 frames, 8×4 | NoF=2 R=2 C=8 SPP=4 PI=RGB | `ValueError: 'Samples per Pixel' value of '4' is invalid` |
| 3 frames, 8×3 | NoF=3 R=3 C=8 SPP=3 PI=RGB | `AttributeError: Missing required element (0028,0006)` |
| 3 frames, 8×8 | NoF=3 R=8 C=8 SPP=1 | round-trips, equal |
| 1 frame, 4×4 | R=4 C=4 SPP=1 | round-trips, equal |

`session.export()` returns `None`, the worker returns `ok=True`, the run
is counted as written, and the compliance report grades it `PASS`.

**Why the export always takes the undecodable variant.** `_export_dicom`
calls `self.release_memory()` unconditionally before building the export
plan (`session.py`, immediately after `self.save()`, with the comment
about holding pending edits and frames at once). So `inst.pixel_array` is
always `None` in the worker, the worker always reloads through
`get_pixel_data()`, and site 1 always fires inside the worker process
*before* the worker reads `0028,0002` back out. That is why #186 measured
`SPP=4` rather than the `SPP=1` #205 measured.

### 1.4 Site 2 in isolation (#205)

To see the export worker's own heuristic without site 1, the pixels have
to be resident with clean attributes — which through `session.export()`
is impossible. It is exactly the `DicomExporter.write_tree()` path that
`scripts/` uses: a hand-built graph, no session, no `release_memory()`.
`write_tree` on such a graph, `pixel_array` assigned directly:

| source | exported header | `ds.pixel_array` |
| --- | --- | --- |
| 2 frames, 8×4 | NoF=2 R=2 C=8 **SPP=1** | shape `(4, 2, 8)`, **decodes, values wrong** |
| 3 frames, 4×4 | NoF=3 R=3 C=4 SPP=1 | shape `(4, 3, 4)`, decodes, values wrong |
| 3 frames, 8×3 | NoF=3 R=3 C=8 SPP=1 | shape `(3, 3, 8)`, decodes, values wrong |
| 3 frames, 8×8 | NoF=3 R=8 C=8 SPP=1 | round-trips, equal |

This is the worse failure of the two: the file is internally coherent and
decodes cleanly, it simply describes a different image. `Rows` and
`Columns` come from the shape heuristic; `SamplesPerPixel` comes from
`attributes` and is not recomputed, so nothing disagrees loudly.

### 1.5 What the two issue reports get right and wrong

Both are correct. #186's "the export writes `SPP=4`" and #205's "the
export writes `SPP=1`" are the same defect observed through
`session.export()` and through `write_tree()` respectively. Neither report
names `release_memory()` as the thing that decides which variant you see;
an implementer needs that fact to write the tests, because a test that
drives `session.export()` cannot exercise site 2 alone.

### 1.6 Can `verify()` or the compliance report see any of this?

**No.** `session.verify()` is `RedactionVerifier` — an OCR pass comparing
detected text boxes against configured redaction zones. It has no
geometry check. `generate_report` counts entities, replays the audit log,
and lists `DATA_LOSS` rows; nothing was dropped here, so there is no row,
and the run grades `PASS`.

This design does not make the corruption **detectable**; it makes it
**unreachable**, and routes the one remaining unresolvable case into the
existing export-failure accounting (#181) so it is reported rather than
written. A positive round-trip check — re-`dcmread` each written file and
compare its descriptors against the graph — is a genuinely useful feature
and is listed in §9 to be filed separately.

---

## 2. The four sites

| # | Location | What it does with the guess | In scope |
| --- | --- | --- | --- |
| 1 | `Instance.set_pixel_data`, `entities.py:625-641` | writes Rows/Columns/SamplesPerPixel/NumberOfFrames/PhotometricInterpretation/PlanarConfiguration into `attributes` | yes |
| 2 | `_export_instance_worker`, `io_handlers.py:955-970` | writes `ds.Rows`/`ds.Columns` for the file on disk | yes |
| 3 | `RedactionService.apply_redaction_to_array`, `services.py:489` | chooses which axes a redaction zone's rows and columns address | yes |
| 4 | `analyze_pixels`, `pixel_analysis.py:209` | splits a multi-frame array into frames for OCR | yes |

Site 3 is the most severe of the four and is not mentioned in either
issue: getting the axes wrong applies the zone to the wrong region, so
**burned-in PHI stays in the exported pixels** while the pipeline reports
a successful redaction. Site 4 degrades the burned-in scan: a 3-frame
8×3 grayscale image is handed to OCR as one RGB frame, so text in frames
1 and 2 is never looked at.

Sites 3 and 4 are the same expression as sites 1 and 2 and both have the
`Instance` in hand at the call, so they are folded in rather than filed.
Leaving site 3 out would be actively worse after this change: the export
worker would write correct descriptors around pixels that had been zeroed
in the wrong place.

Two further sites read `shape[-1]` and are **not** in scope, because
neither is guessing:

- `io_handlers.py:1131,1135` (`_compress_j2k`) squeezes singleton axes
  using `ds.NumberOfFrames`/`ds.SamplesPerPixel` — already attribute-first.
  It becomes more correct for free once site 2 writes coherent descriptors.
- `SidecarPixelLoader.__call__` (`io_handlers.py:1245-1274`) reshapes
  purely from stored metadata. It is the model the rest of this design
  follows.

---

## 3. The rule

### 3.1 The principle

> **Attributes are authoritative for *layout* — which axis means what.
> The array is authoritative for *magnitude* — how large each axis is.**

`set_pixel_data` is a setter whose documented job is to update the
descriptors from the array, and callers legitimately hand it a
differently-sized array (`test_compaction.py:106` replaces a `(100,1000)`
array; `test_blob_storage.py:393` replaces `(16,16)` with another
`(16,16)`). Refusing a size change would break that contract. What the
setter must never do is *guess which axis is which* — that is the defect.

### 3.2 The resolver

A new module, `isocenter/pixel_geometry.py`. It imports only
`typing`, `enum`, and `.logger`; it takes a shape tuple and an attributes
dict, so it does not import `entities`, `io_handlers`, or numpy, and no
import cycle is possible in either direction. It adds no third-party
import, so `tests/test_packaging_contract.py` is unaffected.

```python
class GeometryEvidence(Enum):
    DECLARED   = "declared"    # SamplesPerPixel / NumberOfFrames chose the arm
    STRUCTURAL = "structural"  # only one arm was admissible for this rank
    MATCHED    = "matched"     # Rows/Columns broke the tie
    GUESSED    = "guessed"     # nothing resolved it; legacy last-axis heuristic


class PixelGeometry(NamedTuple):
    frames: int
    rows: int
    cols: int
    samples: int
    evidence: GeometryEvidence


def resolve_pixel_geometry(shape, attributes) -> PixelGeometry: ...
```

`PixelGeometry` is a `NamedTuple` so it stays picklable, though nothing in
this design sends one across a process boundary — the resolver is called
inside the worker, and a module-level function in an importable module is
all the child interpreter needs.

**`resolve_pixel_geometry` never raises on `GUESSED`.** It reports the
evidence and lets each caller apply its own policy (§4). It raises
`ValueError` only for a genuine contradiction (§3.6) and for an
unsupported rank.

### 3.3 Reading the declared descriptors

```python
def _declared(attributes, tag):
    """int(...) or None. Absent, empty, unparseable and non-scalar all read
    as None -- 'not declared' -- never as a default."""
    try:
        raw = attributes.get(tag)
    except Exception:
        return None
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None
```

This coercion is load-bearing, not defensive:

- `set_pixel_data` currently writes `str(frames)` for `0028,0008` while
  `ingest_worker` stores an `int`, so a graph that has been through the
  buggy path once has `"3"` where ingest had `3`. A resolver comparing
  raw values would fail the frames check on exactly the instances that
  already went through the bug.
- `tests/test_pixel_analysis.py` drives `analyze_pixels` with a
  `MagicMock` instance, whose `attributes.get(...)` returns a `MagicMock`.
  `int(str(MagicMock))` raises `ValueError`, so it reads as "not
  declared" and the mock keeps working.

**Absent means unknown, not 1.** A hand-built instance that has declared
nothing is precisely the case that needs derivation; collapsing "absent"
into "1" would silently choose the frames arm for every fixture-generator
RGB image.

**Canonical write form.** New writes of `0028,0008` are `int`, matching
`ingest_worker`. The only two places in the tree that write it as a string
are `scripts/generate_ocr_test_data.py:440` (`str(frames)`) and
`set_pixel_data` itself; the former is left alone (it is a fixture
generator writing a source file, and `_declared` parses it), the latter
changes to `int`. No test asserts the string form —
`tests/detect_memory_leak.py:29` and
`scripts/generate_redaction_example.py:147` both use ints.

### 3.4 Candidate arms by rank

| rank | arms | notes |
| --- | --- | --- |
| 0 | — | `ValueError` (unchanged: today's `Unknown shape`) |
| 1 | flat | reshape from declared descriptors — see §3.7 |
| 2 | `(rows, cols)`, samples=1, frames=1 | unambiguous |
| 3 | **A** `(frames, rows, cols)`, samples=1 <br> **B** `(rows, cols, samples)`, frames=1 | the ambiguity |
| 4 | `(frames, rows, cols, samples)` | unambiguous |
| ≥5 | — | `ValueError` (unchanged) |

### 3.5 Rank 3 — the decision procedure

Let `s_d`, `f_d`, `r_d`, `c_d` be the declared SamplesPerPixel,
NumberOfFrames, Rows, Columns (`None` if not declared).

**Step 1 — admissibility (layout evidence only, magnitudes ignored):**

```
A admissible  ⟺  s_d is None or s_d == 1
B admissible  ⟺  (s_d is not None and s_d == shape[2])
              or (s_d is None and shape[2] in (3, 4))
```

Arm B's implicit set stays `{3, 4}`. Narrowing it to `{3}` is tempting —
`samples == 4` guarantees an undecodable file, which is loud — but that
inverts this repo's preference: an undecodable file fails at the consumer
where someone sees it, whereas choosing arm A for genuine RGBA produces a
plausible wrong image, silently. **Considered and rejected.** The
ambiguous case is refused at the export boundary instead (§4.2).

- Exactly one admissible → take it, `evidence = DECLARED` if `s_d` is not
  `None`, else `STRUCTURAL`.
- Neither admissible → contradiction, §3.6.
- Both admissible → step 2. Two ways to get here: `s_d is None and
  shape[2] in (3, 4)`, and `s_d == 1 and shape[2] == 1` — a declared
  single-sample instance whose array's last axis is 1 satisfies arm A
  (`s_d == 1`) and arm B (`s_d == shape[2]`) at once. *(Amended: the
  original text named only the first and said "reachable only when",
  which is over-strong.)*

**Step 2 — NumberOfFrames as tiebreak:**

```
A_ok = (f_d == shape[0])
B_ok = (f_d == 1)
```

If `f_d` is not `None` and exactly one of `A_ok`/`B_ok` holds, take it,
`evidence = DECLARED`. Otherwise step 3.

**Step 3 — Rows/Columns as tiebreak:**

```
A_ok = (r_d is None or r_d == shape[1]) and (c_d is None or c_d == shape[2])
B_ok = (r_d is None or r_d == shape[0]) and (c_d is None or c_d == shape[1])
```

If exactly one holds and at least one of `r_d`, `c_d` is declared, take
it, `evidence = MATCHED`. Otherwise step 4.

**Step 4 — the guess:** arm B if `shape[2] in (3, 4)` else arm A — i.e.
today's heuristic, unchanged — with `evidence = GUESSED`.

**Magnitudes always come from the array**, whichever arm is chosen. A
declared `Rows` that participated in the tiebreak necessarily equals the
array's, so this only matters on the DECLARED and STRUCTURAL paths, where
it is the setter contract of §3.1.

**`frames` is never `f_d`.** This is worth stating on its own, because
`f_d` is the one declared descriptor that both selects an arm (step 2)
*and* names a magnitude, so it is the one an implementer is tempted to
copy into the result. It must not be: `PixelGeometry.frames` is always
`shape[0]` on the frames-major arms and always `1` on the others. A
declared `NumberOfFrames=5` on a `(3,8,8)` array with `SamplesPerPixel=1`
takes arm A by admissibility alone and resolves to `frames=3`, not 5 —
that is the setter overwriting a stale descriptor, exactly as it
overwrites a stale `Rows`.

The consequence is a bound the callers rely on: `geom.frames <=
shape[0]` for every rank and every arm, with equality on the frames-major
arms. §4.5 iterates `range(geom.frames)` over the array's first axis and
is in bounds *because* of this, not by coincidence. The resolver's unit
tests must assert it directly — a case with `f_d` disagreeing with
`shape[0]` in each direction (`f_d=5` on `(3,8,8)`, `f_d=2` on `(7,8,8)`,
both with `SPP=1`), asserting `geom.frames == shape[0]`.

### 3.6 Contradiction

The only way to reach "neither arm admissible" at rank 3 is
`s_d is not None and s_d != 1 and s_d != shape[2]`: the instance declares
*n* samples per pixel and the array has no axis that can hold them.
At rank 4 the equivalent is `s_d is not None and s_d != shape[3]`.

Both raise:

```python
raise ValueError(
    f"Pixel array shape {shape} cannot be reconciled with the instance's "
    f"declared geometry (SamplesPerPixel={s_d}, NumberOfFrames={f_d}, "
    f"Rows={r_d}, Columns={c_d}). No axis of the array can carry "
    f"{s_d} samples per pixel. Correct the array or the attributes; "
    f"they cannot both be right.")
```

**Why raise rather than pick a winner.** Trusting the attributes writes
descriptors that do not describe the bytes — an undecodable or misread
file. Trusting the array is today's behaviour and is how #186 happened.
Logging a `DATA_LOSS` row misnames it: nothing was dropped, two statements
disagree. Raising is the only outcome that neither corrupts nor lies, and
it lands somewhere the pipeline already handles (§4.2).

**Rejected alternative:** raising on *any* disagreement, including a
declared `Rows` that the array contradicts. That would break "replace the
pixel data with a resized array", which `test_compaction.py:106` does and
which is a reasonable thing for the setter to support. Only a *layout*
contradiction raises; a *magnitude* disagreement is the setter doing its
job.

### 3.7 Rank 1 (flat arrays) — unchanged

The existing 1-D branch of `set_pixel_data` already consults
`0028,0010`/`0028,0011`/`0028,0002`/`0028,0008`, truncates DICOM padding,
reshapes and returns without writing anything. Keep its *resolution
logic* verbatim; move it into the resolver only if that is free. Its one
fall-through — declared size is 0, so `rows, cols = 1, shape[0]` — stays.

**Amended 2026-08-29 — this clause and §3.11 rule 2 contradicted each
other, and rule 2 governs.** The rank-1 branch takes an early `return`
before reaching the descriptor writes, so "keep it verbatim" would have
meant a flat array assigned through `set_pixel_data` leaves the instance
clean — `has_unsaved_changes` False on `692218c`, and False after the
fix. That is the §11.1 defect in its purest form: no descriptor changes
because none is written *at all*, yet `self.pixel_array` was replaced and
the store's copy is now stale.

The early return must therefore call `mark_modified()` before returning.
"Unchanged" in this section's heading means the **geometry it derives**
is unchanged, not that the branch is byte-identical. This is a
behavioural change on `692218c` — the only one in the change that is not
a bug fix — and it needs its own test and its own CHANGELOG line.

The sidecar loader returns a 1-D array only when the stored metadata is
too small for the buffer, in which case the resolver's 1-D branch is
consulting the same metadata that just failed. Nothing is gained or lost.

### 3.8 PhotometricInterpretation (0028,0004)

Not derivable from an array at all: a 3-sample array is equally `RGB`,
`YBR_FULL`, `YBR_FULL_422` or `YBR_RCT`. Today's `if samples >= 3: "RGB"`
is what relabels `YBR_FULL` (§1.1).

New rule — **correct only an outright contradiction, and only to the
neutral default:**

```
_MONOCHROME_PI = {"MONOCHROME1", "MONOCHROME2", "PALETTE COLOR"}

pi = declared PhotometricInterpretation (string, stripped, upper-cased)

if pi is absent:
    write "RGB" if samples >= 3 else "MONOCHROME2"
elif samples >= 3 and pi in _MONOCHROME_PI:
    write "RGB"          # a mono PI beside 3 samples is nonconformant
elif samples == 1 and pi not in _MONOCHROME_PI:
    write "MONOCHROME2"  # a colour PI beside 1 sample is nonconformant
else:
    leave it alone       # YBR_FULL, YBR_ICT, RGB, MONOCHROME1 all survive
```

`PALETTE COLOR` is in the monochrome set deliberately: it is
`SamplesPerPixel = 1`.

This keeps `tests/test_entities.py::test_photometric_defaults` green in
all three of its cases, including case 3 ("even if it said MONOCHROME2,
if we pass RGB data it must switch"), while fixing the YBR relabelling.

### 3.9 PlanarConfiguration (0028,0006)

Write `0` only when `samples >= 3` **and** `0028,0006` is not declared.
Never overwrite a declared value.

Today it is forced to `0` whenever `samples >= 3`. That was defensible on
the loader path — `SidecarPixelLoader` transposes planar-1 data to
interleaved before returning it — but the loader path stops calling
`set_pixel_data` under this design (§4.1), and `ingest_worker`
(`io_handlers.py:400`) already normalises `0028,0006` from `1` to `0` at
ingest with the comment "Isocenter internally manages pixels as standard
contiguous arrays (Interleaved)". Measured: an ingested planar-1 RGB
instance has `PlanarConfiguration=0` in `attributes` before
`get_pixel_data()` is ever called, so the forcing has nothing to correct
on any live path.

### 3.10 BitsAllocated (0028,0100) — a deliberate exception

**Decision: this is *not* the same defect. `BitsAllocated` stays derived
from the array.**

The reviewer's observation is accurate: `self.set_attr("0028,0100",
array.itemsize * 8)` silently corrects any declared value, which is why
`tests/test_float_pixel_data_export.py` has to apply its `attrs` *after*
`set_pixel_data` to create a disagreement at all (and says so, at
`:332-338`).

It is still different in kind from the geometry:

- The frames-vs-samples question has **no** attribute-free answer. The
  storage width does: `array.itemsize * 8` is exact, not a guess.
- The export writes `arr.tobytes()`. A `BitsAllocated` that disagrees with
  the array's `itemsize` produces a file that cannot be decoded at all —
  the attribute cannot be honoured, so "attributes win" is not available.
- Handing the setter an array of a different dtype is as legitimate as
  handing it one of a different shape. `tests/test_entities.py::
  test_pixel_bits_allocated` calls `set_pixel_data` three times on one
  instance with `uint8`, `uint16`, `int32` and asserts 8, 16, 32.

Changes: write it only when the value actually differs (so a no-op call
does not churn `_revision` with a redundant `set_attr` — §3.11 rule 1;
the instance is still marked modified either way, §3.11 rule 2), and log
at `DEBUG` when it changes a previously declared value.

**Rejected alternative:** raise on a declared/derived mismatch, matching
§3.6. Rejected because it breaks `test_pixel_bits_allocated`'s second and
third calls and forbids a dtype change through the public setter. This is
a judgement call; it is recorded here so a reviewer can disagree with it
on the record rather than discover it.

**Where the reconciliation lives (amended 2026-08-29).** On `692218c`
this happened twice: once in `set_pixel_data`, and again — implicitly —
every time `get_pixel_data` called it on the load path. §4.1 deletes the
second, so the setter is no longer reached on a read and a declared width
can survive all the way to the export. §4.3 therefore writes
`ds.BitsAllocated = arr.itemsize * 8` **at the write**, which is where
`arr.tobytes()` fixes the bytes per pixel. The rule in this section is
unchanged; only the second place it is applied is new. §11.4 records why
the original text of §4.3 contradicted this section.

**Out-of-scope hygiene note.** `BitsStored` (0028,0101), `HighBit`
(0028,0102) and `PixelRepresentation` (0028,0103) are *not* derived from
the array, so a corrected `BitsAllocated` can sit beside a stale
`BitsStored`. That is pre-existing and this design does not change it —
noted so a reviewer does not have to ask why one of four width
descriptors is derived.

### 3.11 Write only what changes — but always `mark_modified()`

**Amended 2026-08-29.** The original text made `mark_modified()`
conditional too. That was wrong; see §11.1. The rule is now two rules
that must not be collapsed back into one:

1. **Each descriptor write is conditional** on the value actually
   differing from the parsed declared value. `set_attr` bumps
   `_revision`, and an idempotent call should not churn it.
2. **`mark_modified()` is unconditional.** `set_pixel_data` always
   marks the instance modified, whether or not any descriptor changed.

**Why (2) cannot be conditional.** The descriptors are not the only thing
the store holds for an instance; the pixel bytes are, in the sidecar. The
incremental save path filters on exactly this flag —

```python
# persistence.py:1823-1824
unsaved = [(inst, inst._revision)
           for inst in series.instances if inst.has_unsaved_changes]
```

— so an instance whose array was replaced with one of the same shape and
dtype but different **contents** resolves to an identical geometry, writes
no descriptor, stays clean, and is skipped by `save_all()`. The new pixels
never reach the sidecar. Measured during implementation:
`Save (Inc) complete. P:1 St:1 Se:1 I:0`.

The arrays this happens to are redacted ones. `test_blob_storage.py:201`
replaces a `(64,64) uint8` original with a `(64,64) uint8` zeroed
redaction; `:393` replaces `np.full((16,16), 1)` with
`np.full((16,16), 2)`. Both are same-shape, same-dtype, different-bytes,
and both fail under a conditional `mark_modified()`.

**The justification the original gave no longer applies.** It cited §1.2
— a pure read dirtying the instance and `save()` then writing the wrong
geometry. §4.1 cures that at the source by removing the
`get_pixel_data` → `set_pixel_data` call entirely, so nothing on the read
path calls the setter at all. The suppression bought nothing for the
defect it named and cost redacted pixels.

**Rejected alternative — dirty only when the array is not the identical
object already held** (`array is not self.pixel_array`). It makes
correctness depend on no caller mutating an array in place, and
`RedactionService._apply_roi_to_instance` (`services.py:536-547`) already
does exactly that in its writeable arm: it mutates the instance's own
array and never calls `set_pixel_data`. Redaction survives today only
because `redact()` calls `inst.mark_modified()` explicitly at
`services.py:449`. An identity test would leave a silent invariant that
the next in-place caller breaks with no test to catch it.

**Rejected alternative — keep the suppression and let callers mark.**
That is the invariant above, written out longhand: every caller of
`set_pixel_data` would have to know whether its array's *contents*
changed. `set_pixel_data` is public API; it cannot delegate that.

Note the coercion interaction: comparing `int(str(raw))` against the new
value means an instance holding `"3"` from the old string form and
resolving to `3` compares **equal** and is not rewritten. The `str`→`int`
canonicalisation of §3.3 therefore does not churn existing graphs; it only
affects values written fresh.

---

## 4. Per-site changes

### 4.1 `Instance.get_pixel_data` (`entities.py:437-475`) — stop syncing

Three calls to `self.set_pixel_data(...)` on the load path become direct
assignments:

```python
self.pixel_array = arr
return self.pixel_array
```

at the sidecar-loader branch (`:444`), the `pydicom.dcmread` branch
(`:456`) and the `imagecodecs_handler` fallback (`:473`).

**A read must not write.** This alone removes the entire #186 class of
corruption, including the YBR relabelling and the durable DB corruption,
because the loader already reshaped from the attributes and the sync could
only ever disagree with what the reshape used. The docstring's claim that
the sync is "critical if the loader returns a raw array but attributes
were not yet set/restored" does not hold: `SidecarPixelLoader` needs
`rows`/`cols` to construct at all, and its 1-D fallback is reached only
when those same attributes are already wrong.

Delete the three-line comment at `:441-443` and replace it with one
sentence saying why the sync is gone, per CLAUDE.md's "comments explain
the trap".

Fixing `set_pixel_data` (§4.2) without this change would leave the read
path calling a setter that no longer corrupts but still dirties the
entity. Fixing this without `set_pixel_data` would leave
`RedactionService._apply_roi_to_instance` (`services.py:545`) —
which calls `set_pixel_data(arr)` on an instance with a full set of
attributes when the array is not writeable — corrupting a multi-frame
4-column instance mid-redaction. Both are required.

### 4.2 `Instance.set_pixel_data` (`entities.py:569-660`)

```python
geom = resolve_pixel_geometry(array.shape, self.attributes)  # may raise ValueError
if geom.evidence is GeometryEvidence.GUESSED:
    get_logger().warning(
        "Pixel array shape %s for %s is ambiguous: it is equally a "
        "%d-frame %dx%d image and a %dx%d image with %d samples per "
        "pixel, and the instance declares neither SamplesPerPixel "
        "(0028,0002) nor NumberOfFrames (0028,0008) nor Rows/Columns. "
        "Reading it as %d samples per pixel. Set SamplesPerPixel before "
        "set_pixel_data() to make this explicit.",
        array.shape, self.sop_instance_uid, ...)
```

Then write, conditionally per §3.11: Rows, Columns, SamplesPerPixel;
NumberOfFrames as an `int` when `geom.frames > 1` or `0028,0008` is
already declared; PhotometricInterpretation per §3.8;
PlanarConfiguration per §3.9; BitsAllocated per §3.10. Then
`mark_modified()` **unconditionally** (§3.11) — the array's contents are
part of what the store holds, and the incremental save path filters on
this flag.

`set_pixel_data` **accepts** a GUESSED geometry, because refusing it would
make it impossible to set pixels on a hand-built instance before its
attributes — which is what `DicomExporter.write_tree()` exists to serve
(CLAUDE.md), and what `tests/test_entities.py::test_pixel_unpacking_rgb`
and `::test_photometric_defaults` do. The guess is no longer silent, and
the caller can still correct the attributes afterwards.

### 4.3 `_export_instance_worker` (`io_handlers.py:855-975`)

**Resolve once, early.** Immediately after `arr` is obtained and
**before** the redaction block:

```python
geom = resolve_pixel_geometry(arr.shape, inst.attributes) if arr is not None else None
```

A `ValueError` here propagates to the existing `except` and becomes
`ExportOutcome(ok=False)` — audited by `_report_export_failures`, counted
in `ExportSummary.failed`, surfaced by the compliance report (#181). That
is the machinery a contradiction should land in.

Then:

1. **Redaction** passes `geom` to `apply_redaction_to_array` (§4.4).
2. **The float branch still writes no geometry**, for the reason already
   documented there. It benefits from the resolved redaction axes, and it
   inherits exactly one new failure mode: a §3.6 contradiction raised by
   the early resolve fails the write. It does **not** inherit the
   `GUESSED` refusal, which lives in the integer descriptor block below
   (see §8).
3. **The integer branch** replaces the `ndim` block entirely:

```python
if geom.evidence is GeometryEvidence.GUESSED:
    raise RuntimeError(
        f"Refusing to write {ctx.output_path}: the pixel array's shape "
        f"{arr.shape} is ambiguous and the instance declares no "
        f"SamplesPerPixel (0028,0002), NumberOfFrames (0028,0008) or "
        f"Rows/Columns to resolve it. Writing it would guess the "
        f"image's geometry.")

ds.Rows = geom.rows
ds.Columns = geom.cols
ds.SamplesPerPixel = geom.samples
if geom.frames > 1 or "0028,0008" in inst.attributes:
    ds.NumberOfFrames = geom.frames
ds.PhotometricInterpretation = <§3.8 applied to inst.attributes and geom.samples>
if geom.samples >= 3 and "0028,0006" not in inst.attributes:
    ds.PlanarConfiguration = 0   # amended: was `> 1` here and `>= 3` in
                                 # §3.9; §3.9 governs
```

```python
ds.BitsAllocated = arr.itemsize * 8      # amended -- see below
ds.BitsStored = inst.attributes.get("0028,0101", default_bits)
ds.HighBit = inst.attributes.get("0028,0102", default_bits - 1)
ds.PixelRepresentation = inst.attributes.get("0028,0103", 0)
```

**Amended 2026-08-29 — `BitsAllocated` is written from the array, not
read from `attributes`.** The original text said all four width
descriptors "keep their current `inst.attributes.get(..., default)`
form", which contradicted §3.10's own argument that a `BitsAllocated`
disagreeing with `itemsize` *cannot be honoured*, because the export
writes `arr.tobytes()`. The coder implemented §4.3, faithfully, and it
was a blocker. See §11.4.

Why it became reachable only under this design: on `692218c` the last
act of `set_pixel_data` — called by `get_pixel_data` on every load path —
was an unconditional `set_attr("0028,0100", array.itemsize * 8)`, and
that was the **only** reconciliation between a declared `BitsAllocated`
and the dtype the loader actually produced. §4.1 removes that call, for
good reasons, and removes the reconciliation with it.
`SidecarPixelLoader` buckets dtype as `uint16 if bits > 8 else uint8`, so
a declared `BitsAllocated = 1` yields a `uint8` array; the export then
wrote `BitsAllocated=1` beside bytes at 8 bits per pixel, and pydicom
read a 2-frame 4×8 Segmentation mask back as **16 frames**. Measured:
`BA=8, match=True` on `692218c`; `BA=1, match=False` on the branch.
**Any declared width outside `{8, 16}` reaches the export disagreeing
with the bytes written.**

The reconciliation belongs **at the write, not at the read**.
`ds.PixelData = arr.tobytes()` is three lines above; that is where the
number of bytes per pixel is decided, so that is where the descriptor
naming it is decided.

Use `arr.itemsize * 8`, **not** `default_bits`. `default_bits` caps at
16 (`8 if arr.itemsize == 1 else 16`), so a directly-assigned `uint32`
array would be written `BitsAllocated=16` beside 4-byte pixels — the same
class of bug, reintroduced by the fix. `692218c` wrote 32 there, via
`set_pixel_data`.

`BitsStored`, `HighBit` and `PixelRepresentation` stay declared. They do
not constrain how many bytes `tobytes()` emits, and §8 already scopes
their coherence out.

Both public DICOM write paths funnel through `_export_instance_worker`,
so `write_tree` is covered by the same line and `tests/test_api_coherence.py`
stays satisfied.

**The single most important line is `ds.SamplesPerPixel = geom.samples`.**
Today `Rows`/`Columns` come from the shape and `SamplesPerPixel` comes
from `attributes`, and it is that incoherence — not the wrong axis — that
turns #186 into an *undecodable* file. Writing all four descriptors from
one resolved geometry makes them coherent by construction.

The worker rejects `GUESSED` where the setter accepts it: this is the
boundary at which a guess would become a file on disk that a recipient
cannot tell apart from a correct one. Note the asymmetry between the two
export entry points — `export_batch` audits the failure and continues,
`write_tree` raises `RuntimeError` and hard-fails the whole call. Neither
of the three `scripts/` generators can reach it (§6).

### 4.4 `RedactionService.apply_redaction_to_array` (`services.py:485-495`)

```python
@staticmethod
def apply_redaction_to_array(arr, rois, geometry: Optional[PixelGeometry] = None) -> bool:
    ndim = len(arr.shape)
    if geometry is not None:
        interleaved = geometry.samples > 1
    else:
        interleaved = ndim >= 3 and arr.shape[-1] in [3, 4]
    row_dim, col_dim = (ndim - 3, ndim - 2) if interleaved else (ndim - 2, ndim - 1)
```

Both callers pass it: `_apply_roi_to_instance` (`services.py:538-547`,
which has `inst`) and `_export_instance_worker` (§4.3). The `geometry is
None` arm keeps the current behaviour for any third-party caller of this
public static method; adding a keyword argument rather than a second
spelling of the method satisfies "one spelling per behaviour".

### 4.5 `analyze_pixels` (`pixel_analysis.py:204-215`)

Replace the frame split with:

```python
geom = resolve_pixel_geometry(pixel_array.shape, instance.attributes)
if geom.frames > 1:
    frames = [pixel_array[i] for i in range(geom.frames)]
else:
    frames = [pixel_array]
```

`geom.frames` is bounded by `shape[0]` on every arm, so the range is
always in bounds. This runs *after* `apply_voi_lut`, which preserves
shape.

If `resolve_pixel_geometry` raises, catch it in the existing outer
`except Exception` (which already logs and returns `[]`) — an unscannable
instance must not crash a burned-in scan of a whole cohort.

---

## 5. Worked edge cases

The implementation must produce exactly these. Each row is a test case.

### Rank 3, grayscale multi-frame (the filed defect)

| shape | declared | arm | evidence | result |
| --- | --- | --- | --- | --- |
| (3,4,4) | SPP=1, NoF=3, R=4, C=4 | A | DECLARED | f=3 r=4 c=4 s=1 |
| (3,8,3) | SPP=1, NoF=3, R=8, C=3 | A | DECLARED | f=3 r=8 c=3 s=1 |
| (2,8,4) | SPP=1, NoF=2, R=8, C=4 | A | DECLARED | f=2 r=8 c=4 s=1 |
| (3,8,8) | SPP=1 | A | DECLARED | f=3 r=8 c=8 s=1 |
| (3,8,8) | *nothing* | A | STRUCTURAL | 8 ∉ {3,4}, arm B inadmissible |

### Rank 3, colour

| shape | declared | arm | evidence | result |
| --- | --- | --- | --- | --- |
| (4,4,3) | SPP=3, R=4, C=4 | B | DECLARED | f=1 r=4 c=4 s=3 |
| (8,8,3) | SPP=3, PI=YBR_FULL | B | DECLARED | PI stays `YBR_FULL` |
| (8,8,4) | SPP=4 | B | DECLARED | s=4 (non-conformant, but declared) |
| (100,200,3) | *nothing* | B | **GUESSED** | warn; PI←RGB, PC←0 |
| (10,10,3) | PI=MONOCHROME2 only | B | **GUESSED** | warn; PI corrected to RGB (§3.8) |

### Rank 3, tiebreaks

| shape | declared | arm | evidence |
| --- | --- | --- | --- |
| (1,4,3) | NoF=1 | B | **GUESSED** (step 2 cannot discriminate: `shape[0] == 1` makes both `A_ok` and `B_ok` true; step 3 has nothing declared) |
| (5,4,3) | NoF=5 | A | DECLARED (step 2) |
| (5,4,3) | R=4, C=3 | A | MATCHED (step 3) |
| (5,4,3) | R=5, C=4 | B | MATCHED (step 3) |
| (4,4,4) | R=4, C=4 | B | **GUESSED** (both match; step 4) |

The `(1,4,3)` row is the one place step 2 cannot discriminate:
`shape[0] == 1` makes `A_ok` and `B_ok` both true, so it must fall
through to step 3 (and here, with nothing else declared, to step 4) rather
than pick arbitrarily. An implementation that writes step 2 as
`if f_d > 1: A else: B` returns `B`/`DECLARED` for this row and is wrong —
the answer happens to be arm B either way, but the evidence is `GUESSED`,
and it is the evidence that decides whether the export refuses it.

### Rank 3, contradiction

| shape | declared | outcome |
| --- | --- | --- |
| (5,8,4) | SPP=3 | `ValueError` — no axis carries 3 samples |
| (3,8,8) | SPP=4 | `ValueError` |

### Ranks 1, 2, 4

| shape | declared | result |
| --- | --- | --- |
| (16,) | R=4, C=4, SPP=1, NoF=1 | reshaped `(4,4)`, no attribute writes |
| (20,) | R=4, C=4 (padding) | truncated to 16, reshaped `(4,4)` |
| (7,) | *nothing* | r=1 c=7 s=1 f=1 (existing fall-through) |
| (10,10) | anything | r=10 c=10 s=1 f=1; a declared SPP=3 is **overwritten to 1** (magnitude, not layout — no ambiguity exists at rank 2) |
| (2,4,4,3) | SPP=3, NoF=2 | f=2 r=4 c=4 s=3, STRUCTURAL/DECLARED |
| (1,4,4,3) | SPP=3 | f=1 r=4 c=4 s=3; NumberOfFrames written only if already declared |
| (2,4,4,3) | SPP=1 | `ValueError` (contradiction) |
| (2,3,4,5,6) | — | `ValueError` (unchanged) |

### `NumberOfFrames = 1`

Legal, and must survive: a declared `0028,0008 = 1` is not the same as an
absent one. It participates in step 2, and if it was declared it is
written back (as `1`) rather than dropped.

---

## 6. Backward compatibility — every caller

`Instance.set_pixel_data` has 3 production callers, 3 `scripts/` callers
(via `InstanceContextBuilder.set_pixel_data`, `builders.py:96`) and ~55
test call sites across 27 files.

> **Amended 2026-08-29 — the method that built this table was wrong, not
> just three of its rows.** Each row was checked by asking *"does the
> resolver return the right geometry for this shape?"* and never *"does
> this caller depend on the write's side effects?"*. Three rows asserted
> "no writes, no dirtying" as if that were self-evidently harmless; two of
> them named the exact test lines that then failed. Every row involving a
> **second** `set_pixel_data` call on an instance that already has
> descriptors has been re-checked against the save path, and the ones
> that were wrong are marked below. Rows covering a *first* set on a fresh
> instance are unaffected: the descriptors go from absent to present, so
> they change under any rule.
>
> **Second amendment pass, same day:** the re-audit above was still
> reading code, and it missed the RGB arms of two `scripts/` generators.
> Those two rows are corrected below from *measurement* — the reviewer
> instrumented the child processes (three hooks via `sitecustomize.py` on
> `PYTHONPATH`; 136 pids recorded probe output in one generator run) and
> swept the `get_pixel_data` side that this table cannot reach. It found
> **no fifth instance** of the read-must-not-write conflation: three
> candidates were rank-2, unambiguous, or byte-identical on `692218c`.
> The four sites in §2 are the complete set, established by measurement
> rather than by argument.

| Caller | Shapes passed | Effect of this design |
| --- | --- | --- |
| `Instance.get_pixel_data` ×3 (`entities.py:444,456,473`) | whatever the loader/pydicom returns | **Row corrected — this survey asked the wrong question.** "No longer calls it" is right, and a load no longer writing attributes or dirtying the entity is right. The survey then checked which **tests** asserted on the load-path write and found none (`test_sidecar.py:92`, `test_metadata_refactor_full.py:110`, `test_compaction.py:79,155`, `test_blob_storage.py:220`, `test_entities.py:52`, `test_memory_redaction.py:58,81`, `test_redaction_parallel.py:105,150`, `test_redact_error.py:50`, `test_session.py:77`, `test_redaction_wildcard.py:114` all assert on the array only). It never asked which **non-test consumer** depended on it — and one did: `_export_instance_worker` read `0028,0100` back out, and the load-path `set_attr("0028,0100", array.itemsize * 8)` was the only thing reconciling it with the dtype the loader produced. §4.3 now writes that descriptor at the write instead. See §11.4. |
| `RedactionService._apply_roi_to_instance` (`services.py:545`) | the instance's own array, same shape | **Row corrected.** Same shape, full attributes → DECLARED, so no descriptor is written — but the instance **is** marked modified (§3.11), which is what the redacted bytes need. The earlier text said "no dirtying" and was the first instance of the conflation §11.1 describes. Fixes the multi-frame corruption on the not-writeable path. Note this method's *other* arm mutates the array in place and never calls `set_pixel_data` at all; it relies on `redact()`'s explicit `mark_modified()` at `services.py:449`, unchanged here. |
| `InstanceContextBuilder.set_pixel_data` (`builders.py:98`) | pass-through | Unchanged. |
| `scripts/generate_ocr_test_data.py:405` | 2-D CT `(512,512)`; XA multi-frame `(frames,512,512)`; **and Secondary Capture RGB `(rows,cols,3)`** (`:162-163`), whose `0028,0004` is set at `:419`, after. | **Row corrected — the original said "no guess, no warning" and was wrong.** The CT and XA arms are arm A / rank 2, STRUCTURAL, silent, as predicted. The SC RGB arm is rank 3 with nothing declared, so both arms are admissible and it resolves `GUESSED`. Measured by the reviewer: **270 files, 10 warnings**, all at the setter, every worker resolution DECLARED or STRUCTURAL, all files decode unchanged. Output is identical; only the log is louder. |
| `scripts/generate_redaction_example.py:129` | `(frames,rows,cols)` CT, rows/cols ≥ 64. Sets Rows/Cols/BitsAllocated at `:131+`, after. | Arm A, STRUCTURAL. Unchanged — measured **12 files, 0 warnings**. The one generator row that was right. |
| `scripts/generate_test_dataset.py:258` | 2-D and multi-frame greyscale, **plus US `(480,640,10,3)`, SC `(512,512,1,3)` and OT `(128,128,1,3)` RGB** (`:154-163`) | **Row corrected — the original said "unchanged" and missed the RGB modalities.** The multi-frame US array is rank 4 and unambiguous; the single-frame SC and OT arrays are rank 3 with nothing declared and resolve `GUESSED`. Measured: **18 files, 2 warnings** — one per single-frame RGB instance. Output identical. |
| ~50 test sites passing 2-D `np.zeros((10,10))`-shaped arrays | rank 2 | Unchanged. |
| `tests/test_entities.py::test_pixel_unpacking_rgb` — `(100,200,3)`, nothing declared | GUESSED | Passes; **emits a new WARNING**. |
| `tests/test_entities.py::test_photometric_defaults` case 3 — `(10,10,3)`, PI=MONOCHROME2 | GUESSED | Passes (PI corrected per §3.8, PC set); emits a WARNING. |
| `tests/test_sidecar.py:67` — `(50,50,3)`, nothing declared | GUESSED | Passes; emits a WARNING. Reload still `(50,50,3)`. |
| `tests/test_redaction_rgb.py:19-20` — `(100,100,3)` twice | 1st GUESSED, 2nd DECLARED (SPP=3 now present) | **Row corrected.** Passes. The second call writes no descriptor but still marks the instance modified (§3.11). The earlier text said it "does not dirty the instance", which was the conflation. Nothing asserts `has_unsaved_changes` here either way. |
| `tests/test_compaction.py:106` — replaces `(100,1000)` with `(100,1000)` | rank 2 | Unchanged. |
| `tests/test_blob_storage.py:201` — `(64,64) uint8` original replaced by a `(64,64) uint8` zeroed redaction | rank 2, DECLARED, **no descriptor changes** | **Row corrected — this was predicted safe and is not.** Under the original §3.11 the instance stayed clean, `save_all` skipped it, and `test_compaction_does_not_resurrect_pre_redaction_pixels` failed. Passes under the amended §3.11. |
| `tests/test_blob_storage.py:393` — `np.full((16,16),1)` replaced by `np.full((16,16),2)` | rank 2, DECLARED, no descriptor changes | **Row corrected — same failure.** `test_save_all_keeps_the_blob_table_in_step_with_instances`. Passes under the amended §3.11. |
| `tests/test_blob_storage.py:438` and `:160,247,332,530,624` | rank 2, first set on a fresh `Instance` | Genuinely unchanged: attributes go from empty to populated, so descriptors change under any rule. Re-verified. |
| `tests/test_compaction.py:106` — `(100,1000)` replaced by a different `(100,1000)` | rank 2, DECLARED, no descriptor changes | Passes under either rule, but **not** for the reason the original row implied: the test calls `store_backend.persist_pixel_data(i1)` explicitly at `:113` and never goes through the `has_unsaved_changes` gate. It was luck, not safety. |
| `tests/test_persistence_incremental.py::test_unsaved_tracking_pixel_change` | first set on a fresh instance | Passes under either rule, which is why it did not catch this. It is the closest existing pin on "set_pixel_data dirties the instance" and it does not cover the same-shape case — hence test 17 in §7. |
| `tests/test_entities.py::test_pixel_bits_allocated` — 3 dtypes, one instance | rank 2 | Unchanged (§3.10). |
| `tests/test_float_pixel_data_export.py` `_export_one` | rank 2 float | Unchanged; its "apply attrs after set_pixel_data" comment stays accurate. |
| `DicomExporter.write_tree` (fixture generators, no session) | — | Only newly raises on GUESSED or contradiction, neither of which the three generators produce. |
| `DicomExporter.export_batch` / `session.export()` | — | Newly audits a failure instead of writing a wrong file. `ExportSummary.failed` can now be non-zero where it was zero. |
| `RedactionService.apply_redaction_to_array` third-party callers | — | ~~New keyword argument, defaulted; old behaviour when omitted.~~ **Falsified by #217**, which made `geometry` a required positional: omitting it raises `TypeError`. Left struck rather than rewritten — see the front matter. |

**Costs to accept, stated rather than discovered:**

1. **New WARNING noise** in three or four tests, in **two of the three
   `scripts/` generators** (12 warnings across 300 generated files —
   §6's corrected rows), and in any user code that sets a colour array on
   an instance before its attributes. Declaring `SamplesPerPixel` before
   the call silences it. This is the intended price of "the guess must
   not be silent". No generated file changes.
2. **`session.export()` can now fail an instance** that previously
   succeeded (wrongly). This is the point; the audit trail (#181) reports
   it.
3. **`write_tree` can now raise** where it previously wrote a wrong file.
4. The three `scripts/` generators are unaffected — verified by reading
   the shapes they pass, not assumed.
5. **§4.1 removes a side effect that had a consumer**, and §4.3 replaces
   it at the write (`ds.BitsAllocated = arr.itemsize * 8`). Net effect on
   `692218c` behaviour: none — the same number reaches the file by a
   shorter route. Without the replacement, any instance whose declared
   `BitsAllocated` is outside `{8, 16}` exports a silently wrong image.
6. **No cost from the amended §3.11.** Unconditional `mark_modified()`
   restores exactly today's dirtying behaviour for `set_pixel_data`; the
   only thing this design removes is the *read* path's dirtying, and it
   removes it by not calling the setter at all (§4.1).

---

## 7. Tests the implementation must satisfy

New file `tests/test_pixel_geometry.py` for the resolver, plus additions
to the existing files named. Every row of §5 is a resolver unit test;
those are not repeated here.

### Must now pass (regressions that are currently failures)

1. `ingest → save → release_memory → get_pixel_data` on a 3-frame 4×4
   `MONOCHROME2` instance leaves **every** attribute in §1.1's table
   unchanged, and leaves `has_unsaved_changes` **False**.

   The dirtiness half of that assertion is safe: `set_pixel_data` is the
   only writer reachable from `get_pixel_data` (verified — the other two
   branches call it too, `SidecarPixelLoader.__call__` holds primitives
   and no `Instance` reference at all, and `unload_pixel_data` assigns
   the `pixel_array` field without touching `_revision`). Assert
   `has_unsaved_changes is False` **before** the call as well, so a test
   failure distinguishes "the load dirtied it" from "it arrived dirty".
2. The same, saved and reloaded from a fresh `Session`: the DB holds
   `SamplesPerPixel=1, PI=MONOCHROME2, Rows=4, Columns=4, NumberOfFrames=3`.
3. `ingest → export → pydicom.dcmread(...).pixel_array` round-trips
   `np.array_equal` for `(3,4,4)`, `(2,8,4)`, `(3,8,3)`, `(3,8,8)` and
   `(1,4,4)`, with and without `use_compression`.
4. `DicomExporter.write_tree` on a hand-built `(2,8,4)` graph with
   `SamplesPerPixel=1, NumberOfFrames=2, Rows=8, Columns=4` writes
   `Rows=8, Columns=4, NumberOfFrames=2, SamplesPerPixel=1` and the
   pixels round-trip. (§1.4's exact fixture.)
5. A single-frame 8×8 `YBR_FULL` instance keeps
   `PhotometricInterpretation=YBR_FULL` through
   `get_pixel_data()` and through export.
6. `RedactionService.redact_machine_instances` on a 2-frame 8×4
   grayscale instance zeroes the requested rows of *every frame*, not
   the first two frames entirely. Assert on the array's contents, not
   on a bool.
7. `analyze_pixels` on a 3-frame 8×3 grayscale instance calls
   `detect_text_regions` **three** times. Patch `HAS_OCR` and
   `detect_text_regions`; do not require pytesseract.

### Must now fail loudly (new behaviour, not previously expressible)

8. `set_pixel_data` on an instance declaring `SamplesPerPixel=3` with a
   `(5,8,4)` array raises `ValueError` whose message names both the shape
   and the declared descriptors.
9. `_export_instance_worker` on an instance with a `(100,200,3)` array
   and **no** `0028,0002`/`0028,0008`/`0028,0010`/`0028,0011` returns
   `ExportOutcome(ok=False)`, writes no file, and its error names the
   ambiguity. Assert the file does not exist.
10. `set_pixel_data` on the same instance **succeeds** and logs a WARNING
    (`caplog`) naming `SamplesPerPixel`. The asymmetry between 9 and 10 is
    deliberate and must be pinned, or someone will "unify" it.

### Must keep passing (pins the compatibility decisions)

11. `tests/test_entities.py::test_pixel_unpacking_2d`,
    `::test_pixel_unpacking_rgb`, `::test_pixel_bits_allocated`,
    `::test_photometric_defaults` (all three cases), `::test_lazy_loading`.
12. `tests/test_pixel_export.py`, `tests/test_pixel_integrity.py`,
    `tests/test_ingestion_normalization.py` — planar-config-1 and RGB
    ingest/export round trips.
13. `tests/test_redaction_rgb.py::test_redaction_rgb_dimensions`.
14. `tests/test_float_pixel_data_export.py` in full — the float branch is
    untouched.
15. `tests/test_api_coherence.py` — `write_tree` and `session.export()`
    still produce identical trees.
16. Full suite on 3.12 and 3.14t.

### Must now be pinned directly (previously pinned only by accident)

17. **`set_pixel_data` with a same-shape, same-dtype, different-bytes
    array leaves `has_unsaved_changes` True.** Build an instance with
    full descriptors, `mark_persisted()`, assert
    `has_unsaved_changes is False`, then `set_pixel_data` a second array
    of identical shape and dtype and different contents, and assert
    `has_unsaved_changes is True`. Assert also that no geometry attribute
    changed — the point is that the instance is dirty *despite* that.

    This guarantee is currently pinned only indirectly, through
    `test_blob_storage.py`'s two compaction/blob-table tests, and a
    reader of `set_pixel_data` has no local reason to believe it.
    `test_persistence_incremental.py::test_unsaved_tracking_pixel_change`
    looks like this test but is not: it sets pixels on a *fresh*
    instance, so the descriptors change and it passes under a conditional
    `mark_modified()` too. That is why the defect reached implementation.

18. **`set_pixel_data` with the identical array object still dirties.**
    `inst.set_pixel_data(arr)` twice with the same `arr` leaves
    `has_unsaved_changes` True on the second call. Pins the rejection of
    the identity test in §3.11 so it is not reintroduced as an
    optimisation.

### Must pin `ds.SamplesPerPixel = geom.samples` (added by review)

§4.3 calls this line **"the single most important"** in the change, and
§7 originally asked for no test that touches it. Reverting it to
`inst.attributes.get("0028,0002", 1)` passed the entire suite. The
reviewer proved it reachable in both directions and wrote two tests that
kill the mutation; both fail on `692218c`.

19. **Resolved samples exceed the declared ones.** Export an instance
    whose array is rank 4 colour — `(2, 4, 4, 3)` — with **no** declared
    `0028,0002`. The resolver returns `samples=3`. With the line reverted
    the file is written `SamplesPerPixel=1`, and `pydicom` reshapes it to
    `(6, 4, 4)` — a decodable file describing six greyscale frames where
    there are two colour ones. Assert `ds.SamplesPerPixel == 3` and that
    `ds.pixel_array` round-trips `np.array_equal` against the source.

20. **Resolved samples fall below a stale declared value.** Export an
    instance holding a rank-2 `(8, 8)` array beside a stale
    `0028,0002 = 3` (the shape a caller leaves behind after replacing a
    colour array with a greyscale one — §5's rank-2 row). The resolver
    returns `samples=1`. With the line reverted the file claims 3 samples
    over 64 pixels' worth of bytes and fails to decode. Assert
    `ds.SamplesPerPixel == 1` and that `ds.pixel_array` round-trips.

Together these pin both directions of the coherence §4.3 exists to
create: all four descriptors come from one resolved geometry, so
`SamplesPerPixel` can neither exceed nor undershoot what the bytes hold.

### Must pin the rank-1 behavioural change (§3.7)

21. **A flat array set through `set_pixel_data` dirties the instance.**
    Build an instance with full descriptors, `mark_persisted()`, assert
    `has_unsaved_changes is False`, then `set_pixel_data` a 1-D array
    that reshapes cleanly from those descriptors. Assert
    `has_unsaved_changes is True` and that no geometry attribute changed.
    This is the one behavioural change in the work that is not a bug fix
    (§3.7) and it must not arrive unannounced.

### Must pin the width the export actually writes (added by PR review)

22. **`test_export_writes_bits_allocated_the_array_actually_has`.**
    Export an instance whose declared `0028,0100` is outside `{8, 16}` —
    a `BitsAllocated = 1` Segmentation mask, 2 frames of 4×8, is the
    measured case — and assert that the written file carries
    `BitsAllocated == arr.itemsize * 8` and that `ds.pixel_array`
    round-trips `np.array_equal` against the source. With §4.3's
    amendment reverted, pydicom reads the 2-frame mask back as 16 frames.

    Add a second instance with a directly-assigned `uint32` array and
    assert `BitsAllocated == 32`. That arm is what distinguishes
    `arr.itemsize * 8` from `default_bits`, which caps at 16; without it
    the fix can be silently downgraded to the capped form.

    **This test's polarity is the inverse of every other test on this
    branch** — see the non-vacuity categories below.

### Fixture hygiene — why four tests on this work guarded less than they looked

This is not a general lecture; it is a list of the two specific traps in
`set_pixel_data` that have now produced four misleading tests on this
change alone. **A test here must be built on a fixture in which the
behaviour under test is the only thing that could produce the result.**

The two traps:

- **`set_pixel_data` overwrites `BitsAllocated` from `array.itemsize`**
  (§3.10). Any pixel descriptor written to `attributes` *before* the call
  is silently corrected to agree with the array. A test that sets
  `BitsAllocated = 32` and then calls `set_pixel_data` is asserting the
  array's dtype, not the production assignment — it passes with that
  assignment deleted. `tests/test_float_pixel_data_export.py:332-338`
  documents the workaround: apply the `attrs` **after** the call.
- **A fresh `Instance` has empty `attributes`, so every descriptor write
  is a change** and the instance dirties under any rule, conditional or
  not. A test that sets pixels on a fresh instance and asserts
  `has_unsaved_changes` is asserting that `attributes` was empty.

The four casualties, each with the fixture detail that made it pass for
the wrong reason (a fifth, of the mirrored kind, is item 3 in the
requirements below):

| Test | What it looks like it pins | Why it does not |
| --- | --- | --- |
| the `BitsAllocated == 32` assertion in the float export tests | that the export writes the declared width | trap 1 — corrected by `set_pixel_data` before the assertion could see a disagreement |
| `test_persistence_incremental.py::test_unsaved_tracking_pixel_change` | that `set_pixel_data` dirties the instance | trap 2 — fresh instance, descriptors change, passes under a conditional `mark_modified()` too. This is why §11.1 reached implementation. |
| `test_redaction_dirties_the_instance_it_redacted` | the same, through redaction | trap 2 in partial form — its instance lacks `0028,0004` and `0028,0100`, so those two writes dirty it regardless. Survives the conditional-`mark_modified()` mutation. |
| `ds.SamplesPerPixel = geom.samples` | — | pinned by **nothing**; tests 19 and 20 above exist because reverting it passed the whole suite |

**Requirements that follow, for anything added under §7:**

1. Any test asserting a *descriptor value* must set that descriptor
   **after** `set_pixel_data`, or use an array whose dtype and shape make
   the derived value differ from the asserted one.
2. Any test asserting *dirtiness* must start from an instance with a
   **complete** set of descriptors and an explicit `mark_persisted()`,
   and must assert `has_unsaved_changes is False` immediately before the
   call under test. Tests 17, 18 and 21 are written this way; that is not
   incidental.
3. **The same error, pointed the other way: do not delete a side effect
   because no test covers it — ask who *consumes* it.** §6's survey of
   `get_pixel_data` checked which tests asserted on the load-path
   `set_attr` and found none, and concluded it was safe to remove. One
   non-test consumer depended on it (`_export_instance_worker` reading
   `0028,0100` back out), and removing it shipped a silently wrong image
   (§11.4). The five casualties above trust a test that guards nothing;
   this trusts the *absence* of a test to mean the absence of a
   dependency. Both substitute coverage for consumers.

   Concretely, for anything removed under §4.1 or added under §4.3: for
   each attribute the deleted call wrote, `grep` for readers of that tag
   across `isocenter/` — not across `tests/` — and say for each whether
   it still gets its value and from where. `0028,0100` has readers in
   `io_handlers.py` (the export worker and `SidecarPixelLoader`); a
   survey that had listed them would have caught this before
   implementation.

4. **§3.11 rule 1 (conditional descriptor writes) is currently unpinned
   by design.** Nothing distinguishes "wrote the same value" from "did
   not write". If a test for it is wanted, it has to observe `_revision`
   directly — assert that a `set_pixel_data` call resolving to identical
   descriptors advances `_revision` by exactly the one bump
   `mark_modified()` contributes, not by one per descriptor. Recorded as
   a known gap rather than silently left out.

### Non-vacuity requirement

Tests 1, 3, 4, 5 and 6 must each fail on `main` at `692218c`.
Tests 17 and 18 will *pass* on `692218c` — they pin behaviour that is
already correct and that this design must not break. Say so in the PR
body rather than listing them as regressions. Tests 19 and 20 **fail** on
`692218c` (verified by review) and must also fail against an
implementation with `ds.SamplesPerPixel = geom.samples` reverted — state
both. Test 21 fails on `692218c` because the behaviour is new, not
because it was broken; say which.

**Four polarities, and the PR body must say which each test has** —
"fails on `692218c`" is *not* by itself the mark of a good regression
test here:

| shape | tests | fails on `692218c`? | what it pins |
| --- | --- | --- | --- |
| reproduces a bug this change fixes | 1, 3, 4, 5, 6, 19, 20 | yes | the fix |
| guards existing correct behaviour | 17, 18 | no | that this change does not break it |
| pins a new, deliberate behaviour change | 21 | yes, but only because the behaviour is new | the decision, not a bug |
| **pins a regression this branch introduced** | **22** | **no — it passes on `692218c` and failed on `cf9dcd6`** | that §4.1's deletion did not take a consumer's value with it |

Test 22 is the only one with that fourth polarity. A reviewer scanning
for "fails before the fix" will misread it as broken unless the PR body
names it. State the observed failure mode for each test in the PR body.
For every test in the first row, a test that passes before the fix is
testing something else.

---

## 8. Out of scope

- **`BitsStored`/`HighBit`/`PixelRepresentation` coherence** (§3.10).
- **The float pixel branch** of `_export_instance_worker` — it still
  writes no geometry, and that reasoning is already recorded there. It is
  *not* wholly untouched, and the earlier draft of this spec claimed it
  was: §4.3 resolves the geometry before the redaction block, which is
  above the float branch, so a float instance whose declared
  `SamplesPerPixel` contradicts its array (§3.6) now fails the write
  instead of being written. That is one new failure mode, deliberate and
  consistent with the integer path, and it is the price of giving
  redaction the correct axes on the float path too. A float instance
  with a `GUESSED` geometry is **not** refused — the `GUESSED` check
  lives in the integer descriptor block, because it is writing a guessed
  descriptor that the refusal exists to prevent, and the float branch
  writes none.
- **`SidecarPixelLoader`'s planar-configuration-1 branch.** For
  `frames > 1, planar_conf == 1` it builds `(frames, rows, cols, samples)`,
  which is the interleaved layout, not the planar one. It is **unreachable
  from ingest**, because `ingest_worker` normalises `0028,0006` to `0`
  (verified by measurement), so it is dead code rather than a live bug.
  Listed in §9.
- **A post-write round-trip verification pass.** Listed in §9.
- **Any change to `verify()` or `generate_report`.** This design prevents
  rather than detects; the one unresolvable case reaches the report
  through the existing #181 export-failure path with no new plumbing.
- **`_compress_j2k`'s singleton-squeeze logic** — already attribute-first.
- **Renaming or re-signaturing `set_pixel_data`.** It stays public with
  the same signature.

---

## 9. Found here, to be filed separately

Not folded in; listed for the maintainer to file or discard.

1. **`session.export()` cannot tell whether what it wrote is readable.**
   Every symptom in §1.3 would have been caught by re-`dcmread`ing the
   written file and comparing `Rows`/`Columns`/`SamplesPerPixel`/
   `NumberOfFrames`/`pixel_array.shape` against the graph. Worth an
   opt-in `verify_written=True` on `_export_dicom`, reported through the
   #181 accounting. This is the general answer to "the report graded a
   corrupted export `PASS`", of which #186 is one instance.

2. **`SidecarPixelLoader` reshapes planar-configuration-1 multi-frame
   data as interleaved** (`io_handlers.py:1245-1252`). Currently dead —
   `ingest_worker` normalises `0028,0006` to `0` — but it is a latent
   trap for anyone who constructs a loader from `metadata` directly, and
   the single-frame arm two lines below *does* handle planar-1 correctly,
   so the two arms disagree with each other. Either fix or delete.

3. **`_export_instance_worker` calls `populate_attrs(ds, inst)` on the
   export path**, a helper whose direction is dataset → graph. This is
   the already-filed #184 and is adjacent to, but not the same as, the
   geometry question. Mentioned only to confirm it was seen and left
   alone.

4. **`ingest` rejects 16-bit `YBR_FULL` outright** — `Decompression
   Failed: Invalid ndarray.dtype 'uint16' for color space conversion`
   from pydicom, surfaced as `ERROR: Import Failed` and a silently empty
   store (0 patients). Found while building the colour fixtures for §1.1.
   Unrelated to this design; a distinct ingest-side issue about a
   rejection that produces an empty session rather than a reported skip.

---

## 10. Implementation order

1. `isocenter/pixel_geometry.py` + `tests/test_pixel_geometry.py` (every
   §5 row). No production call site touched. Suite stays green.
2. `entities.py`: `get_pixel_data` stops syncing (§4.1); `set_pixel_data`
   uses the resolver (§4.2). Tests 1, 2, 5, 8, 10, 11.
3. `io_handlers.py`: `_export_instance_worker` (§4.3). Tests 3, 4, 9, 12,
   14, 15.
4. `services.py` (§4.4). Test 6.
5. `pixel_analysis.py` (§4.5). Test 7.
6. `CHANGELOG.md` — a breaking entry naming the exact exceptions
   (`ValueError` from `set_pixel_data`, `ExportOutcome(ok=False)` /
   `RuntimeError` from the export paths), and why writing a guessed
   geometry was wrong. Match the depth of the surrounding entries;
   CLAUDE.md calls the changelog the project's primary design record.

Steps 2–5 are independently revertible. Step 1 is a prerequisite for all
of them.

---

## 11. Amendments

### 11.1 §3.11 dropped redacted pixels (2026-08-29)

**Caught by the implementing coder during step 2, who flagged it rather
than deviating from the spec. The defect was mine.**

The original §3.11 made both the descriptor writes *and* `mark_modified()`
conditional on a descriptor having changed. Implemented literally, a
`set_pixel_data` call with a same-shape, same-dtype, different-bytes array
resolves to an identical geometry, writes nothing, leaves
`has_unsaved_changes` False, and is skipped by the incremental
`save_all()` — so the new pixels never reach the sidecar. The arrays this
happens to are redacted ones.

It was a spec bug, not a test problem, for two reasons:

- It conflated **"no descriptor changed"** with **"nothing changed"**. The
  array's contents are part of what the store holds.
- The spec contradicted itself: §6 predicted
  `tests/test_blob_storage.py:201,393,438` unchanged, and `:201` and
  `:393` were exactly the tests that failed.

The justification also did not survive contact: §3.11 cited §1.2, but
§4.1 already cures that by removing the `get_pixel_data` →
`set_pixel_data` call, so the suppression bought nothing.

Resolution: conditional descriptor writes kept (that half is independent
and fine), `mark_modified()` made unconditional. Two rejected
alternatives are recorded in §3.11 — the literal original, and an
identity test on the array object — so the suppression is not re-derived
later as an optimisation.

Also amended in the same pass: §6 gained a note that the *method* which
produced its rows was wrong, not just the three rows, and every row
involving a second `set_pixel_data` call was re-checked against the save
path; §7 gained tests 17 and 18.

### 11.2 Earlier amendment (2026-08-29, pre-implementation)

`PixelGeometry.frames` pinned to the array's axis rather than to a
declared `NumberOfFrames` (§3.5), and §8 corrected to stop claiming the
float export branch was wholly unchanged (§4.3 gives it one new failure
mode). Both found in design review before any code was written.

### 11.3 Review pass (2026-08-29, post-implementation)

Review returned **ship**, with four spec corrections and one finding that
matters more than the corrections.

**The finding: the spec's own most-emphasised line was pinned by
nothing.** §4.3 calls `ds.SamplesPerPixel = geom.samples` "the single
most important line" in the change — it is what turns #186 from a wrong
file into an *undecodable* one — and §7's test list did not ask for a
test that touches it. Reverting it passed the entire suite. It survived
design, implementation and into review unpinned.

That is worth recording rather than quietly fixing, because **§7 is what
the implementation was checked against.** A spec that emphasises a line
in prose and omits it from the test list has moved the omission
downstream: the coder implemented it correctly and the suite could not
have told anyone if they had not. The lesson for the next spec of this
shape is mechanical — every line the prose singles out as load-bearing
needs a numbered test beside it, and the non-vacuity requirement needs to
name the *mutation* each test kills, not just the bug it reproduces.
Tests 19 and 20 now do that.

The same pass added §7's **fixture hygiene** section. Four tests on this
change guarded less than they appeared to, all with one shape: the
fixture made the assertion pass for a reason unrelated to the behaviour
under test. Two mechanisms account for all four —
`set_pixel_data` silently correcting `BitsAllocated` from the array's
dtype, and a fresh `Instance` dirtying under any rule because its
`attributes` are empty. Both are now named in the spec with the
requirements that follow, because the next person implementing against
this document will hit them.

The other three corrections: §3.7 and §3.11 rule 2 contradicted each
other over the rank-1 early return (rule 2 governs; the branch must
`mark_modified()`, which is the one non-bug-fix behavioural change in the
work and now has test 21); §4.3's PlanarConfiguration snippet said `> 1`
where §3.9 says `>= 3` (§3.9 governs); and §3.5's "reachable only when"
parenthetical missed the `s_d == 1, shape[2] == 1` case.

Review also swept for a fifth instance of the read-must-not-write
conflation, instrumenting child processes rather than reading code, and
found none. §2's four sites are the complete set.

### 11.4 PR review (2026-08-29): §4.3 contradicted §3.10, and the coder implemented §4.3

**The blocker.** §4.1 removes `set_pixel_data()` from `get_pixel_data()`'s
load branches, which is right and is the core of the fix. It also removes
that call's last act — an unconditional
`set_attr("0028,0100", array.itemsize * 8)` — which was the **only**
reconciliation between a declared `BitsAllocated` and the dtype
`SidecarPixelLoader` actually produces. The loader buckets dtype as
`uint16 if bits > 8 else uint8`, so a declared `BitsAllocated = 1` yields
a `uint8` array; the export wrote `BitsAllocated=1` beside bytes at 8
bits per pixel, and pydicom read a 2-frame 4×8 Segmentation mask back as
16 frames. `BA=8, match=True` on `692218c`; `BA=1, match=False` on the
branch. Any declared width outside `{8, 16}` was affected.

**The spec is squarely implicated.** §3.10 argues that a `BitsAllocated`
disagreeing with `itemsize` cannot be honoured, because the export writes
`arr.tobytes()` — so "attributes win" was never available for it. §4.3
then said all four width descriptors "keep their current
`inst.attributes.get(..., default)` form". Those contradict. The coder
implemented §4.3. §4.3 now writes `ds.BitsAllocated = arr.itemsize * 8`
at the write, where `tobytes()` fixes the bytes per pixel — deliberately
not `default_bits`, which caps at 16 and would mis-size a `uint32` array
that `692218c` wrote as 32.

**The methodological point — this is the sixth.** §6's survey row for
`get_pixel_data` checked which **tests** asserted on the load-path write
and found none. It never asked which **non-test consumer** depended on
it. That is the same error as the five fixture-hygiene casualties in §7,
pointed the other way: those trust a test that guards nothing; this
trusts the *absence* of a test to mean the absence of a dependency. Both
substitute coverage for consumers. §7's fixture-hygiene section covered
only the first direction; it now covers both, with the concrete
instruction — for each attribute a deleted call wrote, grep for readers
across `isocenter/`, not across `tests/`, and say for each whether it
still gets its value and from where.

Worth recording plainly: this got through design, implementation, **and a
clause-by-clause conformance review**. A conformance review that matches
the implementation against §6 will faithfully reproduce §6's error. The
only thing that caught it was running the pipeline. That is the third
finding on this work that measurement caught and reading did not (after
§6's two generator rows and the unpinned `SamplesPerPixel` line), and it
is the argument for §7's non-vacuity requirement being stated in terms of
the mutation each test kills.

**Test 22** pins the regression, and has a polarity no other test on this
branch has: it **passes** on `692218c` and failed on the branch. §7's
non-vacuity section now tabulates all four polarities so a reviewer
scanning for "fails before the fix" does not misread it.

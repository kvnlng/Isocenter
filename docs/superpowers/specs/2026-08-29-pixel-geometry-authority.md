# Pixel Geometry Authority

**Date:** 2026-08-29
**Status:** Design approved, not yet implemented
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
- Both admissible → step 2. (Reachable only when `s_d is None and
  shape[2] in (3, 4)`.)

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
reshapes and returns without writing anything. Keep it verbatim; move it
into the resolver only if that is free. Its one fall-through — declared
size is 0, so `rows, cols = 1, shape[0]` — stays.

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
does not dirty the entity, §3.11), and log at `DEBUG` when it changes a
previously declared value.

**Rejected alternative:** raise on a declared/derived mismatch, matching
§3.6. Rejected because it breaks `test_pixel_bits_allocated`'s second and
third calls and forbids a dtype change through the public setter. This is
a judgement call; it is recorded here so a reviewer can disagree with it
on the record rather than discover it.

**Out-of-scope hygiene note.** `BitsStored` (0028,0101), `HighBit`
(0028,0102) and `PixelRepresentation` (0028,0103) are *not* derived from
the array, so a corrected `BitsAllocated` can sit beside a stale
`BitsStored`. That is pre-existing and this design does not change it —
noted so a reviewer does not have to ask why one of four width
descriptors is derived.

### 3.11 Write only what changes

Every descriptor write in `set_pixel_data` becomes conditional on the
value actually differing from the parsed declared value, and
`mark_modified()` is called only if at least one write happened.

This is not an optimisation. `set_attr` bumps `_revision`, so today a pure
read marks the instance dirty and the next `save()` writes the wrong
geometry into SQLite (§1.2). Even with the correct geometry, an idempotent
call must not dirty the graph.

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
PlanarConfiguration per §3.9; BitsAllocated per §3.10. `mark_modified()`
only if something changed.

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
2. **The float branch is unchanged.** It still writes no geometry, for
   the reason already documented there. It benefits only through the
   redaction axes.
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
if geom.samples > 1 and "0028,0006" not in inst.attributes:
    ds.PlanarConfiguration = 0
```

`BitsAllocated`/`BitsStored`/`HighBit`/`PixelRepresentation` keep their
current `inst.attributes.get(..., default)` form.

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

| Caller | Shapes passed | Effect of this design |
| --- | --- | --- |
| `Instance.get_pixel_data` ×3 (`entities.py:444,456,473`) | whatever the loader/pydicom returns | **No longer calls it.** Behaviour change: a load no longer writes attributes and no longer dirties the entity. No test asserts either (checked: `test_sidecar.py:92`, `test_metadata_refactor_full.py:110`, `test_compaction.py:79,155`, `test_blob_storage.py:220`, `test_entities.py:52`, `test_memory_redaction.py:58,81`, `test_redaction_parallel.py:105,150`, `test_redact_error.py:50`, `test_session.py:77`, `test_redaction_wildcard.py:114` all assert on the array only). |
| `RedactionService._apply_roi_to_instance` (`services.py:545`) | the instance's own array, same shape | Same shape, full attributes → DECLARED, no writes, no dirtying. Fixes the multi-frame corruption on the not-writeable path. |
| `InstanceContextBuilder.set_pixel_data` (`builders.py:98`) | pass-through | Unchanged. |
| `scripts/generate_ocr_test_data.py:405` | 2-D CT `(512,512)`; XA multi-frame `(frames,512,512)`. Sets `0028,0008` at `:440`, i.e. **after**. | `512 ∉ {3,4}` → arm A, STRUCTURAL. **No guess, no warning, no change in output.** |
| `scripts/generate_redaction_example.py:129` | `(frames,rows,cols)` CT, rows/cols ≥ 64. Sets Rows/Cols/BitsAllocated at `:131+`, after. | Arm A, STRUCTURAL. Unchanged. |
| `scripts/generate_test_dataset.py:258` | 2-D and multi-frame, 512×512 | Arm A / rank 2. Unchanged. |
| ~50 test sites passing 2-D `np.zeros((10,10))`-shaped arrays | rank 2 | Unchanged. |
| `tests/test_entities.py::test_pixel_unpacking_rgb` — `(100,200,3)`, nothing declared | GUESSED | Passes; **emits a new WARNING**. |
| `tests/test_entities.py::test_photometric_defaults` case 3 — `(10,10,3)`, PI=MONOCHROME2 | GUESSED | Passes (PI corrected per §3.8, PC set); emits a WARNING. |
| `tests/test_sidecar.py:67` — `(50,50,3)`, nothing declared | GUESSED | Passes; emits a WARNING. Reload still `(50,50,3)`. |
| `tests/test_redaction_rgb.py:19-20` — `(100,100,3)` twice | 1st GUESSED, 2nd DECLARED (SPP=3 now present) | Passes. The second call now writes nothing and does not dirty the instance — nothing asserts `has_unsaved_changes` here. |
| `tests/test_compaction.py:106` — replaces `(100,1000)` with `(100,1000)` | rank 2 | Unchanged. |
| `tests/test_blob_storage.py:201,393,438` — 2-D replacements | rank 2 | Unchanged. |
| `tests/test_entities.py::test_pixel_bits_allocated` — 3 dtypes, one instance | rank 2 | Unchanged (§3.10). |
| `tests/test_float_pixel_data_export.py` `_export_one` | rank 2 float | Unchanged; its "apply attrs after set_pixel_data" comment stays accurate. |
| `DicomExporter.write_tree` (fixture generators, no session) | — | Only newly raises on GUESSED or contradiction, neither of which the three generators produce. |
| `DicomExporter.export_batch` / `session.export()` | — | Newly audits a failure instead of writing a wrong file. `ExportSummary.failed` can now be non-zero where it was zero. |
| `RedactionService.apply_redaction_to_array` third-party callers | — | New keyword argument, defaulted; old behaviour when omitted. |

**Costs to accept, stated rather than discovered:**

1. **New WARNING noise** in three or four tests and in any user code that
   sets a colour array on an instance before its attributes. Declaring
   `SamplesPerPixel` before the call silences it. This is the intended
   price of "the guess must not be silent".
2. **`session.export()` can now fail an instance** that previously
   succeeded (wrongly). This is the point; the audit trail (#181) reports
   it.
3. **`write_tree` can now raise** where it previously wrote a wrong file.
4. The three `scripts/` generators are unaffected — verified by reading
   the shapes they pass, not assumed.

---

## 7. Tests the implementation must satisfy

New file `tests/test_pixel_geometry.py` for the resolver, plus additions
to the existing files named. Every row of §5 is a resolver unit test;
those are not repeated here.

### Must now pass (regressions that are currently failures)

1. `ingest → release_memory → get_pixel_data` on a 3-frame 4×4
   `MONOCHROME2` instance leaves **every** attribute in §1.1's table
   unchanged, and leaves `has_unsaved_changes` **False**.
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

### Non-vacuity requirement

Tests 1, 3, 4, 5 and 6 must each fail on `main` at `692218c`. State the
observed failure mode for each in the PR body. A test that passes before
the fix is testing something else.

---

## 8. Out of scope

- **`BitsStored`/`HighBit`/`PixelRepresentation` coherence** (§3.10).
- **The float pixel branch** of `_export_instance_worker` — unchanged; it
  deliberately writes no geometry and that reasoning is already recorded
  there.
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

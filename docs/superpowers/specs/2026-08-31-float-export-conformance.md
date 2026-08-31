# The Float Export Path Writes What The Standard Forbids, And Nothing When It Cannot Decode

**Date:** 2026-08-31
**Status:** Design approved, awaiting implementation
**Tracking:** #223 (three elements PS3.5 §8.2 says "shall not be present"
reach the file, and the fourth member of the same section's exclusion is
deleted by nobody) and #226 (`get_pixel_data()` swallows every
`AttributeError` from `.pixel_array`, so an undecodable float image exports
with no pixel element at all). Second cycle of the **v0.9.1 "Reports
Success While Wrong"** milestone.
**Base:** `main` at `dddb659`.
**Measured on:** `3.12.13` and `3.14.7t` (free-threaded;
`sys._is_gil_enabled()` asserted `False` inside the process after imports),
macOS 25.6.0, pydicom `3.0.2`, numpy `2.5.2`, file-backed `SqliteStore`.
Every figure below was produced from a script run with the worktree on
`PYTHONPATH` and `isocenter.__file__` asserted to resolve inside it —
without that pin a script run from outside the tree imports the main repo's
editable install and measures the wrong code. Every clause that claims "X
changes under mutation Y" was produced by applying Y to a throwaway working
copy and running it; the working copy was reverted with
`git checkout -- isocenter/ tests/` before this spec was committed.

**Not touched by either PR:** `session.py`. #228 is being implemented in it
concurrently. Neither issue needs it.

---

## Context

Both issues are the same sentence with a different object: **an ordinary
`ingest → export` of an ordinary Parametric Map produces a file the
standard rejects, and the run says it went fine.**

- #223 writes *too much*: three elements PS3.5 §8.2 says shall not be
  present sit beside `(7fe0,0008)`, and Pixel Data Provider URL — the
  fourth member of the same section's mutual exclusion — sits beside every
  pixel element on both branches.
- #226 writes *nothing*: a missing-element `AttributeError` from
  `.pixel_array` is turned into "this instance has no pixels" three modules
  away, and the exported file declares a 4×4 32-bit image carrying no pixel
  element of any kind — a missing Type 1.

Both are reachable from `session.ingest()` → `session.export()` on a file
pydicom itself writes. That is the difference from most of #216's blast
radius, which needed a hand-built graph. Both grade `PASS`.

They are nevertheless **two PRs**. §0 rules on that.

---

## 0. One PR or two — the ruling

**Two PRs, in this order: PR 1 (#223) first, PR 2 (#226) rebased on it.**
Clause numbering is contiguous per PR: **§A\*** is PR 1, **§B\*** is PR 2.

They share a subject — the float export seam — and a reviewer will
reasonably ask why not one. Three things decide it:

1. **Disjoint files, disjoint mechanisms.** PR 1 is element emission in
   `isocenter/io_handlers.py`; PR 2 is one `except` clause in
   `isocenter/entities.py`. Neither needs a line of the other's file. A
   combined diff would have no line in common, so bundling buys nothing but
   a shared branch name.
2. **Only one is a behaviour change.** PR 1 removes elements from an
   *outgoing dataset* that the standard says must not be there; nothing
   that used to succeed now fails (measured: the full suite is green on
   both interpreters, §A7). PR 2 makes `session.export()` **fail** an
   instance it used to write, and makes `session.redact()` **raise**
   `RedactionError` where it used to report success (§B3, measured). That
   needs a CHANGELOG entry naming the exception and a migration note; PR 1
   needs an entry that says "this was nonconformant and is now not".
3. **PR 2's ruling is arguable and PR 1's is not.** Whether an undecodable
   instance should fail the export or export empty with a `DATA_LOSS` row
   is a policy call a reviewer may legitimately want to argue (§B0).
   Bundling puts a conformance correction with no downside behind that
   argument.

**The asymmetry a reviewer will find, answered here:** PR 1 itself bundles
a clean silent strip (§A1) with a policy ruling about Pixel Data Provider
URL (§A2), which is exactly the kind of bundling reason 3 rejects. The
difference is that both halves of PR 1 are **one issue, one section of the
standard, and one CHANGELOG entry with two paragraphs** — §8.2's first two
sentences and its third, argued against each other in §A3. Splitting them
would produce two entries that each have to re-derive the other's
reasoning. #226 is a different section, a different module and a different
failure mode.

**Rebase surface:** PR 2 touches `entities.py:477-479` only. PR 1 touches
`io_handlers.py` at `:1086-1102` (comment), `:1130-1132` and `:1169-1170`.

**There is no technical dependency, and the order is not claiming one.**
Disjoint files, disjoint tests, no shared fixture: T7 asserts that *no*
`.dcm` file was written, so PR 1's strip is irrelevant to it, and no other
PR 2 assertion reads an exported element either. The `git rebase` is
empty. The order is for review convenience only — land the conformance fix
that breaks nothing (§A7) before the one whose policy call a reviewer may
want to argue (§B0), so the first is not gated on the second. Either could
land alone.

---

## 1. Reproduction — #223

### 1.1 The claim, checked against the standard

The issue quotes PS3.5 §8.2. Fetched from
`https://dicom.nema.org/medical/dicom/current/output/chtml/part05/sect_8.2.html`
and quoted back verbatim:

> Float Pixel Data (7FE0,0008) is sent in Native Format; the Value
> Representation shall be OF, Bits Allocated (0028,0100) shall be 32, Bits
> Stored (0028,0101), High Bit (0028,0102) and Pixel Representation
> (0028,0103) shall not be present.

> Double Float Pixel Data (7FE0,0009) is sent in Native Format; the Value
> Representation shall be OD, Bits Allocated (0028,0100) shall be 64, Bits
> Stored (0028,0101) and High Bit (0028,0102) and Pixel Representation
> (0028,0103) shall not be present.

> It is not permitted to have more than one of Pixel Data Provider URL
> (0028,7FE0), Pixel Data (7FE0,0010), Float Pixel Data (7FE0,0008) or
> Double Float Pixel Data (7FE0,0009) in the top level Data Set.

And PS3.3 C.7.6.24
(`.../part03/sect_C.7.6.24.html`), also fetched:

> Bits Stored (0028,0101) and High Bit (0028,0102) are not used because the
> stored pixel values always occupy the entire word

> Pixel Representation (0028,0103) is not used because the stored pixel
> values are always signed

and those three attributes **do not appear in Table C.7.6.24-1** at all,
while Float Pixel Data (7FE0,0008), Rows, Columns, Samples per Pixel,
Photometric Interpretation and Bits Allocated are each **Type 1**.

The citation is correct as filed. It is §8.2, not "PS3.5 A.1"; #216's
review already corrected every occurrence of that mistake for this rule,
and the one surviving `A.1` citation (`io_handlers.py:2176`,
`CHANGELOG.md:492`, "`A.1` makes `UN` the VR for an unknown value") is a
different claim and is **not** in scope here.

### 1.2 Measured, from an ordinary ingest

Source: a Parametric Map written with pydicom — `SOPClassUID
1.2.840.10008.5.1.4.1.1.30`, `Modality OT`, `Rows = Columns = 4`,
`SamplesPerPixel 1`, `PhotometricInterpretation MONOCHROME2`,
`BitsAllocated = BitsStored = 32`, `HighBit 31`, `PixelRepresentation 0`,
payload under `(7fe0,0008)` `OF`. This is exactly what
`tests/test_float_pixel_data_export.py::_write_float_src` builds. Run
through `session.ingest(src)` then
`session.export(out, format="dicom", use_compression=False)`:

```
A  float32 → BitsAllocated 32  BitsStored 32  HighBit 31  PixelRepresentation 0
             (7fe0,0008) present   Rows 4  Cols 4  Samples 1  PI MONOCHROME2
             DATA_LOSS/ERROR audit rows: []
C  float64 → BitsAllocated 64  BitsStored 64  HighBit 63  PixelRepresentation 0
             (7fe0,0009) present
             DATA_LOSS/ERROR audit rows: []
```

Three forbidden elements on both float arms, no audit row, no warning.

### 1.3 Where the three come from — traced, then proved by mutation

`_export_instance_worker` writes `ds.BitsAllocated` on the float arms and
nothing else; `ds.BitsStored`/`ds.HighBit`/`ds.PixelRepresentation`
(`io_handlers.py:1202-1204`) are in the **integer** block, which the float
branch cannot reach — it ends `arr = None` at `:1134`, and the integer
block is guarded by `if arr is not None:`. So the only remaining candidate
is `_merge` passing `attributes` straight through, since `populate_attrs`
skips group `7fe0` at ingest but nothing in group `0028`.

Reading that is not proof. Two mutations of the **source file**, both
exported through the ordinary pipeline:

```
F  source declares BitsStored 16, HighBit 15 beside a float32 payload
   (BitsAllocated 32)
   → exported BitsAllocated 32  BitsStored 16  HighBit 15  PixelRepresentation 0
G  source declares NONE of the three
   → exported BitsAllocated 32, and BitsStored / HighBit /
     PixelRepresentation absent from the exported file
     pydicom decodes it: (4, 4) float32 [0.5 1.5 2.5]
```

F settles the origin: the exported values are the *source's* values, not a
`default_bits`-shaped 32/31. G settles the rest: the float branch
contributes nothing of its own, so there is no second writer to also fix —
**and their absence is decodable**, which is the fact the whole of §A1
rests on. Do not take §A1's "pydicom is fine without them" on trust; G is
the run that establishes it.

### 1.4 Measured — Pixel Data Provider URL, both branches

`(0028,7FE0)` has VR `UR`, is not binary, and survives `populate_attrs`, so
it reaches the export from an ordinary ingest of a file that declares it:

```
D  float32 source + declared (0028,7fe0)
   → (7fe0,0008) present AND PixelDataProviderURL present   audit: []
E  uint8   source + declared (0028,7fe0)
   → (7fe0,0010) present AND PixelDataProviderURL present   audit: []
```

Both files violate §8.2's third sentence. Neither branch deletes it, and
#224 declined to add the deletion on the ground that "removing a caller's
element owes them a loss row" — which is the decision §A2 makes.

---

## 2. Reproduction — #226

### 2.1 The mechanism

`Instance.get_pixel_data()` (`entities.py:477-479`):

```python
                except (AttributeError, TypeError):
                    # No pixel data element
                    return None
```

`Dataset.pixel_array` raises `AttributeError` for a family of reasons that
are not "there is no pixel element". Measured on the source below:

```
pydicom.dcmread(src).pixel_array
→ AttributeError: Missing required element: (0028,0006) 'Planar Configuration'
```

Source: the same Parametric Map as §1.2 but with `SamplesPerPixel 3` and no
`PlanarConfiguration`. It is nonconformant input (`MONOCHROME2` beside
three samples violates C.7.6.3.1.2) — but that is not what the export
reports.

### 2.2 Measured, full pipeline, on `dddb659`

`ingest → examine → audit → anonymize → export → generate_report`:

```
ok  (samples=1)  files written = 1   | **Validation Status** | **PASS** |
                 "No exceptions or errors were recorded."
bad (samples=3)  files written = 1   | **Validation Status** | **PASS** |
                 "No exceptions or errors were recorded."
```

and the one file `bad` wrote:

```
Rows 4  Cols 4  SamplesPerPixel 3  BitsAllocated 32  BitsStored 32
(7fe0,0008) present: False        (7fe0,0010) present: False
```

A 4×4 three-sample 32-bit image with no pixel element. Float Pixel Data is
Type 1 in C.7.6.24 (§1.1). This is the defect #193 removed from this path
and #160 removed from the waveform path, arriving through a different door.

### 2.3 The issue's stated cause is wrong, and the correction changes the fix

#226 says:

> `Modality` is `OT` for a Parametric Map, which is not in
> `_IMAGE_MODALITIES`, so the export proceeds

**`"OT"` is in `_IMAGE_MODALITIES`** — `io_handlers.py:175-176`, and has
been since #208 (`git log -L 175,177:isocenter/io_handlers.py`):

```python
_IMAGE_MODALITIES = frozenset({"CT", "MR", "US", "DX", "CR",
                               "MG", "NM", "PT", "XA", "RF", "SC", "OT"})
```

The real reason the export proceeds is that the modality guard sits inside
`except FileNotFoundError:` (`io_handlers.py:944-954`). `get_pixel_data()`
does not raise — it *returns* `None` — so the `except` body never runs and
the guard is never consulted.

This matters because it makes the obvious alternative fix look available
when it is not. **Mutation 1**, run: hoist the guard so it applies to every
`arr is None`:

```python
        if arr is None:
            mod = inst.attributes.get("0008,0060", "OT")
            if mod in _IMAGE_MODALITIES:
                raise RuntimeError(f"Pixels missing for Image Modality {mod}")
```

Result on `dddb659` + that mutation:

```
tests/test_io_no_pixels.py::test_reproduce_no_pixel_data_crash FAILED
E  assert (export_dir / "Subject_123").exists()
E  AssertionError: assert False
```

Its fixture is a CT Defined Procedure Protocol Storage instance — a
genuinely non-image SOP class — that declares **no Modality at all**, so
`inst.attributes.get("0008,0060", "OT")` yields the default `"OT"`, which
is in the set, and a legitimately pixel-less instance is refused. Widening
the guard is therefore ruled out on measurement, not on taste. §B0 fixes
the distinction where it is actually knowable.

---

## 3. Why the suite misses both

- **#223.** No test asserts on `BitsStored`/`HighBit`/`PixelRepresentation`
  in an *exported float* file. `grep -ln` for the three tags across
  `tests/*.py` intersected with the float-touching files yields exactly
  `test_float_pixel_data_export.py` and `test_private_binary_ingest.py`
  (§4). In the first, the three appear only as *fixture inputs*
  (`:83-84,:97,:260-261,:302-303,:348-349,:460-461`); the only assertion
  near them is `exported.BitsAllocated == expected_bits` (`:465`). In the
  second they are on 8-bit integer sources (`:63-67,:592-596`). Nothing
  looks at the outgoing file for their presence.
- **#223, URL half.** `grep -rn "PixelDataProviderURL\|0028,7fe0"` over the
  whole repo returns three hits: `CHANGELOG.md:126`,
  `io_handlers.py:1087` and a docstring at
  `tests/test_float_pixel_data_export.py:705`. All three are *prose about*
  the exclusion. No test constructs the element.
- **#226.** The one fixture that could have hit it is inoculated against
  it. `_write_float_src` writes `PlanarConfiguration = 0` whenever
  `samples > 1` (`tests/test_float_pixel_data_export.py:87-96`) — a
  test-side compensation for this exact defect, with a comment saying so.
  §B8 rules on it.

---

# PR 1 — #223

## §A0. The shape

Two edits in `_export_instance_worker`, both in `isocenter/io_handlers.py`,
both **float-branch-scoped except one**. Nothing else changes. No new
imports; `pixel_geometry.py` is untouched (its pure-stdlib AST test is
unaffected).

## §A1. Strip Bits Stored, High Bit and Pixel Representation on the float arms

Inside the float branch, in the arm that actually wrote a pixel element —
`if arr.itemsize in (4, 8):` at `io_handlers.py:1130` — delete the three
elements from `ds` before the `_write_pixel_geometry` call:

```python
            if arr.itemsize in (4, 8):
                # PS3.5 Section 8.2's *other* sentence, the one this branch
                # has never enforced: "Bits Stored (0028,0101), High Bit
                # (0028,0102) and Pixel Representation (0028,0103) shall
                # not be present." Deleted rather than merely not written,
                # for the same reason the pixel elements below are: `_merge`
                # has already put whatever `attributes` holds onto `ds`, and
                # `populate_attrs` skips only group 7fe0, so all three
                # arrive from an ordinary ingest of a real Parametric Map
                # (#223). Measured before the fix: BitsStored 32, HighBit
                # 31, PixelRepresentation 0 beside (7fe0,0008), no loss row,
                # PASS.
                #
                # No DATA_LOSS row, and that is the deliberate difference
                # from the Pixel Data Provider URL below. These three carry
                # nothing a recipient can want: C.7.6.24 says they are "not
                # used because the stored pixel values always occupy the
                # entire word" and "always signed", so their content is
                # fixed by the standard rather than by the source file. The
                # same sentence fixes BitsAllocated to 32 and this branch
                # has silently overwritten *that* since #170 -- one sentence,
                # one class of element, one silent action.
                #
                # From `ds`, never from `inst.attributes`: the graph is
                # re-exportable and `SidecarPixelLoader` reads "0028,0103"
                # to reconstruct dtype (io_handlers.py:1430). Mutating the
                # graph here would be a read-path write and a real loss.
                for kw in ("BitsStored", "HighBit", "PixelRepresentation"):
                    if kw in ds:
                        del ds[kw]
```

Three constraints on the placement, each of which has a way to get it
wrong:

1. **Inside `if arr.itemsize in (4, 8):`, not the float branch as a whole.**
   The float16 arm writes no pixel element, so nothing there forbids the
   three — the same reason the existing `other` deletion at `:1103-1106` is
   also outside that arm. Measured (§A6/T3): a float16 instance keeps all
   three.
2. **`ds`, not `inst.attributes`.** See the comment. `git grep 0028,0103`
   finds `io_handlers.py:1430` — `SidecarPixelLoader` reconstructs dtype
   from it.
3. **Not inside `_write_pixel_geometry`.** That helper is shared with the
   integer branch and its contract is "write the descriptors that describe
   the pixel element just written". Making it also *delete* broadens the
   contract, and its own docstring already argues that `BitsAllocated` — the
   same sentence, the same class — stays out of it because each branch owns
   its own statement. Considered and rejected; a reviewer will ask.

**Float-branch-only, as required.** The integer arm keeps
`ds.BitsStored`/`ds.HighBit`/`ds.PixelRepresentation` at `:1202-1204`
untouched — they are required there, and T4 is the guard.

## §A2. Delete Pixel Data Provider URL, with a `DATA_LOSS` row, on both branches

`(0028,7FE0)` is the fourth member of §8.2's third sentence. Delete it
wherever a pixel element was written, and file a loss row.

Float branch, inside the same `if arr.itemsize in (4, 8):` block at
`:1130`, immediately after §A1's strip. **Not** next to the existing
`del ds[other]` at `:1105-1106`: that statement is at the float-branch
level and is float16-safe only because `{4: …, 8: …}.get(arr.itemsize)`
returns `None` for a float16 array — an incidental guard, not the one this
clause needs. The `itemsize in (4, 8)` block is the explicit spelling of
"a pixel element was written", and it is what the measurements in §A4/§A6
were taken against.

```python
                if "PixelDataProviderURL" in ds:
                    del ds.PixelDataProviderURL
                    losses.append((
                        LOSS_SCOPE_STANDARD,
                        "Pixel Data Provider URL (0028,7fe0) was not "
                        "exported: PS3.5 Section 8.2 permits only one of "
                        "it, Pixel Data (7FE0,0010), Float Pixel Data "
                        "(7FE0,0008) and Double Float Pixel Data "
                        "(7FE0,0009) in the top level Data Set, and this "
                        "instance's pixels were written under "
                        "(7fe0,0008)/(7fe0,0009). The URL named pixel data "
                        "held elsewhere; the exported file carries its own."))
```

Integer branch, inside `if arr is not None:`, immediately before the
`_write_pixel_geometry(..., float_element=False)` call at `:1169`, with the
tag in the message changed to `(7fe0,0010)`.

### Why deletion-with-a-row rather than silence or refusal

**Not silence.** The URL is a caller's element carrying a value nothing
else in the file records. Deleting it silently is the "not writing an
element was never the same as removing it" trap in reverse, and #224
declined for exactly this reason. `ExportOutcome.losses` is the channel
#126/#146 built for this.

**Not refusal.** #193's lesson is that refusing to write trades a silent
corruption for a quiet nonconformance — a Parametric Map with no pixel
element is invalid in a way #160 had just finished fixing elsewhere. The
input here is repairable: the exporter has the real bytes in hand and the
URL is a stale pointer to pixels held somewhere else. Refusing would fail
an export over an element the file no longer needs.

**Consistency with §A1, stated plainly, because they are the same section
and get different treatment.** The line is not "derived vs. declared" — it
is *whether deletion removes information a recipient could have wanted*:

| element | what deleting it removes | action |
| --- | --- | --- |
| `BitsStored`, `HighBit`, `PixelRepresentation` beside a float element | nothing — C.7.6.24 fixes their meaning; the standard's own words are "not used because…" | silent (§A1) |
| `BitsAllocated` beside a float element | nothing — §8.2 fixes it to 32 or 64 | silent, **already the behaviour** since #170 |
| Pixel Data Provider URL | a URL the exported file does not otherwise carry | delete + `DATA_LOSS` (§A2) |

**The inconsistency this does not resolve, named rather than papered over.**
The existing `del ds.PixelData` (`:1082`) and `del ds[other]`
(`:1103-1106`) delete *payload* silently. By the table above those owe a
loss row. They are left alone here because (a) they are #170/#216's
behaviour and changing them is not what #223 asks for, (b) group `7fe0` is
skipped by `populate_attrs`, so unlike the URL they are unreachable from an
ordinary ingest, and (c) a file carrying two pixel elements is one pydicom
refuses to read back at all, so the "recipient could have wanted it"
question does not arise the way it does for a URL. If a reviewer wants
those rows too, that is a separate issue — §10 files it.

### Grading, stated because a reviewer will ask

`LOSS_SCOPE_STANDARD` (group `0028` is even; the parity rule is
`loss_scope_for_tag`, and the literal is used here to match the neighbouring
float16 loss row at `:1072-1077`, which spells it the same way with the same
comment). Per #146, a `STANDARD` loss row does **not** move
`validation_status`, so the URL case files a visible `DATA_LOSS` row —
surfaced in the compliance report by #148 — and still grades `PASS`.
Inventing a new grading rule here is scope creep; whether `STANDARD` losses
should grade harder is open on #150.

## §A3. The comment that must survive

The existing block comment at `:1086-1102` quotes §8.2's third sentence and
explains why the *pixel* elements are deleted rather than merely not
written. Extend it, do not replace it: the new URL clause is the fourth
direction of the same sentence, and the new §A1 block is the first two
sentences. A future reader who deletes either "because `_merge` already
handles attributes" reintroduces both defects. State in the comment that
`(0028,7FE0)` is *reachable from an ordinary ingest* while the `7fe0`
members are not — that asymmetry is the whole reason one gets a loss row.

## §A4. Measured behaviour of the proposed fix

Prototype applied, ordinary pipeline, 3.12.13:

```
float32,             use_compression=False → TS 1.2.840.10008.1.2
    (7fe0,0008) True | BitsStored False | HighBit False | PxRep False
    | URL n/a | BitsAlloc 32 | decodes (4,4) float32 [0.5 1.5 2.5]
float32,             use_compression=True  → TS 1.2.840.10008.1.2
    identical to the line above (the float branch sets arr = None, so
    nothing is ever handed to _compress_j2k; float export is uncompressed
    either way, before this change and after it)
float32 + URL,       use_compression=False → URL False,
    DATA_LOSS/STANDARD row filed
float32 + URL,       use_compression=True  → same
uint8   + URL,       use_compression=False → (7fe0,0010) True, URL False,
    DATA_LOSS/STANDARD row filed, BitsStored/HighBit/PxRep all still True
uint8   + URL,       use_compression=True  → TS 1.2.840.10008.1.2.4.90,
    URL False, row filed, decodes (4,4) uint8 [0 1 2]
uint8   no URL                             → unchanged, no rows
```

**Why the compressed integer row matters to the implementation.** With
`ctx.compression` set, `ds.PixelData` is never assigned in the worker —
`_finalize_dataset` compresses from the array. So a "did we write a pixel
element?" gate implemented as `"PixelData" in ds` would be **wrong on the
compressed path** and the URL would survive there. The gate must be
structural: *we are inside the integer `if arr is not None:` block*, or
*inside the float `if arr.itemsize in (4, 8):` arm*. Both spellings above
are structural. Do not "simplify" either into a membership test.

**Round-trip idempotence**, measured — source → export (gen1) → re-ingest →
export (gen2):

```
gen0  (7fe0,0008) True | BitsStored True  | Rows 4 Cols 4 Samples 1 | decodes float32
gen1  (7fe0,0008) True | BitsStored False | Rows 4 Cols 4 Samples 1 | decodes float32
gen2  (7fe0,0008) True | BitsStored False | Rows 4 Cols 4 Samples 1 | decodes float32
```

gen1 == gen2. Re-ingesting a stripped file does not resurrect the elements
and does not lose the geometry — the float pair is never sidecar'd
(`ingest_worker` routes on `if "PixelData" in ds`), so `get_pixel_data()`
re-reads the source file and `SidecarPixelLoader`'s dependence on
`0028,0103` is never on this path.

## §A5. Files touched — PR 1

| file | change |
| --- | --- |
| `isocenter/io_handlers.py` | §A1 block inside `:1130`'s arm; §A2 float clause in the same arm; §A2 integer clause before `:1169`; §A3 comment extension at `:1086-1102` |
| `tests/test_float_pixel_data_export.py` | new tests T1–T5 |
| `CHANGELOG.md` | one `Fixed` entry, two paragraphs (§A8) |

## §A6. Tests, with polarity

All in `tests/test_float_pixel_data_export.py`, which already owns this
path and its `_roundtrip` / `_export_one` / `_export_raw` helpers.

**Fixture rule for every test below.** The source or the `attributes` dict
**must declare all three elements explicitly**. Measurement G is the reason:
when the source declares none, the export writes none, so
`assert "BitsStored" not in exported` passes with the fix *reverted*. A
fixture that forgets to declare them produces a vacuous test. `_roundtrip`'s
`_write_float_src` already declares `BitsStored`, `HighBit` and
`PixelRepresentation` (`:83-84,:97`) — use it, and do not silently drop
them.

**Second fixture rule.** For the graph-built cases, set the array with a
direct `inst.pixel_array = arr` assignment or apply `attributes` *after*
`set_pixel_data`. `Instance.set_pixel_data` writes `0028,0100` (and Rows,
Columns, SamplesPerPixel, NumberOfFrames, PhotometricInterpretation,
PlanarConfiguration) — it does **not** write `0028,0101/0102/0103`, so it
cannot supply the values under test here; the rule is stated so a later
edit that switches to a setter does not quietly make the test vacuous.
`_export_one`'s existing "apply `attrs` AFTER `set_pixel_data`" comment
(`:352-361`) is the same rule for `0028,0100`.

### T1 — an ordinary float32 ingest→export carries none of the three. **Detection.**

`_roundtrip` with the default `_write_float_src`. Assert `(0x7FE0,0x0008) in
exported` **and** `"BitsStored" not in exported` and `"HighBit" not in
exported` and `"PixelRepresentation" not in exported` and
`exported.BitsAllocated == 32`. Also assert `exported.pixel_array` decodes
to the source values — absence must not cost decodability (measurement G).
Red on `dddb659` (measured: 32 / 31 / 0 present).

### T2 — the same for float64 under `(7fe0,0009)`. **Detection.**

`_roundtrip(tag=0x7FE00009, vr='OD', dtype=np.float64, bits=64)`. Assert
`BitsAllocated == 64` and the three absent. Red on `dddb659` (measured:
64 / 63 / 0 present). Separate from T1 because §8.2 states the rule twice,
once per element, and a fix keyed on `itemsize == 4` would pass T1 alone.

### T3 — the float16 arm keeps all three. **Selectivity guard — not evidence.**

Graph-built instance, `Modality "SR"` (an image modality here hits the
existing "Pixels missing for Image Modality" refusal — measured, and the
existing float16 tests at `:255` and `:299` already split on exactly that),
`float16` array, all three declared. Assert the export is `ok`, that its
only loss is the existing `Pixel data is float16 and was not written` row,
and that `BitsStored`/`HighBit`/`PixelRepresentation` are **present**.
Measured with the prototype: present, and the URL survives too. Fails if
§A1 is hoisted out of the `itemsize in (4, 8)` arm.

### T4 — an integer export keeps all three. **Selectivity guard — not evidence.**

`uint8` source declaring `BitsStored 8`, `HighBit 7`,
`PixelRepresentation 0`. Assert `(0x7FE0,0x0010) in exported` and all three
**present** with those values. Measured with the prototype: `8 / 7 / 0`.
This is what fails if the deletion is made unconditional; without it every
float test still passes.

### T5 — Pixel Data Provider URL: gone, and a loss row says so. **Detection.**

Parameterise over the two branches. Source declares
`(0028,7FE0) UR "http://example.org/px"`.

- float32 source → assert `(0x7FE0,0x0008) in exported`,
  `0x00287FE0 not in exported`, **and** a `DATA_LOSS` row exists in
  `audit_log` with `loss_scope == "STANDARD"` whose details mention
  `0028,7fe0`.
- uint8 source → the same with `(0x7FE0,0x0010)`.

**Both halves are required.** Absence alone would pass if `_merge` had
dropped the element for an unrelated reason; the row is what pins that the
exporter deleted it deliberately. `_roundtrip` already returns
`(details, loss_scope)` rows from `audit_log` — use them.

Red on `dddb659` for both branches (measured D and E: URL present, `audit:
[]`).

### T6 — an instance with a URL and no pixel array keeps the URL. **Selectivity guard.**

Graph-built, `Modality "SR"`, no `pixel_array`, `(0028,7fe0)` declared.
Assert the URL is **present** in the exported file and `outcome.losses` is
empty. Measured with the prototype: present, no rows. A URL alone in the
top-level Data Set is legal — §8.2 forbids *more than one* of the four —
and this is what fails if the deletion is hoisted above the
`arr is not None` gate.

### Compression is covered by §A4's measurements, not by a test

A `use_compression=True` case adds a J2K dependency to a test file that has
none and pins behaviour identical to the uncompressed case (§A4). The
structural-gate constraint it exists to protect is stated in the code
comment instead. If the coder prefers a test, put it in
`tests/test_compress_handlers.py`, not here.

## §A7. Measured suite result for PR 1

Prototype (§A1 + §A2, both branches) applied to `dddb659`:

```
3.12.13   967 passed, 1 skipped in 141.25s
3.14.7t   967 passed, 1 skipped in 132.29s   (_is_gil_enabled() == False)
```

and the nine files most likely to be affected —
`test_float_pixel_data_export test_pixel_geometry_pipeline
test_private_binary_ingest test_export_contract test_export_pixels
test_api_coherence test_io_no_pixels test_export_loss_audit
test_data_loss_reporting` — `136 passed`. **PR 1 breaks nothing.** That is
the fact that makes it a conformance correction rather than a behaviour
change.

**The IOD validator, asked separately.** A green suite is not proof that
`_finalize_dataset`'s validation tolerates the strip, because every float
export the suite performs -- and every measurement above -- uses
`SOPClassUID 1.2.840.10008.5.1.4.1.1.30` (Parametric Map). The existing
Secondary Capture cases at `:257`/`:304` are on the float16 arm, which
writes no pixel element and never reaches §A1. So the strip was run
against four SOP classes x two float widths, graph-built, all three
elements declared:

```
                          dddb659      with §A1
ParametricMap/f32,f64     ok=True      ok=True   (three elements absent)
SecondaryCapture/f32,f64  ok=True      ok=True   (three elements absent)
MRImage/f32,f64           ok=True      ok=True   (three elements absent)
CTImage/f32,f64           ok=False     ok=False  (identical message)
```

The CT rows fail on **both** for a reason that is not this change -- the
probe's minimal fixture omits Study Date, Study Time, Slice Thickness,
KVP, Image Position/Orientation Patient and Pixel Spacing:

```
Validation Errors: ['[Type 1 Error] Missing 0008,0020 in Common',
 '[Type 1 Error] Missing 0008,0030 in Common',
 '[Type 2 Error] Missing 0018,0050 in CTImage',
 '[Type 2 Error] Missing 0018,0060 in CTImage',
 '[Type 1 Error] Missing 0020,0032 in CTImage',
 '[Type 1 Error] Missing 0020,0037 in CTImage',
 '[Type 1 Error] Missing 0028,0030 in CTImage']
```

byte-identical before and after, and naming none of `0028,0101`,
`0028,0102`, `0028,0103`. The validator has no opinion about the three on
any SOP class tested. That is consistent with §2.2, where it accepted a
file with **no pixel element at all** -- but it was measured rather than
inferred from it, because §0's reason 2 and this section's "breaks
nothing" both rest on it.

## §A8. CHANGELOG entry — PR 1

One `Fixed` entry with two paragraphs, matching the depth of the #216 entry
above it:

- **Paragraph 1.** The three elements. Quote §8.2's first sentence
  verbatim and cite it as **Section 8.2** — not A.1. Give the measured
  before (`BitsAllocated 32 BitsStored 32 HighBit 31 PixelRepresentation
  0` beside `(7fe0,0008)`, from a plain `ingest → export` of a
  pydicom-written Parametric Map, grading `PASS`). Say they came through
  `_merge`, not from a default, and that the float branch never wrote one —
  so "not writing them" was never going to be enough. Say they are deleted
  from the outgoing dataset and **not** from the graph. Say no loss row,
  and why: C.7.6.24's own words, plus the fact that `BitsAllocated` from
  the same sentence has been silently overwritten since #170.
- **Paragraph 2.** Pixel Data Provider URL. Quote the third sentence.
  State that this closes the fourth direction of the exclusion that #216
  left open and #224 declined, that it applies to **both** branches, that
  it is reachable from an ordinary ingest (unlike the `7fe0` members), and
  that it files a `DATA_LOSS` row with `STANDARD` scope — so the element's
  removal is in the compliance report and, per #146, does not by itself
  move `validation_status`.

## §A9. Out of scope for PR 1

- The surviving `PS3.5 A.1` citation at `io_handlers.py:2176` /
  `CHANGELOG.md:492`. Different claim (`UN` as the VR for an unknown
  value), different section, not checked here. #223 says so explicitly.
- Adding loss rows to the existing silent `del ds.PixelData` /
  `del ds[other]` deletions (§A2, and §10 files it).
- Whether `STANDARD` losses should grade harder — #150.
- `BitsStored`/`HighBit`/`PixelRepresentation` *coherence* on the integer
  path. Out of scope by the pixel-geometry spec's §3.10 and unchanged here.

---

# PR 2 — #226

## §B0. The ruling on the undecodable-pixel question

**Raise.** Narrow the existing `except (AttributeError, TypeError)` so it
returns `None` only when the file genuinely holds no pixel element, and
lets the exception through when it holds one and the decode still failed.

Three candidates were on the table.

**Rejected: widen the export-side modality guard (the #193 precedent).**
Ruled out on measurement, not taste — §2.3, Mutation 1:
`tests/test_io_no_pixels.py` goes red because `"OT"` is in
`_IMAGE_MODALITIES` and its fixture declares no Modality at all. The guard
is also in the wrong place to answer the question: at the export site
`arr is None` has already lost the distinction between "no pixels declared"
and "pixels declared, undecodable". Only `get_pixel_data()` can still see
it.

**Rejected: return `None` and file an audit row from `entities.py`.**
`Instance` has no `store_backend`. `grep -n "store_backend\|log_audit"
isocenter/entities.py` returns nothing; the only reporting channel it can
reach is `get_logger()`, and a log line is precisely what #181 established
is not a compliance record. Every existing audit row on this path is filed
by the *parent* from an `ExportOutcome`, which is the machinery raising
already routes into.

**Chosen: raise.** The exception reaches
`_export_instance_worker`'s outer `except Exception` (`:1270`) and becomes
`ExportOutcome(ok=False, error=e)` → an `ERROR` audit row via
`_report_export_failures` → `REVIEW_REQUIRED`. That is the same shape
#215's geometry refusal takes, and the caller is entitled to hear about it
per #191/#209.

### The distinguishing check

The exception text is not a reliable discriminator, and matching on it
would be a fourth spelling of "is there pixel data" in this file. Ask the
dataset instead:

```python
                except (AttributeError, TypeError):
                    # "No pixel data element" was the intent and is still
                    # right -- but `.pixel_array` raises AttributeError for
                    # a whole family of reasons that are not that, and this
                    # arm called every one of them "this instance has no
                    # pixels". Measured: a Parametric Map declaring
                    # SamplesPerPixel 3 with no Planar Configuration raises
                    # `AttributeError: Missing required element: (0028,0006)
                    # 'Planar Configuration'`, and the export wrote a 4x4
                    # 32-bit image with no pixel element of any kind -- a
                    # missing Type 1 -- and graded PASS (#226).
                    #
                    # Ask the dataset, not the message. If the file holds
                    # one of the three pixel elements and the decode still
                    # failed, this is "pixels this library cannot decode",
                    # which is a different outcome from "no pixels" and one
                    # the caller is entitled to hear about (#191, #209).
                    #
                    # `ds is not None` keeps `dcmread`'s own AttributeError
                    # /TypeError on the old path deliberately: that is a
                    # different failure and narrowing this arm is not the
                    # place to change it.
                    if ds is not None and any(
                            t in ds for t in (0x7FE00010, 0x7FE00008,
                                              0x7FE00009)):
                        raise
                    return None
```

**Why a bare `raise` and not a new `RuntimeError`.** The re-raised
`AttributeError` is caught by the *outer* `except Exception` already in
this method (`entities.py:486`), which tries the `imagecodecs_handler`
fallback and then ends in

```python
raise RuntimeError(f"Lazy load failed for {self.file_path}: {e}") from e
```

That interpolates pydicom's original text into the message, which is what
matters: `ExportOutcome.error` crosses a process boundary
(`session.export()` is always processes, #185) and `_report_export_failures`
renders `str(r.error)`. `__cause__` does not survive pickling; the message
does. Measured audit row:

```
ERROR | Export failed for …/….dcm: Lazy load failed for …/f.dcm:
        Missing required element: (0028,0006) 'Planar Configuration'
```

Adding a second `RuntimeError` here would either duplicate that message or
bypass the codec fallback. Do neither.

**Why `0x7FE00010, 0x7FE00008, 0x7FE00009` as int tags here, while §A2
uses keywords.** Both spellings work and both were measured. The
difference is which question each site is asking. §A2 names *one* element
it is about to delete, and the keyword is the element's name — it reads as
`del ds.PixelDataProviderURL`, matching the `del ds.PixelData` two lines
above it. §B0 asks a *set membership* question about the three pixel
elements as a group, which is the same question `_is_routed` /
`_ROUTED_BINARY_TAGS` / `_FLOAT_PIXEL_TAGS` ask in `io_handlers.py:55-114`
— and those spell it with `Tag(0x7fe0, 0x0010)`. One spelling per
question, not one per file.

**Not a reason:** pydicom's `UserWarning: Invalid value … used with the
'in' operator`. That fires on an *invalid* keyword — the
`'_ISOCENTER_REDACTION_HASH'` key this pipeline carries in `attributes`,
observed in every run in this spec. `"PixelData"`, `"FloatPixelData"` and
`"DoubleFloatPixelData"` are valid keywords and produce no warning, so
#144's "do not silence pydicom's warnings" is not an argument against them.
Recorded because it is the plausible-sounding reason a reader would
reconstruct.

**Scope of the narrowing, stated exactly.** Three behaviours are
deliberately *unchanged*: `dcmread` raising `AttributeError`/`TypeError`
(still `None`), a file with no pixel element (still `None`), and the
`"no pixel data" in str(e)` arm below (untouched). Only "pixel element
present, `.pixel_array` raised `AttributeError` or `TypeError`" moves.

## §B1. Measured behaviour of the proposed fix

Prototype applied to `dddb659`, full pipeline
`ingest → examine → audit → anonymize → export → generate_report`:

```
                 dddb659                          with §B0
ok  (samples=1)  1 file,  PASS                    1 file,  PASS         (unchanged)
bad (samples=3)  1 file,  PASS,                   0 files, REVIEW_REQUIRED,
                 "No exceptions or errors        ERROR row: "Lazy load failed
                  were recorded."                 for …: Missing required
                                                  element: (0028,0006)
                                                  'Planar Configuration'"
```

The report's **Exceptions & Errors** section moves from *"No exceptions or
errors were recorded."* to a warning callout with the row. That is the
whole of the issue.

## §B2. Files touched — PR 2

| file | change |
| --- | --- |
| `isocenter/entities.py` | `:477-479` — the narrowed `except`, plus the comment |
| `tests/test_float_pixel_data_export.py` | new tests T7–T9 |
| `tests/test_io_no_pixels.py` | unchanged; named in §4 as the selectivity anchor |
| `CHANGELOG.md` | one `Fixed` entry, breaking-flavoured (§B6) |

`isocenter/services.py` is **not** edited and is nevertheless PR 2's
largest compatibility item. §B3.

## §B3. Blast radius, per caller — surveyed by grep, all four

`grep -rn 'get_pixel_data' --include='*.py' .` (excluding `.venv`,
`build/`) gives four production call sites of `Instance.get_pixel_data`.
`isocenter/imagecodecs_handler.py:88` is a different function with the same
name and is not a caller.

| caller | what catches it | outcome after §B0 | verdict |
| --- | --- | --- | --- |
| `io_handlers.py:942` `_export_instance_worker` | its own outer `except Exception` at `:1270` | `ExportOutcome(ok=False)` → `ERROR` audit row → `REVIEW_REQUIRED`; the instance is not written and is counted in `ExportSummary.failed` | **intended** — this is the fix |
| `services.py:353` `execute_redaction_task` | `except Exception` at `:401` | `RedactionOutcome(ok=False)` → #213's `RedactionError` from `session.redact()`, plus an `ERROR` row | **intended, and the largest change** |
| `services.py:606` `redact_machine_instances` | `except Exception` in the same shape | appended to `failures`, same reporting | same |
| `pixel_analysis.py:182` `analyze_pixels` | `except Exception` at `:220` → `logger.error(...)`, returns `[]` | a log line where there was silence; the OCR scan still finds nothing | no new failure surface; §10 files the gap |

**The redaction change, measured.** Fixture: the samples=3 Parametric Map,
`DeviceSerialNumber "SN-1"`, `session.redact_by_machine("SN-1", [0,2,0,2])`:

```
dddb659   samples=1: no exception        samples=3: no exception, no audit rows
with §B0  samples=1: no exception        samples=3: RedactionError: "Redaction
                                          failed for 1 of 1 instances; their
                                          pixel data still carries whatever the
                                          configured zones were meant to remove"
                                          + ERROR audit row
```

This is a **strengthening, not a regression**: today an instance whose
declared pixels will not decode is reported as successfully redacted while
nothing touched its pixels — which is the same "reports success while
wrong" shape as the export half, on the PHI-bearing path. It must
nevertheless be in the CHANGELOG, because `session.redact()` newly raises
where it used to return.

`_export_instance_worker`'s `except FileNotFoundError` arm and its
`_IMAGE_MODALITIES` guard are **untouched**. An instance with neither a
loader nor an existing `file_path` still reaches `FileNotFoundError` and
the guard still decides; §2.3's Mutation 1 is not applied.

`write_tree` (the serializer path, no session) surfaces the same failure
through its own `RuntimeError("Export incomplete. …First error: …")`
wrapper at `:1986-1991`, which interpolates the first failure's message.
Naming this in the CHANGELOG matters: it is the path the `scripts/` fixture
generators use.

## §B4. Measured suite result for PR 2

Prototype (§B0 alone, `io_handlers.py` reverted) applied to `dddb659`:

```
3.12.13   967 passed, 1 skipped in 133.07s
3.14.7t   967 passed, 1 skipped in 116.84s   (_is_gil_enabled() == False)
```

Nothing in the suite currently reaches the narrowed arm with a pixel
element present. The tests that *do* depend on `get_pixel_data()` returning
`None` reach it another way and are unaffected — §4 names each.

## §B5. Tests, with polarity

### T7 — a float instance whose pixels will not decode fails the export. **Detection.**

`_roundtrip`-shaped, but the source **must be a real file written by
pydicom** with `SamplesPerPixel 3` and **no** `PlanarConfiguration`.

**Do not build this fixture through `set_pixel_data`.** That setter writes
`PlanarConfiguration` itself when the array is colour and the attribute is
undeclared — it would supply the exact value under test and the test would
pin nothing. This is the #216-spec-test-4a trap, one setter over. Write the
source with `_write_float_src(..., samples=3)` and a new
`planar=False`-style switch that omits the element, or write the dataset
inline in the test.

Assert: **no** `.dcm` file was written; an `ERROR` row exists in
`audit_log`; its details contain `Planar Configuration`. Red on `dddb659`
(measured: one file written, no rows, `PASS`).

Assert on the audit row's *text*, not just its existence — the export
worker files `ERROR` rows for every failure shape, so existence alone
would pass if the write had failed for an unrelated reason. Same reasoning
as the geometry-refusal test's message assertion at `:753-760`.

### T8 — the report says so. **Detection, end-to-end.**

Same fixture, full pipeline through `generate_report(path)`. Assert the
markdown contains `REVIEW_REQUIRED` **and** does *not* contain
`No exceptions or errors were recorded`. Measured both ways in §B1. This is
the clause the issue title is about, and it is the one an assertion on the
exported file alone would miss.

### T9 — an instance with genuinely no pixel element still returns `None`. **Selectivity guard — not evidence.**

Two halves, because they fail to different mutations:

- **Unit.** A real DICOM file on disk with no pixel element of any kind;
  `Instance(file_path=...)`; assert `get_pixel_data() is None` and that
  nothing raised. Fails if the narrowing is implemented as an unconditional
  `raise`.
- **Pipeline.** `tests/test_io_no_pixels.py::test_reproduce_no_pixel_data_crash`
  is already exactly this end to end and must stay green — it is the test
  Mutation 1 turned red (§2.3). Cite it in the PR body; do not duplicate
  it.

### Not a test: the redaction change

§B3's `RedactionError` measurement is a compatibility finding, not new
coverage. If the coder wants it pinned, it belongs in
`tests/test_redaction_failure_is_reported.py` alongside #213's other
outcomes, not in the float export file. Optional; state the decision in the
PR body either way.

## §B6. CHANGELOG entry — PR 2

`Fixed`, written breaking-side-first because the observable change is a new
failure:

- Name the exception a previously-working call now raises. `session.export()`
  does not raise — it returns an `ExportSummary` with `failed == 1`, files
  an `ERROR` audit row and grades `REVIEW_REQUIRED`.
  `DicomExporter.write_tree()` **does** raise `RuntimeError("Export
  incomplete. 1 failed. First error: Lazy load failed for …: Missing
  required element: (0028,0006) 'Planar Configuration'")`. `session.redact()`
  and `redact_by_machine()` now raise `RedactionError` for such an instance.
- Say why the old behaviour was wrong, with the measured before: a 4×4
  three-sample 32-bit image with no pixel element, Float Pixel Data being
  Type 1 in C.7.6.24, grading `PASS` with "No exceptions or errors were
  recorded".
- State the exact narrowing: only "the file holds one of `(7fe0,0010)`,
  `(7fe0,0008)`, `(7fe0,0009)` and `.pixel_array` still raised
  `AttributeError`/`TypeError`" changes. Genuinely pixel-less instances
  are unaffected, and `tests/test_io_no_pixels.py` is the anchor.
- Migration: an export that newly fails was writing a file that declared an
  image it did not carry. The remedy is to fix the source instance (here,
  supply `(0028,0006)` or correct `SamplesPerPixel`), not to suppress the
  error.

## §B7. Out of scope for PR 2

- `analyze_pixels`' log-line-instead-of-audit-row gap (§10 files it).
- The `SidecarPixelLoader` arm at `entities.py:444-461`, which already
  raises `RuntimeError` on any loader failure. Unchanged.
- Carrying the float pair in the sidecar so a moved source file does not
  break the export — #183.
- Whether the *source* instance's nonconformance (`MONOCHROME2` beside
  three samples) should be reported at ingest. Different issue.

## §B8. The `_write_float_src` workaround — ruling

`tests/test_float_pixel_data_export.py:87-96` writes
`PlanarConfiguration = 0` when `samples > 1`, with a comment saying the
decode otherwise fails silently and "a test asserting on Photometric
Interpretation passes for the wrong reason".

**Keep the parameter. Rewrite the comment. Do not delete the workaround.**

Measured, by removing the two lines and running the file:

```
dddb659, workaround removed:
  1 failed, 26 passed
  test_a_declared_monochrome_survives_a_float_export_with_three_samples
  fails at:  assert (0x7FE0, 0x0008) in exported
  (the two PhotometricInterpretation assertions above it passed)

with §B0, workaround removed:
  the same single test fails, earlier, via the export ERROR:
  "Lazy load failed for …: Missing required element: (0028,0006)"
```

Three findings, all of which belong in the rewritten comment:

1. **Exactly one test depends on it** — the #222 three-samples case. It
   needs a *decodable* source to reach its subject at all, so the parameter
   stays necessary after PR 2 exactly as before.
2. **The comment overstates the vacuity.** The `PhotometricInterpretation`
   assertions do pass for the wrong reason, but the test as written already
   catches the hollow file at its `(0x7FE0, 0x0008)` assertion. Say that
   precisely; "the test passes" is not true and a future reader who checks
   will distrust the rest of the comment.
3. **PR 2 is what makes the second job disappear.** Before, omitting
   `PlanarConfiguration` produced a silently hollow *file* and the test had
   to assert on the file to notice. After, it produces a loud pipeline
   failure. T7 is the test that proves the second job is gone — which is
   why T7 must build its own source rather than reuse this helper with the
   workaround intact.

---

## 4. Compatibility survey — both PRs

Commands recorded verbatim so a reviewer can re-run them. Every one was run
on `dddb659`.

```
grep -rn 'get_pixel_data' --include='*.py' . | grep -v '\.venv' | grep -v '^\./build/'
grep -rn "PixelDataProviderURL\|0028,7fe0\|0028,7FE0\|00287FE0" --include='*.py' --include='*.md' . | grep -v '\.venv' | grep -v '^\./build/'
grep -rn "0028,0101\|0028,0102\|0028,0103\|BitsStored\|HighBit\|PixelRepresentation" --include='*.py' --include='*.md' isocenter/ scripts/ docs/ README.md CHANGELOG.md
grep -ln "7FE00008\|7fe0,0008\|0x7FE0, 0x0008\|FloatPixelData\|float32\|float64" tests/*.py | sort > a
grep -ln "BitsStored\|HighBit\|PixelRepresentation\|0028,010[123]" tests/*.py | sort > b
comm -12 a b
grep -rn "get_pixel_data" scripts/ docs/ README.md
git log -L 175,177:isocenter/io_handlers.py --oneline
```

### Non-test consumers, asked as its own question

| consumer | result |
| --- | --- |
| `scripts/generate_redaction_example.py:135-137` | sets `0028,0101 = 12`, `0028,0102 = 11`, `0028,0103 = 0` — on a **CT** instance with `0028,0100 = 16` and integer pixels (`:128-140`). Integer branch; §A1 does not reach it. It calls `write_tree`, which routes through the same worker, so §A2's integer URL clause *would* apply — but the script sets no `0028,7fe0` (`grep`: no hits in `scripts/`). Unchanged. |
| `scripts/` generally | `grep -rn "get_pixel_data" scripts/` → **no hits.** No generator calls it, so §B0 cannot reach any of them. |
| `docs/`, `README.md` | `grep -rn "get_pixel_data" docs/ README.md` → no hits outside `docs/superpowers/`. No prose documents the return-`None`-on-undecodable behaviour, so there is nothing to correct; §B6's entry is the first time it is written down. |
| `docs/superpowers/specs/2026-08-29-pixel-geometry-authority.md:524-527, :746, :1205` | states that `BitsStored`/`HighBit`/`PixelRepresentation` are "not derived… That is pre-existing and this design does not change it". Still true — that spec is about the **integer** path, which §A1 leaves alone. Historical documents; not edited. |
| `isocenter/io_handlers.py:1430` | `SidecarPixelLoader` reads `0028,0103` from `instance.attributes` to reconstruct dtype. **This is why §A1 deletes from `ds` and not from the graph.** The float pair is never sidecar'd (`ingest_worker` routes on `if "PixelData" in ds`), so this line is not on the float path at all — but a graph-side deletion would break it for the integer path on any future re-export. |
| `CHANGELOG.md:126` | the #216 entry, which explicitly says Pixel Data Provider URL "is *not* deleted -- removing a caller's element is a data-loss action that owes them a loss row, which is filed rather than added here". §A2 is that filed decision landing. The old entry is **not** rewritten; the new one references it. |

### Tests that must keep passing, and why each survives

Intersection `comm -12 a b` (float-touching ∩ descriptor-touching) is
exactly two files:

| file | why it survives |
| --- | --- |
| `tests/test_float_pixel_data_export.py` | the three tags appear only as fixture inputs (`:83-84,:97,:260-261,:302-303,:348-349,:460-461`); the only assertion near them is `exported.BitsAllocated == expected_bits` (`:465`), which §A1 does not touch. The `:255`/`:299` float16 cases assert on `outcome.losses` and are the shape T3 extends. |
| `tests/test_private_binary_ingest.py` | its `BitsStored`/`HighBit`/`PixelRepresentation` are on 8-bit **integer** sources (`:63-67`, `:592-596`); its float content (`:336`, `:389`, `:634`) is about `7fe0` routing, not descriptors. |

Tests that depend on `get_pixel_data()` returning `None`, asked separately
for PR 2:

| file | why it survives |
| --- | --- |
| `tests/test_io_no_pixels.py` | a real file with no pixel element → still `None`. This is §B5/T9's pipeline half and the anchor for the whole narrowing. Green with the prototype. |
| `tests/test_redaction_robustness.py:40` | `patch.object(Instance, 'get_pixel_data', return_value=None)` — a mock; the real method is never entered. |
| `tests/test_redaction_failure_is_reported.py:352` | `monkeypatch.setattr(Instance, "get_pixel_data", lambda self: None)` — same. |
| `tests/test_compression_deps.py:31`, `tests/test_codecs_strict.py:93` | expect `RuntimeError` from an undecompressable instance. That path is the *outer* `except Exception` arm, untouched by §B0. Green with the prototype. |
| `tests/test_metadata_refactor_full.py:129` | expects `RuntimeError` containing `Integrity Error` from the **sidecar loader** arm (`entities.py:444-461`), untouched. |

Everything above is covered by the two full-suite runs per PR in §A7 and
§B4; the table exists so a reviewer can see the questions were asked
individually rather than inferred from a green bar.

---

## 5. Interpreter coverage, per clause

The gate is **3.12 and 3.14t only**. Neither defect is concurrency-shaped:
#223 is element emission and #226 is one `except` clause, and
`session.export()` takes **processes on every build** (#185) — the
threads/processes lever that made #228 diverge does not exist here. Both
were measured on both builds and behave identically.

| clause | 3.12.13 | 3.14.7t |
| --- | --- | --- |
| §1.2/§1.3/§1.4 (#223 defects) | present | present, identically |
| §A1, §A2 | fixed; suite 967 passed, 1 skipped | fixed; suite 967 passed, 1 skipped |
| T1, T2, T5 | red → green | red → green |
| T3, T4, T6 | green → green (guards) | green → green (guards) |
| §2.2 (#226 defect) | present | present, identically |
| §B0 | fixed; suite 967 passed, 1 skipped | fixed; suite 967 passed, 1 skipped |
| T7, T8 | red → green | red → green |
| T9 | green → green (guard) | green → green (guard) |
| §B3 redaction change | `RedactionError` (processes) | `RedactionError` (threads) — same, because the raise is in the worker on both |
| Mutation 1 (rejected) | `test_io_no_pixels.py` red | not re-run; the mechanism is a frozenset membership test with no concurrency component |

Any measurement on 3.14.7t must assert `sys._is_gil_enabled() is False`
**inside the process after imports** — a free-threaded interpreter silently
re-enables the GIL when an extension without free-threaded support is
imported, and a measurement under a re-enabled GIL is a measurement of the
other build. Every figure here was taken that way (asserted after
`import isocenter`, `numpy`, `pydicom`).

Before any suite run: `find . -name .DS_Store -delete`. Otherwise two
`tests/test_packaging_contract.py` tests fail spuriously and the failure
message recommends the wrong remedy (#234).

`isocenter/pixel_geometry.py` is untouched by both PRs — no clause adds an
import to it, and its pure-stdlib AST test is unaffected.

---

## 6. What the PR bodies must record

**PR 1 (#223)**

- The verbatim §8.2 and C.7.6.24 quotes, with the URLs they were fetched
  from, and the note that the citation is §8.2 and not A.1.
- Measurements A, C, D, E as the before; §A4 as the after.
- Measurement G as the evidence that stripping the three does not cost
  decodability — a reviewer should not have to take that on trust.
- Measurement F as the evidence that `_merge` is the only source, so "also
  fix the default" is not a missing half.
- The compressed-integer row from §A4 and the "structural gate, never
  `"PixelData" in ds`" constraint.
- `967 passed, 1 skipped` on both interpreters.
- The §A2 table and the named-not-resolved inconsistency about the existing
  silent pixel-element deletions.

**PR 2 (#226)**

- The correction to the issue's stated cause (§2.3) and Mutation 1's red
  test, since a reviewer reading the issue will otherwise expect the
  modality-guard fix.
- §B1's before/after table, including the report's Exceptions & Errors
  section text.
- §B3's four-caller table **and** the `services.py` `RedactionError`
  measurement, explicitly, because that file is not in the diff.
- The `write_tree` wrapper's message, because `scripts/` uses that path.
- §B8's ruling on the test workaround.
- `967 passed, 1 skipped` on both interpreters.

---

## 7. The strongest objection to each

**Against PR 1.** *You are deleting three elements a caller explicitly put
in `attributes`, silently, in a codebase whose stated rule is that removing
a caller's element owes them a loss row — and you are deciding that rule
does not apply by asserting the elements carry no information.* The
strongest form of this is that the "derivable" test is the exporter's
judgement about what a recipient wants, made once, in a comment, and a
recipient with a nonconformant reader that keys on `BitsStored` gets a file
that no longer works for them and no row saying why.

The answer that holds: the file *already* had `BitsAllocated` silently
overwritten by the same sentence of the same section (#170), so the
precedent is not being set here, it is being applied consistently for the
first time. The answer that does not hold, and should not be offered: "the
values were wrong anyway" — measurement A shows a case where they were
internally consistent (32/31/0 beside `BitsAllocated 32`) and still
forbidden. If a reviewer insists, the cheap concession is to file the loss
row for all three too; it costs one `losses.append` and changes no
grading, since the scope is `STANDARD` either way. Do not concede the
*refusal* variant — refusing an ordinary Parametric Map export over three
descriptors is worse than every alternative.

**Against PR 2.** *You are converting a silent success into a hard failure
on the strength of one malformed fixture, and the class of `AttributeError`
you are now propagating is defined by pydicom, not by you — a pydicom
upgrade that raises `AttributeError` somewhere new turns working exports
into failures with no warning.* This is real: the check is "a pixel element
is present", not "the error means what we think", so any future
`AttributeError` from `.pixel_array` on a pixel-bearing dataset becomes a
failed export.

The answer: that is the intended contract, not a side effect. "A pixel
element is present and this library could not decode it" is exactly the
condition that should never be reported as success, whatever the reason —
and the alternative reading, that only *some* undecodable instances should
fail, requires enumerating pydicom's error taxonomy, which #226 explicitly
declines. The `pydicom<4.0` cap already pins the surface. The residual risk
is real and is priced: an export that newly fails was writing a file that
declared an image it did not carry.

---

## 8. Explicitly out of scope for both PRs

- `isocenter/session.py` — #228 is being implemented in it concurrently.
- Any change to `_IMAGE_MODALITIES` or the `except FileNotFoundError` guard.
- Ingest-side reporting of nonconformant source instances.
- The `PS3.5 A.1` citation at `io_handlers.py:2176` / `CHANGELOG.md:492`.
- #150's grading question, #183's sidecar question, #222's remaining shapes.

---

## 9. Found here, to be filed separately

1. **`analyze_pixels` reports an undecodable instance to the log, not the
   audit trail.** `pixel_analysis.py:220` catches `Exception` →
   `logger.error(...)` → returns `[]`. After PR 2 an instance whose pixels
   will not decode gets a log line, and the burned-in-identifier scan
   reports zero findings for it — which is indistinguishable in the
   compliance report from an instance with no burned-in text. "OCR could
   not see this instance's pixels" is exactly the #181 shape and deserves a
   row. Same milestone's spirit; not PR 2's job.
2. **The existing silent pixel-element deletions owe a loss row by §A2's own
   test.** `del ds.PixelData` (`:1082`) and `del ds[other]` (`:1103-1106`)
   remove payload with no row. Named in §A2, deliberately not changed, and
   worth a decision of its own.
3. **The `_write_float_src` comment's vacuity claim is overstated**
   (§B8/finding 2). Corrected as part of PR 2; recorded here so the
   correction is not mistaken for a behaviour change.

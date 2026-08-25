# DICOM Waveform → WFDB Export

**Date:** 2026-08-23
**Status:** Approved, not yet implemented
**Tracking:** [v0.7.0 — The Connector](https://github.com/kvnlng/Isocenter/milestone/1) · issues #9–#16

---

## Context

Isocenter de-identifies DICOM. [Murmur Studio](https://github.com/kvnlng/Murmur)
reviews PhysioNet WFDB recordings. Nothing currently connects them: a
clinical ECG that arrives as a DICOM waveform IOD cannot reach Murmur
without a manual conversion step that no de-identification pipeline
covers.

This design adds waveforms as a first-class data type in Isocenter and
emits WFDB records — signal, header, and annotations — that Murmur reads
directly.

### The blocking discovery

Isocenter does not merely lack a WFDB writer. It **discards waveform sample
data at ingest**, silently.

`populate_attrs` (`isocenter/io_handlers.py:69`) skips all bulk binary VRs:

```python
BINARY_VRS = {'OB', 'OW', 'OF', 'OD', 'OL'}
```

Waveform Data `(5400,1010)` is `OW`/`OB`, so it matches. `PixelData` has
an explicit extraction path in `ingest_worker`; waveforms have none.
`process_sequence` still recurses into the Waveform Sequence
`(5400,0100)`, so channel definitions, sampling frequency, and labels
survive — only the samples are lost. The exported object looks
well-formed, which is what makes the failure quiet.

This is tracked separately as a data-loss bug (#9) because it affects
users today regardless of whether this feature ships.

---

## Goals

- Ingest, persist, de-identify, and export DICOM waveform IODs.
- Emit WFDB records readable by both Murmur and PhysioNet's own tooling.
- Bridge DICOM waveform annotations into Murmur's findings layer.
- Disturb the existing, working pixel path as little as possible.

## Non-goals (v1)

| Excluded | Why |
|---|---|
| Multi-rate records | Materially harder to emit correctly; defer until the single-rate path is proven. |
| WFDB ingest (WFDB → WFDB de-identification) | Source data is DICOM. A non-DICOM ingest path would need a Series/Instance mapping for records with no SOP Instance UID. |
| `.atr` emission | Beat-level format; maps poorly to DICOM's interpretive-statement model. |
| Format 212 | A compression optimization, not a compatibility requirement. Murmur reads format 16. |

---

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **DICOM waveform IODs only** — one-way bridge | Keeps everything in Isocenter's native object model; no second ingest concept. |
| 2 | **Generic reader, validated on 12-lead** | The DICOM Waveform module is generic, so breadth is nearly free; scoping *verification* to 12-lead + General ECG avoids claiming support that has not been tested. |
| 3 | **Pseudonymous record name, shifted timing** | Safe Harbor clean while preserving relative timing, which Murmur needs for absolute-time navigation and cross-record alignment. |
| 4 | **Carry annotations across to `annotations.json`** | This is the substance of the bridge — a de-identified cart interpretation reaching Murmur's findings panel. |
| 5 | **Exporter seam with format dispatch** | NIfTI/BIDS (#22) is also planned, and v1.0 commits to freezing the API (#26). Better to freeze a seam than a method with four formats in it. |
| 6 | **`category` = code, `label` = Code Meaning** | Isocenter transcribes; it does not author clinical meaning. A scheme-qualified code is also stable across vendors and sites. |
| 7 | **Generalize the sidecar to blob-kind** | One integrity/compaction/hashing implementation rather than two — and duplicating that subsystem would duplicate the bug surface that produced both 0.6.1 regressions. |

---

## Architecture

```
DICOM ECG ──ingest_worker──┬─→ waveform samples ──→ sidecar blob (kind='waveform')
                           ├─→ channel defs, fs, gains ──→ object graph (already works)
                           └─→ annotation sequence ──→ object graph (already works)
                                        │
                              audit → anonymize (existing tag pipeline
                                        + waveform text surfaces)
                                        │
                              export(format="wfdb") → WfdbExporter
                                        │
                    <record>.hea  +  <record>.dat  +  <record>.annotations.json
```

| Component | Location | Issue |
|---|---|---|
| `Waveform` / `WaveformChannel` model | `isocenter/waveform.py` (new) | #11 |
| Waveform extraction at ingest | `isocenter/io_handlers.py` | #9 |
| Blob-kind sidecar storage | `isocenter/persistence.py` | #10 |
| `Instance` waveform accessors | `isocenter/entities.py` | #11 |
| `Exporter` protocol, `WfdbExporter` | `isocenter/exporters/` (new) | #12, #13 |
| Murmur annotation bridge | `isocenter/murmur.py` (new) | #15 |

**Record granularity:** one WFDB record per DICOM Instance. A waveform
SOP instance *is* one acquisition, so the mapping is 1:1 with no
stitching. Records are written into the same `Patient/Study/Series`
folder tree `DicomExporter` already builds, so both exporters agree on
layout and Murmur's folder picker finds `.hea` alongside its siblings.

---

## Storage (#10)

`SidecarManager` (`isocenter/sidecar.py`) is **already kind-agnostic** —
`write_frame`/`read_frame` operate on raw offsets with no notion of
pixels. The `fcntl` append path, which produced both 0.6.1 regressions,
does not change. Pixel-specificity lives in exactly four places:

| Location | What |
|---|---|
| `persistence.py:75-80` | `pixel_*` columns on `instances` |
| `persistence.py:244` | `_create_pixel_loader()` |
| `persistence.py:747` | `persist_pixel_data()` |
| `persistence.py:1325` | `compact_sidecar()` |

New table:

```sql
CREATE TABLE IF NOT EXISTS instance_blobs (
    instance_uid TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- 'pixels' | 'waveform'
    file_id      INTEGER DEFAULT 0,
    offset       INTEGER,
    length       INTEGER,
    hash         TEXT,
    compress_alg TEXT,
    UNIQUE(instance_uid, kind)
);
```

`persist_pixel_data()` becomes `persist_blob(instance, kind)` with the
old name retained as a wrapper, so every existing pixel test exercises
the new path unchanged. One sidecar **file** holds both kinds; the
existing `*_pixels.bin` filename is kept (a documented misnomer) rather
than breaking every existing session.

**Sizing justifies the offload:** a 10-second 12-lead at 500 Hz is
~80 KB, but a 24-hour 3-channel Holter at 200 Hz is ~104 MB. The
`instance_attributes` EAV table is not a viable home for that.

**Migration:** `_init_db` already uses `CREATE TABLE IF NOT EXISTS`, so
the table appears on open. Back-fill from the legacy `pixel_*` columns on
first open; leave those columns in place as read-only legacy so a 0.6.x
session opens clean and a downgrade does not corrupt.

---

## WFDB writer (#13)

**The fast path is a byte copy.** DICOM multiplexes samples across
channels at each time point; WFDB format 16 interleaves identically, and
both are little-endian. For `Waveform Bits Allocated = 16` and
`Sample Interpretation = SS`, the `.dat` payload is a direct passthrough
— no per-sample transcoding, which matters at Holter sizes.

Other interpretations: `US` needs a zero-offset shift; `SB`/`UB` widen to
16-bit; `MB`/`AB` (µ-law / A-law audio companding) are rejected with an
explicit error rather than guessed at.

### Header derivation

| WFDB field | DICOM source |
|---|---|
| `fs` | Sampling Frequency `(003A,001A)` |
| `nsig` | Number of Waveform Channels `(003A,0005)` |
| `nsamp` | Number of Waveform Samples `(003A,0010)` |
| `adcres` | Waveform Bits Allocated `(5400,1004)` |
| `gain` | `1 / (Channel Sensitivity (003A,0210) × Correction Factor (003A,0212))` |
| `units` | Channel Sensitivity Units Sequence `(003A,0211)` |
| description | Channel Source Sequence `(003A,0208)` — coded, **not** the free-text label |

### Two hazards

**Baseline conversion.** DICOM's Channel Baseline `(003A,0213)` is
expressed in *physical* units; WFDB's baseline is an *ADC* value. Sign
and reference-point conventions are easy to invert, and inverting them
yields a trace that renders plausibly at the wrong level. This must be
settled by a round-trip test against a real record (#16), not by
derivation in this document.

**Gain field convention.** Isocenter writes spec-conformant
`gain(baseline)/units` per `header(5)` — e.g. `200(0)/mV` — so output
remains readable by `rdsamp` and the `wfdb` Python package. Murmur's
current parser reads the parenthesised and slash-delimited parts
swapped; filed as [kvnlng/Murmur#360](https://github.com/kvnlng/Murmur/issues/360)
with a type-based disambiguation fix that accepts both forms.

Never emit `gain 0` for a calibrated signal — WFDB reads 0 as
*uncalibrated* and substitutes a 200 adu/mV default.

---

## De-identification (#14)

### WFDB header surfaces

| Surface | Handling |
|---|---|
| record name | Derived from anonymized PatientID + series. Never the source PatientID or accession. |
| `basedate` / `basetime` | Shifted by the existing per-patient `SHIFT_DATE` offset, emitted as `HH:mm:ss dd/MM/yyyy` UTC to match Murmur's parser. |
| `#` comment lines | **None emitted.** |

MIT-BIH convention places age, sex, and diagnosis in `#` comments, and
Murmur parses `#Age:` / `#Sex:` / `#Dx:` into structured metadata while
rendering comments verbatim. Anything Isocenter wrote there would surface as
patient metadata in a viewer.

Emitting *shifted* dates rather than omitting them is deliberate: a
reader that finds no date field has no record start time at all, which
costs absolute-time navigation and cross-record alignment for no privacy
gain the shift does not already provide.

### DICOM-side surfaces

- Channel Label `(003A,0203)` — `SH`, free text, operator-typed.
- Waveform Annotation Sequence `(0040,B020)` → Unformatted Text Value `(0070,0006)` — `UT`.
- Acquisition Context Sequence `(0040,0555)`.
- Acquisition DateTime `(0008,002A)` — date shift.

`populate_attrs` already indexes `UT` and `SH` into `text_index`, and
`process_sequence` passes that index down recursively
(`isocenter/io_handlers.py:66-99`). These surfaces therefore flow through
the **existing** `PhiInspector` once the data is ingested — they were
invisible only because the data never arrived. No new scanning machinery
is required; #14 is about coverage tests and the header rules.

Lead identity uses the coded Channel Source `(003A,0208)` in preference
to the free-text Channel Label: a coded value cannot contain a typed-in
name.

---

## Annotation bridge (#15)

| Murmur field | DICOM source |
|---|---|
| `kind` | Temporal Range Type `(0040,A130)` — `POINT`/`MULTIPOINT` → `point`, `SEGMENT`/`MULTISEGMENT` → `range` |
| `startSample` / `endSample` | Referenced Sample Positions `(0040,A132)`, rebased 1 → 0 |
| `lead` | Referenced Waveform Channels `(0040,A0B0)` → coded channel source |
| `category` | Concept Name Code Sequence `(0040,A043)` — scheme-qualified code value |
| `label` | Code Meaning `(0008,0104)`, verbatim |
| `note` | Unformatted Text Value `(0070,0006)`, **after** PHI remediation |
| `source` | `isocenter/<version>` plus originating manufacturer |

Where DICOM supplies only Referenced Time Offsets `(0040,A138)`, convert
to sample indices via the sampling frequency.

**Sample indices, never `startUnixMS`.** Murmur resolves sample indices
without precision loss and prefers them when both are present; it also
keeps absolute time out of the annotation file entirely.

**`category` is the code, `label` is the Code Meaning.** Isocenter does not
normalize coded concepts into a clinical vocabulary of its own. This
applies Murmur's stated producer contract — *"Murmur transmits
assertions. It does not author them."* — to the producer side.

---

## Testing (#16)

The repository has no waveform fixtures and no ECG DICOM, so this begins
with a generator: `scripts/generate_waveform_test_data.py`, sibling to
the existing `scripts/generate_test_dataset.py`, synthesizing 12-lead ECG
DICOM with **known** amplitudes, gains, and baselines — including at
least one record with a non-zero baseline and one non-mV signal.

The load-bearing test is a conformance round-trip through an independent
implementation:

```
known signal → DICOM waveform IOD → ingest → export WFDB
                                                  ↓
                        PhysioNet's own `wfdb` package reads it
                                                  ↓
                       assert physical values match the known input
```

Reading back with PhysioNet's `wfdb` rather than our own reader is the
point: it validates *conformance* instead of testing the writer against
itself, and it is the only thing that reliably catches the gain/baseline
sign-and-reference errors above. A self-consistent writer/reader pair
agrees happily on the wrong answer.

Also covered: PHI assertions (no `#` comments, record name free of source
identifiers, dates shifted consistently across a patient's records,
annotation `note` scrubbed); `annotations.json` validated against a
pinned copy of Murmur's Draft 2020-12 schema in `tests/fixtures/`, with a
separate non-blocking job that fetches the live schema and warns on
drift; every existing pixel test passing untouched as the regression gate
for #10; and a 0.6.x-era `isocenter.db` opening with a correct back-fill.

### Prerequisites

Two environment problems block verification of any of this:

1. `.venv/bin/python` points at a pyenv `3.14.0t` that no longer exists,
   so the suite cannot currently run locally.
2. `tests.yml` triggers only on `isocenter/**` and `tests/**` (#19), so
   adding `wfdb` and `jsonschema` to `setup.py` would not run CI.

---

## Implementation order

```
#9  ingest data-loss fix ─┐
#10 blob-kind sidecar ────┴─→ #11 waveform model ─┬─→ #13 WFDB writer ─→ #15 annotations
                                                  ├─→ #14 PHI surfaces
#12 exporter seam ────────────────────────────────┘   #16 tests (throughout)
```

`#19` (CI path filters) should land before `#16`.

---

## References

- DICOM PS3.3 C.10.9 — Waveform Module
- DICOM PS3.3 A.34 — Waveform IODs
- WFDB `header(5)` — [physionet.org/physiotools/wag/header-5.htm](https://physionet.org/physiotools/wag/header-5.htm)
- [Murmur annotation JSON schema](https://kvnlng.github.io/Murmur/annotations.schema.json)
- [Murmur — What Murmur asserts](https://kvnlng.github.io/Murmur/what-murmur-asserts)
- [kvnlng/Murmur#360](https://github.com/kvnlng/Murmur/issues/360) — gain field parsing

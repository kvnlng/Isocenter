# Waveforms and WFDB Export

Gantry ingests any DICOM instance carrying a Waveform Sequence
`(5400,0100)` — 12-Lead ECG, General ECG, Hemodynamic, and similar
Waveform Storage IODs — alongside image data, and exports them as
PhysioNet WFDB records. The bridge is one-way: DICOM waveforms go out
as WFDB, for tools like [Murmur Studio](https://github.com/kvnlng/Murmur)
to read. Gantry does not read WFDB back in.

## Quick start

```python
from gantry import Session

session = Session("ecg_study.db")
session.ingest("/data/ecg")

# Load a PHI tag configuration before auditing -- an unconfigured
# session only checks the hardcoded baseline (see "Privacy" below).
session.create_config("config.yaml")
session.load_config("config.yaml")

session.audit()
session.anonymize()
session.export("/out", format="wfdb")
```

Each waveform instance becomes one WFDB record, written into the same
`Subject_/Study_/Series_` directory tree the DICOM exporter uses --
so a record's `.hea`/`.dat` files sit alongside that series' `.dcm`
files if you also export `format="dicom"` into the same folder:

```
out/<patient>/<study>/<series>/
├─ <record>.hea               header
├─ <record>.dat               format-16 samples
└─ <record>.annotations.json  cart findings, when present
```

## What is exported

| WFDB field | DICOM source |
|---|---|
| `fs` | Sampling Frequency `(003A,001A)` |
| `gain` | Derived from Channel Sensitivity `(003A,0210)` and its correction factor |
| `units` | Channel Sensitivity Units Sequence `(003A,0211)` |
| signal description | Channel Source Sequence `(003A,0208)`, falling back to Channel Label `(003A,0203)` when no coded source is present |

Signals are written as WFDB format 16 (16-bit, little-endian,
channel-interleaved) -- the same layout DICOM already stores them in,
so no sample transcoding happens. The gain field is written in
spec-conformant `header(5)` form, `gain(baseline)/units`; if you see a
downstream tool mis-parse that field, check the tool's parser first --
this is a known issue already filed against Murmur, not a Gantry
nonconformance.

When present, Waveform Annotation Sequence `(0040,B020)` items --
cart-generated findings such as rhythm calls -- are exported as
`<record>.annotations.json`, in the schema
[Murmur Studio](https://github.com/kvnlng/Murmur) expects.

## What is and isn't de-identified

Gantry's PHI scan is **tag-gated, not content-based**: a tag gets
flagged only if it's named by your loaded configuration -- explicit
`phi_tags` entries, or a privacy profile expanded into them. A tag
outside that set is never inspected for what it contains, no matter
how identifying the text inside it is. Separately, odd-group private
tags are removed whenever `remove_private_tags` is `True` (the
default) -- that check is unconditional and is not gated by
`phi_tags` at all.

There are three configurations a reader of this guide can be in:

- **A bare `Session()`, never `load_config()`-ed.** `phi_tags` is
  empty. Only the hardcoded baseline scan runs: Patient Name and
  Patient ID get replaced, Study Date gets shifted. Nothing else is
  touched.
- **The Quick Start above.** `create_config()` scaffolds a config with
  `privacy_profile: basic`; `load_config()` expands that into
  `PRIVACY_PROFILES["basic"]` (`gantry/profiles.py`) -- **28 tags, all
  28 effective** -- covering patient identity, study/series dates and
  times, and institution/physician fields, based on DICOM PS3.15 Annex
  E's Basic Profile. This is what actually runs on the documented path.
  `gantry/resources/phi_tags.json`, a separate 6-tag file, is *not*
  reached from this flow: `create_config()` deliberately drops its
  REMOVE-action tags from the scaffold, on the assumption the Basic
  profile already covers them.
- **Your own `phi_tags` configuration**, loaded standalone or layered
  on top of a profile -- your explicit tags win over the profile's.

Series Description `(0008,103E)` -- and the Study Description it sits
alongside -- are both correctly emptied on the documented path. (A
casing mismatch between the profile's key and the lowercased attribute
keys the object graph actually uses briefly made Series Description
the one exception during this branch's review; `PhiInspector` now
normalizes PHI-tag key casing at load time, in `gantry/privacy.py`, so
this class of bug can't recur regardless of how a tag is spelled in a
profile or config file.) This matters for waveform export specifically
because the series description becomes a **directory name** -- every
`.hea`/`.dat`/`.annotations.json` this exporter writes lives inside it.

**None of the three configurations cover the two free-text fields
specific to waveform export.** Neither the hardcoded baseline nor the
Basic profile includes Channel Label or Unformatted Text Value, so on
every documented path both are written verbatim into every WFDB record:

- **Channel Label `(003A,0203)`** -- becomes the `.hea` signal-line
  description whenever a channel has no coded Channel Source Sequence
  to prefer instead. This is an operator-typed field.
- **Unformatted Text Value `(0070,0006)`** -- becomes the `note` field
  of `annotations.json` whenever an annotation carries one. This is
  also operator-typed, and routinely holds free-text clinical
  commentary.

Both are DICOM tags an operator can type identifying information
into. If your workflow needs them scrubbed, add `003A,0203` and/or
`0070,0006` to your own PHI tag configuration before running
`audit()` / `anonymize()`; Gantry will not do this for you by default.

One more gap worth flagging in the Basic profile itself: it REMOVEs
Study Date `(0008,0020)`, Study Time `(0008,0030)`, Acquisition Date
`(0008,0022)`, and Content Date `(0008,0023)`, but it does **not**
include Acquisition DateTime `(0008,002A)` -- easy to assume is
covered alongside the separate date tag it duplicates, but it isn't,
under any of the three configurations above unless you add it
yourself.

**Record timing** in the `.hea` file combines two independently
sourced parts. The *date* comes from `study.study_date`, which *is*
shifted by `anonymize()` (the same per-patient date shift applied to
the rest of the DICOM metadata). The *time-of-day* comes from the
instance's own timestamp tags -- Acquisition DateTime `(0008,002A)`
when present, else Study Time `(0008,0030)` -- and the date shift
never touches it, whether or not `anonymize()` ran: when Acquisition
DateTime is present (the common case), it isn't covered by the Basic
profile either (see above), so the real acquisition time-of-day ends
up in the header either way. If you export without running
`anonymize()` at all, the date is real too -- exactly like every
other un-remediated field in Gantry.

Beyond content, the export path itself avoids two structural PHI
paths a WFDB writer could otherwise open:

- **No `#` comment lines are ever written.** WFDB readers render
  header comments verbatim, and MIT-BIH convention places age, sex,
  and diagnosis there -- Gantry never emits this line at all.
- **Record names derive from anonymized identifiers only** (the
  patient pseudonym, series number, instance number) -- never from
  raw patient identifiers, and lead identity in the header prefers the
  coded channel source over the free-text label wherever a coded
  source exists.

## Limitations

Format 16 only. The exporter also does not support: multi-rate
records (only the first Waveform Sequence item of an instance is
exported), WFDB ingest, `.atr` annotation output, or mu-law/A-law
companded audio sample interpretations.

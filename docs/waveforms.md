# Waveforms and WFDB Export

Isocenter ingests any DICOM instance carrying a Waveform Sequence
`(5400,0100)` — 12-Lead ECG, General ECG, Hemodynamic, and similar
Waveform Storage IODs — alongside image data, and exports them as
PhysioNet WFDB records. The bridge is one-way: DICOM waveforms go out
as WFDB, for tools like [Murmur Studio](https://github.com/kvnlng/Murmur)
to read. Isocenter does not read WFDB back in.

## Quick start

```python
from isocenter import Session

with Session("ecg_study.db") as session:
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
| signal description | Channel Source Sequence `(003A,0208)`, falling back to Channel Label `(003A,0203)` when no coded source is present *and* the label is a recognisable signal name -- otherwise a positional `ch<N>` token, `N` being the **zero-based** channel index (DICOM ChannelNumber is 1-based) (see "What is and isn't de-identified" below) |

Signals are written as WFDB format 16 (16-bit, little-endian,
channel-interleaved) -- the same layout DICOM already stores them in,
so no sample transcoding happens. The gain field is written in
spec-conformant `header(5)` form, `gain(baseline)/units`; if you see a
downstream tool mis-parse that field, check the tool's parser first --
this is a known issue already filed against Murmur, not a Isocenter
nonconformance.

When present, Waveform Annotation Sequence `(0040,B020)` items --
cart-generated findings such as rhythm calls -- are exported as
`<record>.annotations.json`, in the schema
[Murmur Studio](https://github.com/kvnlng/Murmur) expects.

## What is and isn't de-identified

Isocenter's PHI scan is **tag-gated, not content-based**: a tag gets
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
  `PRIVACY_PROFILES["basic"]` (`isocenter/profiles.py`) -- **34 tags, 34
  effective** -- covering patient identity, study/series dates and
  times, and institution/physician fields, based on DICOM PS3.15 Annex
  E's Basic Profile. This is what actually runs on the documented path.
  `(0070,0006)` was the exception until 0.8.0: it lives inside Waveform
  Annotation Sequence, and the scan never opened sequences (#57), so it
  sat in the profile doing nothing. It fires now.
  `isocenter/resources/phi_tags.json`, a separate 6-tag file, is *not*
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
normalizes PHI-tag key casing at load time, in `isocenter/privacy.py`, so
this class of bug can't recur regardless of how a tag is spelled in a
profile or config file.) This matters for waveform export specifically
because the series description becomes a **directory name** -- every
`.hea`/`.dat`/`.annotations.json` this exporter writes lives inside it.

**The three free-text surfaces specific to waveform export are now
remediated in the exporter itself, not by profile membership** -- the
PHI scan is tag-gated (see above), so a profile entry alone would not
protect a bare `Session()` that never called `load_config()`. Both are
handled unconditionally, regardless of which of the three
configurations above you're in:

- **Channel Label `(003A,0203)`** -- reaches the `.hea` signal-line
  description and the `annotations.json` `lead` field **only** when it
  is a recognisable signal name, checked against the module-level
  `KNOWN_LEAD_NAMES` set in `isocenter/waveform.py`. Anything else --
  including genuinely operator-typed text -- is replaced with a
  positional `ch<N>` token instead of being written verbatim. `N` is
  the **zero-based** channel index, not DICOM's 1-based ChannelNumber
  -- `ch1` is the *second* channel.
- **Unformatted Text Value `(0070,0006)`** -- is omitted from the
  `note` field of `annotations.json` by default. It routinely holds
  free-text clinical commentary, so exporting it is opt-in: pass
  `session.export(folder, format="wfdb", include_annotation_text=True)`
  to restore it. That default-off behaviour is what actually protects
  this field today. `(0070,0006)` is also in the Basic profile, and
  since 0.8.0 that entry works: a configured session that passes
  `include_annotation_text=True` gets the profile's remediated (emptied)
  value.

  Before 0.8.0 it did not. The tag lives inside each Waveform Annotation
  Sequence item rather than at the top level, and the scan only reached
  nested content through the instance's `text_index` -- which the worker
  clones `session.audit()` scans were not given. So the profile entry sat
  there doing nothing, and `include_annotation_text=True` exported the
  **raw** value. That was #57, and it affected every sequence-nested tag,
  not just this one. If you de-identified waveforms with 0.7.x or
  earlier, re-audit them.

- **Concept Name `(0040,A043)`** -- the annotation's Code Meaning
  reaches `annotations.json` as `label`, and its scheme-qualified Code
  Value as `category`, **only when the Coding Scheme Designator
  `(0008,0102)` names a published vocabulary**, checked against
  `KNOWN_CODING_SCHEMES` in `isocenter/waveform.py`. A coded finding is
  unaffected: `SCT:164889003` still arrives with its label
  `"Atrial fibrillation"`, because SNOMED defined that term, not an
  operator.

  For a site-defined scheme the cart populates Code Meaning with typed
  text instead, so `label` is omitted and `category` collapses to
  `uncoded`. The finding itself survives -- kind, sample positions and
  lead are untouched -- because a reviewer seeing fewer marks than the
  record carried, with nothing saying any were withheld, is a worse
  failure than seeing them unnamed. What is lost is the name and the
  ability to group two site-defined annotation types apart from each
  other.

  DICOM reserves designators beginning `99` for locally defined schemes,
  so no `99...` value can be recognised however conformant it looks --
  and adding one to `KNOWN_CODING_SCHEMES` will not work, because the
  prefix is checked independently of the set.

  `include_annotation_text=True` restores both fields, exactly as it
  does for `note`. That flag is the protocol's voice here: a study whose
  auditor has determined that site-defined annotation labels may be
  released says so by passing it.

All three are safe by default: no PHI tag configuration is required to
get this behaviour, and it applies even to a bare `Session()`.

`annotations.json`'s `source` field is producer provenance only: the
running isocenter version plus Manufacturer `(0008,0070)`, e.g.
`isocenter/0.6.0 (AcmeCart)`. It does not read Device Serial Number
`(0018,1000)` or any other equipment identifier.

**Record timing** in the `.hea` file combines two independently
sourced parts. The *date* comes from `study.study_date`, which *is*
shifted by `anonymize()` (the same per-patient date shift applied to
the rest of the DICOM metadata). The *time-of-day* comes from the
instance's own timestamp tags -- Acquisition DateTime `(0008,002A)`
when present, else Study Time `(0008,0030)` -- and the date shift
never touches it, whether or not `anonymize()` ran: `SHIFT_DATE` is a
Study-level remediation that writes `study.study_date`, and time-of-day
is sourced from the instance, so it is genuinely not shifted by that
mechanism. This is deliberate, not a gap: time-of-day alone is not a
Safe Harbor identifier. If you export without running `anonymize()` at
all, the date is real too -- exactly like every other un-remediated
field in Isocenter.

On the documented Quick Start path, both instance-level timestamp tags
above are in the Basic profile (`(0008,002A)` and `(0008,0030)`), so a
configured, anonymized session has no real time-of-day left to write.
Isocenter does **not** substitute a fake `00:00:00` in that case: when
`study.study_date` is real but no real time-of-day is available, the
record line's start time/date fields are omitted entirely. This is a
deliberate limitation, not an oversight -- `header(5)` does not support
a date-only start time (PhysioNet's own spec, and `wfdb-python`'s
reference reader/writer, both treat `base_date` as depending on
`base_time` being present), so the record line's choice is between
omitting both fields and fabricating a time; Isocenter omits both.

That real, de-identified date is not simply lost, though: `SHIFT_DATE`
produces genuinely useful information (e.g. for ordering records within
a cohort), and dropping it outright would be a research-utility
regression on top of the privacy fix. When this happens, the shifted
date is written instead as a single `# de-identified start date:
DD/MM/YYYY` comment line -- the one deliberate exception to "no comment
lines" below -- using the same `DD/MM/YYYY` format the record line's
own date field would have used, so the two can never disagree. A
consumer reading the record line alone sees no timing at all, which is
correct (there is none to report); a consumer that also reads comments
gets the real de-identified date, clearly labeled and nowhere near a
field a parser would mistake for a precise timestamp.

Beyond content, the export path itself avoids two structural PHI
paths a WFDB writer could otherwise open:

- **No `#` comment lines are ever written, with one deliberate
  exception.** WFDB readers render header comments verbatim, and
  MIT-BIH convention places age, sex, and diagnosis there -- Isocenter
  never emits one for content. The sole exception is the de-identified
  start date described above, which is not operator-typed or
  attacker-controlled text: it is a computed `DD/MM/YYYY` string
  written through the exact same sanitizer (`_sanitize_description`)
  every other field on the line gets, not a second, separately
  maintained comment-writing path.
- **Record names derive from anonymized identifiers only** (the
  patient pseudonym, series number, instance number) -- never from
  raw patient identifiers, and lead identity in the header prefers the
  coded channel source over the free-text label wherever a coded
  source exists. That preference is a likelihood argument, not a
  filter: a conformant coded source is far less likely to carry
  operator-typed text than a free-text label, but the coded value is
  **not** run through the lead-name allowlist or any content check --
  a non-conformant source can still put arbitrary text there. Both the
  `.hea` writer and the Murmur `annotations.json` bridge strip
  line-break characters out of it regardless, so it can't forge a
  `.hea` comment line, but the text itself is trusted, not filtered.

## Limitations

Format 16 only. The exporter also does not support: WFDB ingest,
`.atr` annotation output, or mu-law/A-law companded audio sample
interpretations.

**Multi-rate records are truncated at ingest, not at export.** Each
Waveform Sequence `(5400,0100)` item is a multiplex group with its own
sampling frequency and channel set -- how DICOM carries ECG at 500 Hz
alongside respiration at 25 Hz. Isocenter reads group 0 and discards the
rest, so groups 1..n never enter the object graph: they are not in the
session, not in the sidecar, and not reachable by any other export
format or API. Since 0.8.2 this is announced rather than silent -- a
warning naming the number of groups dropped, plus a `DATA_LOSS` entry in
the audit log so it appears in the compliance trail and not only in a
log file. Multi-group support is deferred, but a truncated record should
never look like a complete one.

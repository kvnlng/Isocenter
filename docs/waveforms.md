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

Gantry's PHI scan is **tag-gated, not content-based**: it only ever
flags a tag that appears in your loaded PHI tag configuration. A tag
absent from that list is never inspected for what it contains, no
matter how identifying the text inside it is.

The **shipped default** configuration
(`gantry/resources/phi_tags.json`, loaded via `create_config()` /
`load_config()` as in the Quick Start above) covers exactly six tags:

- Patient Name `(0010,0010)`
- Patient ID `(0010,0020)`
- Patient Birth Date `(0010,0030)`
- Institution Name `(0008,0080)`
- Referring Physician Name `(0008,0090)`
- Accession Number `(0008,0050)`

(Patient Name, Patient ID, and Study Date are additionally checked by
a hardcoded baseline scan that runs regardless of configuration --
Gantry always proposes replacing the first two and shifting the
third. Everything else, including the six tags above, is scanned only
if your loaded configuration names it.)

**None of those six tags cover the two free-text fields specific to
waveform export.** Under the default configuration, both are written
verbatim into every WFDB record:

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

**Record timing** in the `.hea` file is sourced from the study date,
which *is* shifted by `anonymize()` (using the same per-patient date
shift applied to the rest of the DICOM metadata). If you export
without running `anonymize()` first, the header carries the real,
un-shifted acquisition date and time -- exactly as every other
un-remediated field in Gantry behaves.

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

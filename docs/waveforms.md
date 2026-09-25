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

if __name__ == "__main__":
    with Session("ecg_study.db") as session:
        session.ingest("ecg")

        # Write and load a configuration before auditing. An unconfigured
        # session applies the floor policy (see below); the configuration
        # is where you record the policy you actually want.
        session.create_config("config.yaml")
        session.load_config("config.yaml")

        session.audit()
        session.anonymize()
        records = session.export("out", format="wfdb")
        session.save(sync=True)
```

Run it as a script: the `if __name__ == "__main__":` guard is required,
because ingest starts worker processes that re-import the script. A WFDB
export does not save the session (a DICOM export does), so call
`save(sync=True)` before the block ends if you want the de-identified graph
kept in the store; otherwise `close()` warns that the edits were not saved.

Each waveform instance becomes one WFDB record, written into the same
directory tree the DICOM exporter uses, so a record's `.hea`/`.dat` files
sit alongside that series' `.dcm` files if you also export
`format="dicom"` into the same folder. For the `waveform_ecg.dcm` file
bundled with pydicom (`pydicom.data.get_testdata_file("waveform_ecg.dcm")`),
copied into `ecg/`:

```text
out/Subject_ANON_afd45d1d36f4892754cd8246/
└─ Study_2012-07-23__39467/
   └─ Series_NoNumber_ECG_Series_47942/
      ├─ ANON_afd45d1d36f4892754cd8246_0_0.hea               header
      ├─ ANON_afd45d1d36f4892754cd8246_0_0.dat               format-16 samples
      └─ ANON_afd45d1d36f4892754cd8246_0_0.annotations.json  cart findings, when present
```

The folder names are `Subject_<Patient ID>`, `Study_<date>_<description>_<last
5 characters of the Study UID>` and `Series_<number>_<modality>_<description>_<last 5
characters of the Series UID>`, each read from the exported values; a
missing value becomes a placeholder such as `NoNumber` or `Series`. A
record name starts with the exported Patient ID and the Series Number (`0`
when there is none); when two records in one folder would share a name,
the later ones get `_2`, `_3`, and so on. The pseudonym, the shifted date
and the UID suffixes differ in your run: all three are derived from a secret
generated for each store. That file carries two multiplex groups, so ingest
also warns that it kept group 0 and discarded 1 (see
[Limitations](#limitations)).

`export(format="wfdb")` returns the path of each record's `.hea` file. Each
record that fails is logged, written to the audit log as an `ERROR` row
naming the instance, and counted in that export's `EXPORT` row, so the
compliance report lists it and grades the run `REVIEW_REQUIRED`. A partial
export returns the records that did reach disk. When at least one record
was attempted and none was written, the call raises
`io_handlers.ExportError` after those rows, as a DICOM export does; its
`.failures` names each record. A store with no
waveform instances, or only waveforms with no samples (each of which gets
its own `DATA_LOSS` row), attempted nothing and returns `[]`.

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
spec-conformant `header(5)` form, `gain(baseline)/units`. If a
downstream tool mis-parses that field, check the tool's parser first.

When present, Waveform Annotation Sequence `(0040,B020)` items --
cart-generated findings such as rhythm calls -- are exported as
`<record>.annotations.json`, in the schema
[Murmur Studio](https://github.com/kvnlng/Murmur) expects.

## What is and isn't de-identified

Isocenter's PHI scan is **tag-gated, not content-based**: a tag is
flagged only if your policy names it -- explicit `phi_tags` entries, or a
privacy profile expanded into them. A tag outside that set is never
inspected for what it contains, however identifying the text inside it is.
Private (odd-group) tags are removed whenever `remove_private_tags` is
`True`, the default, whatever `phi_tags` says. Setting it `False` does not
keep every private value: large private binary values, which cart vendors
use routinely, are dropped at ingest (see
[Private Tags](configuration.md#private-tags)).

Which policy runs depends on the configuration:

- **A bare `Session()`, never `load_config()`-ed**, applies the floor
  policy: the basic profile plus three research defaults.
- **The Quick Start above** loads `privacy_profile: basic@2026c`, the Basic
  Profile column of DICOM PS3.15 Annex E Table E.1-1 (2026c): **646 tags, 646
  effective**, every one reachable by the scan, including those inside
  sequences. The scaffold `create_config()` writes carries the same three
  research defaults, so it applies the same 646 rules as a bare session.
- **Your own configuration** is layered on the floor, on a profile, or,
  with `privacy_profile: none`, is the whole policy. Your explicit tags win
  over the base, and `action: KEEP` opts a tag out.

[Privacy Profile](configuration.md#privacy-profile) lists the floor's
research defaults and the profile's departures from the table.

The basic profile empties Series Description `(0008,103E)` and
Study Description. That matters for waveform export because the series
description becomes a **directory name**: every `.hea`, `.dat` and
`.annotations.json` file lives inside it.

**The exporter itself handles the three free-text surfaces specific to
waveform export, whatever the profile says.** The
PHI scan is tag-gated (see above), so a profile entry alone protects
only a session whose policy carries it, and `privacy_profile: none`
applies nothing. All three are handled whichever configuration you run:

- **Channel Label `(003A,0203)`** -- reaches the `.hea` signal-line
  description and the `annotations.json` `lead` field **only** when it
  is a recognisable signal name, such as a standard ECG lead name.
  Anything else --
  including genuinely operator-typed text -- is replaced with a
  positional `ch<N>` token instead of being written verbatim. `N` is
  the **zero-based** channel index, not DICOM's 1-based ChannelNumber
  -- `ch1` is the *second* channel. Channel Label is also removed by the
  basic profile (PS3.15 Table E.1-1 gives it `X`), so on a
  configured or bare session a channel with no coded Channel Source is
  named `ch<N>` even when its label was a known lead name. Give
  `(003A,0203)` `action: KEEP` to keep recognisable lead names.
- **Unformatted Text Value `(0070,0006)`** -- is omitted from the
  `note` field of `annotations.json` by default. It routinely holds
  free-text clinical commentary, so exporting it is opt-in: pass
  `session.export(folder, format="wfdb", include_annotation_text=True)`
  to restore it. `(0070,0006)` is also in the basic profile, so a
  configured session that passes `include_annotation_text=True` exports
  the profile's remediated value: the text dummy `ANONYMIZED` in the
  exported DICOM, as Table E.1-1's `D` asks. The bridge reads the dummy as
  no text, so the finding carries no `note`, exactly as when the value was
  emptied.

- **Concept Name `(0040,A043)`** -- the annotation's Code Meaning
  reaches `annotations.json` as `label`, and its scheme-qualified Code
  Value as `category`, **only when the Coding Scheme Designator
  `(0008,0102)` names a published vocabulary** such as SNOMED CT. A
  coded finding is
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
  so a `99...` value is never recognised, however conformant it looks.

  `include_annotation_text=True` restores both fields, exactly as it
  does for `note`. That flag is the protocol's voice here: a study whose
  auditor has determined that site-defined annotation labels may be
  released says so by passing it.

All three are safe by default: no PHI tag configuration is required to
get this behaviour, and it applies even to a bare `Session()`.

`annotations.json`'s `source` field is producer provenance only: the
running Isocenter version plus Manufacturer `(0008,0070)`, for example
`isocenter/1.0.0 (Mortara Instrument, Inc.)`. It does not read Device Serial Number
`(0018,1000)` or any other equipment identifier.

**Record timing** in the `.hea` file combines two independently
sourced parts. The *date* comes from `study.study_date`, which *is*
shifted by `anonymize()` (the same per-patient date shift applied to
every date tag under a `SHIFT` or `JITTER` rule). The *time-of-day* comes from the
instance's own timestamp tags -- Acquisition DateTime `(0008,002A)`
when present, else Study Time `(0008,0030)` -- and the date shift
never changes it. This is deliberate: time-of-day alone is not a
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

The date is not lost, though, because it is useful for ordering records
within a cohort. It is written instead as a single comment line, in the
same `DD/MM/YYYY` format the record line's own date field would have
used: `# de-identified start date: DD/MM/YYYY` when `anonymize()` shifted
the study date, or `# start date: DD/MM/YYYY` when it was not shifted
(an export without `anonymize()`, or a date the policy keeps). That
second form is the real date. A consumer reading the record line alone
sees no timing at all; a consumer that also reads comments gets the
date, labelled by whether it was shifted.

Beyond content, the export path itself avoids two structural PHI
paths a WFDB writer could otherwise open:

- **No `#` comment lines are written, except the start date.** WFDB
  readers render header comments verbatim, and MIT-BIH convention
  places age, sex, and diagnosis there; Isocenter never writes one for
  content. The one exception is the start-date line described above
  (`# de-identified start date:` or `# start date:`), a computed
  `DD/MM/YYYY` string written through the same sanitizer as every
  other field, never operator-typed text.
- **Record names are built from the exported Patient ID, series number
  and instance number**: the pseudonym once `anonymize()` has run (the
  source Patient ID before it, or under a `KEEP` rule on Patient ID),
  and never the patient's name or other free text. Lead identity in
  the header prefers the
  coded channel source over the free-text label wherever a coded
  source exists. That preference is a likelihood argument, not a
  filter: a conformant coded source is far less likely to carry
  operator-typed text than a free-text label, but the coded value is
  **not** run through the lead-name allowlist or any content check --
  a non-conformant source can still put arbitrary text there. Both the
  `.hea` writer and the Murmur `annotations.json` bridge strip
  line-break characters out of it regardless, so it can't forge a
  `.hea` comment line, but the text itself is trusted, not filtered.

## DICOM to DICOM round trips

Exporting with `format="dicom"` writes the waveform samples back. The
bytes are copied from the sidecar **verbatim** rather than re-encoded
from the decoded array, so a round trip is byte-exact and the exported
`Waveform Sample Interpretation (5400,1006)` cannot disagree with the
samples it labels.

That verbatim copy is deliberate. Isocenter decodes `US`/`SB`/`UB` to
int16 internally, rebasing `US` by 32768; re-encoding from that array
while leaving `(5400,1006)` saying `US` would shift every value by 32768
without anything raising. Nothing in the pipeline mutates waveform
samples -- unlike pixels, which redaction burns into -- so there is
nothing a re-encode could add, and the mismatch is made structurally
impossible instead of merely tested against. De-identification is
unaffected: waveform PHI lives in tags (channel labels, annotation
concepts), which are remediated in the object graph and rebuilt into the
exported dataset as usual.

Because no decode happens on this path, companded audio (`MB`/`AB`)
round-trips through DICOM export even though WFDB export refuses it.

**Byte order.** Ingest converts a big-endian source's samples to
little-endian, by the Waveform Bits Allocated it declares, and both exports
write them under a little-endian transfer syntax. So the round trip is
exact against the samples as stored, not against the source file's bytes. A
value whose byte order ingest could not settle whole is kept as read, with a
`WARNING` row naming the tag.

If an instance carries a Waveform Sequence but no samples reached the
sidecar, the export logs a warning rather than writing a
structurally-plausible empty record in silence.

## Limitations

Format 16 only. The exporter also does not support: WFDB ingest,
`.atr` annotation output, or mu-law/A-law companded audio sample
interpretations.

**Multi-rate records are truncated at ingest, not at export.** Each
Waveform Sequence `(5400,0100)` item is a multiplex group with its own
sampling frequency and channel set -- how DICOM carries ECG at 500 Hz
alongside respiration at 25 Hz. Isocenter reads group 0 and discards the
rest, so groups 1..n never enter the object graph: neither their samples
nor their sequence items are in the session, in the sidecar, or reachable
by any export format or API. What is written is a conformant single-group
record. The discard is announced: a warning naming the number of groups
dropped, plus a `DATA_LOSS` entry in the audit log. Multi-group support is
not yet available ([#277](https://github.com/kvnlng/Isocenter/issues/277)).
A store written before 0.9.1 can still hold the discarded groups' items;
see [Upgrading from 0.9.x](migration.md#hollow-waveform-multiplex-items).

That entry is scoped `SIGNAL`, and it **does** move Validation Status:
a session that discarded a multiplex group grades `REVIEW_REQUIRED`,
not `PASS`. The tag is standard -- Waveform Sequence `(5400,0100)` is an
even group -- but what was discarded is acquired signal that was in the
source and is not in the export, which is not routine the way a dropped
overlay is. Routine standard-group losses keep the `STANDARD` scope and
keep grading `PASS`.

**Annotations naming a discarded group are dropped, not re-pointed.**
Referenced Waveform Channels `(0040,A0B0)` identifies a mark by a
`(multiplex group, channel)` pair, and DICOM numbers both from 1. PS3.3
C.10.10.1.1 "Referenced Channels" defines the first value of each pair as
the ordinal of the Waveform Sequence `(5400,0100)` Item, and its worked
example writes an annotation covering the entire first multiplex group
plus channels 2 and 3 of the third as `0001 0000 0003 0002 0003 0003`.
Ordinal 1 is therefore the group Isocenter keeps. (The `0000` in that
example is the same section's rule that a channel number of 0 means every
channel in the group; Isocenter honours it by emitting the mark with no
`lead`.) A mark that names any other group has no place in the exported
record: its sample positions index that group's samples, and its lead is
that group's channel. It is left out of `annotations.json` rather than
resolved against the surviving group, which would give a plausible lead
name at a plausible sample position, both belonging to a signal that is
not in the record.

The same filtering happens on the object graph at ingest, so a **DICOM**
export does not carry a Waveform Annotation Sequence `(0040,B020)` item
whose reference names a group that is not in the file. The filter works
on `(group, channel)` pairs before it drops items: an annotation naming
all of group 1 plus a channel of group 3 keeps its surviving pairs, and an
item goes only when every pair it named is gone. Surviving ordinals are
**never renumbered**: the ordinal is positional, so renumbering after a
discard would make the file internally consistent and wrong relative to
the source, with no way to tell afterwards. An annotation with no
`(0040,A0B0)` at all is untouched: the attribute is Type 1C, and its
absence means the mark applies to the whole waveform.

That drop is announced the same way the group discard is: a warning, and
one `DATA_LOSS` entry per instance naming how many annotations were
dropped and which groups they referenced -- one row, not one per mark,
so a cart that marks forty beats on a discarded group does not fill the
report's Data Loss section with forty near-identical lines. It is scoped
`STANDARD`, although the group discard itself is scoped `SIGNAL`: an
annotation is a mark *about* the signal, the loss of the samples already
moves Validation Status through the group's own row, and grading the
marks too would count one loss twice. A record with two `DATA_LOSS` rows
-- one for the groups discarded, one for the marks that referenced them --
is the expected shape: they report different losses. Both rows are
written at ingest; the WFDB export repeats the drop, with its own row, only
for a graph that never passed through ingest.

# Upgrading from 0.9.x

The code in a new release is fixed; the data an old release wrote is not.
This page lists what changes when a configuration, a store or an export
written by an earlier release meets 1.0, in the order you are likely to meet
it: the configuration first (`load_config()`), then the store (opening it,
`audit()`, `export()`), then identity tokens.

Each change is handled the way its information allows: healed where the
store itself proves what is wrong, reported where it cannot be repaired, and
left to an explicit call where only your site knows the answer. Several of
them write a `WARNING` audit row on every open, so every compliance report
over that store grades `REVIEW_REQUIRED` until you act. Where the fix is
"re-ingest the source files into a new store", that is the only way to give
the data 1.0's guarantees.

**Nothing from before 1.0 is read by a second path or migrated.** A layout
1.0 does not read is refused with an error naming the cause.

## Configuration files

From 1.0 a configuration is read as exactly what it says: a key the schema
does not have, a value of the wrong type, or a `version` this library does
not read raises `ValueError` naming it, where 0.9.x loaded the file and
ignored or misread the part it did not understand
([Configuration](configuration.md#schema-version-2) has the schema).

**Files Isocenter wrote load unchanged.** Every file `create_config()` or
an auto-save wrote in 0.9.x loads, and so does every configuration in this
documentation. A file with no `version` line is version 2.0. A file saying
`privacy_profile: basic` means `basic@2026c`, the profile name pinned to
its PS3.15 edition, and loads the same rules; `configuration.privacy_profile`
then reads `"basic@2026c"`, and `save()` and `create_config()` write that.

**What is refused, and the fix.** Apart from the first two, each of these
loaded in 0.9.x without meaning what it said:

| 0.9.x file | Fix |
| :--- | :--- |
| `version: "1.0"` (the label before version 2) | write `version: "2.0"`; the content loads as before |
| `version: 2.0` (unquoted: a YAML number) | quote it: `version: "2.0"` |
| a misspelt key at any level (`remove_private_tag`, `redaction_zone`, `actoin`) | the refusal names it and, usually, the key you meant |
| `machine_rules:` | rename it `machines:` |
| an unquoted numeric serial, `serial_number: 0123` (loaded as 83) | quote it: `serial_number: "0123"` |
| `remove_private_tags: "false"`, or a bare `remove_private_tags:` | write `true` or `false`, unquoted |
| `date_jitter: -5` | write `date_jitter: {min_days: -5, max_days: -5}` |
| `date_jitter` with `min_days` greater than `max_days` | put the bounds the right way round |
| an external profile file carrying anything but `phi_tags` and `version` | move the other keys into the configuration that names the profile |
| a CTP rule list (`rules:` at the top level) | copy the rules into `machines:`; see [Import CTP rules](ctp-import.md) |

A refusal changes nothing: the session's configuration is what it was
before the call.

Three more readings changed in 1.0:

| 0.9.x file | Fix |
| :--- | :--- |
| `version: "2.00"` or `"02.0"` | write `version: "2.0"` |
| a blank `serial_number: "  "` (matched no machine) | give the machine's serial |
| a bare `privacy_profile:` line | nothing to do unless you meant `none`: it now means the floor, as leaving the line out does, where 0.9.x read it as `none` and applied no base; write `privacy_profile: none` to keep that |

A phi rule's `value: null` and `name: null` read as absent, as before:
`REPLACE` writes `ANONYMIZED`, and the finding is named `Unknown Tag`.

### Configuration calls

- **Auto-save is off.** In 0.9.x, `add_rule()`, `update_rule()`,
  `delete_rule()` and `set_phi_tag()` rewrote the loaded file. They now
  change memory only, and print a line saying the file is unchanged.
  Call `session.configuration.save()`, or set
  `session.configuration.auto_save = True` once per session. A file
  0.9.x's auto-save wrote loads unchanged, and saving it again rewrites
  it without the inlined profile.
- **One loader.** `ConfigLoader.load_redaction_rules()` and
  `ConfigLoader.load_phi_config()` are gone and raise `AttributeError`;
  `PhiInspector(config_path=...)` raises `TypeError`. Read a file
  with `session.load_config(path)` or
  `ConfigLoader.load_unified_config(path)`, and hand `PhiInspector` the
  policy (`config_tags=`).
- **Three keywords renamed.** Each old keyword raises `TypeError`;
  positional calls are unchanged.
  `add_rule(model=, zones=)` is `add_rule(model_name=, redaction_zones=)`,
  the keys a `machines:` rule has in the file;
  `set_phi_tag(replacement=)` is `set_phi_tag(value=)`, the key a phi rule
  has; and `Builder.start_patient(id=)` is `start_patient(patient_id=)`.
- **One spelling per format.** `generate_report(format=)` takes
  `'markdown'` only, and `generate_manifest(format=)` `'html'` or
  `'json'` only; `'md'` and case variants such as `'HTML'` raise
  `ValueError`.

## Configuration and cohort export: what changed at 1.0

**A store anonymized under 0.9.7's profile.** 0.9.7's `basic` profile had 35 rules. Its statuses carry no policy, so an export from such a store writes a `WARNING` row (see [PHI statuses recorded before 1.0](#phi-statuses-recorded-before-10)). Load your configuration, run `audit()` and then `anonymize()` before exporting again. That removes what `basic@2026c` removes, but cannot bring back the Type 2 attributes 0.9.7 removed (Accession Number, Referring Physician's Name, Study ID, Patient's Birth Date); only re-ingesting the source files restores them.

**The Basic Profile's D codes write a dummy.** Before 1.0 a `D` code emptied the attribute and an `X/D` code removed it, so a Type 1 attribute was written zero-length or dropped. From 1.0 every code with a D arm writes a dummy value for the attribute's VR ([What basic@2026c contains](configuration.md#what-basic2026c-contains)). An export of the same data under 1.0 therefore carries attributes a 0.9.x export did not, each holding its dummy.

**`export_to_parquet` is gone.** `session.export_to_parquet(...)` raises `AttributeError`. Use `session.export_dataframe("cohort.parquet")`, which writes Parquet for a `.parquet` path. The columns differ: `export_to_parquet` wrote SQL column names (`patient_id`, `sop_instance_uid`), and `export_dataframe` writes keywords (`PatientID`, `SOPInstanceUID`) for its base columns, so code that reads the old files needs its column names changed as well as its call.

## PHI statuses recorded before 1.0

A store records what the last scan concluded about each patient, study and instance (`phi_status`). From 1.0 it also records the policy that scan ran under (`phi_status_policy`: a fingerprint of the tag rules, `remove_private_tags` and the configuration schema version, and a readable base such as `basic@2026c`). The schema version is in it because a release that changes what an unchanged configuration detects raises that version's minor: a store's statuses from before such a release then read as recorded under another policy, and the export says so as below.

A store written by 0.9.x never recorded which configuration ran, so opening one adds the two columns empty and fills nothing in: each status keeps its value (`REMEDIATED` stays `REMEDIATED`) with no policy, and each open logs one warning counting them. The first `export()` that writes such instances writes one `WARNING` audit row saying so, and its report grades `REVIEW_REQUIRED`. Run `audit()` (and `anonymize()`, if it finds anything) under the configuration you mean, then `save()`: the statuses then carry that policy and the row stops.

**Never save into a 1.0 store from 0.9.x.** A 1.0 store is not for 0.9.x: an older release reopening it reads a nested item's stored status as an attribute, and cannot export it (it reads the stored DS/IS values as dicts). 0.9.x does not know the policy columns either, so a status it records is left beside the policy of the last 1.0 scan -- a pairing no scan concluded, which the export cannot tell from a true one. If that has happened, run `audit()` under your configuration before trusting any status. Keep a copy of the 0.9.x store if you may need to go back, and go back to that copy, not into the 1.0 store.

## UIDs in stores and exports from before 1.0

Before 1.0 Isocenter exported Study, Series, SOP Instance and every other UID as it was ingested, except that redaction gave a redacted instance a random SOP Instance UID under pydicom's root. From 1.0 the floor and `basic@2026c` replace each UID the PS3.15 table codes `U` with one derived from the store's project secret (see [Configuration](configuration.md#privacy-profile)).

Opening an older store changes nothing by itself. Its instances still hold their source UIDs, so the next `audit()` raises a finding on each, and `anonymize()` replaces them. A redacted instance's random UID is replaced like any other, and is stable from then on.

Files exported before 1.0 keep their source UIDs, and 1.0 exports of the same data carry replacements, so the two do not link by UID. That is inherent: it is what replacing them means. A study exported in part before 1.0 and in part after is two studies to anything that groups by Study Instance UID. To keep a project's exports linkable across the upgrade, keep the UIDs: `KEEP` on the `U` rows ([Keeping UIDs](configuration.md#privacy-profile)).

A configuration that gave a UI attribute `REPLACE` with no value failed to load before 1.0 and now means UID replacement. `REPLACE` with a `value:` on a UI attribute writes that value, as before.

## Identity tokens locked before 1.0

Every release before 1.0 wrote the reversible-anonymization token into `(0400,0510)` and the transfer syntax UID into `(0400,0520)`. DICOM defines those two elements the other way round: `(0400,0510)` is the Encrypted Content Transfer Syntax UID and `(0400,0520)` is the Encrypted Content. From 1.0 the token goes into `(0400,0520)`, and 1.x reads only that layout.

A token in the earlier layout is recognised but not read. Whether it comes from an old store or from an old export ingested into a new one, `recover_patient_identity()` raises `RuntimeError` naming the layout and restores nothing, for the whole patient. `lock_identities()` refuses rather than replacing it. Recover such identities with Isocenter 0.9.x and the key they were locked with.

Exporting such a patient from 1.x writes the item as it is, and 0.9.x can still recover it. The export also writes a `WARNING` audit row counting those instances, so the compliance report grades `REVIEW_REQUIRED`.

## A project secret stays in its store

In 0.9.7 and 0.9.8, `store_backend.write_project_secret(path)` and `load_project_secret(path)` copied a store's project secret into a fresh store. Both are gone and raise `AttributeError`: a secret belongs to the store it was generated in (see [What to keep](configuration.md#what-to-keep)). A later batch for the same patients goes into the same store. Nothing reads a secret file written by 0.9.7 or 0.9.8 any more; delete it as you would a key.

A store that loaded a secret it could not verify, under 0.9.7 or 0.9.8, still warns at every `audit()` and still grades `REVIEW_REQUIRED`. The reason it warns is recorded in the store, and removing the load does not remove it.

A store that holds shifted dates, or UIDs replaced since 1.0, but has lost its secret refuses `audit()`, `anonymize()`, `redact()` and `export(check_burned_in=True)` rather than generate a second offset or a second UID for each instance. Nothing in Isocenter deletes the secret, so this is a store whose `project_secret` row was deleted by hand, and the secret cannot be restored from outside it: re-ingest the source files into a new store.

**Starting a new project over another project's export.** A fresh store that ingests an export from another project generates its own secret and writes a `WARNING` row naming the pseudonyms it cannot verify: its offsets are its own, not the source project's, and intervals within each patient are kept. That is the one path for a new project. If the data belongs to an existing project, ingest it into that project's store.

## Files with no Patient ID, grouped before 1.0

Before 1.0 ingest grouped every file whose Patient ID was **empty** under one patient `''`, and every file **without** one under one patient `UnknownPatient`, across the whole store, so either may hold several subjects. From 1.0 a file with no Patient ID belongs to the patient holding its study, or to a new patient of that study alone, and a subject with no Patient ID exports an empty one.

Opening an older store splits nothing, because splitting would give dates already shifted under the group's offset a second one. So:

- New ID-less files ingested into an old store get patients of their own; the old grouping stays as it was.
- A `''` patient's date shift is still declined on every pass ("could not resolve a PatientID"), as before: loud and fail-closed.
- An `UnknownPatient` patient keeps its one pseudonym and one offset for every subject in it.
- Every open of a store holding either writes one `WARNING` audit row, counts only: "N patients were grouped by a release before 1.0 from files with no Patient ID and may be more than one subject". A real Patient ID `UnknownPatient` is counted too. Reports over the store grade `REVIEW_REQUIRED`.

To separate the subjects, re-ingest their **source** files into a new store.

## Stores de-identified before 0.9.7 (GHSA-phg9-vcvc-j4r7)

Before 0.9.7 the `ANON_` pseudonym was the first 12 hex characters of an unsalted SHA-256 of the original Patient ID, and the date offset was read from the same digest. Anyone holding an exported file and the date-jitter range could undo the date shift, and could recover the original ID by hashing candidates. From 0.9.7 both are derived with HMAC-SHA256 under a per-project secret kept in the store, and the pseudonym is `ANON_` plus 24 hex characters.

Opening an older store classifies each patient once. A patient that was already de-identified (an ID of exactly the old `ANON_` shape, or any shifted date) keeps the old scheme, because giving them a new offset would put two offsets on one patient's dates. New studies for that patient are shifted by the old, recoverable offset. Every other patient, and every patient added later, uses the keyed scheme. While any old-scheme patient remains, each open logs a warning and writes one `WARNING` audit row naming how many, so compliance reports over the store grade `REVIEW_REQUIRED`.

Files already exported by an older release stay recoverable, and nothing Isocenter does now changes that. To give those patients the keyed scheme, re-ingest their **source** files into a new store. Re-ingesting an old *export* does not help: an ID that is already `ANON_` is never replaced, so its unkeyed digest is exported unchanged (the load notice and `audit()` count these too). Ingesting raw files for a patient an older release already de-identified, into that same store, makes a second subject: the new data is keyed, its earlier studies keep the old pseudonym and offset, and `audit()` writes a `WARNING` row saying so.

## Dates shifted before 0.9.6

From 0.9.6 Isocenter records, per value, which dates its own shift
produced: a date under a `SHIFT`/`JITTER` rule that the pipeline never
shifted is raised and shifted on a later pass, and a shifted one is never
shifted twice. A store written earlier has no such record. For instances
under a study whose date was already shifted, Isocenter keeps the
pre-0.9.6 rule: their `SHIFT`/`JITTER` values are not re-examined, so a
date first named in a later policy can survive. Nothing is shifted twice.

Opening such a store logs a warning and writes one `WARNING` audit row
naming how many instances and studies are affected -- **on every open**,
because nothing in the store can change their status. That row appears
under *Exceptions & Errors*, so every compliance report generated over the
store grades `REVIEW_REQUIRED`. Re-ingesting the source files into a new
store gives them the full guarantee and a clean audit log. A pre-0.9.6
store with no shifted study writes nothing.

`Instance.date_shifted` was removed in the same release.

## Stores written before 0.9.1

### Resurrected private tags

Before 0.9.1, private (odd-group) tags written to the store's
`instance_attributes` table were never read back, and the writer did not
mirror deletions: a session that ran `remove_private_tags: true`,
anonymized, and saved deleted the vendor block from the graph but left
every row of it in the store. Those rows were inert until 0.9.1 began
reading the table, which is what makes `remove_private_tags: false`
survive a reload. The first open of such a store after upgrading therefore
puts the stripped rows back on the graph, and an export taken from that
session carries them.

The library cannot decide this for you: a stale row and a legitimate
one are byte-identical, and nothing in the database records which
private tags were deleted from the graph. What the store does record is
what every earlier session actually saw -- the core `attributes_json`,
which was the whole graph before the table was readable. So the repair
is explicit and opt-in:

```python
with Session("store.db") as session:
    dropped = session.reconcile_private_tags()
```

`reconcile_private_tags()` drops every `instance_attributes` row whose
tag is absent from the instance's core stored attributes, removes the
same tags from the live graph (undoing the resurrection this session's
open performed), and writes one `RECONCILE_PRIVATE` audit row per
affected instance so the repair is in the compliance trail. It returns
the number of rows dropped.

**Call it only if you know your store was de-identified before the
upgrade.** For that store the core attributes are the complete answer,
and everything the call drops is a row an earlier export never
carried. For a store that legitimately keeps its vendor block
(`remove_private_tags: false`), the table *is* the private data and this
call deletes it -- the same grain as `redact(force=True)`: the repair
exists in the API, nothing changes silently, and the caller is choosing
its cost. A site that does not know its history should re-run the
privacy pipeline over the store instead, which re-strips the graph and
mirrors the deletions into the table on save.

### Corrupted pixel geometry

Releases before 0.9.1 could persist a guessed geometry --
`SamplesPerPixel=3`, `PhotometricInterpretation=RGB`, swapped axes --
for a multi-frame grayscale instance whose Columns was 3 or 4. A store
carrying those descriptors exports garbage while grading `PASS`: every
step downstream behaves correctly on descriptors that are already
wrong.

Opening a session over such a store runs an arithmetic check --
does Rows x Columns x SamplesPerPixel x NumberOfFrames x
bytes-per-sample equal the stored sidecar frame length? -- and logs a
warning naming each instance it flags. The same result reaches the
compliance report as a `COMPLIANCE_CHECK` exception, so a session
holding detected damage grades `REVIEW_REQUIRED` rather than `PASS`.
The check is exact for frames stored uncompressed; a zlib-stored
frame's length is post-compression, so damage behind one is caught
where the bytes are decoded instead: `export(verify_readback=True)`
decodes every written file and compares every pixel sample
with what it meant to write, failing the mismatch at delivery.

There is deliberately no automatic repair. The sidecar's bytes are
shape-free, so a migration would be a best-effort guess, and a
best-effort repair that silently half-works is worse than a detector.
The remedy is to re-ingest the affected instances from their source
files.

### Hollow waveform multiplex items

Isocenter keeps the first multiplex group of a multi-group waveform and
discards the rest (see [Waveforms](waveforms.md#limitations)). A store that
ingested such a waveform with 0.8.2 to 0.9.0 dropped the other groups'
samples but kept their Waveform Sequence items, so it holds one item per
group with samples behind item 0 only, and exported a file declaring
Waveform Data it did not carry.

Opening such a store heals that shape in memory: it prunes the sample-less
items, and with them any annotations referencing the pruned groups, exactly
as ingest does now, and logs a warning naming the instance and why. The
export is then a conformant single-group record. The store itself is
unchanged until the session saves, and the warning repeats on every open
until it does. The discarded samples are not recoverable from the store --
the original discard happened in the session that ingested, and its
`DATA_LOSS` entry is in the same store's audit log -- so re-ingesting the
source files into a new store is the only way to get them back. A graph
built by hand and written with `DicomExporter.write_tree()` is not touched:
the serializer never passes through ingest or hydration.

## Redacted on 0.9.0 or earlier with a multi-zone rule

Releases up to and including 0.9.0 applied only the last applicable zone of a multi-zone rule to an instance loaded from a saved store, and still recorded a full redaction. That record is a hash of the *configuration*, not of the pixels, so the current code agrees with it and skips the instance: `session.redact()` returns `0` and the burned-in identifier stays where it is.

If you redacted with a rule carrying two or more zones, against a store that had been saved and reopened, on 0.9.0 or earlier, repair the store with:

```python
session.redact(force=True)
session.save()
```

No source file is needed: the identifier is still in the store's own pixels. Every instance the rules match is redacted again, and each takes a **new SOP Instance UID**, so its exported filename changes and it stops matching the source file it was ingested from.

## Repairs on a 0.9.x store

Two `Session` methods have entries that point here.

**`redact(force=True)`** repairs a store redacted on 0.9.0 or earlier with a multi-zone rule: see [Redacted on 0.9.0 or earlier with a multi-zone rule](#redacted-on-090-or-earlier-with-a-multi-zone-rule).

**`reconcile_private_tags()`** repairs a store de-identified before 0.9.1, whose stripped private tags come back when it is opened. [Resurrected private tags](#resurrected-private-tags) says when to call it and what it deletes.

`recover_patient_identity()` and `lock_identities()` refuse every token written before 1.0, whichever release wrote it: see [Identity tokens locked before 1.0](#identity-tokens-locked-before-10).

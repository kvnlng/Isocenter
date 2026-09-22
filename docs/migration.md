# Migration Tools

## Upgrading an existing store

The code in a new release is fixed; the data an old release wrote is
not. Four shapes of legacy damage are known, and each is handled the
way its information allows -- healed where the store itself proves what
is wrong, detected where it cannot be repaired, and left to an explicit
opt-in where only the site knows the answer.

### Corrupted pixel geometry (#186, #214)

Releases before the #186 fix could persist a guessed geometry --
`SamplesPerPixel=3`, `PhotometricInterpretation=RGB`, swapped axes --
for a multi-frame grayscale instance whose Columns was 3 or 4. A store
carrying those descriptors exports garbage while grading `PASS`: every
step downstream behaves correctly on descriptors that are already
wrong.

Opening a session over such a store now runs an arithmetic check --
does Rows x Columns x SamplesPerPixel x NumberOfFrames x
bytes-per-sample equal the stored sidecar frame length? -- and logs a
warning naming each instance it flags. The same result reaches the
compliance report as a `COMPLIANCE_CHECK` exception, so a session
holding detected damage grades `REVIEW_REQUIRED` rather than `PASS`.
The check is exact for frames stored uncompressed; a zlib-stored
frame's length is post-compression, so damage behind one is caught
where the bytes are decoded instead: `export(verify_readback=True)`
(#209, #449) decodes every written file and compares every pixel sample
with what it meant to write, failing the mismatch at delivery.

There is deliberately no automatic repair. The sidecar's bytes are
shape-free, so a migration would be a best-effort guess, and a
best-effort repair that silently half-works is worse than a detector.
The remedy is to re-ingest the affected instances from their source
files.

### Hollow waveform multiplex items (#160, #168)

A store that ingested a multi-group waveform between 0.8.2 and the
#160 fix holds one Waveform Sequence item per multiplex group with
samples behind item 0 only, and exported a file declaring Waveform
Data it did not carry. Hydration now heals that shape on load -- see
[Waveforms](waveforms.md) for the full story.

### Resurrected private tags (#158, #172)

Before #158, private (odd-group) tags written to the store's
`instance_attributes` tier were never read back, and the writer did not
mirror deletions: a session that ran `remove_private_tags: true`,
anonymized, and saved deleted the vendor block from the graph but left
every row of it in the store. Those rows were inert -- nothing read
them -- until #158 wired the tier into hydration, which is the fix that
makes `remove_private_tags: false` survive a reload. The first open of
a pre-#158 store after upgrading therefore puts the stripped rows back
on the graph, and an export taken from that session carries them.

The library cannot decide this for you: a stale row and a legitimate
one are byte-identical, and nothing in the database records which
private tags were deleted from the graph. What the store does record is
what every pre-#158 session actually saw -- the core `attributes_json`,
which was the whole graph before the tier was readable. So the repair
is explicit and opt-in:

```python
with Session("store.db") as s:
    dropped = s.reconcile_private_tags()
```

`reconcile_private_tags()` drops every `instance_attributes` row whose
tag is absent from the instance's core stored attributes, removes the
same tags from the live graph (undoing the resurrection this session's
open performed), and writes one `RECONCILE_PRIVATE` audit row per
affected instance so the repair is in the compliance trail. It returns
the number of rows dropped.

**Call it only if you know your store was de-identified before the
upgrade.** For that store the core attributes are the complete answer,
and everything the call drops is a row a pre-upgrade export never
carried. For a store that legitimately keeps its vendor block
(`remove_private_tags: false`), the tier *is* the private data and this
call deletes it -- the same grain as `redact(force=True)`: the repair
exists in the API, nothing changes silently, and the caller is choosing
its cost. A site that does not know its history should re-run the
privacy pipeline over the store instead, which re-strips the graph and
(since #158) mirrors the deletions into the tier on save.

### Dates shifted before 0.9.6 (#510, #518)

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

Also changed at the same release: `Instance.date_shifted` is gone (see
[API stability](api/stability.md)).

### Stores de-identified before 0.9.7 (GHSA-phg9-vcvc-j4r7)

Before 0.9.7 the `ANON_` pseudonym was the first 12 hex characters of an unsalted SHA-256 of the original Patient ID, and the date offset was read from the same digest. Anyone holding an exported file and the date-jitter range could undo the date shift, and could recover the original ID by hashing candidates. From 0.9.7 both are derived with HMAC-SHA256 under a per-project secret kept in the store, and the pseudonym is `ANON_` plus 24 hex characters.

Opening an older store classifies each patient once. A patient that was already de-identified (an ID of exactly the old `ANON_` shape, or any shifted date) keeps the old scheme, because giving them a new offset would put two offsets on one patient's dates. New studies for that patient are shifted by the old, recoverable offset. Every other patient, and every patient added later, uses the keyed scheme. While any old-scheme patient remains, each open logs a warning and writes one `WARNING` audit row naming how many, so compliance reports over the store grade `REVIEW_REQUIRED`.

Files already exported by an older release stay recoverable, and nothing Isocenter does now changes that. To give those patients the keyed scheme, re-ingest their **source** files into a new store. Re-ingesting an old *export* does not help: an ID that is already `ANON_` is never replaced, so its unkeyed digest is exported unchanged (the load notice and `audit()` count these too). Ingesting raw files for a patient an older release already de-identified, into that same store, makes a second subject: the new data is keyed, its earlier studies keep the old pseudonym and offset, and `audit()` writes a `WARNING` row saying so.

### Carrying a project secret between stores

A store makes its own project secret the first time `audit()` or `anonymize()` needs one, so two stores give the same patient different pseudonyms and different offsets unless they share it. To keep offsets consistent across more than one store (a later batch for the same patients, or re-ingesting an export), carry the secret:

```python
first.store_backend.write_project_secret("project.secret")   # refuses to overwrite; mode 0600
second = Session("batch2.db")
second.store_backend.load_project_secret("project.secret")   # before second's first audit()
```

`load_project_secret` refuses a store that already holds a secret, whichever one, because everything it pseudonymized or shifted was derived under that secret; load into a fresh store. A store holding keyed pseudonyms accepts only a secret that minted at least one of them. A store whose shifted patients all kept their Patient IDs has no pseudonym to check a secret against: it accepts the secret, records it as unverified, and writes a `WARNING` row at the load and at every later `audit()`, so its reports grade `REVIEW_REQUIRED` -- permanently: there is no call to acknowledge the warning, and a later load into that store is recorded as unverified too, even when the secret verifies against pseudonyms minted after the first load. Make sure that file is this store's own project's secret: a different one gives each patient's later dates a second offset, and nothing in the store can tell.

The file recovers the dates of every store sharing it: keep it with the store, never with an export. A store that holds shifted dates but has lost its secret refuses `audit()`, `anonymize()` and `export(check_burned_in=True)` rather than generate a second offset; load the secret back to continue.

**Starting a new project over another project's export.** A fresh store that ingests an export from another project, without that project's secret, generates its own secret and writes a `WARNING` row naming the pseudonyms it cannot verify: its offsets are its own, not the source project's, and intervals within each patient are kept. That is the one path for a new project. There is no override to adopt a secret into a store that has one, because that is the only guard against silently mixing two projects' offsets. If the data belongs to the existing project, load that project's secret into a fresh store before its first `audit()` and ingest the export there instead.

## Configs from 0.9.x

From 1.0 a configuration is read as exactly what it says: a key the schema
does not have, a value of the wrong type, or a `version` this library does
not read raises `ValueError` naming it, where 0.9.x loaded the file and
ignored or misread the part it did not understand
([#711](https://github.com/kvnlng/Isocenter/issues/711),
[#712](https://github.com/kvnlng/Isocenter/issues/712),
[#713](https://github.com/kvnlng/Isocenter/issues/713);
[Configuration](configuration.md#schema-version-2) has the schema).

**Files Isocenter wrote load unchanged.** Every file `create_config()` or
an auto-save wrote in 0.9.x loads, and so does every configuration in this
documentation. A file with no `version` line is version 2.0. A file saying
`privacy_profile: basic` means `basic@2026c`, the profile name pinned to
its PS3.15 edition, and loads the same rules; `configuration.privacy_profile`
then reads `"basic@2026c"`, and `save()` and `create_config()` write that
([#714](https://github.com/kvnlng/Isocenter/issues/714)).

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

A refusal changes nothing: the session's configuration is what it was
before the call.

Three more readings changed in 1.0
([#730](https://github.com/kvnlng/Isocenter/issues/730)):

| 0.9.x file | Fix |
| :--- | :--- |
| `version: "2.00"` or `"02.0"` | write `version: "2.0"` |
| a blank `serial_number: "  "` (matched no machine) | give the machine's serial |
| a bare `privacy_profile:` line | nothing to do unless you meant `none`: it now means the floor, as leaving the line out does, where 0.9.x read it as `none` and applied no base; write `privacy_profile: none` to keep that |

A phi rule's `value: null` and `name: null` read as absent, as before:
`REPLACE` writes `ANONYMIZED`, and the finding is named `Unknown Tag`.

- **Auto-save is off.** In 0.9.x, `add_rule()`, `update_rule()`,
  `delete_rule()` and `set_phi_tag()` rewrote the loaded file
  ([#715](https://github.com/kvnlng/Isocenter/issues/715)). They now
  change memory only, and print a line saying the file is unchanged.
  Call `session.configuration.save()`, or set
  `session.configuration.auto_save = True` once per session. A file
  0.9.x's auto-save wrote loads unchanged, and saving it again rewrites
  it without the inlined profile.
- **One loader.** `ConfigLoader.load_redaction_rules()` and
  `ConfigLoader.load_phi_config()` are gone and raise `AttributeError`;
  `PhiInspector(config_path=...)` raises `TypeError`
  ([#729](https://github.com/kvnlng/Isocenter/issues/729)). Read a file
  with `session.load_config(path)` or
  `ConfigLoader.load_unified_config(path)`, and hand `PhiInspector` the
  policy (`config_tags=`).

## Clinical Trial Processor (CTP)

Isocenter includes a utility to convert legacy CTP `DicomPixelAnonymizer.script` files into the CTP rule-list format (YAML), the format of the knowledge base `create_config()` matches machines against.

```bash
# Convert CTP script to Isocenter YAML
python -m isocenter.utils.ctp_parser /path/to/anonymizer.script output_rules.yaml
```

Its output is the rule list the CTP knowledge base is read from (`rules:`,
as in `isocenter/resources/ctp_rules.json`), not a configuration:
`load_config()` refuses it, naming `rules` as an unknown key (0.9.x loaded it
and applied none of its rules). Copy a rule you want into a configuration's
`machines:` list, with the `serial_number` of the machine it is for.

This parser extracts:

- Manufacturer/Model matching criteria.
- Redaction zones (automatically converting `x,y,w,h` to `r1,r2,c1,c2`).

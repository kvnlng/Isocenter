# API stability

What the 1.0 tag promises, in three tiers. The facade is what gets
frozen; the internal seams behind it are not.

- **Frozen (tier 1)** names keep their spelling, their parameter names,
  their return shapes and their documented behaviour for every 1.x
  release; a change is a 2.0.
- **Documented but internal (tier 2)** names are listed on this page,
  and rendered in the API reference or named by a guide where a reader
  needs them. They are safe to call, and may change in a 1.x release with
  a CHANGELOG entry that names the old spelling and the new one. They exist so a
  reader can see the seams, not so a program can lean on them.
- **Private (tier 3)** names — everything with a leading underscore, and
  every module not listed below — may change without notice.

Optional extras (`ocr`, `nlp`) degrade to the documented fallback; the
fallback is frozen, the extra's internals are not. How each method
behaves is on its own page — start at [Session](session.md) — and how a
behaviour came to be is in the
[changelog](https://github.com/kvnlng/Isocenter/blob/main/CHANGELOG.md).

## What 1.0 promises

- **The facade.** `Session`, its 28 methods and their parameters, the
  shapes they return, and the other callables the frozen tables list
  keep their spelling and behaviour for every 1.x
  ([Frozen at 1.0](#frozen-at-10)).
- **The output.** For the same input, configuration and project secret,
  a 1.x exports what the previous release exported, or its changelog
  says what changed ([Data promises](#frozen-at-10)). The grade's
  values and the audit words keep their spelling, and no condition that
  grades a run is removed or narrowed; a 1.x may add words and
  conditions, with a changelog entry.
- **The configuration.** A file 1.0 loads, every 1.x loads, and a 1.x
  that applies an unchanged file differently raises the schema's minor
  version ([Data promises](#frozen-at-10)).
- **Plugins, provisionally.** The exporter registry is usable and
  provisional until 1.1, which may replace it rather than extend it; a
  plugin written against 1.0 should pin `isocenter>=1.0,<1.1`, and in
  1.0 no third-party export grades `PASS`
  ([The exporter registry](#the-exporter-registry-provisional-until-11)).

## Frozen at 1.0

**Package.** `isocenter.Session`, `isocenter.Builder`,
`isocenter.Equipment`, `isocenter.RedactionError`,
`isocenter.ExportError` — the five names in `__all__` — and
`isocenter.__version__`.

**`Session` construction and lifetime.** `Session(persistence_file=None)`,
`None` meaning `ISOCENTER_DB_PATH` then `isocenter.db`; `":memory:"` is
accepted. `with Session(...) as s:` (`__enter__` returns the session,
`__exit__` closes); `close()` is idempotent and releases the executor
and both threads. When a file named `isocenter.key` exists in the
current working directory at construction, `Session()` calls
`enable_reversible_anonymization()` with it, resolved to an absolute
path then, so a later change of directory does not move it; a key
beside the store in another directory is not looked for. With no such
file, reversible anonymization stays off and no key is created. A
malformed one makes `Session()` raise its `ValueError`. Keeping the key
in the directory you run from, beside the store and the exported data,
is not advised: whoever holds the key and an export can read the
identities it carries.

**`Session` methods — all 28 public names, with their parameters.**
`self` is omitted, `*` marks the keyword-only boundary, and a parameter
moved across it is a different call.

| Method | Parameters |
| --- | --- |
| `ingest` | `directory` |
| `save` | `sync=False` |
| `close` | — |
| `examine` | — |
| `create_config` | `output_path` |
| `load_config` | `config_file` |
| `preview_config` | — |
| `audit` | `config_path=None` |
| `auto_remediate_config` | `report` |
| `anonymize` | `findings=None` |
| `enable_reversible_anonymization` | `key_path='isocenter.key'` |
| `lock_identities` | `patient_id, persist=False, *, verbose=True, tags_to_lock=None` |
| `lock_identities_batch` | `patient_ids, auto_persist_chunk_size=0, tags_to_lock=None, *, persist=False, verbose=True` |
| `recover_patient_identity` | `patient_id, restore=True` |
| `redact` | `show_progress=True, force=False` |
| `redact_by_machine` | `serial_number, roi` |
| `scan_pixel_content` | `serial_number=None` |
| `discover_redaction_zones` | `serial_number, sample_size=50, min_confidence=80.0` |
| `reconcile_private_tags` | — |
| `export` | `folder, format='dicom', **options` |
| `export_dataframe` | `output_path='export_metadata.csv', expand_metadata=False, patient_ids=None` |
| `get_cohort_report` | `expand_metadata=False, patient_ids=None` |
| `phi_status_summary` | — |
| `generate_report` | `output_path, format='markdown'` |
| `generate_manifest` | `output_path, format='html'` |
| `save_analysis` | `report` |
| `compact` | — |
| `release_memory` | — |

**Other frozen callables, with their parameters.** Spelled as the
Session table spells them, and each qualified by the class (or module)
that defines it. The classes' fields and the rules each method follows
are in the paragraphs below.

| Callable | Parameters |
| --- | --- |
| `Session.__init__` | `persistence_file=None` |
| `IsocenterConfiguration.save` | — |
| `IsocenterConfiguration.add_rule` | `serial_number, manufacturer='Unknown', model_name='Unknown', redaction_zones=None` |
| `IsocenterConfiguration.update_rule` | `serial_number, updates` |
| `IsocenterConfiguration.delete_rule` | `serial_number` |
| `IsocenterConfiguration.set_phi_tag` | `tag, action, value=None` |
| `IsocenterConfiguration.get_rule` | `serial_number` |
| `DicomItem.set_attr` | `tag, value` |
| `Instance.set_attr` | `tag, value` |
| `Instance.get_pixel_data` | — |
| `Instance.set_pixel_data` | `array` |
| `Instance.unload_pixel_data` | — |
| `Instance.discard_pixel_data` | — |
| `Instance.get_waveform_data` | — |
| `Builder.start_patient` | `patient_id, name` |
| `PhiReport.__init__` | `findings, failures=None` |
| `PhiReport.to_dataframe` | — |
| `DiscoveryResult.filter` | `predicate=0.0` |
| `DiscoveryResult.to_zones` | `pad_x=20, pad_y=10, min_occurrence=0.1` |
| `DiscoveryResult.to_dataframe` | — |
| `entities.is_synthetic_patient_id` | `value` |

`DiscoveryResult.filter(predicate=)` takes either a minimum confidence
(a number, `0.0` by default) or a callable given each candidate, and
keeps what passes; both forms are frozen.

**Module-level names.** `entities.NO_PATIENT_ID_PREFIX`
(`"\\no-patient-id\\"`) and `entities.is_synthetic_patient_id()`: the
key a subject with no Patient ID is held under, and the test for it
(below).

`export(format=)` accepts `'dicom'` and `'wfdb'`, and the option names
are frozen with the method. The `dicom` options are
`use_compression=True, check_burned_in=False,
check_reversibility=True, patient_ids=None, show_progress=True,
subset=None, verify_readback=False`; the `wfdb` options are
`patient_ids` and `include_annotation_text`. An option name the format
does not recognise raises `TypeError`, and nothing is written. The two
formats do not accept the same options, so a caller forwarding one
options dict to both must split it per format.

`patient_ids` means the same thing on every method that takes it:
`export` (both formats), `get_cohort_report`, `export_dataframe` and
`lock_identities_batch`. `lock_identities` sends any argument that is
not a `str` to `lock_identities_batch`. Only `None` means every patient,
and neither lock method accepts `None`. An empty iterable selects
nobody, and an iterator is read once. A bare `str`, a bytes-like value,
a non-iterable, or an element that is not a `str` raises `TypeError`
before anything is written, with one exception: the two lock methods
also take a `PhiReport`, and `PhiFinding` elements mixed with IDs, and
lock the patients those findings name. The report `audit()` returns
locks every patient with at least one finding, by the Patient ID the
finding holds. After `anonymize()` has replaced a patient's ID, the
report no longer names that patient; lock before `anonymize()`. A
report with no findings locks nobody. A patient is selected by its `patient_id`
exactly: after `anonymize()` that is its replacement Patient ID, and a
subject whose files carried no Patient ID is selected by the key
`get_cohort_report` shows in its `PatientID` column, not by `""`. An ID
that no patient in the session holds selects nothing and is counted,
never named. `export`, `get_cohort_report` and `export_dataframe` log
one `WARNING` line for it. `export` also writes one `WARNING` audit row,
which grades the report `REVIEW_REQUIRED`. The lock methods log the
count as an error.

`export(subset=)` (the `dicom` format) takes a pandas query string, a
DataFrame, or any other iterable of UIDs, and reads that iterable the
way `patient_ids` is read: only `None` means no filter, an empty one
selects nothing, and a bytes-like value, a non-iterable, or an element
that is not a `str` raises `TypeError` before anything is written. A
DataFrame is read by the first of `SOPInstanceUID`,
`SeriesInstanceUID`, `StudyInstanceUID` and `PatientID` it carries,
and one with none of them raises `ValueError`. A UID matches at any of
the four levels. A Study, Series or SOP Instance UID taken before
`anonymize()` or `redact()` still names its entity, except a SOP
Instance UID that is none of these three: the one the first move of the
instance's UID (by `anonymize()`, `redact()` or
`Instance.regenerate_uid()`) left, which is the ingested one unless
`sop_instance_uid` was assigned before it; that UID's `anonymize()`
replacement; and the current one. A UID taken between a first redaction
and a `force=True` second, or between two `Instance.regenerate_uid()`
calls, is such a UID. A value that names nothing in the
session is counted, never named, in one `WARNING` line and one
`WARNING` audit row, which grades the report `REVIEW_REQUIRED`.

`generate_report(format=)` accepts `'markdown'` only, and
`generate_manifest(format=)` `'html'` and `'json'`; any other spelling
raises `ValueError`, a case variant included. On
`lock_identities` and `lock_identities_batch`, `persist` and `verbose`
reach every patient.

**`Session` attributes.** `store` (a `DicomStore` whose `.patients` is
the `List[Patient]` the quickstart indexes), `configuration` (an
`IsocenterConfiguration`), `persistence_file`. `audit()`, `anonymize()`
and `recover_patient_identity()` merge two patients that end up with the
same Patient ID into the one that was in the session first, removing the
other from `store.patients`; `audit()` runs inside
`export(check_burned_in=True)`, so that merges too.

**Shapes the frozen methods return** (attribute names; each is
rendered on [Results and errors](results.md), except `DiscoveryResult`,
on [OCR API](ocr.md)).
`IngestSummary(ingested, failures, declined, skipped)` plus `failed`;
`ExportSummary(written_uids, failures)` plus `written`, `failed`;
`written_uids` holds the SOP Instance UID of each written instance and
nothing else: an instance with no UID is not written and is in
`failures`;
`PhiReport(findings, failures)` with `__len__`, `__iter__`, `__getitem__`,
`to_dataframe()`; `PhiFinding(entity_uid, entity_type, field_name,
value, reason, tag, patient_id, entity, remediation_proposal, metadata,
entity_path)`; `DiscoveryResult.filter(...)`, `.to_zones()`,
`.to_dataframe()`; `LockingResult` (a `list` of `Instance`);
`recover_patient_identity()` → `Dict[str, Dict[str, Any]]`, mapping the
SOP Instance UID of each instance that carries an identity token of ours
to a copy of the values that token holds, in graph order, from both
`restore=False` and `restore=True`;
`export(format='dicom')` → `ExportSummary`, and
`export(format='wfdb')` → `List[str]`, the paths written, empty
when nothing was attempted;
`get_cohort_report()` → `pandas.DataFrame`; `phi_status_summary()` →
`Dict[str, Counter]`; `redact()`, `reconcile_private_tags()`,
`auto_remediate_config()` → `int`.

`PhiFinding.entity`, on the findings `audit()` and
`scan_pixel_content()` return, is the live object in `session.store`
that the finding names — the same object whether the pass ran in
threads or in processes — or `None` when that object cannot be found;
it is never a worker's copy. The object is found by its UID.
`ingest()` does not admit a second instance with an SOP Instance UID the
graph already holds, so only a graph built or edited by hand can carry
one UID on more than one instance, and findings on such a UID all
resolve to a single one of those instances.

`anonymize(findings)` never writes to an object outside `session.store`
and does not modify the findings passed. A finding whose `entity` is
itself in the graph is acted on as it is; any other is resolved against
the live graph at its `entity_uid` and `entity_path` and acts on the
object found there, or declines when the address names no single
object. A removal of a value already gone from the object at the
finding's address is satisfied. A Patient ID is written, and a date
shifted, only with a value that belongs to the live patient holding it.

`PhiReport.failures` is a list of `(entity_uid, reason)`, one per
instance `scan_pixel_content()` could not read in full, and is always a
list; `audit()`'s is always empty, because a failure in its workers
raises. Each instance `scan_pixel_content()` or
`discover_redaction_zones()` could not read also writes one audit
warning naming it and the reason, before any raise, so a run with a
scan failure does not grade as passing.

**Entities, as reached from `session.store`.** The graph is `Patient`
→ `Study` → `Series` → `Instance`. Fields, in dataclass order (which is
the positional constructor order, except where marked `init=False`):
`Patient`: `patient_id, patient_name, studies`. `Study`:
`study_instance_uid, study_date, series, date_shifted, study_time`.
`Series`: `series_instance_uid, modality, series_number, equipment,
instances`. `Instance`: `attributes, sequences, attribute_vrs` (inherited
from `DicomItem`, `init=False`), then `sop_instance_uid, sop_class_uid,
instance_number, file_path, source_path` (`pixel_array` and
`waveform_array` follow and are tier 2). `Instance` carried a
`date_shifted` field until 0.9.6; it is gone, and reading it raises
`AttributeError`. `Equipment`: `manufacturer, model_name,
device_serial_number`.

A subject whose files carry no Patient ID (absent, empty or blank) is a
`Patient` per study, whose `patient_id` is
`entities.NO_PATIENT_ID_PREFIX + <its source Study Instance UID>`
(`"\\no-patient-id\\1.2.3..."`); `entities.is_synthetic_patient_id()`
is the test for it. No single-valued Patient ID read from a file
can take that form, because the backslash is DICOM's value delimiter. The
key is never exported: such a subject's exported Patient ID is empty
under `KEEP` and `REPLACE` alike, and its folder is
`Subject_UnknownPatient`.

`attributes` is keyed by lowercase `"gggg,eeee"` strings. On `Instance`:
`get_pixel_data()`, `set_pixel_data()`, `unload_pixel_data()`,
`discard_pixel_data()`, `get_waveform_data()`, and the
two-names-two-behaviours rule between `unload` and `discard` (`unload`
refuses an unsaved replacement; `discard` throws it away, with the
descriptors `set_pixel_data()` wrote for it — and a `set_attr()` edit
to any of those descriptors made since the set). On `DicomItem`:
`set_attr()`. On `Instance` it also keeps resident pixels reading as a
pixel-descriptor edit declares, and raises `ValueError` for an edit that
pixels set through `set_pixel_data()` and not yet saved cannot be read
under. `get_pixel_data()` reads an instance's samples under its pixel
descriptors — Rows, Columns, SamplesPerPixel, NumberOfFrames,
BitsAllocated and PixelRepresentation — whether the samples are in the
store or in the file an `Instance(file_path=...)` names, and raises
`RuntimeError` for descriptors the samples cannot be read under.

**`Builder`.** The name, `Builder.start_patient()`, and `Equipment`'s
three fields. The rest of the fluent chain is tier 2.

**`IsocenterConfiguration`** as `session.configuration`: `save()`,
`add_rule()`, `update_rule()`, `delete_rule()`, `set_phi_tag()`,
`get_rule()`, and the fields `rules`, `phi_tags`, `date_jitter`,
`remove_private_tags`, `privacy_profile`, `config_path`, `auto_save`. On a session that has loaded
no configuration, `phi_tags` is a copy of the floor policy
(`basic@2026c` with the three research defaults, below; the name it is
held under is tier 2), and `audit()`/`anonymize()` apply it; a config
with no `privacy_profile` line, or a null one, extends it, and one with
`privacy_profile: none` opts out of it. `set_phi_tag()` stores lowercase
keys, stores its `value` argument as the rule's `value`, and raises
`ValueError`, leaving the policy and its file unchanged, for an unknown
action or a rule `load_config` would refuse. `add_rule()` and
`update_rule()` raise `ValueError`, leaving the rules and the file
unchanged, for a machine rule `load_config` would refuse. The four
methods that change the configuration write `config_path` only when
`auto_save` is true (default false). With auto-save on, a change whose
write fails is undone and the error raised. `save()` raises
`ValueError` when `config_path` is unset, raises the error of a failed
write, and writes the profile by name with only the `phi_tags` that
differ from it, never the profile's rules. A file `save()` writes loads
to the configuration it was written from. A built-in
profile's name is pinned to the PS3.15 edition its table was taken from,
and what a pinned name contains is frozen: `basic@2026c` holds the rules
1.0 shipped under it in every 1.x. A bare `basic` means `basic@2026c`,
and the floor policy is `basic@2026c` with the three research defaults,
in every 1.x. A later edition arrives in a minor release as a new name,
never as a new meaning for an existing one. The one exception: a 1.x may
correct a row of `basic@2026c` that the published 2026c standard shows
was transcribed wrongly, as a Breaking changelog entry quoting the
standard's row. `configuration.privacy_profile` holds the pinned name of
the built-in profile that was loaded, even when the file said `basic`.

**Exceptions.** Each is raised before any work is done unless it says
otherwise.

- `RedactionError(failures, attempted)`, a `RuntimeError` with
  `.failures` (a list of `(entity_uid, details)`) and `.attempted`,
  raised after the whole pass.
- `ExportError(failures, attempted, folder=None)`, a `RuntimeError`,
  raised last and only when zero of N reached disk, by both formats.
- `compact()`: `RuntimeError` while a pass is open (below).
- `redact()`: `RuntimeError` on a `:memory:` store when the environment
  asks for worker recycling.
- `audit()`, `anonymize()`, `redact()` and `export(check_burned_in=True)`:
  `RuntimeError` on a store holding dates shifted, or UIDs replaced,
  under a project secret it no longer has (on a store with no secret
  yet, each generates one and commits it); and, with `recover_patient_identity(restore=True)`,
  `RuntimeError` when patients sharing a Patient ID were de-identified
  under different date-offset schemes, which `audit()` raises before it
  creates a project secret.
- `scan_pixel_content()` and `discover_redaction_zones()`:
  `RuntimeError` when the `ocr` extra or the `tesseract` binary is
  unavailable to the calling process; and `RuntimeError` after the pass
  when at least one instance failed and none could be read. A scan that
  read some instances returns its report with the others in `failures`.
- `generate_report()` and `generate_manifest()`: `ValueError` for a
  `format` other than the spellings above, naming them; no file is
  written.
- `IsocenterConfiguration.save()`: `ValueError` with no `config_path`,
  or when `phi_tags` lacks a rule its `privacy_profile` (or the floor)
  supplies; the `OSError` of a failed write. With `auto_save` on,
  `add_rule()`, `update_rule()`, `delete_rule()` and `set_phi_tag()`
  raise the same, and leave the configuration as it was.
- `load_config(config_file)` and `audit(config_path=)`: `ValueError`
  when the file fails validation — its extension, YAML syntax or shape,
  a `version` this library does not read, a key the schema does not
  have, a value of the wrong type, an unknown `privacy_profile`
  (including an edition this version does not ship) or `action`, or a
  rule Isocenter cannot honour
  ([Configuration](../configuration.md) lists them) — and
  `FileNotFoundError` when it does not exist; after either, the
  configuration is exactly what it was. `audit()` without `config_path`
  raises the same `ValueError` for such a rule in
  `session.configuration.phi_tags`. Both are raised before a project
  secret is created.
- `recover_patient_identity()`: `FileNotFoundError` when no key file
  exists at the path `enable_reversible_anonymization()` was given,
  without creating one; `ValueError` when no patient holds the ID;
  `RuntimeError` when the patient has no instances or no identity token,
  the key does not decrypt it, or it holds no identity record this
  library writes, or the token is in the layout releases before 1.0
  wrote, in which case nothing on the patient is restored. It
  prints nothing, and no message names a Patient ID.
  It returns the identity rather than printing it.
- `enable_reversible_anonymization()`: `ValueError` for a malformed key
  file, creating none. The first `lock_identities()` creates the key,
  exclusively and with mode 0600, unless the session holds an identity
  token this library wrote that no key here opens, in which case it
  raises `RuntimeError` and creates none.
- `lock_identities()` refusals name no patient: a batch refusal numbers
  each refused patient by its place among the patients found, in Patient
  ID order. A patient any of whose instances holds no value in any tag
  `tags_to_lock` names is refused.
- `lock_identities(persist=True)` and `lock_identities_batch()` raise
  the `sqlite3.Error` of a store write that fails, and `RuntimeError`
  for a write that finds no store row for one of its instances. Both are
  raised **after the tokens are embedded in memory** (marked modified,
  so a later `save()` stores them), and after one audit error row.
  Neither stores any of that write's instances. Writes before it are not
  rolled back.

**Environment.** Every `ISOCENTER_*` name in
[Environment Variables](../environment.md), its default and its
documented semantics — including the order the three
threads-or-processes levers resolve in, which paths each reaches, and
that a value below a variable's floor is reported and replaced by the
default.

**Data promises.**

- For the same input, configuration and project secret, a 1.x release
  exports what the previous release exported, or its changelog says
  what changed.
- A store written by 1.0 opens under every 1.x. The sidecar and schema
  *layout* are not frozen; their forward compatibility is.
- A DICOM file exported with reversible anonymization by 1.0 is
  recoverable by every 1.x with its key. Frozen: the Encrypted
  Attributes Sequence `(0400,0500)`, whose item carries the identity
  token in Encrypted Content `(0400,0520)` (OB) and `1.2.840.10008.1.2`
  in Encrypted Content Transfer Syntax UID `(0400,0510)` (UI), as PS3.6
  lays the item out; and the key file's format (raw Fernet key bytes).
  The token is Isocenter's own Fernet token over a JSON record, not a
  CMS envelope, so only Isocenter reads it; the transfer syntax UID is
  a label. Releases before 1.0 wrote the two item elements the other
  way round (the token in `(0400,0510)`). 1.x does not read that
  layout: recovering such a file, or locking over it, raises an error
  naming the layout, and 0.9.x recovers it with its key.
- An identity token holds exactly the locked values captured from each
  instance that carries it: a lock writes one token per distinct set of
  values, and a restore gives each instance the values of the token it
  carries. Since 1.0 a token's encrypted content says so
  (`"__isocenter_token__": 2`, never returned as a tag), so this holds in
  any store it reaches. The exception is a token written before 1.0 that
  carries no such mark and no stamp from this store, is shared across
  studies, and holds a non-blank value outside group 0010. It is
  restored in full only on the first study carrying it, and as its
  group 0010 on the others. A file carries no stamp, so this also applies
  to a 0.9.8 export ingested elsewhere whose studies' locked values were
  equal. The promise runs forward only: a release before 1.0 reads the
  mark as one more value, so recovering a 1.0 token needs 1.0 or later,
  and a token naming a scheme this release does not know is refused.
- An exported DICOM file whose policy was applied in full carries
  Patient Identity Removed `(0012,0062)` `YES` and one De-identification
  Method `(0012,0063)` value naming the release and the policy, and,
  where its dates determine it, Longitudinal Temporal Information
  Modified `(0028,0303)`. `YES` reflects the attribute policy and no
  declared burned-in text: it is held back from a file whose Burned In
  Annotation `(0028,0301)` says `YES`, and the pixels themselves are not
  read. The fields and the condition are what is promised
  ([What an exported file says about itself](../configuration.md#what-an-exported-file-says-about-itself));
  the value's exact text moves with the release and the policy's
  fingerprint.
- Date jitter and the `ANON_` pseudonym are deterministic per patient
  within a store: the same patient under the same `date_jitter` range
  gets the same offset and pseudonym every time that store derives
  them, and in every copy of its file. So is a replacement UID: the same
  source UID gets the same replacement, and a redaction with the same
  zones the same SOP Instance UID. Pseudonyms, date offsets and
  replacement UIDs are derived from the store's project secret, which
  the store generates and never exports. The same configuration over a
  different store gives different ones.
  A patient a store classed as de-identified before 0.9.7 keeps that
  store's offset.
- A configuration file determines the policy, not the pseudonyms or
  offsets. The three things a de-identification depends on, and what is
  lost with each, are listed in
  [What to keep](../configuration.md#what-to-keep).
- The offset is not derivable from the exported pseudonym, or from any
  other value its derivation uses, without the secret. That is not a
  promise that no exported date is recoverable: a date tag no rule names
  is exported as ingested, and a UID the configuration keeps can embed
  one.
- A configuration file that 1.0 loads, every 1.x loads unchanged. Its
  schema is version 2, and a 1.x never raises the major. A 1.x raises
  the 2.x minor when it adds keys or values, and when it applies an
  unchanged file differently. A 1.x never changes how a file is
  applied without raising the minor. The minor is part of the policy
  each PHI status records, so a store scanned under an older minor is
  asked to re-audit. Its next `export()` writes a `WARNING` row saying
  the statuses were recorded under another policy, and the report
  grades `REVIEW_REQUIRED`. An `audit()` under the new release stops
  further rows; the row already written stays in the store's audit log.

**Output vocabularies.** Five separate vocabularies, not one list.

- The **grade**: `PASS`, `REVIEW_REQUIRED`. There is no `FAIL`. The
  conditions that decide it are listed in
  [How the grade is decided](../analytics.md#how-the-grade-is-decided),
  and they are a promise about the conditions: no condition is removed
  or narrowed in 1.x, and one may be added with a CHANGELOG entry. It
  is not a promise about which way a grade can move: a 1.x fix that
  stops writing a wrong row can move a run from REVIEW_REQUIRED to
  PASS, and the CHANGELOG entry for that fix says so. The report's
  layout and the wording of each Grade Basis line are tier 2.
- The **audit `action_type` strings**, written to the audit table and
  counted by type in section 2 of the report: `DATA_LOSS`, `ERROR`,
  `EXPORT`, `RECONCILE_PRIVATE`, `REDACTION`, `REVERSIBLE_EXPORT`,
  `RISK`, `SCAN_GAP`, `WARNING`; and the four a remediation writes,
  `REMEDIATION_REPLACE`, `REMEDIATION_SHIFT_DATE` and
  `REMEDIATION_REMOVE` when it acts on a proposal and
  `REMEDIATION_DECLINED` when it declines to, or fails to.
- The **remediation-proposal `action_type` strings**, carried on
  `PhiFinding.remediation_proposal`: `REMOVE_TAG`, `REPLACE_TAG`,
  `SHIFT_DATE`. These say what a proposal *will* do and are never an
  audit row.
- The **report exception categories** `COMPLIANCE_CHECK` and
  `AUDIT_DROP`, synthesised into the report's `exceptions` list at
  report time and never written to the audit table. The second says
  audit rows failed to write and were dropped; either one costs the run
  its passing grade.
- The **`loss_scope` strings**: `STANDARD`, `PRIVATE`, `SIGNAL`. The
  third is acquired content that was in the source and is not in the
  export.

An existing string is never renamed or removed in 1.x; new strings may
be added with a CHANGELOG entry. The *method* that returns the rows
(`store_backend.get_audit_losses()`) is tier 2: the words are frozen,
the access path is not.

**Behaviours.** The call order the README documents and its
consequences (a report generated before any export carries a boundary
note; export-time `DATA_LOSS` rows are in a report generated after it);
`audit()` and `redact()` drain the persistence manager on entry;
until `export()`, the session writes only the store, `isocenter.log`
and files you ask for (a configuration, a key, a report or a cohort
table); source files are never
modified; `redact()` on a `:memory:` store runs in threads on every
interpreter; and the two below.

1. **`compact()` raises `RuntimeError` while a `redact()` or `ingest()`
   pass is open on the same store, from any thread of this session, and
   has done nothing when it does.** Frozen: the class, the timing
   (before its leading save), and "has done nothing". Not frozen: the
   message text and the lock file's name.
2. **`redact()` and `ingest()` block while a `compact()` is saving or
   rewriting, bounded, and then proceed.** Frozen: that they wait and
   then proceed, and that the wait is bounded and its expiry is a
   `RuntimeError` raised before any worker is dispatched or UID
   regenerated. Not frozen: the bound itself.

## Documented but internal

Listed here, rendered in the API reference or named by a guide where a
reader needs them, safe to call, and changeable in a 1.x release with a
CHANGELOG entry naming both spellings:

- **`DicomSession`**, the class's own name. `isocenter.Session` is the
  frozen spelling.
- **`session.store_backend` and `SqliteStore`** — the methods
  [Audit trail](persistence.md) renders: `get_audit_summary()`,
  `get_audit_errors()`, `get_audit_losses()`, `get_audit_declines()`,
  `get_audit_scan_gaps()`, `get_audit_drops()` and
  `get_flattened_instances()`. The store's *forward compatibility* is
  frozen; its API is not, and its other methods are private.
- **`session.key_manager`, `session.persistence_manager`,
  `session.reversibility_service`** — attributes that expose services.
- **`TrackedEntity` bookkeeping**: `has_unsaved_changes`, `phi_status`,
  `phi_status_policy`, `mark_modified()`, `mark_persisted()`,
  `mark_subtree_persisted()`, `record_phi_status()`; `PhiStatus`;
  `ScanPolicy`, the policy a status was recorded under, which the store
  keeps beside it; `DicomItem.add_sequence()` and
  `add_sequence_item()`, `record_attr_vr()`; `DicomSequence`;
  `Instance.regenerate_uid()`, `get_waveform_bytes()`,
  `unload_waveform_data()`, `pixel_array`, `waveform_array`;
  `Equipment.from_parts()`.
- **The recording helpers** the scan, remediation and the reversible
  lock call on an entity: `Instance.record_remediation()`,
  `remediation_vouches_for()`, `record_identity_token()`,
  `identity_token_is_this_stores()`; `DicomItem.record_date_shift()`,
  `date_shift_vouches_for()`, `clear_sequence_items()`; and `Study`'s
  own `record_date_shift()` and `date_shift_vouches_for()`. Every other
  public name on a class tier 1 names is listed on this page, in one
  tier or the other.
- **`DicomStore`'s methods** `get_unique_equipment()`,
  `get_ingested_paths()`, `get_superseded_uids()`, `save_state()` and
  `load_state()`. Its `patients` list is frozen (above); what else the
  store object offers is not.
- **`profiles.FLOOR_POLICY`**, the name the floor policy is held under.
  What the floor *contains* is frozen (above); the name is not.
- **`entities` helpers** `clone_sequences`, `exported_patient_id`,
  `iter_item_tree`, `normalize_study_date`, `resolve_item_path`, and the
  constant `SOURCE_SOP_UID_ATTR`, the `attributes` key under which an
  instance keeps the SOP Instance UID it held before its UID first
  changed (the ingested one, unless `sop_instance_uid` was assigned first).
  `exported_patient_id` is on the registry's provisional terms (below).
- **The [OCR API](ocr.md) page**: `DiscoveryCandidate`,
  `ZoneDiscoverer.group_boxes`,
  `RedactionVerifier` (`__init__`, `get_matching_rule`, `is_covered`,
  `verify_instance`), `ConfigAutomator.suggest_config_updates`,
  `pixel_analysis.analyze_pixels`, `pixel_analysis.detect_text_regions`,
  `pixel_analysis.TextRegion`, `pixel_analysis.HAS_OCR`,
  `pixel_analysis.OcrUnavailableError`, `pixel_analysis.PixelScanError`;
  `DiscoveryResult.get_density_matrix`, `visualize_heatmap`,
  `analyze_temporal_stability`, `inspect_clusters`, and its attributes
  `candidates` and `n_sources`.
- **`DicomExporter.write_tree()`** (the serializer alone) and the
  exporter registry `Exporter`, `register()`, `get_exporter()`,
  `available_formats()`, which is provisional (below).
- **`RedactionService.apply_redaction_to_array`** (static).
- **`PhiRemediation`**, the proposal a `PhiFinding` carries in
  `remediation_proposal`. Its `action_type` words are frozen (above);
  the class is not.
- **`Builder`'s fluent chain beyond `start_patient()`.**
- **`ComplianceReport`'s fields** and the report's section layout and
  wording; log messages and `print` lines; the manifest's HTML.
- **The JSON manifest's item keys** (`generate_manifest(format="json")`).
  An item's `anonymized` is `true` when the last tag-policy PHI scan
  left no identifier unremediated on that instance's patient, study or
  instance and none of the three has been edited since. It is not
  "`anonymize()` ran", and it says nothing about burned-in pixel text.
- **The `.pass.lock` / `.lock` file names**, the sidecar's `_pixels.bin`
  suffix, the audit table's columns, the schema's table names.

### The exporter registry: provisional until 1.1

`Exporter`, `register()`, `get_exporter()` and `available_formats()` are
**provisional**. A real plugin API is planned for 1.1, and 1.1 may
replace these four names rather than extend them; the change will be a
CHANGELOG entry naming both spellings, as for anything in this section.
A plugin written against 1.0 should pin `isocenter>=1.0,<1.1`.
`entities.exported_patient_id()`, which the
[exporter registry](exporters.md) page tells plugin authors to call, is
documented but internal on the same terms.

Every export gate lives inside the two built-in formats, so a
third-party exporter receives the graph with none of them applied. Each
of its runs writes one `WARNING` audit row, and **in 1.0 no third-party
export grades `PASS`**. [Exporter registry](exporters.md) lists what
does not run for it and the rules for its author; running the gates
above dispatch is [#783](https://github.com/kvnlng/Isocenter/issues/783).

There is no read-side seam: ingest reads through pydicom and the
library's own codec dispatch, and a reader or codec plugin point is 1.1
or later.

## Private

Every leading-underscore name, and wholesale: `parallel.py`
(`run_parallel` included — the environment registry is the contract,
the function is not), `io_handlers.py` except `DicomExporter.write_tree`,
the two summaries and `ExportError`, `privacy.py` except `PhiFinding`,
`PhiReport` and `PhiRemediation` (tier 2), `persistence.py` except the
`SqliteStore` methods named above, `remediation.py`, `services.py` except `RedactionError` and
`apply_redaction_to_array`, `crypto.py`, `reversibility.py`,
`sidecar.py`, `persistence_manager.py`, `pixel_geometry.py`,
`murmur.py`, `reporting.py` except `ComplianceReport`, `discovery.py`
except `DiscoveryResult`, `DiscoveryCandidate` and
`ZoneDiscoverer.group_boxes`,
`verification.py` except `RedactionVerifier`, `automation.py` except
`ConfigAutomator.suggest_config_updates`, `config_manager.py`,
`profiles.py` except `FLOOR_POLICY` (tier 2),
`builders.py` internals, `utils/`, `logger.py`, `_version.py`'s module
(the `__version__` string is frozen, its module is not).

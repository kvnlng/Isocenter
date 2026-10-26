# API stability

This page states what the 1.0 tag promises, in three tiers. The tag
freezes the facade, not the internal seams behind it.

- **Frozen (tier 1)** names keep their spelling, their parameter names,
  their return shapes and their documented behaviour for every 1.x
  release. A change to them is a 2.0.
- **Documented but internal (tier 2)** names are on this page. Where a
  reader needs them, the API reference renders them or a guide names
  them. They are safe to call. They may change in a 1.x release, with a
  CHANGELOG entry that names the old spelling and the new one. They
  exist so that a reader can see the seams, not so that a program can
  depend on them.
- **Private (tier 3)** names — everything with a leading underscore, and
  every module not listed below — may change without notice.

Optional extras (`ocr`, `nlp`) degrade to the documented fallback. The
fallback is frozen, and the extra's internals are not. Each method's
behaviour is on its own page: start at [Session](session.md). The
[changelog](https://github.com/kvnlng/Isocenter/blob/main/CHANGELOG.md)
says how a behaviour came to be.

## What 1.0 promises

- **The facade.** `Session`, its 28 methods and their parameters, the
  shapes they return, and the other callables that the frozen tables
  list keep their spelling and behaviour for every 1.x
  ([Frozen at 1.0](#frozen-at-10)).
- **The output.** For the same input, configuration and project secret,
  a 1.x exports what the previous release exported, or its changelog
  says what changed ([Data promises](#frozen-at-10)). The grade's
  values and the audit words keep their spelling. No 1.x removes or
  narrows a condition that grades a run. A 1.x may add words and
  conditions, with a changelog entry.
- **The configuration.** Every 1.x loads a file that 1.0 loads. A 1.x
  that applies an unchanged file differently raises the schema's minor
  version ([Data promises](#frozen-at-10)).
- **Plugins, provisionally.** The exporter registry is usable and
  provisional until 1.1. Version 1.1 may replace it rather than extend
  it. A plugin written against 1.0 should pin `isocenter>=1.0,<1.1`.
  In 1.0, no third-party export grades `PASS`
  ([The exporter registry](#the-exporter-registry-provisional-until-11)).

## Frozen at 1.0

**Package.** `isocenter.Session`, `isocenter.Builder`,
`isocenter.Equipment`, `isocenter.RedactionError`,
`isocenter.ExportError` — the five names in `__all__` — and
`isocenter.__version__`.

**`Session` construction and lifetime.** `Session(persistence_file=None)`.
`None` means `ISOCENTER_DB_PATH`, then `isocenter.db`. `Session()` also
accepts `":memory:"`. `with Session(...) as s:` (`__enter__` returns the
session, and `__exit__` closes it). `close()` is idempotent and releases
the executor and both threads. When a file named `isocenter.key` exists
in the current working directory at construction, `Session()` calls
`enable_reversible_anonymization()` with it. `Session()` resolves that
path to an absolute path at construction, so a later change of
directory does not move it. `Session()` does not look for a key beside
the store in another directory. With no such file, reversible
anonymization stays off and `Session()` creates no key. A malformed one
makes `Session()` raise its `ValueError`. So does anything at that path
that is not a regular file, such as a directory. The message names the
path. Before `Session()` raises, it releases the threads that the
session started. Keeping the key in the directory you run from, beside
the store and the exported data, is not advised. Whoever holds the key
and an export can read the identities that the export carries.

**`Session` methods — all 28 public names, with their parameters.**
The table omits `self`. `*` marks the keyword-only boundary, and a
parameter moved across it is a different call.

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
| `export_dataframe` | `output_path, expand_metadata=False, patient_ids=None` |
| `get_cohort_report` | `expand_metadata=False, patient_ids=None` |
| `phi_status_summary` | — |
| `generate_report` | `output_path, format='markdown'` |
| `generate_manifest` | `output_path, format='html'` |
| `save_analysis` | `report` |
| `compact` | — |
| `release_memory` | — |

**Other frozen callables, with their parameters.** This table spells
them as the Session table does, and qualifies each by the class (or
module) that defines it. The paragraphs below give the classes' fields
and the rules that each method follows.

| Callable | Parameters |
| --- | --- |
| `Session.__init__` | `persistence_file=None` |
| `IsocenterConfiguration.save` | — |
| `IsocenterConfiguration.add_rule` | `serial_number, manufacturer='Unknown', model_name='Unknown', redaction_zones=None` |
| `IsocenterConfiguration.update_rule` | `serial_number, updates` |
| `IsocenterConfiguration.delete_rule` | `serial_number` |
| `IsocenterConfiguration.set_phi_tag` | `tag, action, value=None, target=None` |
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
(a number, `0.0` by default) or a callable that is given each
candidate, and keeps what passes. Both forms are frozen.

**Module-level names.** `entities.NO_PATIENT_ID_PREFIX`
(`"\\no-patient-id\\"`) and `entities.is_synthetic_patient_id()`: the
key a subject with no Patient ID is held under, and the test for it
(below).

`export(format=)` accepts `'dicom'` and `'wfdb'`. The option names are
frozen with the method. The `dicom` options are
`use_compression=True, check_burned_in=False,
check_reversibility=True, patient_ids=None, show_progress=True,
subset=None, verify_readback=False`. The `wfdb` options are
`patient_ids` and `include_annotation_text`. An option name that the
format does not recognise raises `TypeError`, and `export()` writes
nothing. The two formats do not accept the same options. So a caller
that forwards one options dict to both must split it per format.

`patient_ids` means the same thing on every method that takes it:
`export` (both formats), `get_cohort_report`, `export_dataframe` and
`lock_identities_batch`. `lock_identities` sends any argument that is
not a `str` to `lock_identities_batch`. Only `None` means every patient,
and neither lock method accepts `None`. An empty iterable selects
nobody, and the method reads an iterator once. A bare `str`, a
bytes-like value, a non-iterable, or an element that is not a `str`
raises `TypeError` before anything is written, with one exception. The
two lock methods also take a `PhiReport`, and `PhiFinding` elements
mixed with IDs, and lock the patients those findings name.

The report that `audit()` returns locks every patient with at least one
finding, by the Patient ID the finding holds. After `anonymize()` has
replaced a patient's ID, the report no longer names that patient. Lock
before `anonymize()`. A report with no findings locks nobody.

A patient is selected by its `patient_id`, exactly. After `anonymize()`,
that is its replacement Patient ID. A subject whose files carried no
Patient ID is selected by the key that `get_cohort_report` shows in its
`PatientID` column, not by `""`.

An ID that no patient in the session holds selects nothing and is
counted, never named. `export`, `get_cohort_report` and
`export_dataframe` log one `WARNING` line for it. `export` also writes
one `WARNING` audit row, which grades the report `REVIEW_REQUIRED`. The
lock methods log the count as an error.

`export(subset=)` (the `dicom` format) takes a pandas query string, a
DataFrame, or any other iterable of UIDs. It reads that iterable as
`patient_ids` is read. Only `None` means no filter, and an empty
iterable selects nothing. A bytes-like value, a non-iterable, or an
element that is not a `str` raises `TypeError` before anything is
written. A DataFrame is read by the first of `SOPInstanceUID`,
`SeriesInstanceUID`, `StudyInstanceUID` and `PatientID` that it
carries. A DataFrame with none of them raises `ValueError`.

A UID matches at any of the four levels. A Study, Series or SOP
Instance UID taken before `anonymize()` or `redact()` still names its
entity, except a SOP Instance UID that is none of these three. The
first is the UID that the first move of the instance's UID (by
`anonymize()`, `redact()` or `Instance.regenerate_uid()`) left, which
is the ingested one unless `sop_instance_uid` was assigned before it.
The second is that UID's `anonymize()` replacement. The third is the
current one. A UID taken between a first redaction and a `force=True`
second, or between two `Instance.regenerate_uid()` calls, is such a
UID. A value that names nothing in the session is counted, never
named, in one `WARNING` line and one `WARNING` audit row, which grades
the report `REVIEW_REQUIRED`.

`generate_report(format=)` accepts `'markdown'` only, and
`generate_manifest(format=)` accepts `'html'` and `'json'`. Any other
spelling raises `ValueError`, a case variant included. On
`lock_identities` and `lock_identities_batch`, `persist` and `verbose`
reach every patient.

**`Session` attributes.** `store` (a `DicomStore` whose `.patients` is
the `List[Patient]` the quickstart indexes), `configuration` (an
`IsocenterConfiguration`), `persistence_file`. When two patients get the
same Patient ID, `audit()`, `anonymize()` and
`recover_patient_identity()` merge them into the one that was in the
session first. They remove the other from `store.patients`. `audit()`
runs inside `export(check_burned_in=True)`, so that call merges too.

**Shapes the frozen methods return.** These are attribute names.
[Results and errors](results.md) renders each one, except
`DiscoveryResult`, which is on [OCR API](ocr.md).

- `IngestSummary(ingested, failures, declined, skipped, hidden)` plus
  `failed`.
- `ExportSummary(written_uids, failures)` plus `written` and `failed`.
  `written_uids` holds the SOP Instance UID of each written instance and
  nothing else. An instance with no UID is not written and is in
  `failures`. So is an instance whose file another instance's write
  replaced, because the export volume treats their two file names as one
  ([Export output](../export-output.md#where-files-land)).
- `PhiReport(findings, failures)` with `__len__`, `__iter__`,
  `__getitem__` and `to_dataframe()`.
- `PhiFinding(entity_uid, entity_type, field_name, value, reason, tag,
  patient_id, entity, remediation_proposal, metadata, entity_path)`.
- `DiscoveryResult.filter(...)`, `.to_zones()` and `.to_dataframe()`.
- `LockingResult` (a `list` of `Instance`).
- `recover_patient_identity()` → `Dict[str, Dict[str, Any]]`. It maps
  the SOP Instance UID of each instance that carries an identity token
  of ours to a copy of the values that token holds, in graph order. Both
  `restore=False` and `restore=True` return it.
- `export(format='dicom')` → `ExportSummary`.
- `export(format='wfdb')` → `List[str]`, the paths written, empty
  when nothing was attempted.
- `get_cohort_report()` → `pandas.DataFrame`.
- `phi_status_summary()` → `Dict[str, Counter]`.
- `redact()`, `reconcile_private_tags()` and `auto_remediate_config()`
  → `int`.

On the findings that `audit()` and `scan_pixel_content()` return,
`PhiFinding.entity` is the live object in `session.store` that the
finding names. It is the same object whether the pass ran in threads or
in processes. When that object cannot be found, `PhiFinding.entity` is
`None`. `PhiFinding.entity` is never a worker's copy. The object is
found by its UID.

`ingest()` does not admit a second instance with an SOP Instance UID
that the graph already holds. So only a graph built or edited by hand
can carry one UID on more than one instance. Findings on such a UID all
resolve to a single one of those instances.

`anonymize(findings)` never writes to an object outside `session.store`,
and does not modify the findings passed to it. When a finding's
`entity` is itself in the graph, `anonymize()` acts on that finding as
it is. `anonymize()` resolves any other finding against the live graph
at its `entity_uid` and `entity_path`. That finding then acts on the
object found there, or declines when the address names no single
object. When the value is already gone from the object at the finding's
address, a removal is satisfied. `anonymize()` writes a Patient ID, and
shifts a date, only with a value that belongs to the live patient
holding it.

`PhiReport.failures` is always a list. It holds one
`(entity_uid, reason)` for each instance that `scan_pixel_content()`
could not read in full. The `failures` list of the report from
`audit()` is always empty, because a failure in its workers raises.
Each instance that `scan_pixel_content()` or
`discover_redaction_zones()` could not read also writes one audit
warning, before any raise. The warning names the instance and the
reason. So a run with a scan failure does not grade as passing.

**Entities, as reached from `session.store`.** The graph is `Patient`
→ `Study` → `Series` → `Instance`. The fields follow, in dataclass
order. That is the positional constructor order, except where a field
is marked `init=False`.
`Patient`: `patient_id, patient_name, studies`. `Study`:
`study_instance_uid, study_date, series, date_shifted, study_time`.
`Series`: `series_instance_uid, modality, series_number, equipment,
instances`. `Instance`: `attributes, sequences, attribute_vrs` (inherited
from `DicomItem`, `init=False`), then `sop_instance_uid, sop_class_uid,
instance_number, file_path, source_path` (`pixel_array` and
`waveform_array` follow and are tier 2). `Instance` carried a
`date_shifted` field until 0.9.6. The field is gone, and reading it
raises `AttributeError`. `Equipment`: `manufacturer, model_name,
device_serial_number`.

Assigning `Instance.sop_instance_uid` a value it does not already hold
also sets `attributes["0008,0018"]` to it and marks the instance edited,
once. So the export writes that instance under the assigned UID: in the
file's name, in `(0008,0018)` and in Media Storage SOP Instance UID.
Some UIDs cannot name a file, and `''` is one of them. At export, the
file-name rule refuses an instance assigned such a UID, as it refuses
any such instance, and the export does not write it. An instance with
the UID `''` is in `ExportSummary.failures`, keyed `UNKNOWN`, with one
`ERROR` audit row. Assigning the value that the field already holds
changes nothing. So that assignment does not restore an element
written straight into `attributes` (#936).

`Series.series_number` and `Series.modality` are the source's Series
Number and Modality as ingested. `Instance.instance_number` is the
file's Instance Number. An export does not write any of the three. It
writes each instance's own `(0020,0011)`, `(0008,0060)` and
`(0020,0013)` after the configuration's rules. The folder and WFDB
record names read the same elements (#869). Assigning
`Series.series_number` changes no exported file and does not mark the
series' instances edited.

A subject whose files carry no Patient ID (absent, empty or blank) is a
`Patient` per study. Its `patient_id` is
`entities.NO_PATIENT_ID_PREFIX + <its source Study Instance UID>`
(`"\\no-patient-id\\1.2.3..."`). `entities.is_synthetic_patient_id()`
is the test for this key. No single-valued Patient ID read from a file
can take that form, because the backslash is DICOM's value delimiter.
The export never writes the key. Such a subject's exported Patient ID
is empty under `KEEP` and `REPLACE` alike, and its folder is
`Subject_UnknownPatient`.

`attributes` is keyed by lowercase `"gggg,eeee"` strings. On `Instance`:
`get_pixel_data()`, `set_pixel_data()`, `unload_pixel_data()`,
`discard_pixel_data()`, `get_waveform_data()`, and the
two-names-two-behaviours rule between `unload` and `discard`. `unload`
refuses an unsaved replacement. `discard` throws it away, with the
descriptors `set_pixel_data()` wrote for it — and a `set_attr()` edit
to any of those descriptors made since the set.

On `DicomItem`: `set_attr()`. It stores the value it is given, with
one exception. It stores a numpy bool, integer or float (a scalar or a
0-d array) as the Python number it equals (`np.int64(7)` as the `int`
7). It stores a `list` or `tuple` that directly holds a numpy value as
a new `list`, with each numpy number in it replaced in the same way. A
converted value is not the same object as the value set. A new `list`
made from a `tuple` does not equal the `tuple`. `set_attr()` raises
nothing for a value that the store cannot hold. The save raises instead
(Exceptions, below).

On `Instance`, `set_attr()` also keeps resident pixels reading as a
pixel-descriptor edit declares. It raises `ValueError` when pixels set
through `set_pixel_data()`, and not yet saved, cannot be read under the
edit.

`get_pixel_data()` reads an instance's samples under its pixel
descriptors: Rows, Columns, SamplesPerPixel, NumberOfFrames,
BitsAllocated and PixelRepresentation. It reads them this way
whether the samples are in the store or in the file an
`Instance(file_path=...)` names. It raises `RuntimeError` for
descriptors that the samples cannot be read under.

**`Builder`.** The name, `Builder.start_patient()`, and `Equipment`'s
three fields. The rest of the fluent chain is tier 2.

**`IsocenterConfiguration`** as `session.configuration`: `save()`,
`add_rule()`, `update_rule()`, `delete_rule()`, `set_phi_tag()`,
`get_rule()`, and the fields `rules`, `phi_tags`, `date_jitter`,
`remove_private_tags`, `privacy_profile`, `config_path`, `auto_save`.
On a session that has loaded no configuration, `phi_tags` is a copy of
the floor policy, and `audit()`/`anonymize()` apply it. The floor
policy is `basic@2026c` with the three research defaults (below). The
name the floor policy is held under is tier 2. A configuration with no
`privacy_profile` line, or a null one, extends the floor policy. One
with `privacy_profile: none` opts out of it.

`set_phi_tag()` stores lowercase keys, and keeps a private creator's
case. It stores its `value` argument as the rule's `value`. It stores no
`value` key when `value` is `None`, an empty `str`, or another value
that is false in Python, such as `0` or `False`. It stores its `target`
argument as the rule's `target`. Only `SCRUB` takes a
`target`, and `SCRUB` requires one. For an unknown action, or a rule
that `load_config` would refuse, `set_phi_tag()` raises `ValueError`
and leaves the policy and its file unchanged. For a machine rule that
`load_config` would refuse, `add_rule()` and `update_rule()` raise
`ValueError` and leave the rules and the file unchanged.

The four methods that change the configuration write `config_path` only
when `auto_save` is true (default false). With auto-save on, when the
write of a change fails, the method undoes the change and raises the
error. `save()` raises `ValueError` when `config_path` is unset, and
raises the error of a failed write. It writes the profile by name, with
only the `phi_tags` that differ from the profile, and never the
profile's rules. A file that `save()` writes loads to the configuration
it was written from.

A built-in profile's name is pinned to the PS3.15 edition that its
table was taken from. What a pinned name contains is frozen: in every
1.x, `basic@2026c` holds the rules that 1.0 shipped under it. In every
1.x, a bare `basic` means `basic@2026c`, and the floor policy is
`basic@2026c` with the three research defaults. A later edition arrives
in a minor release as a new name, never as a new meaning for an
existing one. There is one exception: a 1.x may correct a row of
`basic@2026c` that the published 2026c standard shows was transcribed
wrongly. It does so in a Breaking changelog entry that quotes the
standard's row. `configuration.privacy_profile` holds the pinned name
of the built-in profile that was loaded, even when the file said
`basic`.

**Exceptions.** Each exception is raised before any work is done,
unless its entry says otherwise.

- `RedactionError(failures, attempted)`, a `RuntimeError` with
  `.failures` (a list of `(entity_uid, details)`) and `.attempted`,
  raised after the whole pass.
- `ExportError(failures, attempted, folder=None)`, a `RuntimeError`.
  Both formats raise it last, and only when zero of N are reported
  written. (One file can be in the folder then. That is the case where
  N instances were written under names that the volume resolved to one
  file that could not be read back,
  [Export output](../export-output.md#where-files-land).)
- `compact()`: `RuntimeError` while a pass is open (below).
- `save(sync=True)`, `compact()` and `export()` (both formats begin with
  a save): `TypeError` when an instance with unsaved changes holds an
  attribute value that the store cannot hold. An attribute's value is
  `None`, a `str`, `bytes`, a `bool`, an `int`, a `float`, a pydicom DS
  or IS value, or a list of those. A numpy bool, integer or float is
  saved as the Python number it equals. The message names the
  instance, the tag (with its path inside a sequence) and the type,
  never the value. Nothing of the save is stored. No row reaches the
  database, no instance's revision moves, no file is exported, no audit
  row is written, and the instances stay unsaved. So the same call succeeds
  once the value is one of those types. A pixel frame of another unsaved
  instance may already have been appended to the sidecar. The next save
  reuses that frame, and `compact()` reclaims it otherwise. A private
  tag at the top level of an instance follows the same rule, narrower in
  one respect: a list under it holds no `bytes` and no list. `save()`
  without `sync=True` returns before the write, and logs the same
  message at ERROR. `lock_identities(persist=True)` raises the
  `TypeError` after the tokens are embedded in memory, as it raises the
  `sqlite3.Error` below.
- `save(sync=True)`, `compact()` and `export()`: `ValueError` when an
  instance with unsaved changes holds a SOP Instance UID that is not a
  `str` (`None`, or a value of another type). The message gives the
  count. The save raises it before it stores or appends anything, so
  nothing is saved or exported. Under `export(check_burned_in=True)`,
  the pre-export scan has already run. The same three calls raise
  `ValueError`, at the same point and with the same guarantees, when a
  patient, study or series in the session holds a key that is not a
  `str`: `patient.patient_id`, `study.study_instance_uid` or
  `series.series_instance_uid`. The message gives the count at each of
  the three levels and no value. Neither check refuses an empty `str`.
  `anonymize()` never clears one of those three keys. It declines a
  `REMOVE_TAG` finding that names one, with a `REMEDIATION_DECLINED`
  audit row, and `load_config()` refuses the rule (#949).
- `audit()`, `redact()`, `lock_identities()` and `lock_identities_batch()`,
  after a `save()` without `sync=True` whose write failed: each drains
  the queue. Then it runs that save again itself, on the calling thread,
  before it changes the graph or the store. `audit()` does this on entry.
  The other three first make the checks below. A call that ends at one
  of these checks does not drain and does not run the save:
  - `redact()`: a `burned_in_text` policy that `load_config` would
    refuse (`ValueError`), and no rules with no `burned_in_text` policy
    (it returns 0).
  - Both lock methods: reversible anonymization not enabled
    (`RuntimeError`), a key file that is empty or is not a Fernet key
    (`ValueError`), and no key file while the session holds an identity
    token this library wrote (`RuntimeError`).
  - `lock_identities()` given one Patient ID that no patient holds. It
    returns an empty `LockingResult`.
  - `lock_identities_batch()`, and `lock_identities()` given a list or a
    report: a selection it does not accept (`TypeError`), and a refusal
    of any patient (`RuntimeError`).

  `lock_identities()` given one Patient ID makes its refusals after the
  save. That save writes the session's patients as they stand then
  (`session.store.patients`, read at that moment), not as they stood
  when `save()` was called. A patient removed from the session since
  then, by an edit to the list or by assigning `session.store.patients` a new one, is removed from the
  store. A patient added since then is saved. If the save now succeeds,
  the call raises nothing and says nothing. If the save fails again, the
  call raises what the save raises: the `TypeError` or `ValueError`
  above, an `OSError`, or the sidecar lock's `RuntimeError`. The next
  such call runs the save again, until a save succeeds. A lock that
  raises this way embeds no token and creates no key file.
  The calls that begin with one of those four raise that error too, from
  that inner call and before they write anything:
  - `anonymize()` with no findings (it calls `audit()`)
  - `redact_by_machine()` (it calls `redact()`)
  - `export(check_burned_in=True)` (its pre-export scan is an `audit()`,
    before the export's own save)
  - `lock_identities()` given a list or a report (the batch form)

  `anonymize(report)`, handed its findings, makes no such call and
  returns. So does `recover_patient_identity()`. Both wait for the queue
  where they did and do not run a failed save.
  `save()` itself still returns before the write and raises nothing for
  it, and `close()` does not raise for it. `save(sync=True)`, `export()`
  and `compact()` save the session themselves, as before. `redact()`
  with no rules loaded and no `burned_in_text` policy returns 0 before
  it drains.
  A session can end by `close()`, or when the process exits with the
  session still referenced and never closed. If it ends either way with
  a failed background save that no later save has healed, it writes one
  `ERROR` audit row keyed `SESSION` (`A background save()
  failed and no later save succeeded before the session ended, ...`).
  So a later session's report on that store grades `REVIEW_REQUIRED`.
  `generate_report()` is not one of the calls above, and the session
  writes the row when it ends. So a report generated in the same
  session, with no `audit()` after the failed save, does not show the
  failure. One session used from two threads at once can have a retry
  on one thread fail while a save on the other succeeds. The failure
  then stays recorded, although nothing is unsaved (#941).
- `redact()`: `RuntimeError` on a `:memory:` store when the environment
  asks for worker recycling.
- `audit()`, `anonymize()`, `redact()` and `export(check_burned_in=True)`:
  `RuntimeError` on a store that holds dates shifted, or UIDs replaced,
  under a project secret it no longer has. On a store with no secret
  yet, each of these generates one and commits it. These four, and
  `recover_patient_identity(restore=True)`, also raise `RuntimeError`
  when patients that share a Patient ID were de-identified under
  different date-offset schemes. `audit()` raises that error before it
  creates a project secret.
- `scan_pixel_content()` and `discover_redaction_zones()`:
  `RuntimeError` when the `ocr` extra or the `tesseract` binary is
  unavailable to the calling process. They also raise `RuntimeError`
  after the pass when at least one instance failed and none could be
  read. A scan that read some instances returns its report with the
  others in `failures`.
- `redact()`, when the configuration holds a `burned_in_text` policy:
  `ValueError` when `load_config` would refuse that policy in a file.
  `redact()` raises it before its drain.
  `RuntimeError` when the `ocr` extra or the `tesseract` binary is
  unavailable to the calling process. Under `detector: db`, `redact()`
  also raises it when the `textdetect` extra (OpenCV) or the shipped
  model is unavailable (always, on the free-threaded build). `redact()`
  raises that `RuntimeError` after its drain and before anything is
  redacted, the configured zones included. It raises the same way when the policy's
  `target` names `names` and spaCy or its model cannot load (#1121).
  `export(check_burned_in=True)` under that policy raises too, before
  its pre-export scan and its save. An instance that the pass cannot
  read is a `WARNING` row, not a raise.
- `generate_report()` and `generate_manifest()`: `ValueError` for a
  `format` other than the spellings above. The message names those
  spellings, and no file is written.
- `IsocenterConfiguration.save()`: `ValueError` with no `config_path`,
  or when `phi_tags` lacks a rule that its `privacy_profile` (or the
  floor) supplies. `save()` raises the `OSError` of a failed write. With
  `auto_save` on, `add_rule()`, `update_rule()`, `delete_rule()` and
  `set_phi_tag()` raise the same, and leave the configuration as it was.
- `load_config(config_file)` and `audit(config_path=)`: `ValueError`
  when the file fails validation, and `FileNotFoundError` when the file
  does not exist. After either, the configuration is exactly what it
  was. The file fails validation on any of these:
  - its extension, YAML syntax or shape
  - a `version` this library does not read
  - a key the schema does not have
  - a value of the wrong type
  - an unknown `privacy_profile` (including an edition this version does
    not ship) or `action`
  - a rule Isocenter cannot honour ([Configuration](../configuration.md)
    lists them)

  `audit()` without `config_path` raises the same `ValueError` for such
  a rule in `session.configuration.phi_tags`. Both are raised before a
  project secret is created.
- `recover_patient_identity()`: `RuntimeError` when reversible
  anonymization is not enabled. `FileNotFoundError`, without creating a
  key file, when no key file exists at the path that
  `enable_reversible_anonymization()` was given. `ValueError` when that
  key file is empty or is not a Fernet key, and when no patient holds
  the ID. `RuntimeError` when the patient has no instances or no
  identity token, or when the key does not decrypt a token. It raises
  `RuntimeError` also when a token holds no identity record this library
  writes. The same applies to a token from a later release, and to a
  token in the layout that releases before 1.0 wrote. With
  `restore=True`, it raises `RuntimeError` when patients that would share
  a Patient ID after the restore were de-identified under different
  date-offset schemes. It raises this before it writes anything. After
  each of these errors, nothing on the patient is restored. It prints nothing,
  and no message names a Patient ID. It returns the identity rather than
  printing it.
- `enable_reversible_anonymization()`: `ValueError` for a malformed key
  file, or for a path that is not a regular file. In both cases, no key
  file is created, and the session stays open (#791). The first
  `lock_identities()` that writes a token creates the key, exclusively
  and with mode 0600. A lock that writes no token (a refusal, or no
  patient found, or none with an instance) creates no key file (#813).
  When no key file exists and the session holds an identity token this
  library wrote, which no new key could open,
  `enable_reversible_anonymization()` does not raise. A later
  `lock_identities()` or `lock_identities_batch()` that reaches its key
  check raises `RuntimeError` and creates no key file.
- `lock_identities()` and `lock_identities_batch()`: `RuntimeError`
  when reversible anonymization is not enabled.
- `lock_identities()` refusals name no patient. A batch refusal numbers
  each refused patient by its place among the patients found, in
  Patient ID order. A patient with instances is refused when any of its
  instances holds no value in any tag that `tags_to_lock` names. It is
  also refused when `tags_to_lock` is an empty list. For `0010,0010` and
  `0010,0020`, the lock reads the patient's value when an instance holds
  none. For `0010,0020`, that is the Patient ID as the export writes it.
  So under the default `tags_to_lock`, which names `0010,0020`, this
  refusal does not occur for a patient whose Patient ID is a `str`. A
  blank value is a value, with one exception. A patient that holds a
  Patient ID (or a Patient's Name) is refused when every instance's copy of `0010,0020`
  (or `0010,0010`) is present and blank. The token would restore the
  patient without it (#761).
- `lock_identities(persist=True)` and `lock_identities_batch()` raise
  the `sqlite3.Error` of a store write that fails, and `RuntimeError`
  for a write that finds no store row for one of its instances. Both are
  raised **after the tokens are embedded in memory** (marked modified,
  so a later `save()` stores them), and after one audit error row.
  Neither stores any of that write's instances. Writes before it are not
  rolled back.

**Environment.** Every `ISOCENTER_*` name in
[Environment Variables](../environment.md), its default and its
documented semantics. These include the order that the three
threads-or-processes levers resolve in, and which paths each lever
reaches. They also include the rule that a value below a variable's
floor is reported and replaced by the default.

**Data promises.**

- For the same input, configuration and project secret, a 1.x release
  exports what the previous release exported, or its changelog says
  what changed.
- A store written by 1.0 opens under every 1.x. The sidecar and schema
  *layout* are not frozen. Their forward compatibility is frozen.
- Every 1.x can recover a DICOM file that 1.0 exported with reversible
  anonymization, given the project's key file. Two things are frozen.
  The first is the Encrypted Attributes Sequence `(0400,0500)`. Its item
  carries
  the identity token in Encrypted Content `(0400,0520)` (OB) and
  `1.2.840.10008.1.2` in Encrypted Content Transfer Syntax UID
  `(0400,0510)` (UI), as PS3.6 lays the item out. The second is the key
  file's format (raw Fernet key bytes). The token is Isocenter's own
  Fernet token over a JSON record, not a CMS envelope, so only Isocenter
  reads it. The transfer syntax UID is a label. Releases before 1.0
  wrote the two item elements the other way round (the token in
  `(0400,0510)`). 1.x does not read that layout. Recovering such a file,
  or locking over it, raises an error that names the layout. 0.9.x
  recovers such a file with its key.
- An identity token holds exactly the locked values captured from each
  instance that carries it. A lock writes one token per distinct set of
  values. A restore gives each instance the values of the token it
  carries. Since 1.0, a token's encrypted content says so
  (`"__isocenter_token__": 2`, never returned as a tag). So this
  promise holds in any store that the token reaches. The exception is a
  token written before 1.0 that has all three of these properties:
  - It carries no such mark and no stamp from this store.
  - It is shared across studies.
  - It holds a non-blank value outside group 0010.

  Such a token is restored in full only on the first study that carries
  it. On the other studies, it is restored as its group 0010. A file
  carries no stamp, so this also applies to a 0.9.8 export ingested
  elsewhere whose studies' locked values were equal. The promise runs
  forward only. A release before 1.0 reads the mark as one more value,
  so recovering a 1.0 token needs 1.0 or later. A token that names a
  scheme this release does not know is refused.
- An exported DICOM file whose policy was applied in full carries these
  elements:
  - Patient Identity Removed `(0012,0062)` `YES`
  - one De-identification Method `(0012,0063)` value that names the
    release and the policy
  - where the file's dates determine it, Longitudinal Temporal
    Information Modified `(0028,0303)`

  `YES` reflects the attribute policy and no declared burned-in text.
  The export holds it back from a file whose Burned In Annotation
  `(0028,0301)` says `YES`. The pixels themselves are not read. The
  fields and the condition are the promise
  ([What an exported file says about itself](../configuration.md#what-an-exported-file-says-about-itself)).
  The value's exact text moves with the release and the policy's
  fingerprint.
- The date shift and the `ANON_` pseudonym are deterministic per patient
  within a store. The same patient under the same `date_jitter` range
  gets the same offset and pseudonym every time that store derives
  them, and in every copy of its file. A replacement UID is
  deterministic too. The same source UID gets the same replacement, and
  a redaction with the same zones gets the same SOP Instance UID.
  Isocenter derives pseudonyms, date offsets and replacement UIDs from
  the store's project secret. The store generates that secret and never
  exports it. The same configuration over a different store gives
  different ones. A patient that a store classed as de-identified before
  0.9.7 keeps that store's offset.
- A configuration file determines the policy, not the pseudonyms or
  offsets. [What to keep](../configuration.md#what-to-keep) lists the
  three things a de-identification depends on, and what is lost with
  each.
- The offset is not derivable from the exported pseudonym, or from any
  other value its derivation uses, without the secret. That is not a
  promise that no exported date is recoverable. A date tag that no rule
  names is exported as ingested, and a UID that the configuration keeps
  can embed a date.
- Every 1.x loads, unchanged, a configuration file that 1.0 loads. The
  promise runs forward. Every later 1.x loads a file that a 1.x loads.
  But an earlier 1.x refuses a file that declares a newer minor than its
  own, and refuses to save over one. The configuration schema is
  version 2, and a 1.x never raises the major. A 1.x raises the 2.x
  minor when it adds keys or values, and when it applies an unchanged
  file differently. A 1.x never changes how a file is applied without
  raising the minor.
- The schema's minor is part of the policy that each PHI status
  records. So a
  store scanned under an older minor is asked to re-audit. The store's
  next `export()` writes a `WARNING` row that says the statuses were
  recorded under another policy, and the report grades
  `REVIEW_REQUIRED`. An `audit()` under the new release stops further
  rows. The row already written stays in the store's audit log.

**Output vocabularies.** There are five separate vocabularies, not one
list.

- The **grade**: `PASS`, `REVIEW_REQUIRED`. There is no `FAIL`.
  [How the grade is decided](../analytics.md#how-the-grade-is-decided)
  lists the conditions that decide it. The list is a promise about the
  conditions. No 1.x removes or narrows a condition, and a 1.x may add
  one with a CHANGELOG entry. The list is not a promise about which way
  a grade can move. A 1.x fix that stops writing a wrong row can move a
  run from REVIEW_REQUIRED to PASS, and the CHANGELOG entry for that fix
  says so. The report's layout and the wording of each Grade Basis line
  are tier 2.
- The **audit `action_type` strings**. Isocenter writes them to the
  audit table, and section 2 of the report counts them by type. They are
  `DATA_LOSS`, `ERROR`, `EXPORT`, `RECONCILE_PRIVATE`, `REDACTION`,
  `REVERSIBLE_EXPORT`, `RISK`, `SCAN_GAP`, `WARNING`, and the four that a
  remediation writes. A remediation writes `REMEDIATION_REPLACE`,
  `REMEDIATION_SHIFT_DATE` and `REMEDIATION_REMOVE` when it acts on a
  proposal. It writes `REMEDIATION_DECLINED` when it declines to act on
  a proposal, or fails to.
- The **remediation-proposal `action_type` strings**, carried on
  `PhiFinding.remediation_proposal`: `REMOVE_TAG`, `REPLACE_TAG`,
  `SHIFT_DATE`. These say what a proposal *will* do and are never an
  audit row.
- The **report exception categories** `COMPLIANCE_CHECK` and
  `AUDIT_DROP`. The report synthesises them into its `exceptions` list
  at report time, and never writes them to the audit table. The second
  says that audit rows failed to write and were dropped. Either one
  costs the run its passing grade.
- The **`loss_scope` strings**: `STANDARD`, `PRIVATE`, `SIGNAL`. The
  third is acquired content that was in the source and is not in the
  export.

No 1.x renames or removes an existing string. A 1.x may add new strings
with a CHANGELOG entry. The *method* that returns the rows
(`store_backend.get_audit_losses()`) is tier 2. The words are frozen,
and the access path is not.

**Behaviours.** These are frozen:

- The call order that the README documents, and its consequences. A
  report generated before any export carries a boundary note, and
  export-time `DATA_LOSS` rows are in a report generated after the
  export.
- `audit()` drains the persistence manager on entry. `redact()` drains it
  after it reads its `burned_in_text` policy, and before it changes the
  graph or the store. With no rules and no `burned_in_text` policy,
  `redact()` returns 0 and does not drain.
- Until `export()`, the session writes only the store, `isocenter.log`
  and files you ask for (a configuration, a key, a report, a manifest or
  a cohort table).
- Isocenter never modifies source files.
- `redact()` on a `:memory:` store runs in threads on every interpreter.
- The two numbered behaviours below.

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

This page lists the names below. Where a reader needs them, the API
reference renders them or a guide names them. They are safe to call. A
1.x release can change them, with a CHANGELOG entry that names both
spellings.

- **`DicomSession`**, the class's own name. `isocenter.Session` is the
  frozen spelling.
- **`session.store_backend` and `SqliteStore`** — the methods
  [Audit trail](persistence.md) renders: `get_audit_summary()`,
  `get_audit_errors()`, `get_audit_losses()`, `get_audit_declines()`,
  `get_audit_scan_gaps()`, `get_audit_drops()` and
  `get_flattened_instances()`. The store's *forward compatibility* is
  frozen. Its API is not frozen, and its other methods are private.
- **`session.key_manager`, `session.persistence_manager`,
  `session.reversibility_service`** — attributes that expose services.
- **`TrackedEntity` bookkeeping**: `has_unsaved_changes`, `phi_status`,
  `phi_status_policy`, `mark_modified()`, `mark_persisted()`,
  `mark_subtree_persisted()` and `record_phi_status()`. `PhiStatus` is
  tier 2 too. So is `ScanPolicy`, the policy a status was recorded
  under, which the store keeps beside it. So are
  `DicomItem.add_sequence()`, `add_sequence_item()` and
  `record_attr_vr()`, and `DicomSequence`. So are
  `Instance.regenerate_uid()`, `get_waveform_bytes()`,
  `unload_waveform_data()`, `pixel_array` and `waveform_array`, and
  `Equipment.from_parts()`.
- **The recording helpers** that the scan, remediation and the
  reversible lock call on an entity. They are
  `Instance.record_remediation()`, `remediation_vouches_for()`,
  `record_identity_token()` and `identity_token_is_this_stores()`. They
  are also `DicomItem.record_date_shift()`, `date_shift_vouches_for()`
  and `clear_sequence_items()`, and `Study`'s own `record_date_shift()`
  and `date_shift_vouches_for()`. Every other public name on a class
  that tier 1 names is listed on this page, in one tier or the other.
- **`DicomStore`'s methods** `get_unique_equipment()`,
  `get_ingested_paths()`, `get_superseded_uids()`, `save_state()` and
  `load_state()`. Its `patients` list is frozen (above). Nothing else
  that the store object offers is frozen.
- **`IsocenterConfiguration.burned_in_text`**, the `burned_in_text`
  policy a configuration file holds (`{"action": ..., "scope": ...,
  "min_confidence": ...}`, plus `"modalities"` under `scope: modalities`,
  or `None`), as `load_config()` sets it. The
  [Configuration](../configuration.md#burned-in-text) page documents the
  file key and what each action does. The attribute's name and shape
  are not frozen.
- **`profiles.FLOOR_POLICY`**, the name the floor policy is held under.
  What the floor *contains* is frozen (above). The name is not.
- **`entities` helpers** `clone_sequences`, `exported_patient_id`,
  `iter_item_tree`, `normalize_study_date`, `resolve_item_path`, and the
  constant `SOURCE_SOP_UID_ATTR`. That constant is the `attributes` key
  under which an instance keeps the SOP Instance UID it held before its
  UID first changed (the ingested one, unless `sop_instance_uid` was
  assigned first). `exported_patient_id` is on the registry's
  provisional terms (below).
- **The [OCR API](ocr.md) page**: `DiscoveryCandidate`,
  `ZoneDiscoverer.group_boxes`,
  `RedactionVerifier` (`__init__`, `get_matching_rule`, `is_covered`,
  `verify_instance`), `ConfigAutomator.suggest_config_updates`,
  `pixel_analysis.analyze_pixels`, `pixel_analysis.detect_text_regions`,
  `pixel_analysis.TextRegion`, `pixel_analysis.HAS_OCR`,
  `pixel_analysis.OcrUnavailableError`, `pixel_analysis.NlpUnavailableError`
  (an `OcrUnavailableError`, #1121), `pixel_analysis.PixelScanError` and
  `text_detection.TextDetectorUnavailableError` (an
  `OcrUnavailableError`, #1121). Also `DiscoveryResult.get_density_matrix`,
  `visualize_heatmap`, `analyze_temporal_stability` and
  `inspect_clusters`, and its attributes `candidates` and `n_sources`.
- **`DicomExporter.write_tree()`** (the serializer alone) and the
  exporter registry `Exporter`, `register()`, `get_exporter()`,
  `available_formats()`, which is provisional (below).
- **`RedactionService.apply_redaction_to_array`** (static).
- **`PhiRemediation`**, the proposal a `PhiFinding` carries in
  `remediation_proposal`. Its `action_type` words are frozen (above).
  The class is not.
- **`Builder`'s fluent chain beyond `start_patient()`.**
- **`ComplianceReport`'s fields**, the report's section layout and
  wording, log messages, `print` lines and the manifest's HTML.
- **The JSON manifest's item keys** (`generate_manifest(format="json")`).
  An item's `anonymized` is `true` when the last tag-policy PHI scan
  left no identifier unremediated on that instance's patient, study or
  instance and none of the three has been edited since. It is not
  "`anonymize()` ran", and it says nothing about burned-in pixel text.
- **The `.pass.lock` / `.lock` file names**, the sidecar's `_pixels.bin`
  suffix, the audit table's columns, the schema's table names.

### The exporter registry: provisional until 1.1

`Exporter`, `register()`, `get_exporter()` and `available_formats()` are
**provisional**. A real plugin API is planned for 1.1. Version 1.1 may
replace these four names rather than extend them. As for anything in
this section, the change will be a CHANGELOG entry that names both
spellings. A plugin written against 1.0 should pin
`isocenter>=1.0,<1.1`. `entities.exported_patient_id()` is documented
but internal on the same terms. The [exporter registry](exporters.md)
page tells plugin authors to call it.

Every export gate lives inside the two built-in formats, so a
third-party exporter receives the graph with none of them applied. Each
run of such an exporter writes one `WARNING` audit row, and **in 1.0 no
third-party export grades `PASS`**. The [Exporter registry](exporters.md)
page lists what does not run for such an exporter, and the rules for
its author. Running the gates above dispatch is
[#783](https://github.com/kvnlng/Isocenter/issues/783).

There is no read-side seam. Ingest reads through pydicom and the
library's own codec dispatch. A reader or codec plugin point is 1.1 or
later.

## Private

Every leading-underscore name is private, and so are these, wholesale:

- `parallel.py`, `run_parallel` included. The environment registry is
  the contract, and the function is not.
- `io_handlers.py` except `DicomExporter.write_tree`, the two summaries
  and `ExportError`
- `privacy.py` except `PhiFinding`, `PhiReport` and `PhiRemediation`
  (tier 2)
- `persistence.py` except the `SqliteStore` methods named above
- `remediation.py`
- `services.py` except `RedactionError` and `apply_redaction_to_array`
- `crypto.py`, `reversibility.py`, `sidecar.py`,
  `persistence_manager.py`, `pixel_geometry.py` and `murmur.py`
- `reporting.py` except `ComplianceReport`
- `discovery.py` except `DiscoveryResult`, `DiscoveryCandidate` and
  `ZoneDiscoverer.group_boxes`
- `verification.py` except `RedactionVerifier`
- `automation.py` except `ConfigAutomator.suggest_config_updates`
- `config_manager.py`
- `profiles.py` except `FLOOR_POLICY` (tier 2)
- `builders.py` internals
- `utils/` and `logger.py`
- `_version.py`'s module. The `__version__` string is frozen, and its
  module is not.

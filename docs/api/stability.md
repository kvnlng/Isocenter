# API stability

What the 1.0 tag promises, in three tiers. Decided for #379 against the
owner's ruling on #26 — *the facade is what gets frozen, and the internal
seams behind it are not* — and pinned by `tests/test_frozen_surface.py`:
the set of public `Session` methods equals the frozen list in both
directions, every parameter name matches, the frozen shapes' fields
match, and `api/session.md` renders every frozen method. The design
record is `docs/superpowers/specs/2026-09-08-frozen-surface-and-strategy-bunch-3.md` §5.

**Frozen (tier 1)** names keep their spelling, their parameter names,
their return shapes and their documented behaviour for every 1.x
release; a change is a 2.0. **Documented but internal (tier 2)** names
are rendered on this site and safe to call, and may change in a 1.x
release with a CHANGELOG entry that names the old spelling and the new
one; they exist so a reader can see the seams, not so a program can
lean on them. **Private (tier 3)** names — everything with a leading
underscore, and every module not listed below — may change without
notice. Optional extras (`ocr`, `nlp`) degrade to the documented
fallback; the fallback is frozen, the extra's internals are not. For
`ocr`, the fallback of `scan_pixel_content()` and
`discover_redaction_zones()` is a `RuntimeError` (see Exceptions), and
a scan that ran reports the instances it could not read in
`PhiReport.failures`.

## Frozen at 1.0

**Package.** `isocenter.Session`, `isocenter.Builder`,
`isocenter.Equipment`, `isocenter.RedactionError`,
`isocenter.ExportError` — the five names in `__all__` — and
`isocenter.__version__`.

**`Session` construction and lifetime.** `Session(persistence_file=None)`,
`None` meaning `ISOCENTER_DB_PATH` then `isocenter.db`; `":memory:"` is
accepted. `with Session(...) as s:` (`__enter__` returns the session,
`__exit__` closes); `close()` is idempotent and releases the executor
and both threads.

**`Session` methods — all 28 public names, with their parameters.**
These are the literal pins in `tests/test_frozen_surface.py`, row
for row (the test parses this table): `self` omitted, `*` marks the
keyword-only boundary, and a parameter moved across it is a different
call.

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

`export(format=)` accepts `'dicom'` and `'wfdb'`. The `dicom` options
are `use_compression=True, check_burned_in=False,
check_reversibility=True, patient_ids=None, show_progress=True,
subset=None, verify_readback=False`; the `wfdb` options are
`patient_ids` and `include_annotation_text`. Those option names are
frozen with the method: `tests/test_frozen_surface.py` pins the `dicom`
options through `_export_dicom`'s signature, and
`tests/test_wfdb_privacy.py` pins the two `wfdb` options -- both that
they are the only two the exporter reads, and that `patient_ids`
actually limits what is written.

**An option name neither format recognises raises `TypeError`, and
nothing is written.** The `dicom` path has always done this, because
`_export_dicom` has a real signature; the `wfdb` path did not until
0.9.5, and a mistyped `patient_ids` therefore exported every patient in
silence (#410). Because the two formats do not accept the same options,
a caller forwarding one options dict to both must split it per format.
`tests/test_wfdb_option_strictness.py` pins the refusal, the acceptance
of the two frozen names, that both formats refuse the same typo, and the
allow-list constant itself -- the last separately, because the AST pin in
`tests/test_wfdb_privacy.py` collects the keys the body *reads* and is
blind to a name admitted and never used.

`generate_report(format=)` accepts `'markdown'` only and raises
`ValueError` otherwise.

`lock_identities` took `_patient_obj=None, **kwargs` until 0.9.4; both
were stripped before the tag rather than frozen, and `verbose` and
`tags_to_lock` are keyword-only so a caller still filling the old third
positional slot gets a `TypeError` rather than a `Patient` read as a
flag (#379, Q7). `persist` and `verbose` reach every patient on the
batch path -- `lock_identities(report, persist=True)` writes the rows,
which until 0.9.4 it silently did not -- and are the batch method's own
keyword-only parameters with the same defaults (#379, Q10).

**`Session` attributes.** `store` (a `DicomStore` whose `.patients` is
the `List[Patient]` the quickstart indexes), `configuration` (an
`IsocenterConfiguration`), `persistence_file`.

**Shapes the frozen methods return** (attribute names).
`IngestSummary(ingested, failures, declined, skipped)` plus `failed`;
`ExportSummary(written_uids, failures)` plus `written`, `failed`;
`PhiReport(findings, failures)` with `__len__`, `__iter__`, `__getitem__`,
`to_dataframe()`; `PhiFinding(entity_uid, entity_type, field_name,
value, reason, tag, patient_id, entity, remediation_proposal, metadata,
entity_path)`; `DiscoveryResult.filter(...)`, `.to_zones()`,
`.to_dataframe()`; `LockingResult` (a `list` of `Instance`);
`get_cohort_report()` → `pandas.DataFrame`; `phi_status_summary()` →
`Dict[str, Counter]`; `redact()`, `reconcile_private_tags()`,
`auto_remediate_config()` → `int`.

`PhiFinding.entity`, on the findings `audit()` and
`scan_pixel_content()` return, is the live object in `session.store`
that the finding names -- the same object whether the pass ran in
threads or in processes -- or `None` when that object cannot be found
in the graph; it is never a worker's copy (#412). The object is found
by its UID. `ingest()` does not admit a second instance with an SOP
Instance UID the graph already holds (#431), so only a graph built or
edited by hand can carry one UID on more than one instance, and findings
on such a UID all resolve to a single one of those instances.

`PhiReport.failures` is a list of `(entity_uid, reason)`, one per
instance `scan_pixel_content()` could not read in full, and is always a
list; `audit()`'s is always empty, because a failure in its workers
raises (#423). Each instance `scan_pixel_content()` or
`discover_redaction_zones()` could not read also writes one `WARNING`
audit row naming it and the reason, before any raise, so a run with a
scan failure grades `REVIEW_REQUIRED` (#479).

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
`date_shifted` field until 0.9.6; it is gone (#510) — reading it raises
`AttributeError`. `Study.date_shifted` is unchanged.
`Equipment`: `manufacturer, model_name, device_serial_number`.
`attributes` is keyed by lowercase `"gggg,eeee"` strings; on `Instance`: `get_pixel_data()`,
`set_pixel_data()`, `unload_pixel_data()`, `discard_pixel_data()`,
`get_waveform_data()`, and the two-names-two-behaviours rule between
`unload` and `discard` (`unload` refuses an unsaved replacement;
`discard` throws it away, with the descriptors `set_pixel_data()`
wrote for it -- and a `set_attr()` edit to any of those descriptors
made since the set, which described the replacement (#434)). On
`DicomItem`: `set_attr()`.

**`Builder`.** The name, `Builder.start_patient()`, and `Equipment`'s
three fields. The rest of the fluent chain is tier 2.

**`IsocenterConfiguration`** as `session.configuration`: `save()`,
`add_rule()`, `update_rule()`, `delete_rule()`, `set_phi_tag()`,
`get_rule()`, and the fields `rules`, `phi_tags`, `date_jitter`,
`remove_private_tags`, `privacy_profile`. On a session that has loaded
no configuration, `phi_tags` is a copy of the floor policy,
`profiles.FLOOR_POLICY`, and `audit()`/`anonymize()` apply it; a config
with no `privacy_profile` line extends it, and one with
`privacy_profile: none` opts out of it (#495). `set_phi_tag()`
stores lowercase keys, as every other key in the policy is. What the
floor and `privacy_profile: basic` contain is **not** frozen: the basic
profile is PS3.15 Annex E Table E.1-1 of a named edition (2026c since
0.9.8, #547), its membership follows that edition, and a change to it
can arrive in a minor release, listed under Breaking in the changelog.

**Exceptions.** `RedactionError(failures, attempted)`, a `RuntimeError`,
with `.failures` (a list of `(entity_uid, details)`) and `.attempted`,
raised after the whole pass; `ExportError(failures, attempted,
folder=None)`, a `RuntimeError`, raised last and only when zero of N
reached disk. `compact()` raises `RuntimeError` while a pass is open
(below); `redact()` raises `RuntimeError` on a `:memory:` store when
the environment asks for worker recycling, after the persistence
drain and before any work is done (#400). `audit()`, `anonymize()` and
`export(check_burned_in=True)` (which runs `audit()` first) raise
`RuntimeError`, before any work, on a store holding dates shifted under
a project secret it no longer has; on a store with no secret yet, each
of the three generates one and commits it to the store (0.9.7). `scan_pixel_content()` and
`discover_redaction_zones()` raise `RuntimeError` when the `ocr` extra
or the `tesseract` binary is unavailable to the calling process, before
any worker is dispatched and before either method reads the graph
(#422). A worker process runs the `tesseract_cmd` the caller set, so it
uses the binary that check probed (#458). The check still covers only
the calling process's view of OCR: both methods also
raise `RuntimeError` after the pass when at least one instance failed
and none could be read (#423); a scan that read some instances returns
its report with the others in `failures`, and discovery counts only the
instances it read in `n_sources`. `ValueError` from
`generate_report` on an unknown format. `load_config(config_file)` and
`audit(config_path=)` raise `ValueError` when the file fails validation
(not `.yaml`/`.yml`, YAML syntax, a root that is not a mapping, an
unknown `privacy_profile`, an unknown `action`, a `phi_tags`,
`date_jitter` or `machines` of the wrong shape, a rule
`_validate_rule` rejects) and `FileNotFoundError` when it does not
exist; after either, the configuration is exactly what it was before
the call (#456).

**Environment.** Every `ISOCENTER_*` name in
[Environment Variables](../environment.md), its default and its
documented semantics — including the order the three
threads-or-processes levers resolve in, which paths each reaches, and
that a value below a variable's floor is reported and replaced by the
default.

**Data promises.** A store written by 1.0 opens under every 1.x (the
`user_version` migration chain); the sidecar and schema *layout* are
not frozen, their forward compatibility is. A DICOM file exported with
reversible anonymization by 1.0 is recoverable by every 1.x with its
key: the tags `(0400,0500)`, `(0400,0510)`, `(0400,0520)` and the key
file's format (raw Fernet key bytes). Date jitter stays deterministic
per patient within a project: the same keyed patient under the same
project secret and the same `date_jitter` range gets the same offset
in every store holding that secret. A patient a store classed as
de-identified before 0.9.7 keeps that store's unkeyed offset, which
another store holding the same secret would not give it; and raw data
for such a patient arriving in the same store is a keyed subject with
a different offset. The offset is not derivable from the exported
pseudonym, or from any other value its derivation uses, without the
secret; that is not a promise that no exported date is recoverable
(a date tag no rule names is exported as ingested, and UIDs can
embed dates, #544). The project secret's file format (`write_project_secret`) is
not a data promise.

**Output vocabularies.** These are five separate vocabularies, not one
list. The page conflated them until 0.9.5, and the category it gave was
wrong for four of the thirteen words: it sent a reader looking in the
audit table for strings that are never written there (#396).

- The **grade**: `PASS`, `REVIEW_REQUIRED`. There is no `FAIL`.
- The **audit `action_type` strings**, written to the audit table and
  counted by type in section 2 of the report: `DATA_LOSS`, `ERROR`,
  `EXPORT`, `RECONCILE_PRIVATE`, `REDACTION`, `REVERSIBLE_EXPORT`,
  `RISK`, `SCAN_GAP`, `WARNING`; and the four a remediation writes,
  `REMEDIATION_REPLACE`, `REMEDIATION_SHIFT_DATE` and
  `REMEDIATION_REMOVE` when it acts on a proposal and
  `REMEDIATION_DECLINED` when it declines to, leaving the value it
  targeted in the graph.
- The **remediation-proposal `action_type` strings**, carried on
  `PhiFinding.remediation_proposal`: `REMOVE_TAG`, `REPLACE_TAG`,
  `SHIFT_DATE`. These say what a proposal *will* do and are never an
  audit row; acting on one writes one of the `REMEDIATION_*` words
  above instead.
- The **report exception categories** `COMPLIANCE_CHECK` and
  `AUDIT_DROP`, synthesised into the report's `exceptions` list at
  report time and never written to the audit table. The second says
  audit rows failed to write and were dropped, so the report
  under-counts what was done; either one costs the run its PASS.
- The **`loss_scope` strings**: `STANDARD`, `PRIVATE`, `SIGNAL`.

An existing string is never renamed or removed in 1.x; new strings may
be added with a CHANGELOG entry. The *method* that returns the rows
(`store_backend.get_audit_losses()`) is tier 2: the words are frozen,
the access path is not.

`tests/test_frozen_surface.py` is what makes each of the five checkable:
it collects the words from the write sites themselves, by AST --
resolving a word passed through a variable or a module constant, as
remediation passes its four -- and compares each vocabulary for set
equality. Until 0.9.5 it grepped the
package for the word as a quoted literal, which a docstring or a SQL
string satisfied.

**Behaviours.** The call order the README documents and its
consequences (a report generated before any export carries a boundary
note; export-time `DATA_LOSS` rows are in a report generated after it);
`audit()` and `redact()` drain the persistence manager on entry;
nothing reaches disk before `export()`; source files are never
modified; `redact()` on a `:memory:` store runs in threads on every
interpreter, and refuses when worker recycling is asked for (#381,
#400); and the two behaviours below.

### Compaction and passes (#368)

1. **`compact()` raises `RuntimeError` while a `redact()` or `ingest()`
   pass is open on the same store, from any thread of this session, and
   has done nothing when it does** — no save, no rewrite, every blob row
   and the sidecar's inode as they were. Frozen: the class, the timing
   (before its leading save), and "has done nothing". Not frozen: the
   message text and the lock file's name.
2. **`redact()` and `ingest()` block while a `compact()` is saving or
   rewriting, bounded, and then proceed.** Frozen: that they wait and
   then proceed, and that the wait is bounded and its expiry is a
   `RuntimeError` raised before any worker is dispatched or UID
   regenerated. Not frozen: the bound itself (180 s today), which sits
   inside the timeout inequality #280 records and may move with it.

Both are pinned by `tests/test_compact_refuses_during_a_pass.py` on
both gate interpreters, and stated in the `compact()`, `redact()` and
`ingest()` docstrings.

## Documented but internal

Rendered by this site or named by a guide, safe to call, and changeable
in a 1.x release with a CHANGELOG entry naming both spellings:

- **`DicomSession`**, the class's own name. `isocenter.Session` is the
  frozen spelling; the class stays importable and unrenamed for 1.x by
  courtesy, but the freeze is on `Session`.
- **`session.store_backend` and `SqliteStore`** — everything
  [Persistence](persistence.md) renders: `__init__(db_path)`,
  `__getstate__`, `__setstate__`, and the public methods including
  `get_flattened_instances(patient_ids, instance_uids, page_size)` (the
  0.9.1 migration path from `export_to_parquet`; #142's "the surface
  #26 will freeze" is reversed here), `get_audit_losses()` and the other
  `get_audit_*`, `persist_pixel_data`, `save_all`, `compact_sidecar`,
  `stop`. The store's *forward compatibility* is frozen; its API is not.
  That includes the project-secret carry (0.9.7):
  `write_project_secret(path)` (`FileExistsError` rather than overwrite;
  mode `0600`) and `load_project_secret(path)` (`RuntimeError` on a store
  that already holds a secret, `ValueError` for a malformed file or a
  secret that minted none of the store's pseudonyms,
  `FileNotFoundError`; a store with shifted dates and no keyed pseudonym
  to verify against accepts the secret as unverified and writes a
  `WARNING` row at the load and at every later `audit()`, and a store
  that has ever done so loads every later secret as unverified; nothing
  clears it, so such a store's reports grade `REVIEW_REQUIRED` for
  good, by design); see the
  [Migration Guide](../migration.md#carrying-a-project-secret-between-stores).
- **`session.key_manager`, `session.persistence_manager`,
  `session.reversibility_service`** — attributes that expose services.
- **`TrackedEntity` bookkeeping**: `has_unsaved_changes`, `phi_status`,
  `mark_modified()`, `mark_persisted()`, `mark_subtree_persisted()`,
  `record_phi_status()`; `PhiStatus`; `DicomItem.add_sequence()` and
  `add_sequence_item()`,
  `record_attr_vr()`; `DicomSequence`; `Instance.regenerate_uid()`,
  `get_waveform_bytes()`, `unload_waveform_data()`, `pixel_array`,
  `waveform_array`; `Equipment.from_parts()`.
- **`entities` helpers** `clone_sequences`, `iter_item_tree`,
  `normalize_study_date`, `resolve_item_path`.
- **The [Intelligent OCR](ocr.md) page**: `ZoneDiscoverer.group_boxes`,
  `RedactionVerifier` (`__init__`, `get_matching_rule`, `is_covered`,
  `verify_instance`), `ConfigAutomator.suggest_config_updates`,
  `pixel_analysis.analyze_pixels`, `pixel_analysis.detect_text_regions`,
  `pixel_analysis.HAS_OCR`, `pixel_analysis.OcrUnavailableError`,
  `pixel_analysis.PixelScanError`;
  `DiscoveryResult.get_density_matrix`,
  `visualize_heatmap`, `analyze_temporal_stability`, `inspect_clusters`.
- **`DicomExporter.write_tree()`** (the serializer alone, used by the
  fixture generators) and the exporter registry `Exporter`,
  `register()`, `get_exporter()`, `available_formats()`.
- **`RedactionService.apply_redaction_to_array`** (static).
- **`Builder`'s fluent chain beyond `start_patient()`.**
- **`ComplianceReport`'s fields** and the report's section layout and
  wording; log messages and `print` lines; the manifest's HTML.
- **The JSON manifest's item keys** (`generate_manifest(format="json")`).
  Each item's `anonymized` is `true` when the last tag-policy PHI scan
  left no identifier unremediated on that instance's patient, study or
  instance, and none of the three has been edited since: each carries
  `REMEDIATED` or `CLEARED` at its current revision (#486). Three things
  it is not. It is not "`anonymize()` ran": an input the scan found clean
  reads `true` after `audit()` alone. And it says nothing about burned-in
  pixel text, which the tag scan does not read. And it does not see
  inside sequences: `anonymize()` stamps the nested item it changed,
  not the instance, so an instance whose only findings were inside a
  sequence reads `false` until the next `audit()` records the instance
  `CLEARED` (#494). `false` means the status does not establish it: a
  session that never scanned, an entity edited since its scan, an
  entity whose last pass declined a remediation on it, or that nested
  case.
- **The `.pass.lock` / `.lock` file names**, the sidecar's `_pixels.bin`
  suffix, the audit table's columns, the schema's table names.

## Private

Every leading-underscore name, and wholesale: `parallel.py`
(`run_parallel` included — the environment registry is the contract,
the function is not), `io_handlers.py` except `DicomExporter.write_tree`
and the two summaries, `privacy.py` except `PhiFinding` and `PhiReport`,
`remediation.py`, `services.py` except `RedactionError` and
`apply_redaction_to_array`, `crypto.py`, `reversibility.py`,
`sidecar.py`, `persistence_manager.py`, `pixel_geometry.py`,
`murmur.py`, `reporting.py` except `ComplianceReport`, `discovery.py`
except `DiscoveryResult` and `ZoneDiscoverer.group_boxes`,
`verification.py` except `RedactionVerifier`, `automation.py` except
`ConfigAutomator.suggest_config_updates`, `config_manager.py`,
`builders.py` internals, `utils/`, `logger.py`, `_version.py`'s module
(the `__version__` string is frozen, its module is not).

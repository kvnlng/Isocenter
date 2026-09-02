# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Isocenter is a Python library for indexing, de-identifying, and exporting DICOM datasets. It never modifies source files: it builds a SQLite metadata index plus a binary pixel/waveform sidecar, mutates an in-memory object graph, and writes clean copies to a new directory. There is no CLI and none is planned — #29 proposed one and was closed as not planned, so the Python API is the whole interface.

## Commands

```bash
pip install -e ".[dev]"          # contributor environment: tests + pylint (there is deliberately no requirements.txt)
pytest                           # full suite, ~600 tests
pytest tests/test_session.py     # one file
pytest tests/test_session.py::test_name -x   # one test
pylint isocenter                    # lint (target >8.5/10; NOT enforced by CI)
mkdocs serve                     # docs preview (needs the `docs` extra)
python -m tests.benchmarks.run_stress_test   # benchmark suite
python -m scripts.mutation_probe            # do the tests notice when behaviour changes?
```

Testing is tiered, and the tier decides the breadth, never the depth — every tier runs the whole suite:

- **Local**: run the tests for what you touched (see the mapping below), then the full suite before pushing.
- **PR gate** (`.github/workflows/tests.yml`): `pytest -v` on **3.12 and 3.14t** only. Those are the two promises the package makes and neither backs the other — 3.12 is the `python_requires` floor, and 3.14t is the free-threaded build where `run_parallel()` takes its threads-not-processes path. 3.13 and 3.14 are interpolation between them; the four-way matrix produced zero divergences in 30 runs that were not step timeouts. Not path-filtered: `main` requires these checks, and a required check that never runs blocks a PR forever rather than failing it.
- **Release** (`publish.yml`): all four versions. `test-floor` (3.12, 3.14t) blocks the upload; `test-supported` (3.13, 3.14) only reports — a red job turns the run red and the release still ships, which is the annotation. Shipping with one red means deleting that classifier from `setup.py` in the same release.

`tests.yml` is `workflow_call`-able with a `python-versions` input, which is how `publish.yml` reuses it. `tests/test_packaging_contract.py` asserts the gate still runs the floor and a free-threaded build, so narrowing the matrix cannot silently unback a classifier.

Which tests cover which module, for the local tier — `scripts/mutation_probe.py`'s `TARGETS` is the maintained version of this list:

| Module | Tests |
| --- | --- |
| `crypto.py` | `test_crypto.py`, `test_reversibility.py` |
| `privacy.py` | `test_analysis.py`, `test_analysis_persistence.py`, `test_audit_suppression.py`, `test_automation.py`, `test_config_tags_shapes.py`, `test_multiprocessing.py`, `test_mutation_gaps.py`, `test_ocr_formal.py`, `test_persistence.py`, `test_privacy.py`, `test_private_sequence_implicit_vr.py`, `test_profile_end_to_end.py`, `test_remediation.py`, `test_remediation_actions.py`, `test_remediation_invariants.py`, `test_scaffold_features.py`, `test_sr_anonymization.py` |
| `remediation.py` | `test_audit_suppression.py`, `test_deid_tags.py`, `test_mutation_gaps.py`, `test_persistence.py`, `test_private_sequence_implicit_vr.py`, `test_remediation.py`, `test_remediation_accounting.py`, `test_remediation_actions.py`, `test_remediation_dates.py`, `test_remediation_invariants.py`, `test_phi_retention.py`, `test_scaffold_features.py` |
| `io_handlers.py` | `test_api_coherence.py`, `test_audit_read_barrier.py`, `test_binary_retention_threshold.py`, `test_check_reversibility.py`, `test_codecs_strict.py`, `test_compress_handlers.py`, `test_compress_j2k_coverage.py`, `test_data_loss_reporting.py`, `test_export_atomic_write.py`, `test_export_contract.py`, `test_export_date_error.py`, `test_export_delivery_counters.py`, `test_export_error.py`, `test_export_failure_audit.py`, `test_export_loss_audit.py`, `test_export_merge_shape.py`, `test_export_pixels.py`, `test_export_readback.py`, `test_export_redaction_hash_warning.py`, `test_export_worker_graph_purity.py`, `test_float_pixel_data_export.py`, `test_ingest_failure_audit.py`, `test_io.py`, `test_legacy_waveform_hydration.py`, `test_logging.py`, `test_metadata_refactor_full.py`, `test_missing_study_date.py`, `test_multiprocessing.py`, `test_murmur_annotations.py`, `test_naming_structure.py`, `test_nested_phi_audit.py`, `test_pixel_geometry_pipeline.py`, `test_private_binary_ingest.py`, `test_private_tag_export.py`, `test_pydicom_deprecations.py`, `test_recursive_import.py`, `test_redaction_export.py`, `test_redaction_optimization.py`, `test_redaction_rgb.py`, `test_redaction_robustness.py`, `test_redaction_wildcard.py`, `test_remediation_accounting.py`, `test_reporting_features.py`, `test_reversibility.py`, `test_safe_export.py`, `test_services.py`, `test_session.py`, `test_shared_executor_lifecycle.py`, `test_sr_anonymization.py`, `test_structured_export.py`, `test_waveform_dicom_roundtrip.py`, `test_waveform_ingest.py`, `test_waveform_model.py`, `test_wfdb_conformance.py`, `test_wfdb_writer.py`, `test_worker_loss_is_reported.py` |

Three other workflows exist. `publish.yml` releases to PyPI by Trusted Publishing (OIDC, no token anywhere) when a GitHub Release is published; it refuses to publish if the tag disagrees with `setup.py`'s version, or if the built wheel does not carry its own `resources/*.json`. Its *filename and environment names are pinned by PyPI's publisher config* — renaming either breaks publishing until PyPI is updated to match. `docs.yml` runs `mkdocs gh-deploy` on pushes to `main` only, filtered to `docs/**`, `mkdocs.yml`, its own file, and `isocenter/**.py` (the API reference is generated from docstrings, so a code-only push still changes the site); it deploys straight to the live site, so the branch filter is load-bearing. `schema-drift.yml` checks Murmur's published annotation schema weekly.

`setup.py` is the single source of truth for dependencies; `tests/test_packaging_contract.py` fails if a module imported unguarded at module scope isn't in `install_requires`. Adding an import means adding a dependency there.

Tests write `*.db`, `*_pixels.bin`, `isocenter.log`, and a few config/CSV artifacts into the repo root. All are gitignored; leave them alone rather than adding cleanup.

Optional extras degrade gracefully and must keep doing so: `ocr` (pytesseract — `pixel_analysis.HAS_OCR`), `nlp` (spacy — `ZoneDiscoverer` falls back to regex; the `en_core_web_sm` model is deliberately not declared, because PyPI refuses direct-URL requirements), `docs`, `tests` (includes `setuptools`, which the build-based contract tests need and 3.12+ venvs no longer ship), and `dev` (`tests` plus pylint — contributor tooling that `pip install isocenter` must never pull in).

## Architecture

### The pipeline

`Session` (`isocenter/session.py`, aliased from `DicomSession`) is the only public entry point and the facade over everything else. The intended call order is ingest → examine → create_config/load_config → audit → (enable_reversible_anonymization + lock_identities) → anonymize → redact → verify → export → generate_report. The report comes *last* because export-time `DATA_LOSS` rows are written during `export()`; this file used to document the reverse order, in which those losses missed the report and the grade read PASS (#153). A report generated before any export carries a boundary note saying so. Anonymize and redact operate purely in memory; nothing reaches disk until `export()`.

`Session` supports `with`; `close()` releases a `ProcessPoolExecutor` plus two threads holding sqlite handles, and leaks worker subprocesses if skipped.

### Object graph and dirty tracking

`DicomStore` (`store.py`) holds `List[Patient]`; `Patient → Study → Series → Instance` live in `entities.py`. `DicomItem` is the shared base carrying `attributes` (a `{"gggg,eeee": value}` dict — tags are lowercase-hex strings throughout, not pydicom keywords) and `sequences`.

Persistence bookkeeping lives on `TrackedEntity` and is driven by a monotonic counter, not a boolean. `_revision` increments on every `set_attr`/`add_sequence_item`, `_persisted_revision` records what reached the store, and the `has_unsaved_changes` property is the comparison. Move the state with `mark_modified()` and `mark_persisted(revision=None)`; read it with `has_unsaved_changes`.

**There is deliberately no setter.** An entity can be told what happened to it, never told what it is — "declare this saved" is precisely the operation that let a rolled-back save leave instances claiming they had been written. Three further traps, each of which exists because it was got wrong once:

- `mark_persisted()` defaults to the *current* revision, which is only correct when nothing can have changed since the write. A save that takes time must capture the revision before it starts and pass that in; otherwise an edit that landed mid-save is written off by a commit that never contained it.
- It never moves backwards (`max`), so a retried or out-of-order save cannot un-persist a revision that already reached the store.
- `mark_subtree_persisted()` speaks for a whole graph and is for hydration only. `mark_persisted()` speaks for one entity — committing a single row must not vouch for its unsaved siblings.

Persistence-dirty and PHI-dirty are separate questions with separate vocabularies; both were called "dirty" once, which made them indistinguishable in the code and in the output users read. The PHI half is `phi_status`/`record_phi_status()`, keyed on `_phi_status_revision`: a status recorded against a revision the entity has since left reads as `UNSCANNED`, structurally, rather than by convention. Note that `record_phi_status()` **also advances `_revision`** — a new status is a change the store should hold. That coupling is why three `mark_modified()` calls in `remediation.py` are individually redundant and survive deletion untested (#132); do not "simplify" one away without reading that issue.

Instances hold pixel and waveform data lazily via `SidecarPixelLoader`/`SidecarWaveformLoader` callables. `get_pixel_data()` materializes, `unload_pixel_data()` releases. Heavy arrays are never kept resident by default — memory scaling on 100GB+ datasets depends on this.

### Hybrid storage (`persistence.py`, `sidecar.py`)

Three tiers, split by DICOM group parity and size:

- Standard (even-group) tags → `instances.attributes_json`, queried with SQLite JSON operators, so 10k instances load without 10k joins.
- Private (odd-group) tags → the `instance_attributes` EAV table, because they're sparse and vendor-specific.
- Pixel/waveform bytes → an append-only `<name>_pixels.bin` sidecar, referenced by (offset, length). `SidecarManager` uses `fcntl.flock` for process-safe appends; `session.compact()` rewrites the sidecar to reclaim space and rewires every offset.

`PersistenceManager` (`persistence_manager.py`) drains saves on a background thread with an `atexit` flush. `SqliteStore` runs a separate audit-log writer thread.

### Parallelism (`parallel.py`)

Everything heavy funnels through `run_parallel()`, which picks processes vs. threads and worker counts from env vars — `ISOCENTER_MAX_WORKERS`, `ISOCENTER_CHUNKSIZE`, `ISOCENTER_FORCE_THREADS`, `ISOCENTER_MAX_TASKS_PER_CHILD`, `ISOCENTER_DISABLE_GC`, `ISOCENTER_SHOW_PROGRESS` (see `docs/environment.md`). Worker functions live at module scope (`scan_worker`, `_verify_worker`, `ingest_worker`, `_export_instance_worker`) because they must pickle. Findings strip their `entity` back-reference before crossing a process boundary and are rehydrated on the far side (`_rehydrate_findings`).

### Privacy path

`privacy.py` (`PhiInspector` → `PhiFinding` → `PhiReport`) detects; `remediation.py` (`RemediationService`) applies. Date jitter is deterministic per patient so intervals survive. `crypto.py`/`reversibility.py` implement optional reversible anonymization: Fernet-encrypted original identities stashed in a private tag, recoverable with `isocenter.key`.

Nested-sequence remediation works, and the way it works is worth knowing before you change either half. `PhiInspector._scan_instance` walks the instance structurally, using `iter_item_tree`. It read a prebuilt `Instance.text_index` until 0.8.0, and that index was built once at ingest and never rebuilt on load from the store — so the scan silently went top-level-only on every real path. The index was retired entirely in #84 once it was clear nothing else read it; if one is ever wanted back, derive it per scan rather than storing it, because a stored index is a second answer to "where does text live" that can disagree with the graph. `_make_lightweight_copy` (`session.py`) deep-copies `sequences` into the worker clone, and a finding raised inside a sequence carries an `entity_path` down to its item; `_rehydrate_findings` resolves that path against the live graph. When it cannot, `_live_target` returns **None** rather than the enclosing instance — deliberately, because remediation skips a finding with no entity, whereas writing a nested tag onto the instance fabricates a top-level element that was never in the file and leaves the real value inside the sequence: an export carrying the PHI plus a decoy. This was #57, fixed in 0.8.0 and covered by `tests/test_nested_phi_audit.py`.

### Export (`exporters/`)

`session.export(folder, format="dicom", **options)` dispatches through a registry. `Exporter` subclasses call `register(name, cls)`; built-ins are `dicom` and `wfdb` (PhysioNet format 16, with a `<record>.annotations.json` bridge to Murmur Studio via `murmur.py`).

`exporters/__init__.py` imports its submodules at the *bottom* of the file on purpose — `dicom.py` and `wfdb.py` re-enter the partially-initialized package. Moving that import up breaks every export path, and no isort/pre-commit config will stop you.

There are two public ways to write DICOM, and the split is deliberate. `session.export()` is the **pipeline**: burned-in identifier scan, subset filter, recoverable-identity disclosure, redaction rules, then the write. `DicomExporter.write_tree()` is the **serializer alone** — it applies none of that and writes the graph as it stands, which is what the fixture generators in `scripts/` need since they build a graph by hand with no session behind it. It was called `save_patient`/`save_studies` until #78, a name that read as though it were the export path rather than half of one; don't reintroduce a spelling that hides the missing gates.

`io_handlers.export_folder_names()` is the single source of the output *directory* layout and both paths route through it. Filenames are the SOP Instance UID in both — InstanceNumber is not unique and collides silently within a series. `tests/test_api_coherence.py` asserts the two paths produce identical trees, comparing full relative paths rather than just directories, which is how the filename divergence went unnoticed.

## Conventions

**One spelling per behaviour.** Pre-1.0, duplicate parameters, aliases, and dead arguments are *deleted*, not deprecated (see commit dc61148 and `tests/test_api_coherence.py`, which pins this). Don't add a compatibility alias for a renamed parameter.

**CHANGELOG.md carries the real reasoning.** Breaking entries state the exact exception a previously-working call now raises, and why the old behaviour was wrong. Match that depth; it's the project's primary design record.

**Comments explain the trap, not the code.** Several non-obvious constraints exist only as comments (the exporter import order, the pydicom `<4.0` cap, the CI branch filter). When a fix depends on a subtlety a future reader would "clean up", write that down.

Commits are conventional-commit style (`fix:`, `feat:`, `refactor:`, `ci:`, `docs:`, `test:`) with a trailing issue/PR number.

Design specs and implementation plans live in `docs/superpowers/{specs,plans}/` (dated filenames) and are excluded from the mkdocs site via `exclude_docs`. Operational runbooks are in `.agent/workflows/` (release process, GCP benchmark).

Planning lives in GitHub Issues/milestones, not `ROADMAP.md`.

## Documentation

`README.md` and `docs/` are kept true to the code; the drift that used to be listed here (a 3.9 floor, a non-existent `.[dev]` extra, three different checkpoint counts, a hand-copied changelog 97 lines behind) is fixed. `CHANGELOG.md` has exactly one home — the docs site links to it on GitHub rather than keeping a copy, because a changelog with two homes only ever has one that is current.

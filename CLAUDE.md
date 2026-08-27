# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Isocenter is a Python library for indexing, de-identifying, and exporting DICOM datasets. It never modifies source files: it builds a SQLite metadata index plus a binary pixel/waveform sidecar, mutates an in-memory object graph, and writes clean copies to a new directory. There is no CLI and none is planned — #29 proposed one and was closed as not planned, so the Python API is the whole interface.

## Commands

```bash
pip install -e ".[dev]"          # contributor environment: tests + pylint (there is deliberately no requirements.txt)
pytest                           # full suite, ~414 tests
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
| `privacy.py` | `test_privacy.py`, `test_remediation.py`, `test_mutation_gaps.py`, `test_profile_end_to_end.py`, `test_audit_suppression.py`, `test_sr_anonymization.py` |
| `remediation.py` | `test_remediation.py`, `test_remediation_actions.py`, `test_mutation_gaps.py`, `test_remediation_dates.py`, `test_deid_tags.py`, `test_remediation_accounting.py` |

Three other workflows exist. `publish.yml` releases to PyPI by Trusted Publishing (OIDC, no token anywhere) when a GitHub Release is published; it refuses to publish if the tag disagrees with `setup.py`'s version, or if the built wheel does not carry its own `resources/*.json`. Its *filename and environment names are pinned by PyPI's publisher config* — renaming either breaks publishing until PyPI is updated to match. `docs.yml` runs `mkdocs gh-deploy` on pushes to `main` only, filtered to `docs/**`, `mkdocs.yml`, its own file, and `isocenter/**.py` (the API reference is generated from docstrings, so a code-only push still changes the site); it deploys straight to the live site, so the branch filter is load-bearing. `schema-drift.yml` checks Murmur's published annotation schema weekly.

`setup.py` is the single source of truth for dependencies; `tests/test_packaging_contract.py` fails if a module imported unguarded at module scope isn't in `install_requires`. Adding an import means adding a dependency there.

Tests write `*.db`, `*_pixels.bin`, `isocenter.log`, and a few config/CSV artifacts into the repo root. All are gitignored; leave them alone rather than adding cleanup.

Optional extras degrade gracefully and must keep doing so: `ocr` (pytesseract — `pixel_analysis.HAS_OCR`), `nlp` (spacy — `ZoneDiscoverer` falls back to regex; the `en_core_web_sm` model is deliberately not declared, because PyPI refuses direct-URL requirements), `docs`, `tests` (includes `setuptools`, which the build-based contract tests need and 3.12+ venvs no longer ship), and `dev` (`tests` plus pylint — contributor tooling that `pip install isocenter` must never pull in).

## Architecture

### The pipeline

`Session` (`isocenter/session.py`, aliased from `DicomSession`) is the only public entry point and the facade over everything else. The intended call order is ingest → examine → create_config/load_config → audit → (enable_reversible_anonymization + lock_identities) → anonymize → redact → verify → generate_report → export. Anonymize and redact operate purely in memory; nothing reaches disk until `export()`.

`Session` supports `with`; `close()` releases a `ProcessPoolExecutor` plus two threads holding sqlite handles, and leaks worker subprocesses if skipped.

### Object graph and dirty tracking

`DicomStore` (`store.py`) holds `List[Patient]`; `Patient → Study → Series → Instance` live in `entities.py`. `DicomItem` is the shared base carrying `attributes` (a `{"gggg,eeee": value}` dict — tags are lowercase-hex strings throughout, not pydicom keywords) and `sequences`.

Persistence is driven by a monotonic counter, not a boolean: `_mod_count` increments on every `set_attr`/`add_sequence_item`, `_saved_mod_count` records what was written, and `_dirty` is the comparison. `mark_saved(version)` is the safe call — it tolerates edits that landed during a save. `mark_clean()` and the `_dirty` setter are legacy escape hatches that clobber that guarantee; prefer `mark_saved`.

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

A known live gap: `_make_lightweight_copy` (`session.py`) copies flat `attributes` but not `sequences`/`text_index`, so profile rules targeting tags nested inside sequences never fire during audit/anonymize. Tracked as issue #57 — assume nested-sequence remediation does not work until that's fixed.

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

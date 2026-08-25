# Changelog

All notable changes to the "Isocenter" project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-01-20

### Added

- **Hybrid Storage Model**: Major refactor of the persistence layer to split metadata into **Core Attributes** (JSON) and **Vertical Attributes** (EAV Table). This allows Isocenter to handle sparse private tags elegantly without bloating the main index, enabling unlimited private tag support.
- **Sidecar Binary Offloading**: Pixel data is now eagerly extracted to a parallel sidecar file (`_pixels.bin`) during ingestion. This drastically reduces the size of the SQLite index and ensures fast start-up times even for massive datasets.
- **Configuration API 2.0**:
  - Introduced `isocenter.configure()` / `session.create_config()` workflow.
  - New `IsocenterConfiguration` class providing programmatic access to Rules, Redaction Zones, and PHI Tags.
  - Automatic `version: 2.0` schema migration.
- **Bytes Persistence**: Full support for persisting raw `bytes` in metadata via the JSON Core layer, ensuring complex VRs (like `OB`/`OW`) survive round-trips correctly.
- **Planar Configuration Support**: Added native handling for `PlanarConfiguration=1` (RRRGGGBBB layout) in `SidecarPixelLoader`, fixing RGB corruption in some Ultrasound/Secondary Capture images.
- **Deprecation Fix**: Updated persistence to avoid deprecated SQLite date adapters for Python 3.12+.

### Changed

- **Database Schema**: `isocenter.db` now contains `instances` (horizontal) and `instance_attributes` (vertical) tables.
- **API**: `DicomSession.active_rules` is deprecated; use `DicomSession.configuration.rules` instead.
- **API**: `DicomSession.active_phi_tags` is deprecated; use `DicomSession.configuration.phi_tags` instead.

### Fixed

- **Integrity Checks**: Resolved a critical hash mismatch issue where updating pixels via `persist_pixel_data` failed to update the integrity hash.
- **Config Scaffolding**: Fixed a bug where the generated YAML config had commented-out keys due to header formatting issues.
- **Shape Errors**: Fixed `Unknown shape: (2,)` errors when loading minimal/flattened 1D pixel arrays; `set_pixel_data` now intelligently reshapes based on image metadata.

## [0.5.4] - 2026-01-14

### Added

- **Compliance Reporting**: Added `session.generate_report()` to produce HIPAA/GDPR-ready Markdown reports containing:
  - **Cohort Manifest**: Summary of processed studies.
  - **Audit Trail**: Aggregated counts of all remediation actions.
  - **Exception Tracking**: Detailed listing of warnings and errors.
  - **Safety Checks**: Automated detection of high-risk tags (e.g., `BurnedInAnnotation=YES`).
- **Safety**: Added automatic validation failure in reports if "Burned-In Annotation" is detected without explicit handling.

### Fixed

- **Export Bug**: Resolved issue where `DeviceSerialNumber` (0018,1000) was dropped during export, preventing machine detection in subsequent runs.
- **UX**: Suppressed excessive console output from `lock_identities` in interactive environments.
- **Regression**: Fixed `ingest` method visibility in `DicomSession`.

## [0.5.3] - 2026-01-13

### Fixed

- **Free-Threaded Stability**: Fixed a race condition in `PersistenceManager` during shutdown that caused data loss in no-GIL environments (Python 3.13t+).
- **Export Reliability**: Fixed a "Pickling Error" regression in `run_parallel` when using `maxtasksperchild` with memory leak mitigation.
- **Export Safety**: Enforced strict exception raising in export workers; failed decompression now correctly fails the export instead of failing silently.
- **Testing**: Resolved `MagicMock` serialization errors during tests ensuring test suite passes cleanly on all platforms.
- **Debug Cleanup**: Removed residual debug output from Sidecar pixel loading and Benchmark stress tests.

### Changed

- **Dependencies**: Bumping version for maintenance release.

## [0.5.2] - 2026-01-08

### Added

- **Free-Threaded Stability**: Implemented Versioned Dirty Tracking in `DicomItem` to correctly handle concurrent modifications in no-GIL environments (Python 3.13t+).
- **Memory Optimization**: Implemented `Instance.unload_pixel_data()` and automatic pixel swapping to `_pixels.bin`. This allows the session to process datasets larger than available RAM by offloading modified pixels to disk.
- **Global Export Parallelism**: Export process now utilizes a global pool of workers across all patients, significantly improving throughput for datasets with many small studies.
- **Async Audit Queue**: Implemented an asynchronous queue for writing audit logs to SQLite, preventing database locking and contention during highly parallel operations.
- **Redaction Progress UI**: Consolidated multiple per-machine progress bars into a single, clean "Redacting Rules" indicator.
- **Verbose Logging**: Added `verbose` flag to Redaction Service methods to allow optional debugging of missing pixels/rules.

### Changed

- **Removed Legacy Config**: Dropped support for legacy list-based configuration files and internal list-parsing logic. Configuration must now be the standard Unified YAML format.
- **Thread Tuning**: Adjusted default parallel worker count to `1.5 * CPU_CORES` (previously `min(32, cpu+4)`).
- **Warning Suppression**: Redaction warnings (e.g., missing pixel data) are now suppressed by default to reduce console noise.
- **Redaction Execution**: Switched `redact()` to enforce threading (`force_threads=True`) to correctly handle in-memory state updates and avoid pickling errors with SQLite connections.

### Fixed

- **Persistence Race Condition**: Fixed a critical race condition where modifications made during an asynchronous save operation were lost/overwritten.
- **Memory Leak**: Resolved memory accumulation in `lock_identities` by implementing batch chunking (`auto_persist_chunk_size`).
- **Progress Reporting**: Fixed broken/instant completion progress bars in `lock_identities`.
- **Logging Regression**: Fixed assertion failure in `test_full_logging_coverage` regarding suppressed log messages.
- **NameError**: Fixed a variable scoping issue in `RedactionService.process_machine_rules`.
- **Parallel Redaction Bugs**: Resolved `pickle` errors and state synchronization issues in parallel redaction by enforcing threading.

## [0.5.1] - 2025-12-31

### Added

- **Python 3.13t+ Support**: Full compatibility with Free-threaded Python (no-GIL).
- **Benchmarks**: Documented performance achieving ~770k instances/sec for metadata operations.
- **Migration Tools**: Added `isocenter.utils.ctp_parser` to convert legacy CTP scripts to Isocenter YAML.

### Changed

- **Dependencies**: Merged `[images]` extra into core install. Isocenter now installs `pillow` and `imagecodecs` by default.
- **Documentation**: Complete rewrite of `README.md` to reflect v2.0 Architecture.

### Fixed

- **Decompression**: Robust support for encapsulated Multi-Frame images and JPEG Lossless (Process 14) via `imagecodecs`.
- **Robustness**: Implemented automatic fallback to installed codecs if standard `pydicom` handler discovery fails (e.g. environment path issues).
- **Handling**: Fixed `UnboundLocalError` regressions in error reporting.
- **Correctness**: Fixed bug where encapsulated pixel data was passed incorrectly to decoders.

## [0.5.0] - 2025-12-18

### Added

- **Performance**:
  - **Split-Persistence**: Introduced a binary sidecar (`_pixels.bin`) for high-speed append-only pixel storage, reducing SQLite metadata size by 99%+.
  - **Database Indexing**: Added indexes to Foreign Keys (`patient_id_fk`, etc.) and `audit_log` for O(1) query performance.
  - **Multithreaded Redaction**: `redact_pixels` now uses `ThreadPoolExecutor` to process Machine Rules in parallel, achieving near-linear speedup on multi-core systems.
- **Optimization**:
  - **Inverted Redaction Loop**: Refactored logic to iterate images once per machine (O(M)) instead of applying every rule to every image (O(NM)).
  - **Empty Zone Skipping**: Automatically skips processing machines with no configured ROIs.
- **Benchmarks**:
  - Verified throughput of **140,000 metadata inserts/sec** and **580 MB/s pixel writes** in stress tests.
- **UX**:
  - Added realtime `tqdm` progress bars for redaction.

### Fixed

- **Multiprocessing**: Fixed "Pickling Error" on Windows/spawn start methods by creating lightweight copies of the object graph for worker communication.
- **Redaction**: Fixed crash when `get_pixel_data` returns `None` (missing file).
- **Redaction**: Fixed "Completely Outside" warning logic for RGB images (interpreting Channels as Columns).

## [0.4.1] - 2025-12-12

### Added

- **Configuration Actions**: Support for `REMOVE` and `EMPTY` actions in `privacy_config.json` for precise tag handling.
- **Ingest Summary**: `ingest` command now provides a detailed count of imported objects.

### Fixed

- **Persistence Priority**: Fixed "Split Brain" issue where remediated `Study`/`Series` metadata was overwritten by original file attributes during export.
- **Export Error**: Fixed validation strictness to allow export of files with stripped Command Set (Group 0000) tags.
- **API Consistency**: Unified `scan_for_phi` and `audit` methods.

## [0.4.0] - 2025-12-11

### Added

- **Features**:
  - **Safe Export**: New `export(safe=True)` mode ensuring no PHI leaves the system.
  - **Reversible Anonymization**: Securely embed encrypted original identities (`isocenter.key`).
  - **Manual Persistence**: Changed default behavior to manual `.save()` for better user control.
  - **Background Persistence**: Non-blocking saves via `PersistenceManager`.
  - **PHI Analysis Reports**: `scan_for_phi` now returns a rich `PhiReport` object with Pandas DataFrame support.
  - **Parallel Processing**: Multi-process support for Import and PHI Scanning.
- **Improvements**:
  - **Console Output**: Suppressed noisy `pydicom` warnings and improved `tqdm` progress bars.
  - **Batch UX**: Better feedback during long-running operations.
  - **Test Coverage**: specific tests for `crypto`, `config`, and `safe_export`.

### Fixed

- **Regression**: Addressed silent failure in pixel export when source files are missing.
- **Bug**: Fixed `TypeError` in Remediation Date Shifting.
- **Bug**: Fixed `MultiValue` JSON serialization error in persistence.
- **Bug**: Fixed `ValueError` regarding Group 0000 elements during export.

## [0.3.0] - 2025-12-11

### Added

- **Robust Persistence (SQLite)**: Replaced `Pickle` with `SQLite` for session storage (`isocenter.db`). Allows for scale and external querying.
- **Audit Trail**: Implemented a comprehensive audit system. Actions such as `Redaction` and `Remediation` are now logged to the `audit_log` table in the database.
- **Automated PHI Remediation**:
  - **Metadata Anonymization**: Automatically detects and anonymizes Patient Names and IDs.
  - **Deterministic Date Shifting**: Shifts study dates by a consistent offset (based on Patient ID hash) to preserve temporal relationships while obscuring actual dates.
- **`apply_remediation` API**: Added top-level API to `DicomSession` to easily apply fixes found by the privacy inspector.
- **Documentation**: Significant updates to `README.md` and architecture documentation.

### Changed

- **Breaking Change**: The internal persistence format has changed from `.pkl` to `.db`. Existing sessions from v0.2.0 cannot be loaded and must be re-imported.
- **Dependency Update**: Added `sqlite3` (stdlib) as a core dependency for the store backend.

## [0.2.0] - 2025-12-10

### Added

- **JSON Configuration Validation**: `ConfigLoader` now rejects rules with missing fields or invalid/illegal ROI definitions.
- **ROI Safety Checks**: Redaction operations now explicitly check image bounds, clipping ROIs to the image dimensions and warning if they are completely out of bounds.
- **File Deduplication**: `DicomImporter` now detects and skips files that have already been imported into the current session.

### Fixed

- **Recursive Sequence Import**: Nested sequences (e.g., in Structured Reports) are now correctly recursed and indexed.
- **Pixel Depth Export**: `DicomExporter` now correctly preserves 8-bit usage for relevant modalities (e.g., US, SC) instead of hardcoding 12/16-bit depth.

## [0.1.0] - 2025-12-09

### Added

- **Core Architecture**: Implemented the semantic object graph (`Patient` → `Study` → `Series` → `Instance`) to replace flat dictionary handling.
- **Facade Interface**: Added `isocenter.Session` class as the primary entry point for user interaction, managing imports, persistence, and inventory.
- **Lazy Loading**: Implemented a Proxy Pattern for `Instance` objects. Metadata is loaded into memory during import, while heavy pixel data is read from disk only upon request.
- **De-Identification Service**: Added `RedactionService` to modify pixel data (burn-in removal) based on specific machine serial numbers.
- **Configuration Management**: Added support for `redaction_rules.json` to define Redaction Regions of Interest (ROIs) externally.
- **Machine Indexing**: Created `MachinePixelIndex` to efficiently group and retrieve instances by their Equipment attributes (Manufacturer, Model, Serial Number).
- **Builder Pattern**: Added `DicomBuilder` (and fluent sub-builders) to allow programmatic construction of complex DICOM hierarchies for testing and synthetic data generation.
- **IOD Validation**: Implemented `IODValidator` to enforce Type 1 and Type 2 attribute compliance for standard SOP Classes (e.g., CT Image Storage) before export.
- **Persistence**: Added `pickle`-based serialization to save and resume session state (`DicomStore`).
- **Import/Export**: Created `DicomImporter` for fast metadata scanning and `DicomExporter` for writing valid, standards-compliant `.dcm` files.

### Security

- Pixel data redaction is performed in-memory and committed to new files; original files are treated as read-only during the session to prevent accidental data loss.

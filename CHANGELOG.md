# Changelog

All notable changes to the "Isocenter" project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`PhiStatus`: what the session knows about each entity, as recorded state.** Whether an item still carried identifiers used to exist only as a set built inside `_export_dicom` and discarded when it returned -- to ask "which items in my session still carry identifiers?" you had to re-run a scan, and the answer was never attached to the items. Every patient, study and instance now carries one of UNSCANNED, IDENTIFIED, REMEDIATED or CLEARED, stamped by `audit()` and by remediation, persisted, and readable through `session.phi_status_summary()`.

  **A status is valid only for the revision it was computed at.** Edit an entity after a scan and it reports UNSCANNED again rather than leaving a stale claim behind. This is structural, not a convention -- there is no way to read a status that describes content the entity no longer holds -- because a persisted REMEDIATED surviving a later edit would read as an assurance, which is worse than admitting nothing is known. For the same reason an unrecognised stored value, or a row written before the column existed, reads as UNSCANNED.

  What CLEARED does *not* mean: it says the configured **tag** scan found nothing. It is not a statement about burned-in pixel text, which is a separate scan (`scan_pixel_content`), and not an approval to release. Series carry no status at all -- the inspector reports on patients, studies and instances only, so a series has never been examined and does not claim to have been.

  Existing databases pick up the new columns through a guarded `ALTER TABLE` at open; no rebuild is needed.

- **Trusted Publishing workflow**: `.github/workflows/publish.yml` publishes to PyPI on a GitHub Release via OIDC, so no API token exists in repository secrets or on any developer machine. It refuses to publish a release whose tag disagrees with `setup.py`'s version, and installs the built wheel into a clean environment outside the source tree to prove it carries its own `resources/*.json` before anything reaches an index.
- **Citation metadata**: `CITATION.cff` drives GitHub's "Cite this repository" button, and `.zenodo.json` supplies deposit metadata for archiving. Isocenter is the upstream half of a pair -- it builds and de-identifies the corpus that [Murmur Studio](https://doi.org/10.5281/zenodo.21077528) reviews -- so the deposit records that relationship, and work using both should cite both. De-identification tooling is a methods citation, not an acknowledgement. No DOI is claimed until the first Zenodo deposit exists: a DOI that does not resolve is worse than none, because it is what tooling copies into bibliographies.
- **`dev` extra**: `pip install -e ".[dev]"` installs the `tests` extra plus pylint. It is contributor tooling and nothing else -- `pip install isocenter` must never drag a linter or a test runner into a user's environment. `docs/developer_guide.md` documented this command for months while no such extra existed, so the command it gave contributors failed outright.
- **Developer Guide**: Added `docs/developer_guide.md` covering linting and testing standards.
- **Advanced Discovery**: Added `DiscoveryResult.to_dataframe()` and `get_density_matrix()` for data science integration.
- **Iterability**: Made `DiscoveryResult` iterable, yielding `(tag_string, count)` tuples.
- **DICOM Waveform Support**: Waveform IODs are now ingested, persisted, and de-identified as a first-class data type alongside pixel data.
- **WFDB Export**: `session.export(folder, format="wfdb")` writes `header(5)`-conformant PhysioNet WFDB records (format 16).
- **Murmur Annotation Bridge**: Waveform Annotation Sequence `(0040,B020)` is exported as `<record>.annotations.json` for [Murmur Studio](https://github.com/kvnlng/Murmur).
- **Export Format Registry**: `isocenter.exporters` provides a pluggable `Exporter` seam; `session.export()` dispatches on `format`.
- **Context Manager Support**: `DicomSession` now supports `with DicomSession(...) as session:`. `close()` releases a `ProcessPoolExecutor` and two threads holding sqlite handles; forgetting to call it leaks worker subprocesses. `__exit__` returns `None` and never suppresses an exception raised in the `with` body — but if `close()` itself also raises (e.g. one of its shutdown steps fails), it is `close()`'s exception that propagates, not the body's; the body's original exception is chained as `__context__` rather than raised directly, per normal Python exception-handling semantics.

### Changed

- **BREAKING: one vocabulary for persistence state, and one mechanism behind it.** A session tracked two unrelated things about an item -- whether it held changes not yet written, and whether it still carried identifiers -- and called both of them *dirty*. The first now says what it means, on every entity, through one shared `TrackedEntity` base:

  | before | now |
  |---|---|
  | `entity._dirty` (read) | `entity.has_unsaved_changes` |
  | `entity._dirty = True` | `entity.mark_modified()` |
  | `entity._dirty = False` | `entity.mark_persisted()` |
  | `entity.mark_saved(version)` | `entity.mark_persisted(revision)` |
  | `entity.mark_clean()` | `entity.mark_subtree_persisted()` |
  | `_mod_count` / `_saved_mod_count` | `_revision` / `_persisted_revision` |

  There were two implementations of this, not one. `Instance` had a revision counter and a `_dirty` property computed from it; `Patient`, `Study` and `Series` each had a plain boolean field of the same name. That is why `save_all` asked every level with `getattr(entity, '_dirty', True)` rather than simply asking -- and why only instances could survive a concurrent edit during a save, since only instances had a revision to compare. All four now share the counter, so the guarantee is the same everywhere. Behaviour is otherwise unchanged: a new entity still reports unsaved changes, and nothing marks patients, studies or series persisted yet.

  `has_unsaved_changes` is **read-only**. State moves through `mark_modified()` and `mark_persisted()` only, so an entity can be told what happened to it but not told what it is. "Declare this saved" is exactly the operation that let a rolled-back save leave instances claiming they had been written (see the `save_all` entry below).

- **`DicomSession._export_dicom()` split into named pieces, and the PHI report stopped borrowing the word "dirty".** It was a single 306-line method with 45 branches holding the pre-export safety scan, its console report, the suggested-config block, subset resolution, the four-level export-plan walk and the parallel batch. It is now 53 lines plus eight named helpers. `tests/test_export_contract.py` pins what a caller can observe.

  The vocabulary change is the user-visible half. `_dirty` means "has unsaved changes" on every entity in a session -- it is `_mod_count > _saved_mod_count`, a persistence flag with no connection to PHI. The safety report used the same word for "still carries identifiers", printing `The following tags were flagged as dirty:` and logging `Skipping dirty`, which made the two indistinguishable to anyone who had read either. The report now says what it means: `The following tags still carry identifiers:`, and `Skipping <uid>: it or one of its parents still carries identifiers.`

- **`SqliteStore.save_all()` split into named pieces.** It was a single 336-line method holding the entire Patient -> Study -> Series -> Instance walk, four SQL statements, the private-tag split, the pixel deduplication, the sidecar write and the summary logging in one body -- ending with forty lines of unresolved commentary arguing with itself about when to mark instances clean. It is now ten functions, the largest 49 lines. `tests/test_save_all_contract.py` states what the method promises: what lands in which table, that identical pixels are not appended to the sidecar twice, that `instance_blobs` mirrors `instances`, that an instance dropped from memory is deleted from the database, and what happens when the save fails.

- **`DicomSession.create_config()` split into named pieces.** It was a single 320-line method that loaded two knowledge bases, matched machines against them by three different criteria, counted burned-in-annotation flags, reshaped the PHI tag defaults, and hand-rolled a YAML-with-comments renderer -- with a `class FlowList` and a representer defined and re-registered inside the function body on every call. The parts now have names and can be read (and tested) one at a time; `create_config` itself is the ~35 lines that say what happens in what order. This is a pure refactor of the output: `tests/test_create_config_output.py` pins the generated file byte-for-byte against a recorded fixture, so a change to a single character of the scaffold fails the suite. The behaviour changes it exposed are listed under Fixed.

- **Linting configuration reflects the codebase rather than the reverse.** `logging-fstring-interpolation` (104 occurrences) and `too-few-public-methods` (16) are disabled in `pylintrc.toml`, with the reasoning recorded there. Obeying the first means rewriting readable f-string log calls into `%s`-style lazy interpolation for a saving that does not exist on log lines that actually emit; the second fires on dataclasses and DTOs, where few public methods is the correct design. Everything else stays enabled.
- **BREAKING: the project is now `isocenter`, not `gantry` -- distribution name, import name, environment variables and on-disk artefacts.** `pip install gantry` was never going to be possible: the name has been held on PyPI since 2022 by an unrelated ML-observability library (`gantry`, 48 releases, last published 2023-09-29), and *its* import name is `gantry` too, so no distribution-name workaround existed -- `pip install gantry-dicom` would still have unpacked a `gantry/` directory into site-packages for one package to overwrite the other. The owner was contacted and did not respond. Since this project had never been published, renaming cost no installed base, and the import name moved with the distribution rather than leaving `import gantry` colliding permanently. There is no compatibility shim: one spelling per behaviour.
  - `import gantry` becomes `import isocenter`; `from gantry.session import DicomSession` becomes `from isocenter.session import DicomSession`. `Session`, `DicomSession` and every other public name are otherwise unchanged.
  - All twelve `GANTRY_*` environment variables are now `ISOCENTER_*` (`ISOCENTER_MAX_WORKERS`, `ISOCENTER_DB_PATH`, `ISOCENTER_FORCE_THREADS`, ... -- the full list is in `docs/environment.md`). These fail quietly rather than loudly: a leftover `GANTRY_MAX_WORKERS` in a shell profile or CI config is now simply an unrecognised variable, so tuning silently reverts to defaults instead of erroring. Re-export them.
  - Default artefact names follow: `gantry.log` -> `isocenter.log`, `gantry.db` -> `isocenter.db`, and the reversible-anonymization key `gantry.key` -> `isocenter.key`. **The key file needs manual action.** `DicomSession.__init__` auto-enables reversible anonymization when it finds `isocenter.key` in the working directory (`isocenter/session.py:153`), and `CryptoManager` defaults to that path. An existing `gantry.key` will therefore not be found, and identities encrypted under it cannot be recovered until the file is renamed. The key material itself is unchanged -- `mv gantry.key isocenter.key` is the whole migration.
  - The repository moved to `github.com/kvnlng/Isocenter` and the documentation to `kvnlng.github.io/Isocenter/`. GitHub redirects the old paths, but update any local git remote.
  - Entries below this one were written while the project was named Gantry. Their file paths and identifiers have been rewritten to the current spelling so the document stays navigable; the history they describe is otherwise unchanged.

- **`DicomSession.close()` now runs every shutdown step even if an earlier one raises.** Previously the persistence-manager shutdown, the audit-thread stop, and the `ProcessPoolExecutor` shutdown ran as a bare sequence with no `try`/`finally`; an exception from the first step aborted the rest, leaking the executor's worker processes for the life of the interpreter. Each step now runs independently and any failures are logged; if more than one step fails, `close()` raises the first failure (treated as the likely root cause) rather than the last.
- **BREAKING: `annotations.json` no longer includes `note` by default.** Unformatted Text Value `(0070,0006)` routinely holds free-text clinical commentary. Pass `session.export(folder, format="wfdb", include_annotation_text=True)` to restore the old behaviour. `(0070,0006)` was also added to the Basic profile -- but a configured session that opts in does **not** currently receive a remediated value. `(0070,0006)` lives inside each Waveform Annotation Sequence item, and `PhiInspector._scan_instance` (`isocenter/privacy.py`) only reaches nested-sequence tags when it has the instance's `text_index`; the worker clone `session.audit()`/`session.anonymize()` actually scan (`_make_lightweight_copy`, `isocenter/session.py`) copies only flat `attributes`, not `sequences`/`text_index`, so this tag's profile entry never fires (#57 -- out of scope here, filed separately). The WFDB `note` leak this entry was meant to close is still closed, but by the export default being off, not by remediation: opting in via `include_annotation_text=True` gets you the tag's **raw** value even on a fully configured session. (#39)

- **BREAKING: Python 3.12 or newer is now required.** `python_requires` previously claimed `>=3.9`, which was not merely untested but false: `isocenter/entities.py` and `isocenter/privacy.py` use `@dataclass(slots=True)` (Python 3.10+), so a 3.9 install succeeded and then raised at import. The declared dependency set (numpy, imagecodecs) resolves only on 3.12+. CI now tests 3.12, 3.13, 3.14 and 3.14t, so the floor is a tested claim rather than an assertion.
- **BREAKING: `pytesseract` moved from a hard dependency to the `ocr` extra.** It was already imported defensively (`isocenter/pixel_analysis.py` sets `HAS_OCR = False` when absent), so it was never truly required. Install with `pip install "isocenter[ocr]"` to keep burned-in-text detection.
- **BREAKING: `requirements.txt` removed.** `setup.py` is now the single source of truth for dependencies. The two lists had drifted — `python-dotenv` was in one and `pytesseract` in the other — and CI installed both, which hid the drift. Use `pip install -e ".[tests]"` for a development environment.
- **`pydicom` is now capped below 4.0.** `isocenter/__init__.py` assigns `pydicom.config.pixel_data_handlers`, which 3.x deprecates and 4.0 removes. The cap prevents a silent break on a future pydicom release.
- **BREAKING: `session.export()` signature**: `export(folder, version=None, use_compression=True, ...)` is now `export(folder, format="dicom", **options)`. Positional argument 2 now means `format`, not `version` — existing code calling `session.export("/out", "v2")` previously set `version="v2"`; it now raises `ValueError: Unknown export format 'v2'`. There is no keyword workaround: `version` was later deleted from `_export_dicom()` entirely (see the next entry), so `session.export("/out", version="v2")` also raises, with `TypeError: DicomSession._export_dicom() got an unexpected keyword argument 'version'`.
- **BREAKING: `version` parameter removed from `session.export()`/`_export_dicom()`.** It was accepted, documented as "Deprecated/Unused", never read by any code path, and passed by no test. `session.export(folder, version=...)` — previously silently accepted and ignored — now raises `TypeError: DicomSession._export_dicom() got an unexpected keyword argument 'version'`.
- **BREAKING: `compression`/`safe` aliases removed from `session.export()`.** Each aliased a parameter that already existed, so two spellings produced one effect: `compression=` is now `use_compression=`, and `safe=` is now `check_burned_in=`. `_export_dicom` carried a "Legacy Argument Mapping" block that translated the alias to the canonical parameter at call time; that block is gone, and the alias keyword arguments now raise `TypeError: unexpected keyword argument`.
- **BREAKING: `DicomExporter.generate_export_from_db` removed.** A public staticmethod that streamed `ExportContext` objects directly from the database for O(1)-memory export; it had no production caller and was reached only by its own test. There is no drop-in replacement with the same memory profile: `session.export(folder, ...)` exports from `self.store.patients`, which `DicomSession` fully loads into RAM via `store_backend.load_all()` at session start, not from the database on demand.
- **BREAKING: `DicomExporter.save_patient`/`save_studies` now write the same folder layout as `session.export()`.** Both methods previously built their tree with a private, unshared naming helper; that helper is deleted and both now call the same `isocenter.io_handlers.export_folder_names` that `session.export()` has always used, so a `save_patient`/`save_studies` caller gets files in new locations:

  Before:
  ```
  Subject_<PatientID>/
    Study_<YYYYMMDD>_<StudyDescription>/
      Series_<SeriesNumber>_<SeriesDescription>/
  ```
  (`"Unknown"` when the PatientID was empty — `_sanitize` returned that for any falsy input, `"UnknownDate"` when the study date was empty, `"Study"`/`"Series"` as description fallbacks, `"0"` as the series-number fallback, sanitized by the now-deleted `DicomExporter._sanitize`: keep only alphanumerics/space/`.`/`-`/`_`, then `.strip()`, then replace spaces with `_`.)

  After:
  ```
  Subject_<PatientID>/
    Study_<YYYY-MM-DD>_<StudyDescription>_<last 5 chars of StudyInstanceUID>/
      Series_<SeriesNumber>_<Modality>_<SeriesDescription>_<last 5 chars of SeriesInstanceUID>/
  ```
  (`"UnknownPatient"` when the PatientID is empty, not `"Unknown"` as before; `"NoDate"` when the study date is empty — `io_handlers.py:876` is a raw `str(study.study_date or "NoDate")`, so a real `datetime.date` renders ISO (`YYYY-MM-DD`), not `strftime("%Y%m%d")` as the old `format_study_date()`-based path produced; `"Study"`/`"Series"` description fallbacks; `"Unknown"` UID fallback before the 5-char slice; the series number is now an unguarded `str(series.series_number)` — a `None` series number renders the literal string `"None"`, whereas the old path fell back to `"0"`; sanitized by `ConfigLoader.clean_filename`: `.strip()`, replace spaces with `_`, then drop every character that isn't a word character, `-`, or `.`.)

  The sanitizer change matters independently of the template: the two functions filter and collapse whitespace in the opposite order and diverge on punctuation adjacent to whitespace — e.g. `"*  foo"` became `"foo"` under the old sanitizer but becomes `"__foo"` under `ConfigLoader.clean_filename`. A folder name can differ even where the template above looks unchanged.
- **`session.export()`'s folder names can now include Study/Series Description text that previously never reached them — a PHI-surface change, not just a layout detail.** The case-insensitive Description-tag lookup added to `export_folder_names` (`_get_attr_case_insensitive`, `isocenter/io_handlers.py`, to fix a `save_patient` regression) applies to `session.export()` too, since `_export_dicom` calls the same shared `export_folder_names`. Before, the Series/Study Description lookup checked only the lowercase tag spelling (e.g. `"0008,103e"`); an instance whose attributes were keyed with the uppercase DICOM tag spelling (`"0008,103E"`, as e.g. `scripts/generate_test_dataset.py` sets it) fell through to the `"Series"`/`"Study"` fallback, and its actual description text never reached the exported folder name. It is now found and used instead of the fallback. Example, reproduced directly: a series with Series Description `"ECG Series"` stored under the uppercase key produced the folder `Series_1_CT_Series_88888` before and produces `Series_1_CT_ECG_Series_88888` now. Because folder names are written to disk as plaintext directory entries, and Description fields are free text that can carry identifying information, a value that previously never reached disk now does.
- **Planning Moved to GitHub Issues**: `ROADMAP.md` and `docs/roadmap.md` are now pointers to the issue tracker. Open work is tracked under versioned milestones; `CHANGELOG.md` remains the canonical record of shipped features.
- **Pylint Compliance**: Addressed hundreds of linting issues across `isocenter/` and `tests/`.
  - Enforced `encoding='utf-8'` on all file operations.
  - Standardized import ordering.
  - Added missing docstrings.
  - Refactored `isocenter/discovery.py` lazy imports.
- **Testing**: Cleaned up test suite (whitespace, indentation, imports) to achieve a clean lint score (7.62/10).
- **BREAKING: `pip install "isocenter[nlp]"` no longer installs spaCy's `en_core_web_sm` model.** The extra pinned it by direct URL (`en_core_web_sm @ https://github.com/explosion/spacy-models/releases/...whl`) because the model has no PyPI release. That is legal for a local `pip install -e .` and fatal for publication: PyPI refuses any distribution whose metadata contains a direct URL requirement, so `twine upload` rejected the build outright with "Can't have direct dependency" -- Isocenter could not be released at all while that line existed. The extra now installs spaCy only; install the model separately with `python -m spacy download en_core_web_sm`. Without it `ZoneDiscoverer` falls back to regex, exactly as it already did when spaCy was absent.

### Fixed

- **BREAKING (data): `anonymize()` left the original patient row in the session database.** Patients are upserted on `patient_id`. De-identification changes that value, so the write inserted a *second* row and orphaned the first -- name and identifier intact. The studies were re-parented to the new row, so nothing ever visited the old one again and no deletion reached it. Reopening the session showed two patients: the anonymised one, and the original with no studies. The exported copies were always correct; the working database that produced them was not, and it is the artefact that stays on disk. `save_all` now deletes patient rows the in-memory store no longer holds, and the same rule was extended to studies and series -- only instances had it.
- **Deleting an instance left its private tag values behind.** `instance_attributes` stores private tags as text keyed by SOP Instance UID. Its foreign key declares `ON DELETE CASCADE`, but SQLite enforces foreign keys only under `PRAGMA foreign_keys=ON`, which this store never sets -- so nothing cascaded, and the values stayed in the database, still attributable by UID. Deletions now clear `instance_attributes` and `instance_blobs` explicitly.
- **A removed PHI tag could go unwritten.** `REMOVE_TAG` deletes straight out of `entity.attributes`, a plain dict; unlike `set_attr`, that bumps no revision. An instance that had already been saved therefore reported no unsaved changes after its PHI was stripped, so the next save skipped it and the identifier stayed in the database. `action: REMOVE` is the ordinary case -- it is what the basic profile does and what `create_config` scaffolds.
- **A malformed item on the persistence queue hung every later save.** `PersistenceManager._worker` called `task_done()` in a `finally`, but only around the save itself; anything failing before that block was caught by the outer handler, which never marked the item done. `flush()` waits on that count, so one bad item blocked all subsequent saves indefinitely. The whole item-handling path now runs under the same `try`/`finally`.

- **`export(show_progress=False)` drew a progress bar anyway.** The parameter was accepted and documented, then discarded by a bare `show_progress = True` three lines before the value was used. Four tests in this suite passed `show_progress=False` and none of them noticed, because none asserted on it.
- **BREAKING: an unusable `subset` no longer exports the entire cohort.** `subset` accepts a query string, a DataFrame, or a list of UIDs; anything else fell through every branch and left the filter unset, so a caller who asked to export one series and got the argument's type wrong exported everything instead. Over-exporting is the dangerous direction for a de-identification tool to fail in. `session.export(folder, subset=<anything else>)` now raises `TypeError`.
- **BREAKING: a `subset` query that does not run is now an error.** A failed `DataFrame.query` was logged and the export returned, so a mistyped query and a query that legitimately matched nothing produced the same result: an empty directory and a successful return. It now raises `ValueError`, chained to the pandas error that caused it.
- **The safety report's suggested config closed its braces twice.** A duplicated pair of `print` calls emitted the closing `}` lines again, so the snippet the report told the user to copy was malformed on its own terms.

- **A failed `save_all()` reported the wrong exception.** `_get_connection` already rolls back and closes on the way out, but `save_all`'s own handler then called `rollback()` on that closed connection. The call raised `sqlite3.ProgrammingError: Cannot operate on a closed database`, which replaced the real exception -- so every distinct save failure, whatever caused it, reached the caller under one misleading name. If the failure happened *before* the connection was opened, the handler was worse still: it guarded with `hasattr(conn, "rollback")` on a name the `with` statement had never bound, and died with `UnboundLocalError`. The handler now logs and re-raises; the transaction belongs to `_get_connection`.
- **A rolled-back save left instances marked clean, so the retry skipped them.** Instances were marked saved inside the walk, one series at a time, while the transaction covering all of them was still open. When a later failure rolled it back, the rows were gone from the database but the already-processed objects no longer considered themselves dirty -- the next `save_all` passed over them, and one failed write became permanent data loss with no error anywhere. Instances are now marked clean only after the commit returns.

- **`create_config()` edited the configuration it was scaffolding.** The scaffold shows research-friendly defaults for Study Date, Patient Sex and Patient Age, and it guaranteed their presence by inserting them into `self.configuration.phi_tags` -- the live policy dict, not a copy. Asking for a scaffold therefore added three tags to the policy the session would go on to apply, and a session that had loaded a config with any of them absent came out of `create_config()` with a different de-identification policy than it went in with. The three tags are now added to a copy, so generating a scaffold reads the configuration and never writes it.
- **`create_config()` re-read the CTP rules once per machine.** The knowledge-base load sat inside the per-machine loop, so a cohort with 40 distinct scanners opened and parsed the same shipped rules file 40 times. Both knowledge bases are now read once per call.
- **Machine order in a generated config was not reproducible.** `DicomStore.get_unique_equipment()` returned `list(set(...))`, whose order depends on `PYTHONHASHSEED`, so two runs over the same dataset emitted the machines of a scaffolded config in different orders -- and, in the same repository, a diff that was pure noise. Equipment is now sorted by manufacturer, model and serial number.
- **Waveform Data Loss**: Waveform Data `(5400,1010)` was silently discarded at ingest because `populate_attrs` skips all `OB`/`OW` VRs. Waveform IODs now round-trip intact.
- **Broken install from `setup.py` alone**: `isocenter/config_manager.py` imports `python-dotenv` unguarded, but it was declared only in `requirements.txt`. A `pip install` therefore succeeded and then failed with `ModuleNotFoundError: No module named 'dotenv'` on `import isocenter`. CI installed both files, so it never saw this. `tests/test_packaging_contract.py` now asserts that every unguarded module-scope import is declared.
- **`export(compression=None)` silently compressed anyway.** The now-removed legacy mapping only overrode `use_compression` when `compression is not None`, so a caller passing `compression=None` to mean "do not compress" fell through the guard and kept the parameter's default of `True`, producing a JPEG2000 export. `tests/benchmarks/run_stress_test.py`'s "uncompressed" benchmark arm computed exactly this (`compression=None` when `compress_export` was `False`) and had been measuring compressed exports. `use_compression=None` (now the only spelling) is plain falsy and correctly means no compression.
- **Acquisition DateTime survived `anonymize()`.** `(0008,002A)` was absent from `PRIVACY_PROFILES["basic"]` while the plain `(0008,0022)` Acquisition Date it duplicates was removed, so raw acquisition timing reached exported DICOM after a full audit/anonymize pass. The four Performed Procedure Step date/time tags had the same gap. (#38)
- **Operator free text reached the WFDB header.** Channel Label `(003A,0203)` is operator-typed and was written verbatim into the `.hea` signal description and the `annotations.json` `lead` field whenever a channel carried no coded Channel Source Sequence. It is now emitted only when it is a recognisable signal name (see `KNOWN_LEAD_NAMES` in `isocenter/waveform.py`); anything else becomes a positional `ch<N>` token, where `N` is the **zero-based** channel index -- DICOM's own ChannelNumber is 1-based, so `ch1` names the *second* channel. Fixed in the exporter rather than the privacy profile because the PHI scan is tag-gated, so a profile entry would not protect a bare `Session()`. (#39)
- **`WaveformChannel.wfdb_description()`'s no-argument return changed.** It is public API. Called without an `index` (no positional token available to fall back to), an uncoded, non-allowlisted channel now returns the literal string `"signal"` instead of the raw, unfiltered Channel Label -- consistent with the positional-token fix above, which this method also implements. Any caller invoking it without an index and expecting the old raw-label passthrough will observe this change.
- **WFDB header fabricated a `00:00:00` timestamp.** `(0008,002A)` Acquisition DateTime and `(0008,0030)` Study Time are both now removed by the Basic profile (see the Acquisition DateTime fix above), so on a fully configured, anonymized session the instance's real time-of-day is always unavailable -- but `WfdbExporter._start_datetime` combined the (real, shifted) study date with `time_of_day or datetime.min.time()`, silently substituting midnight and presenting it as real acquisition timing in every such record. header(5) does not support a date-only start time (base_date requires base_time, both per the PhysioNet spec and per `wfdb-python`'s own writer, and its reader cannot positionally disambiguate a bare date from a bare time), so the record line is now written with **both start time and date fields omitted** rather than a fabricated time, whenever a real date exists but no real time-of-day does. That date is not simply lost: since it's genuine, useful `SHIFT_DATE` output, it is instead preserved as a single `# de-identified start date: DD/MM/YYYY` header comment -- the same `DD/MM/YYYY` format the record line's own date field uses, sanitized through the same `_sanitize_description` path every other field on the line gets, not a new one. This is Isocenter's one deliberate exception to never emitting `#` comment lines. A session that never anonymizes, or whose instance still carries a real Acquisition DateTime, is unaffected -- its true time-of-day is written on the record line exactly as before, with no comment.

- **A redaction zone that failed to apply was silently discarded** (#66). `RedactionService.apply_redaction_to_array` wrapped the pixel-zeroing itself in `except Exception: pass`, and the enclosing loop in a second handler returning `False`. A zone that could not be applied -- a read-only array, an unexpected dtype, a malformed ROI -- was skipped with no log at any level, and the return value was indistinguishable from "there were no zones to apply". `_export_instance_worker` ignores that return value and writes `arr.tobytes()` immediately afterwards, so the unredacted image was exported as though clean. A failed zone now logs the ROI and array shape at ERROR and raises; the export worker already contains per-instance failures, so the instance fails instead of shipping PHI.
- **The redaction knowledge base has never loaded.** `create_config` read `resources/redaction_rules.json` inside a `try` guarded by `except BaseException: pass`. Because `import json` appeared *later in the same function*, `json` was a function-local name and the `json.load` call raised `UnboundLocalError` on every single invocation -- caught and discarded by that handler. Every scaffolded config was therefore produced as though the knowledge base were empty, and no machine was ever pre-filled with its known redaction zones. Verified against `main`: a session with equipment `SN-SCANNER-01` produced a config containing none of that machine's zones before, and its full entry after.
- **Partial export failures reported as success.** `session.export()` captured `DicomExporter.export_batch`'s success count and discarded it, then logged "Export complete." unconditionally -- identical output whether 1200 of 1200 instances were written or 3. It now warns with the ratio and points at the audit log when any instance fails.
- **Exception handling narrowed across the package.** 17 `except BaseException` handlers became 2. `BaseException` catches `KeyboardInterrupt` and `SystemExit`, so Ctrl-C during a long ingest or export was swallowed by whichever handler happened to be on the stack. Each site now catches what it actually expects and logs or re-raises rather than passing. The two that remain are deliberate and commented: the sidecar compaction rollbacks, which must restore the backup file even when interrupted, and re-raise afterwards.
- **The documentation workflow deployed from any branch.** `docs.yml` triggered on `push` filtered by path but not by branch, and its job runs `mkdocs gh-deploy`, which publishes straight to the live site. Any push to any branch touching `docs/**` therefore deployed unreviewed documentation -- which is what happened twice from a feature branch during the isocenter rename, before the PR was merged. It is now `branches: [main]`.
- **`release.yml` deleted.** Named "Build and Publish", it fired on `release: published` -- the same event as `publish.yml` -- built the package, and uploaded it to the GitHub Actions artifact store. It published nothing. Both ran on the v0.7.0 release and only `publish.yml` did anything.
- **Documentation corrected against the code.** `docs/developer_guide.md` claimed Python 3.9+ (the floor is 3.12) and told contributors to run `pip install -e ".[dev]"` when no `dev` extra existed. `docs/architecture.md` titled its safety pipeline "The 8 Checkpoints" above a nine-item list that omitted **Report**, while `README.md` correctly described ten. Installation instructions still sent users to `git+https://github.com/kvnlng/Isocenter.git` now that releases are on PyPI. `mkdocs.yml`'s `edit_uri` pointed at a `docs_site/` directory that does not exist.
- **`docs/changelog.md` removed** (#52). It was a hand-maintained copy of `CHANGELOG.md` that had drifted 97 lines behind and stopped at 0.6.0. The documentation site's nav now links to the real file on GitHub: a changelog with two homes only ever has one that is current.
- **The compliance report asserted HIPAA Safe Harbor over sessions that were never configured for it.** `ComplianceReport.deid_method` was a dataclass default reading `"Safe Harbor (Basic Profile)"` that no caller ever assigned, and the Markdown renderer's methodology paragraph hardcoded "the dataset was processed using the Gantry Safe Harbor pipeline". Both printed unconditionally -- including for a bare `Session()`, whose PHI scan covers the six tags in `resources/phi_tags.json`, and while #57 leaves rules targeting nested sequences unfired -- directly above the report's Data Protection Officer signature line. The `Privacy Profile` row was no better: `session.generate_report` filled it with the literal string `"See Config"`.

  Both fields are now derived from the live configuration. `deid_method` reads e.g. `"DICOM PS3.15 'basic' profile: 34 tag rules, 0 pixel redaction rules"`, or `"Session defaults: 6 tag rules, 0 pixel redaction rules"` when no config was loaded -- the tag count mirrors what `PhiInspector` actually scans with, defaults included. The methodology paragraph now states what was configured and logged, and hands the standard back to the reader as the open question it always was: whether the output meets Safe Harbor, a Limited Data Set, or anything else is the data steward's determination, not Isocenter's.

  Supporting this, `IsocenterConfiguration` gained a `privacy_profile` field and `ConfigLoader.load_unified_config` now returns a fifth element naming the applied profile, so `phi_tags, machines, jitter, remove_private = ConfigLoader.load_unified_config(path)` raises `ValueError: too many values to unpack (expected 4)`; unpack five. A profile reference that fails to resolve was already logged and ignored, and is now dropped from the parsed config entirely, so it can never be reported as applied -- and `IsocenterConfiguration.save()` no longer relabels a `basic` config as `custom` on round-trip.
- **A `pip install`ed Isocenter audited against an empty PHI tag list and reported clean.** `setup.py` declared no `package_data`, so `isocenter/resources/*.json` -- `phi_tags.json`, `ctp_rules.json`, `redaction_rules.json`, `research_tags.json` -- shipped in neither the wheel nor the sdist. Nothing raised: every loader guards on `os.path.exists` and degrades to a default, so `ConfigLoader.load_phi_config()` returned `{}`, `PhiInspector` (`isocenter/privacy.py`) took that empty policy, and `session.audit()` found no PHI in data full of it. Verified directly against the built wheel: 0 default PHI tags loaded before, 6 after. The whole test suite passed throughout, because tests import from the source tree where the files are present -- which is why the new contract tests in `tests/test_packaging_contract.py` build the real wheel and sdist and read what is inside them, rather than reading `setup.py`.
- **Installing Isocenter dropped a top-level `scripts` package into site-packages.** `scripts/` carries an `__init__.py` so the benchmarks can import it, and a bare `find_packages()` swept it into the distribution: `top_level.txt` listed `isocenter` *and* `scripts`, a name Isocenter does not own and many other projects also use. `packages=` is now `find_packages(include=["isocenter", "isocenter.*"])`. `scripts/` remains in the sdist as source; it is no longer installed.
- **The sdist shipped a test suite that could not be collected.** setuptools swept in 106 `tests/*.py` modules but not `tests/conftest.py`, `tests/fixtures/` or `pytest.ini`, so `pytest` inside an unpacked sdist failed at collection. A `MANIFEST.in` now grafts the suite whole.
- **The build-based contract tests could not run in CI.** They shell out to a real `setup.py sdist bdist_wheel`, which needs a build backend in the *test* environment, not merely in pip's isolated build environment. Python 3.12 dropped `setuptools` from newly created virtualenvs, so `pip install -e ".[tests]"` left none behind and all five failed with `ModuleNotFoundError: No module named 'setuptools'`. This was invisible on any machine whose venv predates 3.12 or where setuptools was installed by hand, and failed every run on CI, which builds its environment fresh. `setuptools>=70.1` is now declared in the `tests` extra -- 70.1 being the first release with `bdist_wheel` built in, so no separate `wheel` dependency is needed.
- **Builds relied on setuptools' legacy fallback backend.** There was no `pyproject.toml`, so pip inferred `setuptools.build_meta:__legacy__`. A `pyproject.toml` now declares the backend and nothing else: distribution metadata stays in `setup.py`, because `tests/test_packaging_contract.py` parses that file to prove the declared dependencies match what the code imports, and a `[project]` table would silently override it with a second dependency list those tests cannot see.

## [0.6.1] - 2026-01-23

### Fixed

- **Integrity Check Regression**: Fixed a hash mismatch error in `SidecarPixelLoader` where the loader was initialized with a stale hash before the new pixel data was persisted.
- **Free-Threaded Stability**: Fixed a critical race condition in `SidecarManager` on Python 3.14t (free-threaded) where `ab` file mode caused `tell()` to report incorrect offsets. Switched to `r+b` with explicit seeking.
- **Regression**: Fixed `ValueError` in `isocenter/session.py` due to incorrect f-string brace escaping.
- **Regression**: Restored optional `Pillow` support in `isocenter/io_handlers.py` to prevent crashes when the library is missing.

### Changed

- **Code Quality**: Major refactor to align with Pylint standards (imports moved to top-level, docstrings added, formatting standardized).

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

# Changelog

All notable changes to the "Isocenter" project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking

- **`DicomExporter.save_patient` and `save_studies` are now one method, `write_tree`, and it says what it does.** Calls to either raise `AttributeError: type object 'DicomExporter' has no attribute 'save_patient'`. Replace `DicomExporter.save_patient(patient, out_dir)` with `DicomExporter.write_tree(patient, out_dir)`; `save_studies(patient, studies, out_dir, ...)` becomes `write_tree(patient, out_dir, studies=studies, ...)`. `session.export()` is unaffected.

  `save_patient` was a one-line wrapper around `save_studies`, and `save_studies` had no caller outside `io_handlers.py` itself -- two public names for one behaviour, which the pre-1.0 convention deletes rather than keeps.

  The rename is the point, not the collapse. This method applies **no de-identification**: no burned-in identifier scan, no subset filter, no redaction zones, no recoverable-identity disclosure. That is legitimate -- it is the serializer, and `session.export()` is the pipeline that runs the gates and then calls the same write -- but `save_patient` named it as though it were the export path rather than half of one. On a de-identification tool, a public method whose name invites you to use it for export and which silently skips every safety gate is the most consequential kind of misnomer. The new name and its docstring both say what is missing.

  It stays public because the need is real: building an object graph by hand and writing it out has no session behind it, which is how `scripts/generate_test_dataset.py` and the other fixture generators work.

- **The two export paths wrote the same instance under two different filenames.** `DicomExporter` named files by InstanceNumber `(0020,0013)` when it parsed as an integer -- `0001.dcm` -- while `session.export()` used the SOP Instance UID. A tree built by one could not be diffed against a tree built by the other, and InstanceNumber is not unique: two instances claiming the same number silently overwrote each other. Both paths now use the SOP Instance UID.

  `tests/test_api_coherence.py` already asserted the two paths agree, and passed throughout: it compared `path.parent`, so it saw matching directories and never looked at the filename inside them. It now compares full relative paths. (#50)

- **Tag keys now have one spelling, enforced where hand-authored keys enter the graph.** `set_attr` and `add_sequence_item` lowercase the tag before storing it, so `inst.set_attr("0008,103E", ...)` is retrievable as `attributes["0008,103e"]` and the two spellings are one attribute rather than two.

  A caller who wrote both casings previously got two entries and now gets one, last-write-wins. That is the behaviour change, and it is the point: a lookup written in one casing silently missed a value written in the other, and a missed key reads as *absent* rather than raising -- so the failure looked like ordinary missing data. Two of the three recorded encounters were silent PHI defects. The Basic profile keyed Series Description as `0008,103E`, the only entry with a hex letter, so the rule never matched and Series Description was never remediated despite the profile declaring it -- and it becomes the exported directory name (#41). A folder-naming helper read the uppercase spelling while its replacement read the lowercase one, silently dropping descriptions (#40).

  `set_attr` sits on the revision counter persistence reads, so the counter has its own assertions rather than relying on the suite to notice: an edit spelled with an uppercase hex letter still marks the item dirty, and so does one that normalises onto a key already present -- otherwise the edit would be made in memory and never written.

  The shipped sources no longer rely on the normalisation either. `session.py` stamped `0020,000D`, `0020,000E` and `0008,103E` onto exported instances and `validation.py` declared `0020,000E` a Type 1 requirement; all were safe only by coincidence, because their consumers parse tags with `int(x, 16)`, which happens to be case-insensitive. Nothing enforced that. A test now walks every shipped module's AST for uppercase tag literals, skipping docstrings and comments -- `privacy.py` and `io_handlers.py` both quote `"0008,103E"` in prose precisely to name the spelling that used to break things, and lowercasing those would delete the explanation. (#51)

### Fixed

- **Export folder names invented a word when a UID was missing.** `export_folder_names` built its disambiguating suffix as `(uid or "Unknown")[-5:]`, which is `"nknow"` -- a token that looks like real data, sorts among real UID suffixes, and tells a reader nothing. The suffix exists to separate two studies sharing a date and description; with no UID there is nothing to separate them *with*, so it now says `NoUID`. `series_number` had the same defect one line down: `str(None)` is `"None"`, reading as a series numbered "None" rather than one whose number was never recorded, now `NoNumber`. This is the shared helper, so it was wrong for `session.export()` too. (#53)

## [0.8.2] - 2026-08-27

### Changed

- **The Python matrix moved from every PR to the release that actually needs it.** Four versions ran on every pull request, and again on every push to `main`. In 30 runs the four-way matrix produced **zero** divergences that were not step timeouts -- it was answering a release question four times a day.

  PRs are now gated on **3.12 and 3.14t**. Those are two independent promises on orthogonal axes, and neither backs the other:

  - `python_requires=">=3.12"` is broken by 3.13+ syntax or a stdlib API absent a version earlier, and only 3.12 can show that. A newer version passing proves nothing about the floor.
  - Free-threading is broken by a data race or by code assuming GIL atomicity, and only a `t` build with `PYTHON_GIL=0` can show that. This is not a formality: `run_parallel()` chooses threads over processes when there is no GIL to escape, and everything heavy funnels through it, so a GIL-enabled interpreter never executes that path.

  3.13 and 3.14 with the GIL are interpolation between a passing floor and a passing ceiling. All four now run in `publish.yml` at release time, split by authority over the upload: `test-floor` (3.12, 3.14t) blocks it, `test-supported` (3.13, 3.14) only reports -- a red job turns the run red and the release still ships. Failing at that point is cheap, because nothing has been uploaded and the version number is not spent.

  `tests.yml` gained a `workflow_call` trigger with a `python-versions` input so `publish.yml` reuses it rather than duplicating the setup. The publishing step stays inline in `publish.yml`, because PyPI matches the *workflow filename* that carries it.

  `tests.yml`'s concurrency group now varies with the requested version list, and it does not cancel in progress when it was called rather than triggered. A reusable workflow's `github.workflow` is the *caller's* name, so both of `publish.yml`'s calls evaluated to `Publish-refs/heads/main` -- one group, `cancel-in-progress: true`, and the second call killed the first. The TestPyPI rehearsal caught it: `test-floor` was cancelled, `publish` was correctly skipped by its `needs`, and nothing was uploaded. The half worth fixing for is the other side of that race -- `test-supported` loses instead, `test-floor` passes, and a release ships having run half the matrix behind a green check. `publish.yml` sets `cancel-in-progress: false` precisely so an upload cannot be interrupted; a called workflow that cancelled anyway defeated that from one level down.

  `PYTHON_GIL` is now derived from the version (`endsWith(..., 't')`) instead of a parallel `include:` block, so adding a version cannot silently run a free-threaded build with the GIL back on -- which would pass while testing none of what it was added for.

### Added

- **`setup.py` states its free-threading support, and something checks it.** `Programming Language :: Python :: Free Threading :: 3 - Stable`. This claim could not be made anywhere else: 3.14t *is* 3.14 -- the `t` is the build variant, not the version -- and the wheel is `py3-none-any`, so neither `python_requires` nor the ABI tag carries it. Until now the promise existed only as a line in CI and a sentence in CLAUDE.md.

  `tests/test_packaging_contract.py` now asserts the PR gate still runs the floor `python_requires` declares, and still runs a free-threaded build whenever that classifier is present. The existing classifier test only caught advertising *below* the floor; neither caught the other direction, a claim nothing runs -- which matters more now that the gate is two versions rather than four, because narrowing it is a one-character change.

### Fixed

- **A de-identification exemption justified by a mechanism that moved four releases ago.** `privacy.py`'s private-tag sweep exempts `(0099,0010)` and `(0099,1001)`, and the comment said they were the reversibility service's Private Creator and encrypted identities. They are not, and have not been since v0.5.0 (`47278f8`) migrated reversibility to the Encrypted Attributes Sequence `(0400,0500)` -- an *even* group, which this sweep never touches, so reversibility was never at risk from it in the first place.

  This matters more than a wrong comment usually would, because the justification is the only thing standing between a de-identification exemption and a future reader deleting it -- or extending it on the same false reasoning to tags that are not exempt.

  The entries are not vestigial. `(0099,0010)`/`(0099,1001)` were the encrypted-identity payload in exactly one release, `gantry` v0.4.1, before both that migration and the rename to Isocenter, and the exemption keeps `remove_private_tags` from stripping the identities out of a store written by that version and leaving it unrecoverable with its own key. `DicomExporter._merge` is the other half of the same affordance: it hands those two tags explicit VRs on the way out, because `pydicom.datadict.dictionary_VR` raises for private tags and the fallback only logs. Each half now names the other, so removing one does not leave the exporter preserving a tag the sweep strips.

  Nothing changed but the comments, and the docstring of the test that pinned the exemption -- which had copied the false justification, putting it in the place most likely to be believed. (#113)

- **`config_tags={"0008,0020": "SHIFT"}` replaced the date instead of shifting it, and said nothing.** A tag's value has two shapes: a dict is a rule (`{"name": ..., "action": ...}`), and anything else is the tag's *display name*, leaving the action at `REPLACE`. That is coherent, and nothing stated it -- the docstring typed the parameter `Dict[str, str]`, which makes the string form look like the primary shape and the string itself look like the choice. A caller asking for a shift got `ANONYMIZED`, destroying the interval information shifting exists to preserve, with nothing raised and nothing logged.

  Constructing a `PhiInspector` now warns once per offending tag when a string value reads as an action name (`REMOVE`, `EMPTY`, `SHIFT`, `JITTER`), naming the tag and the rule form that would do what was asked. The docstring documents both shapes and says outright that the string names the tag rather than choosing what happens to it.

  **The behaviour is unchanged**: the string form still means `REPLACE`. Rejecting it would break calls that work today, and a caller may legitimately have a tag *described* as "Shift" -- so this reports rather than raises. The warning is emitted at construction rather than during the scan, where it would fire once per tag per instance. (#111)

- **A correct refusal announced itself as a fault on stdout.** `Instance.unload_pixel_data()` printed `DEBUG: FAILED TO UNLOAD <uid> - No file path or loader!` whenever it declined to clear pixel data. Declining is the guard working: data held only in memory -- edited but not yet saved -- cannot be re-loaded, so clearing it would be a silent discard rather than a free. `release_memory()` over a store with unsaved edits printed one of these per instance, straight to the terminal, with no way to quiet it. It is now a `debug` log line, matching `unload_waveform_data`, which has always declined silently. (#108)

- **The safe-export report suggested a config nothing could read.** When `export(check_burned_in=True)` found identifiers it printed the one actionable instruction in the whole report -- a config fragment resolving the findings -- as JSON with `//` comments and a trailing comma. That is not valid JSON, and user-facing configs are YAML only, so even valid JSON would have been the wrong format. Both defects had the same cause: JSON has no comments, so the per-tag counts had to be smuggled in as `//`.

  The fragment is now YAML in the shape `create_config()` writes, so it can be pasted into the file the user already has, and the counts sit above their entries as ordinary comments. Quoting is left to the YAML dumper -- a tag key contains a comma, and hand-rolling that is how the previous version produced a document nothing could load.

  `_suggested_tag_name()` recognised three tags by hand and labelled everything else `unknown_tag`, while `resources/phi_tags.json` already named more -- two spellings of one mapping, with the smaller facing the user at the moment it needed to be right. It now reads the shipped defaults, and the three names only `_scaffold_phi_tags` knew (Study Date, Patient Sex, Patient Age) moved to one constant both use. An unrecognised tag falls back to the tag itself rather than `unknown_tag`: the name is a comment to the reader, and a tag repeated is at least true, where three rules all called `unknown_tag` are indistinguishable.

  The tests that pinned the old output asserted its punctuation -- `'"action": "REMOVE"'`, and a closing-brace count. They now assert the fragment parses as YAML and round-trips through `ConfigLoader`, which is the property that was actually missing: the previous strings were exactly as intended and the document was still unusable. (#20)

- **`export(check_reversibility=...)` was accepted, documented and inert; it now checks something.** It defaulted to `True`, so every caller believed a safety check was running, and anyone passing it explicitly was asking for a behaviour and being told nothing. The implementation was a comment and a `pass`. #70 stopped the docstring promising a check and said the flag was inert, which was honest but left the parameter doing nothing.

  What it checks is real. `lock_identities()` embeds the original identifiers, encrypted, in an Encrypted Attributes Sequence (0400,0500) -- that is the point of reversible anonymisation, not a defect. But the exported file then looks de-identified while carrying everything needed to undo it, and nothing in the file says so to whoever receives the cohort. The export now warns when the files it is about to write carry those tokens, naming how many of how many, and records a `REVERSIBLE_EXPORT` entry in the audit log so the disclosure outlives the session rather than scrolling past in a terminal.

  Keyed on the data, not on whether `reversibility_service` is enabled in this session: a store can hold tokens embedded by an earlier one, and it is the bytes about to be written that matter. It runs against the export plan, so it counts what will actually be written -- after the subset filter and the burned-in scan have removed whatever they remove.

  **Nothing is withheld.** This reports; it does not block, and the exported files are byte-for-byte what they were before. `check_reversibility=False` is the caller stating they already know, and silences both the warning and the audit entry. (#76)

- **A child moved between two parents in a single save lost its own children.** `save_all` deletes rows the in-memory graph no longer holds, scoped one parent at a time and run inside that parent's pass. Move series `SE1` from study `A` to study `B` and save once: the walk reaches `A` first, finds `SE1` absent from its list, and deletes the series *and its instances*. `B` then re-inserts `SE1` -- but its instances are written only when they report unsaved changes, and untouched ones do not. The instances stayed intact in the session while their rows were gone, with nothing reporting it; it surfaced on the next reload.

  Two things had to change, because deferring the deletions alone was not enough. Re-parenting mutates the *parent's* list, which marks nothing dirty, so the child's own upsert never runs and its foreign key goes on naming the parent that no longer holds it. Each parent now re-points the rows it holds -- one statement, restricted to keys that actually differ, so the ordinary case updates nothing -- and every deletion pass is deferred until the whole save has been walked, so a scoped delete only runs once the row it might remove has had its chance to be claimed.

  Deletion is otherwise unchanged: a genuinely removed series still takes its instances with it, and the pruning precondition `prune_absent_patients` carries is untouched -- deletes stay scoped per parent rather than becoming a whole-store reconciliation. The limitation was documented in `_save_patient` rather than fixed; that comment is gone. (#77)

- **The codec priority list never did anything, and would have narrowed codec support if it had.** `isocenter/__init__.py` assigned a four-entry list to `pydicom.config.pixel_data_handlers`. On pydicom 3.x nothing reads it: decoding takes its backend from `Dataset._pixel_array_opts`, which defaults to `{"use_pdh": False}`, and the handler list is consulted only on the `use_pdh` branch. The assignment succeeded and the attribute held the list, so the file read as correctly configured -- silent precisely because the attribute is writable. `setup.py` requires `pydicom>=3.0.0`, so there was no supported version on which it had any effect.

  Worse than inert if it had ever been read: the assignment *replaced* pydicom's defaults rather than reordering them, dropping the `jpeg_ls`, `pylibjpeg` and `rle` handlers that ship with pydicom. The stated intent -- "GDCM first, then imagecodecs, then Pillow/numpy" -- would have removed RLE and JPEG-LS support to express it.

  The assignment is removed. Nothing replaces it: pydicom 3.x has no priority list to migrate to, since `pixel_array(..., decoding_plugin=...)` names a single plugin and the `pydicom.pixels` backend orders its own fallbacks per transfer syntax. Expressing a codec preference is a feature rather than a repair, and belongs with #33.

  **`imagecodecs` support is unchanged**, because it never came from this list: `Instance.get_pixel_data()` calls `isocenter.imagecodecs_handler` directly when pydicom fails to decode. The README and docs claims about JPEG Lossless and JPEG 2000 support remain accurate.

  `tests/test_codecs_strict.py::test_handler_registration` asserted that the dead assignment had happened -- a test pinning a no-op, whose own comments record the author's uncertainty about what it checked. It is replaced by one asserting the opposite contract: importing `isocenter` must leave `pydicom.config.pixel_data_handlers` untouched, because mutating it silently promises control Isocenter does not have. (#46)
- **A test that had never once run now runs.** `tests/test_discovery_integration.py` skipped unconditionally because `faker` was undeclared, making it the single skip in an otherwise green suite -- dead coverage reporting as a skip rather than as a gap, over redaction-zone logic on a de-identification product. The skip message compounded it by naming `pillow`, which is in `install_requires` and always present, sending anyone who investigated to the wrong place.

  `faker` is now in the `tests` extra, so its absence is an error rather than a silent loss of coverage. The remaining skip is narrow and accurate: this test discovers zones from *burned-in pixel text*, so it needs the `ocr` extra **and** a `tesseract` binary on PATH -- something pip cannot install, which is why it stays a skip rather than becoming a failure. That dependency was not identified in the issue; without OCR every machine yields zero zones and the assertions are vacuous rather than merely unrun.

  CI now installs `tesseract-ocr` and the `ocr` extra, so the test executes there rather than skipping. The graceful-degradation path is unaffected: it is covered by patching `HAS_OCR` (`tests/test_pixel_analysis.py`), not by leaving OCR uninstalled. The suite now reports **545 passed, 0 skipped**.

  The test needed no repair once it could run -- it passes as written, without spacy, because the regex fallback's `PROPER_NOUN_CANDIDATE` classification satisfies it. Leftover exploratory comments referencing a `reproduce_issue_v2.py` scratch script, and debug `print()` calls in the assertion loop, are gone. (#44)

- **`release_memory()` never freed waveform samples, and overcounted what it did free.** The traversal called `unload_pixel_data()` and nothing else, so the one operation Isocenter offers for reclaiming RAM did nothing for waveform-bearing instances. `Instance.unload_waveform_data()` has existed since #11 as the exact counterpart, with the same safety guard; nothing called it. Samples cache as int16 of shape (num_samples, num_channels) -- ~80 KB for a 10-second 12-lead, but ~104 MB for a 24-hour 3-channel Holter, so a cohort of Holter studies pinned hundreds of megabytes per record with no way to release it.

  The count was wrong in the same direction. Both `unload_pixel_data()` and `unload_waveform_data()` return `True` when there was nothing cached -- "already absent" is a successful unload -- so `freed` counted every instance it visited. A session holding nothing in RAM reported every instance as reclaimed. That is the same false assurance as not freeing at all: the user is told memory was returned when none was.

  `release_memory()` now unloads both, and counts an instance only when something was actually resident before the call. The report distinguishes the two ("pixels: N, waveform samples: M") rather than calling both "images", since a waveform is not one. Neither unload discards: each still refuses when nothing could restore the data. (#35)

- **A DICOM to DICOM round trip lost the waveform.** #9 fixed the ingest half -- samples reach the sidecar -- but `DicomExporter` never wrote them back. `populate_attrs` skips OB/OW, so `WaveformData` (5400,1010) never entered `attributes` in the first place, and nothing on the export path put it back. The result was a structurally plausible file: correct SOP Class, correct Modality, a complete Waveform Sequence carrying `NumberOfWaveformSamples`, `SamplingFrequency` and a full Channel Definition Sequence -- and no signal. Anyone de-identifying an ECG cohort and re-emitting DICOM got files that pass a structural check and contain nothing.

  The samples are now copied back from the sidecar **verbatim**, not re-encoded from the decoded array, so the round trip is byte-exact. This is the opposite of the pixel path, deliberately: pixels are decoded and re-encoded because redaction mutates them, and nothing in this pipeline mutates waveform samples. A re-encode could therefore only lose -- it would have to undo the int16 rebasing `decode_samples` applies to `US`, and any slip there shifts every value by 32768 while (5400,1006) still says `US`, silently. Copying the original bytes makes that mismatch structurally impossible rather than something a test has to catch. `SidecarWaveformLoader.read_raw()` carries the existing sha256 integrity check, so export inherits it.

  De-identification is unaffected: waveform PHI lives in tags -- channel labels, annotation concepts -- which are remediated in the object graph and rebuilt into the exported dataset as before. Two consequences worth stating: companded audio (`MB`/`AB`) now round-trips through DICOM export even though WFDB export still refuses it, because this path never decodes; and an instance carrying a Waveform Sequence with no samples in the sidecar now logs a warning rather than writing a plausible empty record in silence. (#34)
- **A multi-rate waveform lost every multiplex group after the first, without saying so.** Each Waveform Sequence (5400,0100) item is a multiplex group with its own sampling frequency and channel set -- how DICOM carries ECG at 500 Hz alongside respiration at 25 Hz. `ingest_worker` read item `[0]` and dropped the rest, so groups 1..n never entered the object graph at all: not in the session, not in the sidecar, not reachable by any export format. Ingest reported success, group 0's metadata was complete and correct, and export produced a valid record containing a strict subset of the acquisition. Nothing downstream could detect that the other groups had ever existed.

  Multi-group support remains deferred -- emitting multi-frequency WFDB correctly is real work, and this release does not attempt it. What is fixed is the silence. Ingesting a record with more than one group now logs a warning naming how many were discarded, and records a `DATA_LOSS` entry against the instance in the audit log, so the loss reaches the compliance trail rather than a log file the user may never open. `docs/waveforms.md` described this as an *exporter* limitation, which misplaced it: a reader could reasonably conclude the data was in the session and merely unexported. It is discarded at ingest. (#36)

- **An annotation's Concept Name carried operator free text into `annotations.json`.** `CodeMeaning` (0008,0104) was copied into each finding's `label` and the scheme-qualified `CodeValue` into `category`, both verbatim. Neither tag is in any privacy profile, so `anonymize()` never touched them: a `CodeMeaning` of `"ZQANNMEAN01 Jane Doe"` survived a full `create_config()` / `audit()` / `anonymize()` pass into the exported file as `"label": "ZQANNMEAN01 Jane Doe"`.

  This is the surface #39 missed. `CodeMeaning` sits one line above `note` in the same loop and has the same property -- for a **site-defined** coding scheme the cart populates it with typed text rather than a term from a vocabulary -- but #39 was scoped to Channel Label and Unformatted Text Value, so `docs/waveforms.md` was left describing two free-text surfaces when there were three.

  Both fields are now emitted only when the Coding Scheme Designator (0008,0102) names a published vocabulary, checked against `KNOWN_CODING_SCHEMES` in `isocenter.waveform`. **Coded findings are unchanged**: `SCT:164889003` still arrives with `"Atrial fibrillation"`, because SNOMED defined that term and an operator did not. For a site-defined scheme, `label` is omitted and `category` becomes `"uncoded"`.

  **The finding itself is still emitted** -- kind, sample positions and lead are untouched. Dropping it would have been safer in the narrow sense, but a reviewer seeing fewer marks on the strip than the record carried, with nothing indicating any were withheld, is silent under-reporting on a review tool, and the channel-label path already set the opposite precedent by substituting a positional token rather than deleting the channel. What is given up is the name, and the ability to tell two site-defined annotation types apart from each other -- which a local code never conveyed to Murmur anyway, since Murmur has no dictionary to resolve `99LOCAL:ZQ01` against.

  `include_annotation_text=True` restores both fields, exactly as it does for `note`. De-identification here is protocol-conformant rather than maximal, and that flag is where the protocol speaks: a study whose auditor has determined site-defined annotation labels may be released says so by passing it. The allowlist is not a second policy layer competing with the profile -- it answers a question a `{tag: action}` profile is structurally unable to express, namely "empty this *only when* the scheme is site-defined".

  Designators beginning `99` are reserved by DICOM for locally defined schemes and can never be recognised, and adding one to `KNOWN_CODING_SCHEMES` will not work: the prefix is checked independently of the set, because the tempting fix for "our site's codes are being suppressed" is to edit that list. (#58)

## [0.8.1] - 2026-08-25

Three defects of one shape: the exported artefact asserting something
that is not true, where a consumer has no way to tell. A study date that
was invented, an acquisition time that was misread, and a provenance
claim that was unearned. If you have exported WFDB records or used
study-date filtering with 0.8.0 or earlier, re-export them.

### Fixed

- **A four-digit Study Time was read as the wrong time.** `strptime` accepts one- and two-digit fields, and the formats were tried longest-first, so `%H%M%S` *matched* `"1430"` -- as 14:03:00, not 14:30. `"14"` matched `%H%M` as 01:04:00, and `"202304171430"` matched `%Y%m%d%H%M%S` the same way. Nothing raised, so nothing fell through to the correct format. `HH`, `HHMM` and `HHMMSS` are all legal for a TM, so this is ordinary data: an ECG recorded at half past two was exported claiming three minutes past, on the ordinary anonymized path. Parsing is now keyed by the stamp's length, in one place, for both TM and DT.

- **The WFDB header labelled real study dates as de-identified.** When a date survives but no time-of-day does, the date is preserved as a header comment. That comment read `# de-identified start date: ...` unconditionally -- so exporting without ever calling `anonymize()` wrote the patient's real, unshifted study date under a claim that it had been de-identified, in the one place a consumer would check. The wording now follows `study.date_shifted`: `de-identified start date:` only when a shift actually happened, `start date:` otherwise.

- **`_instance_only_datetime` invented midnight.** With a Study Date and no Study Time it appended `000000`, so the record line carried `00:00:00` as acquisition timing Isocenter made up -- indistinguishable to a reader from a study that really was acquired at midnight. It now reports the date as a comment and omits the record-line timestamp, matching what the sibling path has done since 0.7.0. A date whose time could not be parsed also no longer takes the date down with it. (#59)

- **A study with no Study Date was given one, near 1900, and it was exported as real.** Ingest filled a missing or unreadable date with the sentinel `19000101`, and nothing downstream could tell that apart from a recorded date. `SHIFT_DATE` jittered it like any other, so an instance that never had a date acquired a confidently de-identified one: in the exported folder tree (`Study_1900-01-01_...`), in the WFDB header comment (`# de-identified start date: 04/11/1899`), and in any date-based cohort filter. This is invention rather than leakage, and the more insidious of the two -- a consumer cannot distinguish "acquired in 1899, de-identified" from "we never knew". Absent now stays absent: the study carries no date, no shift is proposed for it, and the `NoDate` branch in `export_folder_names` -- which existed but was unreachable, because every study arrived carrying a parsed sentinel -- is now the one that runs. An unreadable date is logged rather than guessed at. (#60)


## [0.8.0] - 2026-08-25

**If you de-identified data with 0.7.x or earlier, re-audit it.** Two
defects in this release meant the session could retain or export
identifiers it reported as handled: the PHI scan never opened DICOM
sequences (#57), and the session database kept rows the session had
removed. Neither announced itself -- both reported success. The
individual entries below say what was affected.

### Added

- **The version is declared once, in `isocenter/_version.py`.** `setup.py` parses that file (without importing the package, which would require pydicom and numpy in the build environment) and `isocenter.__version__` re-exports it, so the distribution, the runtime value and the source tree cannot disagree. `tests/test_version_contract.py` holds the files that restate it to the same number -- `CITATION.cff`, `CHANGELOG.md` -- and rejects the old `0.0.0` placeholder.

- **`PhiStatus`: what the session knows about each entity, as recorded state.** Whether an item still carried identifiers used to exist only as a set built inside `_export_dicom` and discarded when it returned -- to ask "which items in my session still carry identifiers?" you had to re-run a scan, and the answer was never attached to the items. Every patient, study and instance now carries one of UNSCANNED, IDENTIFIED, REMEDIATED or CLEARED, stamped by `audit()` and by remediation, persisted, and readable through `session.phi_status_summary()`.

  **A status is valid only for the revision it was computed at.** Edit an entity after a scan and it reports UNSCANNED again rather than leaving a stale claim behind. This is structural, not a convention -- there is no way to read a status that describes content the entity no longer holds -- because a persisted REMEDIATED surviving a later edit would read as an assurance, which is worse than admitting nothing is known. For the same reason an unrecognised stored value, or a row written before the column existed, reads as UNSCANNED.

  What CLEARED does *not* mean: it says the configured **tag** scan found nothing. It is not a statement about burned-in pixel text, which is a separate scan (`scan_pixel_content`), and not an approval to release. Series carry no status at all -- the inspector reports on patients, studies and instances only, so a series has never been examined and does not claim to have been.

  Existing databases pick up the new columns through a guarded `ALTER TABLE` at open; no rebuild is needed.

- **Citation metadata**: `CITATION.cff` drives GitHub's "Cite this repository" button, and `.zenodo.json` supplies deposit metadata for archiving. Isocenter is the upstream half of a pair -- it builds and de-identifies the corpus that [Murmur Studio](https://doi.org/10.5281/zenodo.21077528) reviews -- so the deposit records that relationship, and work using both should cite both. De-identification tooling is a methods citation, not an acknowledgement. No DOI is claimed until the first Zenodo deposit exists: a DOI that does not resolve is worse than none, because it is what tooling copies into bibliographies.

- **`dev` extra**: `pip install -e ".[dev]"` installs the `tests` extra plus pylint. It is contributor tooling and nothing else -- `pip install isocenter` must never drag a linter or a test runner into a user's environment. `docs/developer_guide.md` documented this command for months while no such extra existed, so the command it gave contributors failed outright.

### Changed

- **`run_parallel()` split into named pieces, and its three execution paths stopped each keeping their own copy of everything.** A 183-line function wrapping a 124-line nested generator that reached back into its enclosing scope with `nonlocal` to rewrite four of its own parameters. The three strategies -- a caller's executor, a recycling `multiprocessing.Pool`, and a fresh executor -- shared four copies of the progress-bar setup and three near-identical readings of the environment between them. Settings are now resolved once into a `_Strategy` record before any work starts, each strategy is its own function, and the progress-bar wrapper exists once.

- **`SqliteStore.compact_sidecar()` split into named pieces.** 211 lines holding the blob-index queries, the file rewrite, the atomic swap, the offset update and two rollback paths; now 61 lines of ordering plus seven helpers. The logic was already carefully reasoned -- this moves nothing, it just puts the reasoning where it applies. `tests/test_compaction_recovery.py` pins the failure behaviour first, since none of the rollback paths were covered: a database write that fails after the file has been swapped must put the original file back, and must leave no `.compact.tmp` or `.compact.bak` beside the sidecar.

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

### Fixed

- **Remediation deduplicated on a display name, so some findings were silently dropped.** The key deciding whether a finding had already been handled was `(entity_uid, field_name)`, and both halves were wrong.

  `field_name` comes from the config entry's `name` and falls back to the literal `"Unknown Tag"` when one is omitted -- so two hand-written config entries without names collapsed into a single key and the second tag was never remediated. That is reachable in every released version; `create_config()` and the shipped profiles always write names, which is why it went unnoticed.

  And a finding raised inside a sequence carries the *instance's* UID, nested items having none of their own, so two annotation items on one instance holding the same tag collapsed as well. That half became reachable only when the scan started opening sequences, in this same release.

  The key is now the attribute the proposal actually writes, plus the path to the item holding it.

- **`anonymize()` reported a count of `None`.** `apply_remediation` returned nothing, so every run printed the literal `Anonymized/Remediated None tags according to policy.` -- the line an operator reads after de-identifying, carrying no information. It now returns the number applied, `anonymize()` returns it too, and remediations that fail are excluded from the count and summarised in one warning rather than only appearing as individual errors.

- **The release workflow's version gate had never once run, and failed the first time it did.** It called `python setup.py --version`, and the runner's Python has no setuptools -- so the step aborted on a real release rather than checking anything. It could not have been caught earlier: the gate is guarded by `if: github.event_name == 'release'`, so every TestPyPI rehearsal skipped it. Reading the version is now a separate step that runs on every path, including rehearsals, and it reads the version out of the built wheel -- the artifact that actually gets uploaded -- rather than re-deriving it from source. Only the tag comparison stays release-only, because a `workflow_dispatch` has no tag to compare.

- **`audit()` never opened a single DICOM sequence, so the scan reported clean on data it had not read.** Nested tags are where the free text lives: structured-report content, waveform annotations, operator notes. `PhiInspector` could scan them and had tests proving it -- but nothing reached that code from `create_config()` / `audit()` / `anonymize()`. The scan selected its targets from `Instance.text_index`, and that index is built once at ingest: it is not rebuilt when a session is loaded from the store, and it was not carried into the per-worker instance copies `audit()` actually scans, which took `attributes` and left `sequences` behind. Every real path therefore arrived with an empty index and silently fell back to a top-level-only scan. (#57)

  The scan now walks the instance and everything nested below it, so it no longer depends on an index that can be absent or out of step with the graph. Walking also drops the index's text-VR filter, which the top-level scan never applied either -- a configured PHI tag is now matched wherever it sits and whatever its VR. `remove_private_tags` reaches nested private tags for the same reason.

  **If you de-identified data containing sequences with 0.7.x or earlier, re-audit it.** The exports were written from a scan that had not looked inside them.

- **`anonymize()` could write a nested tag to the top level of the instance, leaving the real value in place.** The second half of #57, and the worse half: findings return from workers holding unpickled copies, and rehydration matched them on SOP Instance UID and rebound every instance-level finding to the instance itself. A finding raised against a sequence item was handed the wrong object, so the remediation wrote `""` to a newly invented top-level element while the PHI stayed where it was -- an export carrying the original value plus a decoy reading as remediated. Findings now record the path to the item they were raised against, and rehydration follows it. An item that has since been removed resolves to nothing and the finding is skipped with a log entry, rather than falling back to the instance.

- **`(0070,0006)` Unformatted Text Value now actually fires.** It is the one Basic-profile tag that lives inside a sequence, so it was inert for the reason above -- present in the profile, documented in `docs/waveforms.md` as "34 tags, 33 effective", doing nothing. The profile is now 34 effective, and `tests/test_nested_phi_audit.py` proves that entry remediates rather than asserting it does.


- **`redact()` reports what it did, and stops claiming success when it failed.** The whole body was wrapped in `except Exception`, which logged, printed `Execution interrupted`, and returned normally -- and the two lines above it printed `Remember to call .save() to persist.` immediately followed by `Execution Complete. Session saved.`, unconditionally and contradicting each other. Redaction is the step that removes burned-in PHI from pixels, so a run that removed nothing was indistinguishable from one that removed everything, to a caller and to a script. It now returns the number of instances it updated, and re-raises after logging. (#48)

  A shortfall is now reported rather than absorbed: a worker that fails returns `None`, and the result loop skipped falsy mutations without counting them, so a run where two of three images failed to redact looked exactly like a clean one. A malformed `ISOCENTER_MAX_WORKERS` is no longer fatal either -- its `ValueError` was raised inside that same handler, so a typo in a shell profile turned redaction into a silent no-op that reported success; it now warns and uses the default.

  The `DEBUG:` lines printed once or twice per instance are gone, along with a `try: ... finally: pass`.

- **A threaded redaction would have discarded every result.** Redacting an image gives it a new SOP UID, and the map used to apply results back onto the in-memory instances was keyed on UIDs read after the workers were dispatched. Under process isolation the workers mutate copies, so this happened to work by accident. In threads they share those very objects, so the map would have been keyed on the post-redaction UIDs and every result logged as unmatched and dropped -- a redaction that ran, reported no error, and changed nothing in the session. That is not a hypothetical configuration: `run_parallel` chooses threads on a free-threaded build, and on any build when `ISOCENTER_FORCE_THREADS=1` is set. The map is now taken before any worker starts.


- **`isocenter.__version__` could report a version the source tree was not.** It came from `importlib.metadata.version("isocenter")`, which answers "what is installed under this name" -- a different question, and in an editable checkout that had drifted from `setup.py`, a different answer. It also fell back to `0.0.0` when the package was not installed at all. That string is stamped into WFDB `annotations.json` as producer provenance, so a wrong version became a wrong claim inside a delivered dataset. (#17)
- **v0.7.0 was released with no changelog section.** Everything it contained sat under `[Unreleased]`, so the published release had no record of what was in it. The 39 entries present at the `v0.7.0` tag now appear under `[0.7.0] - 2026-08-25`; the rest remain unreleased. The release runbook already listed moving `[Unreleased]` as step 1 -- `tests/test_version_contract.py` now checks it, rather than relying on the step being remembered.

- **A malformed `ISOCENTER_*` tuning variable is now reported instead of ignored.** Each of the three integer settings was read inside `except ValueError: pass`, so `ISOCENTER_MAX_WORKERS=banana` silently reverted to the default. The only symptom was a cohort running at the wrong width with nothing anywhere saying why. A value that cannot be read is now logged as a warning naming the variable and the value.

- **Removed an unreachable timeout handler in `run_parallel`.** The recycling-pool path caught `multiprocessing.TimeoutError` from `next(iterator)` and re-raised it as `RuntimeError("Worker Pool Hung")`. `next()` with no timeout never raises it -- as the comment beside the handler acknowledged -- so the code claimed a hang-detection guarantee it did not provide.

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

- **A redaction zone that failed to apply was silently discarded** (#66). `RedactionService.apply_redaction_to_array` wrapped the pixel-zeroing itself in `except Exception: pass`, and the enclosing loop in a second handler returning `False`. A zone that could not be applied -- a read-only array, an unexpected dtype, a malformed ROI -- was skipped with no log at any level, and the return value was indistinguishable from "there were no zones to apply". `_export_instance_worker` ignores that return value and writes `arr.tobytes()` immediately afterwards, so the unredacted image was exported as though clean. A failed zone now logs the ROI and array shape at ERROR and raises; the export worker already contains per-instance failures, so the instance fails instead of shipping PHI.

- **The redaction knowledge base has never loaded.** `create_config` read `resources/redaction_rules.json` inside a `try` guarded by `except BaseException: pass`. Because `import json` appeared *later in the same function*, `json` was a function-local name and the `json.load` call raised `UnboundLocalError` on every single invocation -- caught and discarded by that handler. Every scaffolded config was therefore produced as though the knowledge base were empty, and no machine was ever pre-filled with its known redaction zones. Verified against `main`: a session with equipment `SN-SCANNER-01` produced a config containing none of that machine's zones before, and its full entry after.

- **Partial export failures reported as success.** `session.export()` captured `DicomExporter.export_batch`'s success count and discarded it, then logged "Export complete." unconditionally -- identical output whether 1200 of 1200 instances were written or 3. It now warns with the ratio and points at the audit log when any instance fails.

- **Exception handling narrowed across the package.** 17 `except BaseException` handlers became 2. `BaseException` catches `KeyboardInterrupt` and `SystemExit`, so Ctrl-C during a long ingest or export was swallowed by whichever handler happened to be on the stack. Each site now catches what it actually expects and logs or re-raises rather than passing. The two that remain are deliberate and commented: the sidecar compaction rollbacks, which must restore the backup file even when interrupted, and re-raise afterwards.

- **The documentation workflow deployed from any branch.** `docs.yml` triggered on `push` filtered by path but not by branch, and its job runs `mkdocs gh-deploy`, which publishes straight to the live site. Any push to any branch touching `docs/**` therefore deployed unreviewed documentation -- which is what happened twice from a feature branch during the isocenter rename, before the PR was merged. It is now `branches: [main]`.

- **`release.yml` deleted.** Named "Build and Publish", it fired on `release: published` -- the same event as `publish.yml` -- built the package, and uploaded it to the GitHub Actions artifact store. It published nothing. Both ran on the v0.7.0 release and only `publish.yml` did anything.

- **Documentation corrected against the code.** `docs/developer_guide.md` claimed Python 3.9+ (the floor is 3.12) and told contributors to run `pip install -e ".[dev]"` when no `dev` extra existed. `docs/architecture.md` titled its safety pipeline "The 8 Checkpoints" above a nine-item list that omitted **Report**, while `README.md` correctly described ten. Installation instructions still sent users to `git+https://github.com/kvnlng/Isocenter.git` now that releases are on PyPI. `mkdocs.yml`'s `edit_uri` pointed at a `docs_site/` directory that does not exist.

- **`docs/changelog.md` removed** (#52). It was a hand-maintained copy of `CHANGELOG.md` that had drifted 97 lines behind and stopped at 0.6.0. The documentation site's nav now links to the real file on GitHub: a changelog with two homes only ever has one that is current.

## [0.7.0] - 2026-08-25

### Added

- **Trusted Publishing workflow**: `.github/workflows/publish.yml` publishes to PyPI on a GitHub Release via OIDC, so no API token exists in repository secrets or on any developer machine. It refuses to publish a release whose tag disagrees with `setup.py`'s version, and installs the built wheel into a clean environment outside the source tree to prove it carries its own `resources/*.json` before anything reaches an index.

- **Developer Guide**: Added `docs/developer_guide.md` covering linting and testing standards.

- **Advanced Discovery**: Added `DiscoveryResult.to_dataframe()` and `get_density_matrix()` for data science integration.

- **Iterability**: Made `DiscoveryResult` iterable, yielding `(tag_string, count)` tuples.

- **DICOM Waveform Support**: Waveform IODs are now ingested, persisted, and de-identified as a first-class data type alongside pixel data.

- **WFDB Export**: `session.export(folder, format="wfdb")` writes `header(5)`-conformant PhysioNet WFDB records (format 16).

- **Murmur Annotation Bridge**: Waveform Annotation Sequence `(0040,B020)` is exported as `<record>.annotations.json` for [Murmur Studio](https://github.com/kvnlng/Murmur).

- **Export Format Registry**: `isocenter.exporters` provides a pluggable `Exporter` seam; `session.export()` dispatches on `format`.

- **Context Manager Support**: `DicomSession` now supports `with DicomSession(...) as session:`. `close()` releases a `ProcessPoolExecutor` and two threads holding sqlite handles; forgetting to call it leaks worker subprocesses. `__exit__` returns `None` and never suppresses an exception raised in the `with` body — but if `close()` itself also raises (e.g. one of its shutdown steps fails), it is `close()`'s exception that propagates, not the body's; the body's original exception is chained as `__context__` rather than raised directly, per normal Python exception-handling semantics.

### Changed

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

- **Waveform Data Loss**: Waveform Data `(5400,1010)` was silently discarded at ingest because `populate_attrs` skips all `OB`/`OW` VRs. Waveform IODs now round-trip intact.

- **Broken install from `setup.py` alone**: `isocenter/config_manager.py` imports `python-dotenv` unguarded, but it was declared only in `requirements.txt`. A `pip install` therefore succeeded and then failed with `ModuleNotFoundError: No module named 'dotenv'` on `import isocenter`. CI installed both files, so it never saw this. `tests/test_packaging_contract.py` now asserts that every unguarded module-scope import is declared.

- **`export(compression=None)` silently compressed anyway.** The now-removed legacy mapping only overrode `use_compression` when `compression is not None`, so a caller passing `compression=None` to mean "do not compress" fell through the guard and kept the parameter's default of `True`, producing a JPEG2000 export. `tests/benchmarks/run_stress_test.py`'s "uncompressed" benchmark arm computed exactly this (`compression=None` when `compress_export` was `False`) and had been measuring compressed exports. `use_compression=None` (now the only spelling) is plain falsy and correctly means no compression.

- **Acquisition DateTime survived `anonymize()`.** `(0008,002A)` was absent from `PRIVACY_PROFILES["basic"]` while the plain `(0008,0022)` Acquisition Date it duplicates was removed, so raw acquisition timing reached exported DICOM after a full audit/anonymize pass. The four Performed Procedure Step date/time tags had the same gap. (#38)

- **Operator free text reached the WFDB header.** Channel Label `(003A,0203)` is operator-typed and was written verbatim into the `.hea` signal description and the `annotations.json` `lead` field whenever a channel carried no coded Channel Source Sequence. It is now emitted only when it is a recognisable signal name (see `KNOWN_LEAD_NAMES` in `isocenter/waveform.py`); anything else becomes a positional `ch<N>` token, where `N` is the **zero-based** channel index -- DICOM's own ChannelNumber is 1-based, so `ch1` names the *second* channel. Fixed in the exporter rather than the privacy profile because the PHI scan is tag-gated, so a profile entry would not protect a bare `Session()`. (#39)

- **`WaveformChannel.wfdb_description()`'s no-argument return changed.** It is public API. Called without an `index` (no positional token available to fall back to), an uncoded, non-allowlisted channel now returns the literal string `"signal"` instead of the raw, unfiltered Channel Label -- consistent with the positional-token fix above, which this method also implements. Any caller invoking it without an index and expecting the old raw-label passthrough will observe this change.

- **WFDB header fabricated a `00:00:00` timestamp.** `(0008,002A)` Acquisition DateTime and `(0008,0030)` Study Time are both now removed by the Basic profile (see the Acquisition DateTime fix above), so on a fully configured, anonymized session the instance's real time-of-day is always unavailable -- but `WfdbExporter._start_datetime` combined the (real, shifted) study date with `time_of_day or datetime.min.time()`, silently substituting midnight and presenting it as real acquisition timing in every such record. header(5) does not support a date-only start time (base_date requires base_time, both per the PhysioNet spec and per `wfdb-python`'s own writer, and its reader cannot positionally disambiguate a bare date from a bare time), so the record line is now written with **both start time and date fields omitted** rather than a fabricated time, whenever a real date exists but no real time-of-day does. That date is not simply lost: since it's genuine, useful `SHIFT_DATE` output, it is instead preserved as a single `# de-identified start date: DD/MM/YYYY` header comment -- the same `DD/MM/YYYY` format the record line's own date field uses, sanitized through the same `_sanitize_description` path every other field on the line gets, not a new one. This is Isocenter's one deliberate exception to never emitting `#` comment lines. A session that never anonymizes, or whose instance still carries a real Acquisition DateTime, is unaffected -- its true time-of-day is written on the record line exactly as before, with no comment.

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

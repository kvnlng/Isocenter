# Changelog

All notable changes to the "Isocenter" project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A broken same-page anchor is a test failure now.** The five Quick Reference links in `docs/configuration.md` pointed at headings that had been renamed and were dead for months (#152); every gate that ran on them was green throughout. `mkdocs build --strict` is not the missing gate and cannot be made into one -- strict mode fails on a broken *file* reference, and a `#fragment` that resolves to nothing is not an error to it. So there was no zero-code option: `tests/test_doc_anchors.py` renders every root and site markdown page with Python-Markdown and asserts that each fragment link lands on an id the renderer actually emits. Run against `4fa1635`, the commit before the fix, it reports exactly those five; against `main` it reports none. 22 pages, 8 fragment links, ~115ms.

  **It lives in `tests/`, next to the two checks of the same shape.** `test_api_coherence.py` and `test_packaging_contract.py` both pin defects that would look identical from the outside and that nothing else would report, and both run on every PR rather than on a path filter. A `docs.yml` home would have been cheaper -- that workflow is already filtered to `docs/**` and `mkdocs.yml` -- and it would have graded the docs only on pushes to `main`, after review, on a workflow that deploys straight to the live site. A code-only PR that renames a heading in a docstring-generated page would pass its gate and break the site on merge. The check costs ~115ms on a suite that takes four minutes.

  **The renderer is the real one, deliberately.** A stdlib-only slugifier was written and run during the #152 review and reproduced both counts exactly -- and its emphasis-stripper ate underscores, producing `addrule` where Python-Markdown produces `add_rule`. Neither name was a link target, so it reached the right verdict for the wrong reason: a second answer to "what is the slug" that can disagree with the real one and did. That is the argument CLAUDE.md already makes about the retired `text_index`, so `markdown>=3.4` is declared in the `tests` extra (it arrives transitively with the `docs` extra via mkdocs, but the suite runs on `.[tests,ocr]`). It is a test-only import in `tests/`, which `test_packaging_contract.py` does not scan -- that test walks `isocenter/` -- so it is not an `install_requires` obligation.

  Two things the check gets right because #152 got them wrong first. It parses **rendered HTML rather than raw markdown**: a link whose *text* wraps across two source lines is invisible to a line-based regex, and `docs/architecture.md:95` is such a link. And it **reads `mkdocs.yml`'s `markdown_extensions` rather than assuming them**: today there is no `toc` slugify override and no `pymdownx.slugs`, so the default slug applies, but an extension this check does not recognise -- or a `toc` option that can replace the slug function -- stops the test with a message rather than being graded against slugs the site never emits. `pymdownx.snippets` is the one deferral: an anchor inside a `--8<--` include resolves against the including page, there are zero includes in the tree today, and the deferral is written down in the module docstring rather than left to be discovered. (#162)

### Changed

- **Private tags with a binary VR are a documented limitation now, not an open question.** Nothing about the behaviour changes: `populate_attrs` still skips every element whose VR is in `BINARY_VRS` (`OB`, `OW`, `OF`, `OD`, `OL`), those bytes still never enter the object graph, and `remove_private_tags: false` still cannot keep what was never held. What changes is that `docs/configuration.md` now says so where the flag is configured, and says it as a decision with reasons rather than as a caveat awaiting a fix. #125 closes on that basis.

  **Warn-plus-audit is the answer, not a placeholder for one.** The reporting half of #125 added the warning and the `DATA_LOSS` entry and left "whether these bytes should be retained at all is still open" in the docs; #146 carried those entries into section 3 of the compliance report. A reader who found the note in that state had no way to tell whether to plan around the limitation or wait for it to go away. The note now names both storage options and why each was rejected. Holding vendor binary in `attributes` puts an arbitrary blob permanently resident, which is the exact failure `BINARY_VRS` exists to prevent -- memory scaling on 100GB+ datasets rests on heavy arrays never being resident by default, and that guarantee outweighs an unread vendor block. Routing them to the sidecar means giving private tags an offset/length representation the EAV table does not have, plus a lazy loader and an export re-merge path, and `session.compact()` rewrites the sidecar and rewires every offset it knows about -- a class of offset it does not know about is silent corruption after the first compaction. Reporting the loss was cheap; that is not, and it is spec-first work rather than a docs note.

  **The note draws the line, because the previous one only drew half of it.** It said binary-VR private tags cannot be kept and left the reader to infer what happens to the rest, immediately under a bullet reading "`false`: Retains them" -- which reads as "private tags do not work". A table now states both directions: a private tag read with any VR outside `BINARY_VRS` is governed by the flag exactly as documented, swept when `true` and kept and written to the exported file when `false`. The table names that row "text, numeric, and `UN`" rather than by VR family, because `UN` is neither -- it is Unknown, raw bytes, and it is kept out of `BINARY_VRS` deliberately, on the assumption that a private `UN` is a small value rather than a blob. Verified end to end rather than read off the source: a file carrying `0009,0010` (`LO`), `0009,1001` (`LO`), `0009,1002` (`OB`), `0009,1003` (`SH`), and `0009,1004` (`UN`) ingests to a graph holding all but `0009,1002`, exports with all but `0009,1002` present, and files exactly one `DATA_LOSS` row.

  **The note also records a case the same test found by changing one constant: the drop depends on the transfer syntax.** Rewrite that fixture as `ImplicitVRLittleEndian` and `0009,1002` survives -- graph, export, and no `DATA_LOSS` row. Implicit VR carries no VR field, so pydicom resolves it from the standard dictionary, which has no entry for a private tag, and returns `UN`; `UN` is not in `BINARY_VRS`, so the blob is held in `attributes` in full. It is not only resident, it is *stored*: querying the index after that ingest finds the bytes base64-encoded in `instances.attributes_json`, the column the architecture guide's storage table labels "All Standard Tags (Even Groups)", while the EAV table that same row assigns to private tags holds only the private creator. That is the behaviour today and the docs say so rather than promising a drop that only half of real inputs get. It is also the exact case that undercuts the reason `UN` is excluded -- the exclusion assumes `UN` means "small private value", and under implicit VR it means "any private value, including the megabyte one". Documented, not changed: whether `UN` should stay out of `BINARY_VRS` is a behaviour question with a resident-memory blast radius, and this entry only closes the documentation. Filed as [#151](https://github.com/kvnlng/Isocenter/issues/151).

  It also says where the loss surfaces, which the old note did not. "Written to the audit log" is not an instruction: the reader who hits this reads a compliance report, not `sqlite3 audit_log`. Three places are named -- the session log, **section 3, Data Loss** of `session.generate_report(path)`, and `store_backend.get_audit_losses()` for the rows -- because a limitation whose evidence you cannot find is indistinguishable from a silent one.

  `docs/architecture.md`'s hybrid-storage table gains two paragraphs for the same reason. The three rows describe where standard tags, private tags, and pixels go, and a reader counting them concludes private tags are stored, full stop; the absent fourth row is the point, and resident memory -- the guarantee section 2 already makes about the pixel array -- is why it is absent. That file then hedges its own paragraph rather than delegating the hedge to `docs/configuration.md`, because a link reads as *more detail* and not as *the opposite*: it says outright that "stored nowhere" holds only when the VR is read as binary, and that "Private Tags (Odd Groups)" in the table describes where the *text* private tags go, since the real split is whether a value serializes to text. Neither is a wording problem and neither is fixed here; both are #151. (#125)

### Fixed

- **`store_backend.get_flattened_instances()` hung a `:memory:` store the moment anyone actually streamed from it.** The method yielded rows from inside `with self._get_connection()`, and on an in-memory store that context manager holds `_memory_lock` across its own yield. That lock is a plain, non-reentrant `threading.Lock`, so a generator parked between rows held the store's only database lock for as long as it stayed parked, and every other call on that store blocked forever -- including the audit-log writer thread, which reads through the same context manager and simply stopped draining its queue. `next(gen)` once, then `store.get_total_instances()`, and nothing returns.

  **The sting is that partial consumption is the advertised use.** The docstring offered the method for "streaming exports or analysis without loading the entire graph into RAM", and streaming *is* partial consumption. The two usages that ever worked -- the deleted `DicomSession.export_to_parquet` and the two tests standing in for it since #55 -- did `rows = list(generator)`, which runs the generator to completion, exits the `with`, releases the lock, and materialises the whole cohort first, defeating the one property the method exists for. So the suite was green because nothing in it had ever used the method as documented. `CHANGELOG.md` above points users here for the eight columns #55 removed, and `DicomSession(":memory:")` is an ordinary shape, so the path from the project's own migration note to a silent hang was three lines long.

  **Fixed by paging the walk, not by materialising it.** Rows come back `page_size` at a time (default 500, `SqliteStore.FLATTENED_PAGE_SIZE`), each page in its own `_get_connection` block, and nothing is yielded while a handle is held. Fetching everything under the lock and yielding afterwards -- the obvious one-line fix -- would have made the docstring a lie: a method whose whole claim is that it does not load the cohort into RAM cannot begin by loading the cohort into RAM. Paging keeps the claim and bounds the lock to one query.

  **`fetchmany` on a live cursor is not an option, which is why this is a keyset walk.** On a file-backed store `_get_connection` **closes** the connection when its block exits, so a cursor cannot survive to a second page at all -- the two backends could not share the code. Each page is therefore its own query, resuming from `WHERE i.id > ?` on `instances.id`. That column is an `INTEGER PRIMARY KEY` and so the rowid, making the resume a seek rather than the O(n^2) rescan an `OFFSET` would cost, and the three joins are all primary-key lookups. `i.id` is selected as the first column purely as the walk cursor and stripped before yielding: the row dicts keep exactly the eighteen keys they had, with no `id` among them.

  **Two behaviours changed with it, both deliberate.** Iteration is no longer a single snapshot -- writes landing between pages are visible and rows deleted between pages are not returned -- because the only way to keep one snapshot is to hold a read open across the yield, which is the defect. And the order is now defined, by `instances.id`, rather than whatever the join happened to emit. `page_size` below 1 raises `ValueError` at the call rather than on the first `next()` (the method is a plain function wrapping the generator so that it can): `LIMIT 0` returns an empty page, and an empty page is exactly how the walk decides it has reached the end, so a zero would have reported an empty store instead of failing.

  **The file-backed path was not deadlocking but was not clean either, and that half is pinned too.** `_get_connection` opens a fresh connection per call there, so nothing blocked -- but a parked generator held that connection open with a stepping `SELECT` on it. File stores run in WAL mode, where a reader does not block writers; what it blocks is checkpointing, since SQLite cannot reset the `-wal` file past the oldest live read snapshot. A generator left parked therefore let the WAL grow without bound. `PRAGMA wal_checkpoint(TRUNCATE)` reports `busy=1` against the old code and `busy=0` against the new one.

  **The regression tests must not be able to hang CI**, since the failure mode under test is a hang. The mechanism is pinned deterministically with no threads at all -- park the generator, assert `_memory_lock.locked() is False` -- and the behavioural probe runs `get_total_instances` on a daemon thread with a two-second join, asserting the returned *value* rather than merely that the thread finished, since a probe that raised also finishes. Either way the test closes the generator in a `finally`, which throws `GeneratorExit` in at the yield point, unwinds the `with`, and releases the lock: a regression here fails one test in two seconds instead of wedging the suite. `signal.alarm` was rejected as main-thread-only. (#164)

  Whether the method keeps its place at all is [#142](https://github.com/kvnlng/Isocenter/issues/142)'s question and stays open. This defect is independent of it: if the method survives it should not hang, and if it is deleted the method and its tests go together.

- **A waveform annotation on a discarded multiplex group came out of `annotations.json` wearing another signal's lead name, at another signal's sample position.** `isocenter/murmur.py` resolved every annotation against multiplex group 1 without ever reading which group the annotation named. Referenced Waveform Channels (0040,A0B0) is a list of `(multiplex group, channel)` pairs -- `_lead_for`'s own docstring said so -- and the code read `values[1]` for the length check, took the channel, and dropped `values[0]` on the floor. `waveform.channels` is the ingested group's channel list, so an annotation on group 2 channel 2 was labelled with **group 1's** channel 2. The Referenced Time Offsets fallback had the same root: `fs = waveform.sampling_frequency` is the ingested group's rate, and groups in a multi-group record differ in sampling rate -- that is what makes them separate groups. A 1.0 s offset on a 1000 Hz median-beat group was converted at the 500 Hz rhythm strip's rate and landed at sample 500.

  What a consumer of `annotations.json` received was a well-formed, schema-valid document with a plausible lead name at a plausible sample position, both belonging to a signal that is not in the record. Nothing warned, and no `DATA_LOSS` entry was written, because from the bridge's point of view nothing had failed. A mislabelled clinical mark is worse than a missing one for the same reason a wrong grade is worse than a missing row: it is not visibly absent.

  **An annotation naming a group that was not ingested is now dropped, and said out loud.** Ingest keeps Waveform Sequence item 0 and discards the rest (#36), so item 0's sample axis is the only one the exported record has. `build_annotations` drops the annotation and appends its distinct multiplex group ordinals to a caller-supplied `dropped_groups` list -- all of them, because (0040,A0B0) is VM 2-2n and one annotation may name several discarded groups, so reporting only the first under-reports the loss while the message's plural promises otherwise; `WfdbExporter._write_instance` logs a warning and files a `DATA_LOSS` audit entry -- warn-plus-audit, the shape #36 established for the group discard this follows from. **One entry per instance, naming the annotation count and the distinct groups**, not one per mark: #36's emitter reports its discard once with a count, and a cart that marks forty beats on a discarded group would otherwise put forty near-identical rows into section 3 of the compliance report -- a section nobody can read reports nothing, which is the bare `DATA_LOSS: 3` defect #146 opened against, relocated. The out-parameter carries ordinals rather than prose for the same reason `populate_attrs` hands back `(tag, vr)` and lets its caller word the message (#125): only the caller can see the whole instance, and only the caller has the logger and the store handle. It nests one list per dropped annotation, because the count of lost marks and the set of groups they pointed at are different axes of the same data. A two-group annotated record therefore files two `DATA_LOSS` rows -- one at ingest for the groups, one at export for the marks that referenced them -- which is two different losses, not a double count. Clearing only the lead would not have been enough: Referenced Sample Positions index the named group's samples, so a lead-less finding would still place a clinical mark at the wrong sample in the exported signal. The drop is required for sample-position annotations, not just the time-offset fallback.

  **The behaviour is the one that is correct whichever way #150 goes.** #150 asks whether the multiplex discard should still grade `PASS`, and #160 asks what to do with the discarded groups' sequence items; neither is decided, and nothing here presumes an answer. While only item 0 survives, an annotation on any other group is unplaceable and dropping it is the honest result. If multi-rate support later lands and groups 1..n stop being discarded, the check becomes "resolve against the right group" -- the group index is read and converted to an item index in one place (`_item_index`) already -- and the drop path stops firing on its own. The audit entry is scoped `STANDARD`, because Waveform Annotation Sequence `(0040,B020)` is an even group and the scope states what the element *was*, not how bad the loss felt (#146). Grading this one harder than the discard it descends from would decide #150 here, on the wrong ticket.

  **Two smaller readings of (0040,A0B0) were wrong alongside the group and are fixed with it.** The attribute is VM 2-2n, so one annotation may name several groups; only the first pair was ever read, which would have dropped a finding that genuinely applies to the exported signal as well. And DICOM numbers multiplex groups from **1** -- PS3.3 C.10.10.1.1 "Referenced Channels" defines the first value of each pair as "the ordinal of the Item of Waveform Sequence (5400,0100)", and its worked example writes an annotation covering the entire *first* multiplex group plus channels 2 and 3 of the *third* as `0001 0000 0003 0002 0003 0003` -- while #159's prose, and Isocenter's own fixture generator, count from 0. Those two conventions differ by exactly one at exactly the place the defect lives: reading "group 1" as ordinal 1 and writing the check 0-based would drop every annotation in every conformant file. The same section is the normative source for two behaviours that had been implemented correctly and asserted without a citation: a channel number of 0 means every channel in that multiplex group (so the mark is emitted with no `lead` rather than dropped or assigned one), and an odd trailing value cannot be paired. `_item_index` converts ordinal to item index once, and reads a group ordinal of 0 as the first group rather than rejecting it -- 0 is not a valid 1-based ordinal, so it cannot be confused with a group that survived, and rejecting it would discard every annotation a 0-counting source carries. `scripts/generate_waveform_test_data.py`'s `add_annotation` now writes a conformant `group=1` by default, gains a `group` argument, and gains a `time_offsets` argument, which is the only way to reach the seconds-to-samples fallback from a fixture.

  `docs/waveforms.md` records both halves beside the existing #36 note. The `.hea` cannot drift from `annotations.json` as a result: the header's record and signal lines are built from Waveform Sequence item 0 and the sample array, and this guard reads only (0040,B020). A dropped finding removes a mark; it moves no channel count, gain, rate, or sample count. Asserted directly -- the same two-group record exported with its annotation on group 1 and on group 2 produces byte-identical `.hea` files. (#159)

- **Private tags survive a session reload. They did not, and nothing said so.** `_split_core_and_private` routes odd-group values to the `instance_attributes` EAV table, and `SqliteStore.load_all` -- which `DicomSession.__init__` calls unconditionally on every open of an existing database -- never SELECTed from it. `load_vertical_attributes` existed, worked, and was unit-tested by `tests/test_vertical_table.py`; it had no production caller. `git log --all -S load_vertical_attributes` returns the commit that created the tier (075b7b9, hybrid metadata storage, then still `gantry/persistence.py`) and nothing else but the rename, and that commit adds the method with `tests/test_vertical_table.py` as its only caller. The read path has been unreferenced since the day it was written; there has never been a release in which the tier round-tripped.

  **What that lost.** `remove_private_tags: false` is a site saying "keep the vendor block", and it was honoured for exactly as long as the session stayed in memory. Save, close, reopen: the private tags were gone from the graph and gone from any export taken afterwards. No warning and no `DATA_LOSS` entry, reasonably -- nothing was lost at ingest and the write half succeeded. The values were sitting in the database, correct, and nothing went and got them, so a site inspecting the store saw its private tags and the export that ran after a reload had none of them.

  **The surviving subset was decided by the source file's transfer syntax, not by anything a site chose.** `_split_core_and_private` keeps `bytes` values inline in `attributes_json` because the vertical table's column is TEXT, and `attributes_json` *is* read back. Under implicit VR the private elements pydicom resolves to `UN` arrive as `bytes` and take that path, so the tags that came back from a reload were precisely the ones that took the unintended tier at ingest (#151). That is what made the loss look partial rather than total.

  One exception to "private tags survive a reload", stated because the headline does not cover it: a private tag whose value is an **empty list** produces no rows -- there is no atom to write -- so it is absent from the graph after a reload rather than present and empty. That is not a regression (it did not survive before either, along with everything else on the tier) and it is the same missing-arity gap as the one-element list further down, but a reader checking whether their vendor block came back should not have to discover it.

  **The read is one query, not one per instance.** The whole reason for the standard/private split is that 10k instances load without 10k joins, and `load_vertical_attributes` takes a single SOP Instance UID -- calling it in a loop would have put an N+1 on the default session-open path. `load_vertical_attributes_bulk()` is the new entry point: `load_all` calls it with no filter for one `SELECT ... ORDER BY instance_uid, group_id, element_id, atom_index` over the table, the same move as the `wave_refs` pre-fetch a few lines above it; `load_patient` passes its instance UIDs and gets them chunked 500 at a time to stay under SQLite's bound-parameter limit. It is fixed alongside `load_all` because it hydrates the same rows and had the same hole: it is `scan_worker`'s DB-rehydration branch, the one that rebuilds a patient from a `(db_path, patient_id, ...)` tuple rather than being handed a lightweight copy. `session.py:1155` builds only the object form today, so nothing inside the library reaches it -- but it is public API, and a second hydration path that disagrees with the first about what an instance contains is the shape of #142. `load_vertical_attributes` now delegates to it, so the reassembly logic has one home. Both callers pass their open connection: `_memory_lock` is a plain non-reentrant lock, so a nested `_get_connection` inside a `:memory:` load would deadlock outright.

  **A read failure is no longer silent.** `load_vertical_attributes` returned `{}` on a `sqlite3.Error`, logging and carrying on; the bulk loader it now delegates to lets the error propagate. An empty result from this tier is indistinguishable from an instance that genuinely has no private tags, so swallowing a read failure reproduces #158 exactly -- tags absent from the graph, absent from the export, and nothing saying so. `load_all` and `load_patient` have their own handlers and turn a store-level failure into a logged empty load, which is loud. The write path's `raise` is now load-bearing for the same reason and says so in a comment: its `DELETE` is what strips a private tag the graph no longer has, and swallowing that would let the caller's transaction commit the instance row and mark it persisted -- a save that failed to de-identify, reporting success.

  **The write half had to change too, and it is the half that mattered for de-identification.** `save_vertical_attributes` deleted only the keys it was about to re-insert, and `_build_instance_writes` skipped the call entirely when an instance had no private tags left. `REMEDIATION_REMOVE` deletes the key from `inst.attributes`, so after `remove_private_tags: true` an instance's private set is empty and neither the per-key delete nor the skipped call touched the stripped rows. Inert while nothing read them; the moment hydration does, a reload puts a vendor block back onto a graph that was de-identified on purpose -- a worse bug than the one being fixed, and shipped by the read alone. The write now replaces an instance's whole vertical set: delete by `instance_uid`, then insert, with an empty mapping meaning "clear it" rather than "do nothing".

  **VM > 1 was being flattened into a string that looked like a list.** `atom_index` exists so a multi-valued element becomes one row per atom, but the gate was `isinstance(val, list)` and pydicom hands back a `MultiValue`, which is a `MutableSequence` and not a `list`. Every real multi-valued private tag went down the scalar arm and was stored as `"['alpha', 'beta', 'gamma']"` in a single row -- which would have reloaded as a string that survives a glance. The gate now reads `(list, MultiValue)`, matching what `IsocenterJSONEncoder` already does for the other tier.

  **Hydration does not look like an edit, and the reason is `phi_status`.** The loaded values are assigned into `attributes` directly, exactly as `_deserialize_into` does, and never through `set_attr`. A reloaded instance reports `has_unsaved_changes == False` and keeps its stored `phi_status` rather than reading `UNSCANNED`. The second half is the one that needs the rule: `set_attr` advances `_revision`, and a status recorded against a revision the entity has since left reads as `UNSCANNED` structurally -- so an instance rebuilt from a row that recorded a conclusion would report that nothing is known about it. `has_unsaved_changes` is not at risk either way, because `mark_subtree_persisted()` runs last in both callers and absorbs any bump.

  That absorption is why the rule needed a test of its own rather than a comment. Both callers also apply these values ahead of their `record_phi_status` loop, so replacing the assignment with `set_attr` changes nothing observable through a reload: it leaves every round-trip test in the new file green, and 23 more across `test_persistence`, `test_vertical_table`, `test_save_all_contract`, `test_analysis_persistence` and `test_phi_retention`. The ordering is kept -- it is what makes the mistake survivable -- but it is defence, not the invariant, and an earlier draft of this entry and of the comments in `persistence.py` had it the other way round. `test_applying_a_loaded_private_tag_is_not_an_edit` asserts on the helper, where the rule is written, and is the only test in the suite that fails when that line changes.

  **Types are not restored, and this does not pretend to.** `save_vertical_attributes` writes `str(val)`, so a private `5` reloads as `"5"`. The table records no VR to reconstruct one from -- `value_rep` is hardcoded to `"UN"` on write and is not read here -- and inferring a type from the text would be a storage-shape decision. That is #154's, and it is left untouched, `value_rep` included. For the same reason a saved one-element list reloads as a scalar: no arity is recorded either. Both are pinned by tests so a later fix has to change them deliberately. Nothing here touches #151 -- `bytes` still never enters the EAV, so there is no path that loads such a value twice -- and a multi-valued private tag still does not reach an exported file, because `_fallback_encoding` has no arm for a sequence; the reload makes that path *more* reachable and it is filed as #165 rather than fixed here.

  **Upgrading does not clean a store that was already written, and it can go the wrong way.** The write half being wrong means a database saved by 0.8.x or earlier may hold private tag rows the graph does not -- a session that ran `remove_private_tags: true`, anonymized, and saved left the whole stripped block in `instance_attributes` untouched. Those rows were inert while nothing read them. They are not now: the first open of such a store after upgrading puts them back on the graph, and an export taken from that session carries them. It is not fixed here, because the discriminator is not where a migration could just read it. A stale row and a legitimate one are byte-identical, and the store carries no schema version -- no `PRAGMA user_version`, no version table -- so *which release wrote this database* is genuinely unknowable. *Was this tag stripped?* is a different question and the store does have material on it: `remediation.py` writes a `REMEDIATION_REMOVE` audit row per removed tag. That material is prose in a `details` string rather than a structured column, and the audit writer is a background thread, so it is best-effort evidence and not a ledger -- but it is evidence, and #172 carries it as a third option rather than this entry claiming there is nothing to work with. What a migration cannot do is guess: dropping the tier discards the vendor block of every site that set `remove_private_tags: false`, undoing this fix, and keeping it resurrects every stripped one. Filed as #172 with the options; the behaviour is pinned by a test so the choice is visible rather than latent. A site that de-identified a store before upgrading should re-run the privacy pipeline over it, or clear the table.

  `tests/test_private_tag_reload.py` is the coverage the round trip never had. `tests/test_private_tag_export.py` uses one in-memory session and never reloads; `test_save_all_contract.py::test_private_tags_are_split_out_into_the_vertical_table` asserts the write half only, correctly for what it is named after. Every test in the new file crosses `load_all`, and one of them counts the SQL statements that touch `instance_attributes` during a 40-instance load and requires exactly one. (#158)

- **A multi-group waveform exported a file that declared a multiplex group it did not carry.** Ingest keeps group 0's samples and discards the rest (#36), but the samples and the metadata arrive by different paths: `ingest_worker` pulls `WaveformData` out of `ds.WaveformSequence[0]` alone, while `populate_attrs` walks the *whole* sequence and builds a `DicomItem` for every group. The graph therefore held one item per group and the sidecar held one group's bytes, and `_export_instance_worker` wrote every item back:

  ```
  # before, on a rhythm + median-beat ECG:
  item 0: WaveformData present=True,  fs=500.0,  nsamp=5000, nch=8
  item 1: WaveformData present=False, fs=1000.0, nsamp=1200, nch=8
  # after:
  item 0: WaveformData present=True,  fs=500.0,  nsamp=5000, nch=8
  ```

  Waveform Data `(5400,1010)` is **Type 1** under PS3.3 C.10.9 -- required, and with no permitted absent-or-empty state. The exported file stated it had 8 channels at 1000 Hz over 1200 samples and carried none of them: a conformant reader is entitled to reject it, and one that trusts Type 1 without checking reads a sample count with nothing behind it. `ingest_worker` now drops the sequence items whose samples it discarded, so what is exported is a conformant single-group record.

  **This is not the #150 question and does not answer it.** #150 asks whether discarding a group should flip the compliance grade; that is open, and the `DATA_LOSS` entry is untouched here -- same text, same `STANDARD` `loss_scope`, still reported and still ungraded. What is fixed is a different defect that #150's investigation surfaced. It also changes the *shape* of the loss rather than its size: a record that drops a group entirely is smaller and honest, indistinguishable from a single-group record, which is exactly the state #36's warn-plus-audit was designed to make visible. A record that advertises data it does not have is not. The audit entry said a group was discarded while the file said the group was present, and the file is what a downstream reader parses.

  **Dropped at ingest, not at export, because the graph is what every consumer reads.** The DICOM writer, the WFDB record, the annotation bridge and the PHI report all resolve against `Instance.sequences`; patching the writer would have made one of the four honest and left the other three describing a group whose samples this pipeline does not hold. The objection to the ingest side is that the graph stops recording that another group existed in the source -- but the `DATA_LOSS` entry carries the original group count, which is the compliance record, and a graph that describes samples it cannot produce is not evidence, it is a second answer to "what does this record contain" that disagrees with the first. Nothing else in the pipeline moves: `SidecarWaveformLoader`, `DicomExporter` and `exporters/wfdb.py` all index `items[0]`, so on a two-group ECG the WFDB path emits the same single 8-channel 500 Hz record before and after.

  **The boundary, stated rather than papered over: a store written before this fix still exports the hollow item.** `ingest_worker` is the only place the drop happens, and it does not run again on a session opened from an existing index. A database populated between 0.8.2 and 0.9.0 holds two sequence items and one waveform blob; `persistence.py` hydrates that graph faithfully and the export writes it out exactly as described above. Verified, not assumed -- persisting a two-item graph, reopening the session from that database and exporting still yields `item 1: WaveformData present=False`. Nothing self-heals it: `session.compact()` rewires sidecar offsets and does not re-read source files, so re-ingesting the originals into a fresh index is the remedy today. `DicomExporter.write_tree()` has the same boundary for the same reason -- it is the serializer alone, so a hand-built graph carrying orphaned items is written as it stands; verified, not assumed. An export-side guard would cover it, and it is deliberately not added here -- it would put a second, quieter answer to "which groups does this record have" in the writer, which is the shape of defect this entry exists to remove, and dropping items at export without an entry of its own re-creates the silent truncation #36 closed. Filed as [#168](https://github.com/kvnlng/Isocenter/issues/168).

  The fix is also stable under every #150 outcome, which is why it could land ahead of that decision. If multi-rate support ever arrives (#150's option 3), the block stops firing on its own: the items are dropped *because* the samples are, and then they would not be. (#160)

- **The Data Loss `Scope` column could render wrong and the whole suite stayed green.** Hand mutation-testing the #146 branch left 4 of 21 mutations alive on `main`, all of them in the column a reader uses to trace `REVIEW_REQUIRED` back to the row that caused it. Nothing about the rendering changes here; what changes is that it is now held in place. This is the #132/#140 family: the code was right, and no test would have noticed it changing.

  **The column was checked for presence, not for correctness.** `test_the_report_says_which_losses_are_graded` asserted `LOSS_SCOPE_PRIVATE in loss_section` and `LOSS_SCOPE_STANDARD in loss_section` -- set membership over the whole section. With one private and one standard loss on the page, *every permutation* of the column satisfies that, including the one that prints each row's opposite. Appending `scope = {"PRIVATE": "STANDARD", "STANDARD": "PRIVATE"}.get(scope, scope)` after the scope is computed passed 9 of 9. The test now splits the table into cells and asserts the pairing -- the row naming `0009,1002` carries `PRIVATE`, the row naming `6000,3000` carries `STANDARD` -- which is what its own docstring promised and did not deliver. A column that can lie points the reader at the wrong element, which is the bare `DATA_LOSS: 3` defect #146 opened against, one layer up.

  **A malformed table passed too, in three ways.** Dropping the `Scope` header while still emitting four-cell rows renders the scopes under "Element"; markdown does not complain and neither did the suite. Narrowing the `| :--- |` delimiter to three cells, or deleting it outright, is worse: under GFM a delimiter row that disagrees with the header means the block is not recognised as a table at all, so section 3 degrades to literal pipe characters and the Scope column stops existing for the reader. `test_the_loss_table_is_well_formed` names the header cells, requires the delimiter to be present and to match the header's width, and requires every data row to agree with it too.

  The delimiter case is worth recording because of *how* it stayed alive. The first version of this test read the table through a helper that filtered the delimiter out by its content -- correct as far as it went, since identifying that line by content rather than by position is what keeps the parse honest when a mutation drops it, but it meant the one row of a markdown table that declares the column count was the one row no assertion could observe. The helper now returns that line instead of discarding it. A test-side blind spot hides a production defect exactly as well as a missing test does, and this one sat inside the test named for well-formedness.

  **`unrecorded` had zero test hits, and `loss_scope_for_tag()` had zero direct tests.** `grep -rn "unrecorded" tests/` and `grep -rn "loss_scope_for_tag" tests/` both returned nothing on `main`. Changing `loss[3] or "unrecorded"` to `loss[3] or ""` passed 9 of 9, and wrapping the tag parse to return `LOSS_SCOPE_STANDARD` on a malformed tag -- the exact thing that function's docstring argues against, since it would silently downgrade a real loss -- passed 36 tests across four files. Both are covered now: the word is asserted, because a blank cell reads as an omission a reader fills in with "standard", which is what the NULL exists to refuse; and the function's parity and its documented `ValueError` are asserted where they are written, rather than only through the emitters.

  **The upgrade path is covered for the first time.** The legacy-store test builds a store with only NULL-scope rows. A store that predates the column and then records new losses -- one ungraded row and one graded one on the same page, neither borrowing the other's scope -- is what real installations get, and nothing exercised it. That setup moves into a `_legacy_store()` helper carrying the reason it builds the 0.8.x table by hand instead of dropping a column: it is what makes the `ALTER TABLE` migration run, and this file is that migration's only coverage. Verified by mutation rather than asserted -- disabling the ALTER still fails `test_a_database_written_before_the_scope_column_existed_still_opens`, and now fails the two new legacy-store tests alongside it. #157 records that removing the migration "fails that test and nothing else"; that was true when it was written and is not any more, which is the refactor widening the migration's coverage rather than moving it.

  Test-only; `reporting.py`, `io_handlers.py`, and `persistence.py` are untouched. Each new assertion was checked against the mutation it exists to kill: applied, observed red, reverted, observed green. One result from that exercise is worth keeping: inverting `loss_scope_for_tag()`'s parity fails 4 tests suite-wide, but inside this file it is caught by the new direct parity test and by nothing else -- every report-level fixture here passes `loss_scope` in explicitly, so no amount of end-to-end assertion on the rendered table can see the rule itself change. That is the argument for testing the function where it is written. `io_handlers.py` is still not a `scripts/mutation_probe.py` target, which is how these survived a probe run in the first place -- left open on #132/#140 rather than widened here. (#157)

- **A run that dropped a private element no longer grades `PASS`.** The reporting half of #146 put every `DATA_LOSS` entry in front of the reader; the grade still ignored them, so a session that discarded a vendor block rendered `validation_status: PASS` above a section listing what it had lost. `generate_report()` now reads a per-entry scope and grades on it:

  ```python
  # before: a dropped private OB graded PASS
  validation_status="PASS" if audit_summary and not exceptions else "REVIEW_REQUIRED"
  # after: a dropped private OB grades REVIEW_REQUIRED; a dropped overlay does not
  ```

  **The rule discriminates by group, and the asymmetry is deliberate.** A private (odd-group) loss flips the grade. A standard one -- Overlay Data `(60xx,3000)`, the palette LUTs `(0028,120x)` -- does not. A private element that vanished is a vendor block nobody outside the vendor can size or identify, and one `remove_private_tags=False` may have been set specifically to keep; a run that discarded one has questions to answer. The standard ones are dropped from ordinary images by the thousand, and a grade that flipped on those would read `REVIEW_REQUIRED` for most real cohorts -- which is the failure #146 was opened about (a signal that is always the same value carries nothing), relocated rather than fixed. `loss_scope_for_tag()` is where the split is written down, with the reasoning, because a future reader meeting two rules will otherwise "simplify" them into one.

  **The classification is made by the emitter and stored, not re-derived.** `audit_log` gains a `loss_scope` column, written by whichever emitter dropped the element -- ingest, private/standard binary, or export -- and read by the report. The alternative was parsing the tag back out of the `details` prose, which three differently-shaped emitters write and one of which names no tag at all; that would have coupled the grade to message wording, so rephrasing a warning could silently change a compliance verdict. `ExportOutcome.losses` is therefore `List[Tuple[str, str]]` -- `(scope, detail)` -- rather than `List[str]`: the worker holds the tag, and by the time the parent logs the entry it holds only a sentence.

  Existing databases keep opening: the column is added by the `PRAGMA table_info` / `ALTER TABLE` path that already backfills `phi_status`. **A `DATA_LOSS` row written before the column exists reads NULL, is reported, and is not graded.** Back-filling it by parsing `details` is precisely the coupling the column exists to avoid, and guessing "standard" would silently downgrade a real loss. The report's Data Loss table gains a **Scope** column showing `PRIVATE`, `STANDARD`, or `unrecorded`, so a reader who sees `REVIEW_REQUIRED` can see which row caused it -- a grade nobody can trace to a row is the same defect as a count with no detail.

  **A second limitation, in the call order CLAUDE.md documents.** `generate_report()` grades the audit log as it stands when it is called, and two of the emitters write during `export()` -- `_merge`'s failure arm and the empty-waveform loss. CLAUDE.md's stated order is `... verify -> generate_report -> export`, so under it only the ingest-side loss can reach the grade: the export-side ones classify correctly, are stored correctly, and are then never consulted. `README.md` puts `export()` before `generate_report()`, and in that order all of them count. Varying nothing but the call order on one session gives `report_then_export -> PASS` and `export_then_report -> REVIEW_REQUIRED`. The two documents disagree, and the grade follows whichever one the reader obeyed. Since #148 that cost a missing row in a report section; grading on those rows escalates it to a wrong verdict, which is why it is stated here rather than left to be discovered. Changing the order, warning on it, or re-grading after export are all on the table and none is done here -- #153.

  **Knowingly deferred: the discarded waveform multiplex group still grades `PASS`.** #36 records "kept group 0 and discarded N" against Waveform Sequence `(5400,0100)`, an even group, so the rule above scopes it `STANDARD`. That is the one loss the rule sits badly on -- discarding N-1 groups is not routine the way an overlay is -- and it is filed as #150 rather than special-cased here. It is not fixable by widening the grading test, which would take every overlay with it; the scope is set at the emitter and states what the element *was*, not how bad the loss felt. Both the emitter and the grading site carry a comment saying so, because the mismatch reads as an oversight from either direction.

  One wording fix rides along, because the new Scope column would otherwise contradict it: the ingest emitter's message said "Private tag ..." for every element in the `dropped_private_binary` list, and #137 added standard elements to that list. The report would have shown `Private tag 6000,3000` on a row scoped `STANDARD`. The message is now chosen per tag. (#146)


- **BEHAVIOUR: importing Isocenter no longer silences pydicom's warnings for the whole host application.** `isocenter/__init__.py` ran `warnings.filterwarnings("ignore", module="pydicom.*")` before anything else. `filterwarnings` prepends to the process-wide filter list, so it won even over the host's own `-W` flag:

  ```
  $ python -W always::DeprecationWarning app.py     # app.py uses pydicom directly
  without importing isocenter -> 1 pydicom warning shown
  with    importing isocenter -> 0 pydicom warnings shown
  ```

  An application that imported Isocenter lost pydicom warnings **in its own pydicom code**, unasked, with nothing pointing at the cause. Choosing to silence a dependency's diagnostics is a reasonable thing for an *application* to do; it is not a library's call to make on its host's behalf, and `-W` is about as explicit as a user instruction gets.

  **The filter was redundant for the reason it existed.** Its comment said "e.g. strict UID validation", and that noise came entirely from this project's own test fixtures -- `SERIES_UID_1`, `SOP_UID_1`, and one private-creator name of ours -- not from user data. `pytest.ini` carries `ignore:::pydicom.*` independently, and with the library filter removed the suite is exactly as quiet as before. So it was paying for a test-suite problem in every user's process.

  **What you may now see:** pydicom `UserWarning`s about non-conformant values in *your* data, most likely `Invalid value for VR UI` on a UID that is not a valid DICOM UID. That is information a de-identification tool should not have been swallowing. Suppress it in your own application if you want it gone -- which is now your decision to make rather than one already made for you.

  It also hid pydicom's deprecation announcements, which are the only signal that says how to lift the `pydicom<4.0` cap in `setup.py`. That is how the calls fixed in #141 accumulated unnoticed.

  Tests run in a subprocess deliberately: `warnings.catch_warnings()` saves and restores the filter list, so a filter installed at import time is invisible from inside a `catch_warnings` block and an in-process test would pass either way. The structural assertion is scoped to filters naming pydicom rather than to "no new filters at all", because importing Isocenter pulls in numpy, urllib3, and requests, each of which installs its own. Making the fixture UIDs conformant, so the suite is quiet honestly rather than by filter, is left as separate work. (#144)


- **`DATA_LOSS` audit entries now reach the compliance report, in a section of their own.** #36, #125, and #137 each settled on the same pattern -- warn, *and* write a `DATA_LOSS` audit entry, because the log line alone is not a compliance trail. The entries were written correctly, carrying the tag and its VR, into a table `generate_report()` did not surface.

  What the report showed was a bare `| DATA_LOSS | 3 |` row in the audit summary, because `get_audit_summary()` groups by `action_type`, and nothing else. The detail could not reach the Exceptions section either: `get_audit_errors()` filters to `('ERROR', 'WARNING')`. A count with no detail is worse than silence -- it invites the reader to conclude the number is benign, and the person who hits this reads the report rather than `sqlite3 audit_log`.

  The report gains **section 3, Data Loss**, listing every dropped element with its instance and VR; Exceptions & Errors moves to 4 and Validation & Verification to 5. `SqliteStore.get_audit_losses()` is a separate query rather than a widened `get_audit_errors()`, and that separation is the point rather than an implementation detail -- see below.

  **`validation_status` was left unchanged by this half, and has since been changed by the next one** -- see the entry above. What this change refused to do was fold `DATA_LOSS` into `get_audit_errors()`: that would have surfaced the detail in one line and, as a side effect, flipped every ingest of a file carrying an overlay to `REVIEW_REQUIRED`, because the grade keys off `exceptions` being empty. The grade did need to move, but not for every loss alike, and not by reclassifying a routine drop as an error. The test that asserted `PASS` named itself as the one to change when that decision landed; it landed, and it is now `test_a_private_tag_loss_grades_review_required`. Its sibling, asserting the loss does *not* leak into the exceptions section, is unchanged and still holds. (#146)


- **Overlay Data and the palette color LUTs were dropped at ingest without a word, and the exported file still claimed to have them.** `populate_attrs` skips every element with a binary VR. #125 made that visible for *private* tags only, on the reasoning that the standard ones this rule catches are routed rather than lost. That was true of `(7fe0,0010)` and `(5400,1010)` and false of everything else: Overlay Data `(60xx,3000)` and the palette LUTs `(0028,120x)` are `OW`, standard, and written nowhere.

  The gate is now **"binary VR and routed nowhere"** rather than "odd group", so these are reported as `DATA_LOSS` entries naming the tag and its VR, exactly as private binary tags already were. `_ROUTED_BINARY_TAGS` names the two exclusions and why they exist -- widening the gate naively is not the one-line change it looks like, because reporting `(5400,1010)` would file a loss on every waveform ever ingested, and `tests/test_private_binary_ingest.py` now pins all three cases rather than the odd/even line it pinned before.

  **The descriptors are deliberately left in place.** An overlay's `OverlayRows`, `OverlayColumns`, and `OverlayBitPosition` are `US`, so they survive, and an exported file therefore declares a plane whose data it does not carry -- the shape 0.8.1 removed from the WFDB header. Stripping them anyway would be worse: Overlay Data is Type 1C, required only when the overlay is *not* in the Pixel Data, and the retired bit-plane mechanism put overlays in the unused high bits of `PixelData` addressed by `OverlayBitPosition`. Isocenter preserves `PixelData` intact, so for those files the overlay survives and its descriptors are the only pointer to it. Removing them would turn a correct passthrough into silent destruction. Reporting the loss does not foreclose sidecar routing, which is the same open question as #125's remaining half.

  This closes the reporting half only; the bytes are still dropped. And the entries land somewhere the reader of a compliance report will not see them -- `generate_report()` surfaces a bare `DATA_LOSS: n` count with no detail, and a session that dropped data still renders `validation_status: PASS`. That blind spot predates this change and degrades #125 identically; it is filed as #146. (#137)


- **Isocenter no longer calls pydicom APIs that 4.0 removes, and `setup.py`'s cap comment no longer names a blocker that does not exist.** Four changes, none of which alter a single output byte:

  - `pixel_analysis.py` imports `apply_voi_lut` from `pydicom.pixels` rather than `pydicom.pixel_data_handlers.util`. This was the only *unguarded module-scope* import of a package 4.0 deletes, and so the only one that would have failed at import time, before any caller could degrade gracefully.
  - `save_as(..., write_like_original=False)` is now `save_as(..., enforce_file_format=True)`. Verified as a pure rename rather than a behaviour change: byte-identical output on a valid dataset, and identical failure (`AttributeError`) on datasets missing the SOP Class UID, the SOP Instance UID, or the transfer syntax. The distinction matters because the export path catches broadly and turns a raise into `ExportOutcome(ok=False)`, so a newly-raising write would have converted silent success into reported failure.
  - Six `is_little_endian`/`is_implicit_VR` assignments are deleted. All six were no-ops. pydicom resolves encoding from the transfer syntax first and only falls back to these attributes when there is no valid one; every site had already set a transfer syntax on the line above. The two on sequence items were doubly dead -- a sequence item is never encoded independently, so pydicom writes it with the enclosing file's encoding and never consults the item's own flags.

  The cap itself stays at `<4.0`. 4.0 does not exist to test against, and `setup.py` is the project's shipping promise: a bound that no passing matrix backs is not a bound to widen. What changed is that the comment now says that, instead of pointing a reader at `isocenter/__init__.py` for a migration that had already happened.

  `tests/test_pydicom_deprecations.py` keeps the claim true. It has to work around two things that each silently destroy the signal, and either alone would have made it pass for the wrong reason: `export()` fans out through `ProcessPoolExecutor`, so a warning raised in a worker reaches neither the parent nor the caller (the #126 loss channel again) -- hence it drives `_export_instance_worker` directly; and `isocenter/__init__.py` sets a process-wide pydicom warning filter at import, which is why the suite never showed any of this. That filter is a separate defect, filed as #144 and deliberately untouched here. A companion test asserts the fixture still produces compressed output and a nested sequence, because if it stopped, the deprecation test would keep passing while covering one site instead of four. (#141)

### Removed

- **BREAKING: `Session.export_to_parquet()` is gone; call `export_dataframe()` with a `.parquet` path -- and update the column names you read.** `session.export_to_parquet(path)` now raises `AttributeError: 'DicomSession' object has no attribute 'export_to_parquet'`. The replacement is `session.export_dataframe(path, patient_ids=...)`, which sniffs the extension exactly as it always has.

  **The rename is the easy half.** The two methods did not produce the same frame, which is why keeping both was untenable rather than merely redundant. `export_to_parquet` read the *database* and emitted SQL column names; `export_dataframe` reads the *in-memory graph* and emits DICOM keywords. Downstream code that indexes the output by name breaks on a column lookup, not at the call site:

  | `export_to_parquet` | `export_dataframe` |
  | --- | --- |
  | `patient_id` | `PatientID` |
  | `patient_name` | `PatientName` |
  | `study_instance_uid` | `StudyInstanceUID` |
  | `study_date` | `StudyDate` |
  | `series_instance_uid` | `SeriesInstanceUID` |
  | `modality` | `Modality` |
  | `sop_instance_uid` | `SOPInstanceUID` |
  | `manufacturer` | `Manufacturer` |
  | `model_name` | `Model` |
  | `device_serial_number` | `DeviceSerial` |

  Eight columns have no counterpart and are simply gone: `series_number`, `sop_class_uid`, `instance_number`, `file_path`, `pixel_offset`, `pixel_length`, `compress_alg`, and `attributes_json`. `attributes_json` is the closest to recoverable -- `expand_metadata=True` spreads the same attributes into real columns instead of one JSON blob. The storage-internal four (`pixel_offset`, `pixel_length`, `compress_alg`, `file_path`) were sidecar bookkeeping that a metadata inventory had no business publishing. If you need them, `store_backend.get_flattened_instances()` is still there and still returns exactly those rows.

  **Why the in-memory graph won.** It is what every other stage of the pipeline reads, so it is the only source that agrees with what `anonymize()`, `redact()`, and `export()` just did. The database answers a different question -- what was last *saved* -- and `export_to_parquet` papered over the gap by calling `self.save()` first, silently committing pending edits as a side effect of what a caller had every reason to read as a read. An export must not decide to write.

  The performance argument for the database path was not real either. Its docstring promised streaming, and it did open a generator over `get_flattened_instances`, but the very next statement was `rows = list(generator)` before handing the whole list to `pd.DataFrame`. It materialized the full cohort exactly like the method it was supposed to scale past.

  Two behaviours were worth keeping and moved across: `export_dataframe` now takes `patient_ids` (`None` means everyone; `[]` means nobody -- a filter that matched nothing is not an absent filter, or a caller computing an empty cohort would export the entire dataset), and it creates a missing output directory instead of letting pandas raise `Cannot save file into a non-existent directory`. `get_cohort_report()` takes the same `patient_ids` argument, since that is where the filtering belongs.

  One behaviour deliberately did not move: `export_to_parquet` returned early without writing anything when the cohort was empty, logging a warning. `export_dataframe` writes the empty frame. A downstream job that reads its input on a schedule should find an empty file, not last week's file.

  That is only useful if the empty file has a schema, so `get_cohort_report` now names its columns (`COHORT_REPORT_COLUMNS`) instead of letting `pd.DataFrame` infer them from the rows. A bare `pd.DataFrame([])` has no columns at all, which means the obvious `df[df.Modality == "CT"]` raises on an empty export and works on every other one -- a break that surfaces only when the filter happened to match nothing. The names are applied only when there are no rows; with rows, pandas takes the union of the dicts' keys, and forcing the list there would clip `expand_metadata`'s attributes back out. (#55)

- **BREAKING: `Session.scan_for_phi()` is gone; call `audit()`.** `session.scan_for_phi()` now raises `AttributeError: 'DicomSession' object has no attribute 'scan_for_phi'`. The replacement is a rename at the call site and nothing else: same argument, same `PhiReport` back.

  It was a one-line body returning `self.audit(config_path)`, under a `# DEPRECATED` banner and a docstring reading "Legacy alias for audit()". That is the same shape as the `safe=`/`compression=` aliases #40 removed, and it was missed only because it is a method rather than a parameter.

  The internal pre-export safety scan called it too, so `_report_recoverable_identities`'s sibling in `_export_dicom` now calls `audit()` directly -- one spelling in the code as well as in the API.

  `test_api_coherence.py` pins the absence by name rather than by comparing signatures: a signature check would keep passing if the alias came back with a *changed* signature, which is a worse state than the one being removed. (#55)

### Fixed

- **`remove_private_tags=False` could not keep a private tag with a binary VR, and did not say so.** `populate_attrs` skips every element whose VR is in `BINARY_VRS`, which is right for pixels and waveforms -- they belong in the sidecar, and holding them in `attributes` would undo the memory scaling the design depends on. A private `OB` is collateral: a vendor block routinely carries one, and it never reaches the object graph at all, so there is nothing left for the flag to keep by the time it is read.

  The flag therefore reported success on a tag that had been gone since ingest. Each dropped element is now logged and written to the audit log as a `DATA_LOSS` entry naming the tag *and its VR* -- "a tag was dropped" is not actionable; the VR is what says whether it was a four-byte serial number or a megabyte of vendor telemetry. `docs/configuration.md` states the limitation where the flag is documented.

  This reports the loss; it does not change it. Keeping the bytes is a real decision -- an arbitrary private `OB` can be megabytes, which is what `BINARY_VRS` exists to keep out of resident memory, and routing them to the sidecar means giving private tags an offset/length representation the EAV table does not have. That half of #125 stayed open until the #125 documentation entry in this same release settled it: warn-plus-audit is the answer, and storage is not planned.

  The report travels in `meta` rather than a ninth slot on `ingest_worker`'s return tuple, which is the channel #36's multiplex-group loss already uses: the worker may be in a subprocess with no store handle, so the parent records it. Same constraint as #126, on the other end of the pipeline. Nested vendor blocks are covered -- the accumulator recurses with `process_sequence`, or the report would cover the top level only and read as "nothing else was dropped". (#125)

  Standard binary elements -- Overlay Data, palette LUTs -- are dropped by the same rule and are deliberately *not* reported here, because that is a documented design choice rather than a broken promise, and dropping them cannot leak. `test_a_standard_binary_element_is_not_reported` pins that boundary and says why. Filed as #137 rather than folded in.

### Removed

- **BREAKING: `Instance.text_index` is gone, and `populate_attrs` no longer takes a third argument.** `inst.text_index` now raises `AttributeError: 'Instance' object has no attribute 'text_index'` -- on assignment too, since `Instance` is a slots dataclass, so no shim can quietly reintroduce it. `populate_attrs(ds, item, index)` raises `TypeError: populate_attrs() takes 2 positional arguments but 3 were given`; drop the argument. `entities.clone_sequences()` returns the clone dict alone rather than `(clones, mapping)`, so unpacking it into two names raises `ValueError`.

  The index was described on the class as "Index of all text-based nodes for O(1) PHI scanning". It had not been that since 0.8.0. #57 moved `PhiInspector._scan_instance` to a structural walk precisely because the index was empty on every path that mattered -- built once at ingest, never rebuilt on load from the store, not carried into the worker copies `audit()` scans. After that fix nothing in `isocenter/` read it. Two functions still built it and four test files still asserted on it.

  So this removes a stored second answer to "where does text live", which could disagree with the object graph. That disagreement was not hypothetical: it *was* #57. The structural walk is not a new risk being taken here -- it is what 0.8.0 and 0.9.0 have shipped. If an index is ever wanted for speed, it should be derived per scan rather than stored, and the comment in `privacy.py` now says so.

  `populate_attrs`'s text-VR filter (`TEXT_VRS`) goes with it and is not replaced. It only ever decided what to index, never what to scan, and the top-level scan never applied it: a configured PHI tag is a configured PHI tag wherever it sits and whatever its VR.

  Removed rather than deprecated per the project's pre-1.0 convention. The attribute was `init=False, repr=False` and commented "Transient", so it was never part of the constructor or the repr. (#84)

- **A test asserting a claim the code contradicts.** `test_channel_label_is_indexed_for_phi_scanning` asserted that Channel Label `(003A,0203)` appeared in `text_index`, under the docstring "Free-text SH/UT inside the waveform sequence must reach the inspector". It does not, by design: `waveform.py` says so in as many words -- "The check lives here rather than in the privacy profile on purpose: the PHI scan is tag-gated, so a profile entry protects only sessions that loaded a configuration. A bare `Session()` would still leak." The label is protected by a recognisable-lead-name allowlist in the WFDB writer, not by the PHI scan, and `test_uncoded_channel_label_free_text_never_reaches_the_header` in the same file already covers that end to end.

  The test therefore asserted a mechanism nothing consumed, in service of a claim the design rejects, while the real protection was tested elsewhere. That is the shape that let #57 ship for two releases: a green test pinning an index rather than an outcome. Deleted rather than rewritten, because its behaviour is not uncovered.

  `test_sr_recursive_indexing` is replaced by `test_the_scan_finds_the_same_tag_at_every_level_it_appears`, which keeps the shape worth keeping -- PatientName at the top level *and* inside a nested Content Sequence item -- and asserts the scan raises a finding for each, on distinct entities. A scan that deduplicated by tag would have satisfied the old assertion and left the clinician's name in the report. (#84)

### Fixed

- **Data lost on the way out of an export reached nobody.** `_merge` drops an element it cannot encode and warned; the export worker writes a Waveform Sequence with no samples in it and warned. Both are real losses in the artefact the caller keeps, and both were reported only to a logger -- from inside `_export_instance_worker`, which `session.export()` runs in a subprocess.

  A subprocess is not a quieter channel, it is a different one. The existing test for the empty-waveform warning had to drive the worker directly and says why in its own docstring: through `session.export()` the worker's logger lives in a child process where `caplog` cannot reach it. That is not a testing inconvenience -- it is the finding. Through the only path a user takes, the warning reached neither the audit log nor the caller's log handlers. The element vanished and nothing anywhere recorded that it had.

  #36 settled what to do at ingest: warn *and* write a `DATA_LOSS` audit entry, because "the log line alone is not a compliance trail". The export side now uses the same channel. Since the code that notices the loss cannot hold a store handle, the loss travels instead: `_export_instance_worker` returns an `ExportOutcome` -- written or not, plus what was lost -- and the parent, which has the backend, logs it and writes the audit entry.

  The worker's return contract changed from `Optional[bool]` to that dataclass, which is why this is not a one-line fix; it is private, and both `run_parallel` consumers in `io_handlers.py` were updated with it. `error` is a field on the outcome rather than a bare exception returned in its place, so the worker has one return shape and a call site that forgets to check cannot raise `AttributeError` on the failure path -- the path that only runs when something has already gone wrong. Sites still filter for `Exception`, because `run_parallel` can return one of its own when a worker dies.

  Warning and auditing are deliberately not the same condition. `DicomExporter.write_tree()` is the serializer, with no session behind it, and can never supply a backend; gating the report on one would make every fixture generator in `scripts/` lose elements in silence. The warning is unconditional, the audit entry is what a store adds. (#126)

- **Three behaviours in `remediation.py` that nothing was holding in place.** The module that *applies* de-identification had 14 of 35 sampled mutations survive once #106 gave the probe operators that reach straight-line code. The code was right in each case below; no test would have noticed it changing. Now 11 of 35, and the three closed are the ones with consequences.

  `add_global_deid_tags` writes the De-identification Method Code Sequence `(0012,0064)` as a triple: Code Value `113100`, Coding Scheme Designator `DCM`, Code Meaning. Deleting the designator left `113100` naming nothing -- code values are unique only within a scheme, so a reader cannot tell the Basic Application Confidentiality Profile from any other registry's 113100. That is the 0.8.1 family exactly, and here the false assertion is the de-identification conformance claim itself.

  `_resolve_patient_id` had no caller in any test. It is the input to deterministic date jitter, which is per-patient so that intervals survive; returning None where an ID exists is how the jitter collapses to a single shift for everybody. That is #104's failure reached from upstream.

  And `Patient`, `Study` and `Series` are `TrackedEntity` but not `DicomItem`, so they have no `set_attr` and remediation reaches them through a different branch that writes the Python attribute directly. Every test that drove remediation through an `Instance` stopped at the first branch, so that path was never exercised -- a mutation there survived the entire 608-test suite.

  **Not fixed, deliberately.** Three surviving `entity.mark_modified()` mutants are equivalent mutants individually: `record_phi_status()` advances `_revision` too, so deleting either mechanism alone changes nothing observable and no test can kill it. The exposure is a refactor removing *both*, which reproduces the bug the line-206 comment records -- PHI stripped in memory, instance reports no unsaved changes, next save skips it, identifier stays in the database. `test_remediating_an_instance_leaves_it_needing_a_save` and its `Patient` counterpart pin the invariant the two jointly provide, so losing both fails rather than shipping. The remaining survivors are `logger` calls and comparison flips on logging severity; they are listed in #132 rather than papered over with assertions on log text. (#132)

- **`TARGETS` was still missing coverage an import scan cannot see.** `tests/test_phi_retention.py` is the regression test for the PHI-stays-in-the-database bug, and it reaches `remediation.py` through `session.anonymize()` rather than by importing it -- so #106's contract test, which follows imports, could not find it either. Added as a curated extra alongside `test_remediation_actions.py`, with the limitation named where the list is defined.

### Changed

- **`mutation_probe` can now see modules that do not branch, and says so when it cannot.** It reported `isocenter/crypto.py` as **0 mutation sites, 0 survived** -- which, in a table next to `privacy.py 11/36`, reads as the healthiest row and actually meant the module was never measured. `crypto.py` is the reversible-anonymisation core: 73 lines of key derivation, encrypt and decrypt with almost no branching, so the three operators the probe had -- comparison flips, `and`/`or`, boolean constants -- had nothing to find.

  Three operators now reach straight-line code: dropping a `not`, replacing a returned value with `None`, and deleting a bare expression statement (rewritten to `pass` rather than removed, because deleting the only statement in a body leaves an AST that will not unparse, and that would look like a skipped mutant instead of a bug in the probe). `crypto.py` goes from 0 sites to 6, and the suite kills all six -- a clean result that now means something. A module with 0 sites prints `NOT MEASURED` instead of a zero.

  Still unreached, and named in the docstring so the next `0/0` is not mistaken for coverage: argument swaps between same-typed parameters, string and bytes constant mutation, and exception-handler removal.

- **The probe was running a fraction of the tests that cover its targets, and reporting the difference as survivors.** `TARGETS` listed 6 test files for `privacy.py`; 15 exercise it. `tests/test_config_tags_shapes.py` was added for #111 and never added to the list, so two mutations of the warning it covers were reported as `SURVIVED` -- a phantom gap in the de-identification core, of exactly the kind that costs a reviewer a real investigation. With the complete list, `privacy.py` drops from 10 survivors to 5. Half were the tool's own bookkeeping.

  `tests/test_mutation_probe_targets.py` now fails if a test file imports a target module and is not listed. Extra entries stay allowed and are used: `test_remediation_actions.py` exercises `remediation.py` without importing it, which no import scan can see. The same test pins CLAUDE.md's module-to-test table to `TARGETS`, since CLAUDE.md calls that dict "the maintained version of this list" and the two had drifted apart -- one copy of a mapping is a fact, two is a bet on whoever edits next. (#106)

### Added

- **A test that can never run is now a test failure.** `tests/test_discovery_integration.py` skipped unconditionally for months because `faker` was undeclared -- the single skip in an otherwise green suite, dead coverage over redaction-zone logic, reporting as "not applicable here" rather than "nobody has run this since it was written". `tests/test_skip_contract.py` pins the rule that would have caught it: a skip's condition must be capable of being both true and false in a documented environment.

  For a skip gated on importing a module, that means the module has to sit in exactly the optional extras. In `install_requires`, its absence is a broken installation and `import isocenter` would already have failed. In the `tests` extra, `pytest` itself ships there, so anything that can run the suite at all already has it. Declared in no extra at all, nothing can install it, so the skip can never be false -- the `faker` case. What remains is `ocr`/`nlp`/`docs`, which a user may legitimately lack, and `ocr` additionally needs a `tesseract` binary pip cannot supply. The rule reads the extras rather than a hand-kept allowlist, so declaring a new one is enough.

  Applying it found three live instances, together guarding 40 tests. `test_query_export.py` skipped on **pydicom**, a hard dependency -- a skip that could only ever fire in an installation too broken to import the package, hiding that behind a green run. `test_wfdb_conformance.py` (14 tests) and `test_murmur_annotations.py` (23 tests) skipped on `wfdb` and `jsonschema`, both declared test dependencies: a contributor who forgot `pip install -e ".[tests]"` lost 37 tests and still saw green. All three now import directly, so a missing package is an error naming itself.

  Two things keep the guard from going quietly vacuous, which is the failure mode it exists to prevent. A dumb text scan cross-checks the AST walk: if the scan finds a skip on a line the walk never accounted for, an unrecognised form has appeared and the guard has a blind spot. And a separate check rejects skips that do not depend on the environment at all -- `@pytest.mark.skip`, `@unittest.skip`, `skipIf(True, ...)`, `skipUnless(False, ...)` -- which is #107's opening line and the one shape the extras check structurally cannot see.

  Verified by mutation: each of the five failure modes was introduced in turn and the guard caught all five. (#107)

## [0.9.0] - 2026-08-27

Five defects of one shape, which is why they ship together: a gap
between what the API said and what landed on disk. A public method named
as though it were the export path when it is half of one; two export
paths writing the same instance under two different filenames; two
spellings of a tag key, so a profile rule declared for Series
Description silently never fired; a flag that said *keep the private
tags* and exported a file without them; and a folder name that invented
the word `nknow` when it had no UID to work with.

None of them raised. Each produced a plausible artefact that a consumer
had no way to tell was wrong -- which is the failure mode a
de-identification tool can least afford, and the reason three of these
are breaking rather than deprecated.

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

- **`remove_private_tags=False` kept the private tags and then exported a file without them.** The flag worked everywhere it was visible: the odd-group tags survived ingest, sat in the object graph and the `instance_attributes` table, and the audit report listed them as retained. `DicomExporter._merge` then dropped every one on the way out, because it asked `dictionary_VR` for the tag's VR, that raises for anything outside the standard dictionary, and the `except` arm only logged. So the flag appeared to do its job right up until the exported file was read back, which is the one place nobody looks when the setting is the one that *keeps* data.

  The exporter now picks a VR from the value when the dictionary has none. Picking it is not the obvious part: PS3.5 A.1 makes `UN` the VR for an unknown value, and `UN` is the wrong answer for almost everything that reaches this code. It is an OB-family VR, so pydicom accepts a `str` at `add_new` without complaint and raises only when the dataset is written -- and everything the EAV table hands back for a private tag is a `str`, because `value_text` is where it was stored. A blanket `UN` would have swapped a silent per-tag drop for a `TypeError` from `filewriter` that fails the entire export, thousands of instances later, naming a tag that is fine. `UN` is now used for `bytes` only; text gets `LO`, or `UT` past `LO`'s 64-character limit; numbers are stringified, as the EAV table would have done to them anyway, so that whether a private tag exports does not depend on whether a save has happened yet. Choosing the VR and encoding the value are one decision and are made in one place, `_fallback_encoding`.

  What is left when nothing fits is still a warning, but it now says `not exported (data loss)` rather than `Failed to merge`. It should be near-unreachable, which is exactly why it needed to stop reading like an internal hiccup. It cannot yet write the `DATA_LOSS` audit entry the #36 precedent calls for: `_merge` runs inside `_export_instance_worker`, potentially in a subprocess, with no store handle and no way to be passed one -- #126.

  The four hardcoded VR special-cases above it (`0099,0010`, `0099,1001`, `0400,0510`, `0400,0520`) are now redundant for two of the four and were left in place deliberately. The `0099` pair is half of the read-back path for stores written by v0.4.1, and pairs with the `WHITELIST_TAGS` exemption in `privacy.py`; removing them here would quietly undo the affordance #113 had just finished documenting.

  A separate loss, found by the same probe and **not** fixed here: private tags with a binary VR never reach the object graph at all, because `populate_attrs` skips `OB`/`OW`/`OF`/`OD`/`OL` at ingest to keep blobs out of `attributes`. No flag can keep what was never held. That is a design decision rather than a bug, and it is #125. (#118)

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

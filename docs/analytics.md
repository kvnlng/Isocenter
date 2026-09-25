# Analytics & Reporting

Isocenter is designed not just for de-identification, but for understanding your data. It includes built-in tools for compliance verification, cohort analysis, and data exploration.

## Compliance Reports

For regulatory audits (HIPAA/GDPR), Isocenter can generate a formal **Compliance Report**. This single-document artifact summarizes the entire session, ensuring transparent documentation of your de-identification process.

```python
# Generate a Markdown report
session.generate_report("compliance_report.md")
```

The report includes:

1. **Executive Summary**: the grade (`PASS` / `REVIEW_REQUIRED`), how many patients and instances the session holds, the privacy profile and de-identification method, and the pre-export boundary note described below. An **Instances Written** row (`191 of 192 requested`) appears only when the last `export()` call in this session was a DICOM export that ran; it describes that export alone. A WFDB export, or an export that raised, removes the row, and a session that has not exported, a reopened store included, has none.
2. **Processing Audit**: aggregated counts of actions taken (e.g., tags remediated, pixels redacted, export runs completed).
3. **Data Loss & Unscanned Content**: 3.1 lists every element that was
   present in the source and is not in the exported data, named with its
   VR. 3.2 lists content the PHI scan could not open -- a private value
   whose bytes look like a sequence and do not parse as one -- with a
   **Disposition** column resolved against the object graph at report
   time: `removed before export` (the private-tag sweep deleted the
   bytes; grades `PASS`, exactly as a swept parseable sequence does),
   `retained for export`, or `unresolved` (the instance's UID changed
   after ingest, e.g. by `redact()`, so the row cannot be matched back).
   The latter two grade the session `REVIEW_REQUIRED`, with no scope
   test: a run that exports bytes it could not read does not get to
   call itself PASS. 3.3 lists remediations that were **proposed and
   did not run** -- a finding whose entity could not be resolved
   against the live graph, a target the action has no arm for, a date
   with no PatientID to seed its jitter, a value that is not a date, a
   remediation that raised (its value may be unchanged or partly
   written) -- each with its reason. The element each row names is still in the
   object graph and reached the exported files, so one row grades the
   session `REVIEW_REQUIRED` on the same argument 3.2 makes. Unlike 3.1
   and 3.2, 3.3 is omitted entirely from a run that declined nothing.
4. **Exceptions & Errors**: every `ERROR` and `WARNING` audit row, plus report-time checks (`COMPLIANCE_CHECK`, `AUDIT_DROP`). Any row here grades the run `REVIEW_REQUIRED`.
    - An `ERROR` means something requested failed: a file refused at ingest, an instance that failed to write.
    - A `WARNING` means the run did what it should, but something about the source data could not be honoured or read, or was deliberately held back. Among them:
        - a file declined because its SOP Instance UID is already held;
        - an instance `export(check_burned_in=True)` withheld because it still carries an identifier (it is not written, and nothing failed);
        - an instance OCR could not read;
        - a store an older release de-identified, where what that release did cannot be vouched for (see [Upgrading from 0.9.x](migration.md));
        - a Photometric Interpretation the written transfer syntax does not admit, written as declared because correcting it would invent a claim;
        - an ambiguous value representation whose deciding attribute the source omits or contradicts: no Waveform Bits Allocated above a waveform element, no LUT Descriptor in a LUT, a value the Pixel Representation names cannot hold, or a value the unsigned default cannot hold where no Pixel Representation is declared.
5. **Validation & Verification**: the **Grade Basis** -- every reason this run is not `PASS`, one line each, or a statement that nothing costs it its `PASS` -- then how many `REMEDIATION_*` rows the audit trail holds, what each `scan_pixel_content()` call in this session read and could not read, and the configured method. When the grade surprises you, read the Grade Basis first: it names the section that holds the row. Every condition behind it is listed under [How the grade is decided](#how-the-grade-is-decided).

A per-instance manifest is not part of the report. It is a separate document, written by `generate_manifest()`; see [Manifests](#manifests).

### How the grade is decided

Worked example: [De-identify a cohort and read the grade](tutorials/deidentify-and-read-the-grade.md).

The report grades a run `PASS` or `REVIEW_REQUIRED`. There is no `FAIL`: a failure the run records costs it its `PASS` (condition 2); one it cannot record raises.

**The grade is `PASS` exactly when none of the conditions below holds.** Section 5's *Grade Basis* lists each one that does, one line per condition, naming the section that holds the evidence. The grade and that list are computed from one list, so they cannot disagree.

The conditions are read from the store's audit log and from the store itself. The audit log holds everything any session ever recorded in this store, not only the session generating the report. A row written once stays; only a new store starts clean.

1. **Nothing is attested.** The audit log holds no rows at all. A clean ingest followed by `audit()` alone writes none, for example.
2. **Something failed, or the source could not be honoured.** Any `ERROR` or `WARNING` row, or a report-time check (section 4):
    - `COMPLIANCE_CHECK`: an instance stored with Burned In Annotation `YES`, or stored pixel descriptors that cannot describe its frame.
    - `AUDIT_DROP`: audit rows that failed to write.
3. **Graded data was lost.** A `DATA_LOSS` row scoped `PRIVATE` or `SIGNAL` (section 3.1). A `STANDARD` loss is listed but does not grade.
4. **The PHI scan could not read something the export still carries.** A `SCAN_GAP` row whose element is retained for export, or whose disposition cannot be resolved (section 3.2).
5. **A remediation was proposed and did not run.** A `REMEDIATION_DECLINED` row, including a proposal that raised (section 3.3).
6. **A verb left no evidence.** `anonymize()` or `redact()` ran in the session generating the report, and none of the rows it writes is in the audit log.
7. **A finding raised under your policy was not acted on.** A patient, study or instance whose last PHI scan (`audit()`, or the scan `export(check_burned_in=True)` runs) found a value that a rule of the policy that scan ran with acts on, and which no `anonymize()` pass since has acted on. "Acted on" means the value was replaced, shifted or removed, or was already what the rule asks. A remediation that declined did not act, so its entity reads `IDENTIFIED` after the pass and counts here as well as under condition 5, unless it was edited after its scan and the pass changed nothing else on it, which leaves it `UNSCANNED`. This is the entity reading `IDENTIFIED` in `session.phi_status_summary()`, counted over the whole store; section 5's line gives the count per level. Series are never scanned and never counted. An entity edited after its scan reads `UNSCANNED`, and grades under condition 8 instead.
8. **A patient, study or instance was edited after its PHI status was recorded.** Its content was changed after a scan or a pass recorded its status, and no scan has read the change since. For an instance that is a change through its methods -- `set_attr`, a sequence added, filled or cleared, `set_pixel_data` -- or an assignment of `instance.sop_instance_uid`; a change to an item nested in it, at any depth; and an assignment of a field of its series the export writes into it: `series.series_instance_uid`, `series.modality`, `series.series_number` or `series.equipment`. A series is counted through its instances, never by itself: the scan records nothing on a series, so a status of its own could never be cleared by one. For a patient or a study it is an assignment of a field the export writes from it into every file beneath it, or the scan reads on it: `patient.patient_name`, `patient.patient_id`, `study.study_date`, `study.study_time` or `study.study_instance_uid`. Such an entity reads `UNSCANNED`, and it grades until a scan reads it: `audit()` reads it, and the line goes. A save and a reopen keep it: the store records the status the edit left behind. An entity never scanned is not counted here -- it is the absence of a measurement, as above. Neither is what the library writes itself after a scan: a pass records its status after what it writes, redaction carries the status it found, and so does the reversible lock (`lock_identities`), whose token is not PHI; each carries it only when it still applied, so an edit made before them is not hidden by them. Assigning the value a field already holds is not an edit. A nested item's own status is not counted. A write that goes around the entity -- straight into `instance.attributes`, into a sequence's `items` list, or `instance.pixel_array = ...` -- is not seen. Section 5's line gives the count per level.

**What `PASS` does not mean.** The grade is about what the run recorded doing, and what its own scan found and its own passes left. It does not say the exported data holds no identifiers:

- **Data never scanned does not grade.** An export without `audit()` grades `PASS` if nothing else is recorded: an unscanned instance is the absence of a measurement, not a finding. Section 5 says how many instances have no PHI scan at their current revision, so a `PASS` over data no scan has read does not pass for a `PASS` over data a scan cleared. The per-entity answer is `session.phi_status_summary()` and the manifest's `anonymized`.
- **The scan finds what your policy names, plus Patient's Name, Patient ID and Study Date, which it always checks, and, with `remove_private_tags` on, every private tag.** An identifier anywhere else is not a finding, and the grade does not see it.
- **An edit made between `audit()` and `anonymize()`** can be stamped `REMEDIATED` by the pass without any scan having read it, and then does not grade ([#752](https://github.com/kvnlng/Isocenter/issues/752)).
- **Burned-in pixel text is not graded.** Text `scan_pixel_content()` finds is counted in section 5 and costs no `PASS`; an instance it could not read does (condition 2), as does a stored Burned In Annotation `YES`.

The grade describes a run. Whether the result meets a protocol or a regulation is the data steward's determination.

The conditions are a 1.x promise: none is removed or narrowed in a 1.x release, and one may be added, with a CHANGELOG entry. The promise covers the conditions, not the direction a grade can move: a 1.x fix that stops writing a wrong row can move a run from `REVIEW_REQUIRED` to `PASS`, and the CHANGELOG entry for that fix says so. The wording of the report and of each Grade Basis line is not part of that promise ([API stability](api/stability.md)).

!!! note "A `WARNING` row needs a person; a correction is not reported"

    What you will **not** find in section 4 is Isocenter correcting a descriptor of its own making -- PixelRepresentation or BitsStored rewritten to match the pixels actually written. Those corrections are exact, lose nothing, and say nothing about your data, so they are logged at `INFO` and are neither recorded in the audit log nor graded. The default console handler shows `WARNING` and above, so they do not appear on screen either. A `WARNING` row, by contrast, needs a person to read it: most say something about the source dataset, and some about the run itself -- statuses recorded under another policy, or a `patient_ids` that named a patient the session does not hold, or a `subset` that named a UID it does not hold.

    Four grade reasons have no row anywhere else, so the Grade Basis is the only place they appear: **an empty audit trail** (a clean ingest followed by `audit()` alone writes no row, and grades `REVIEW_REQUIRED` because nothing the run did is attested), **a verb with no evidence** (`anonymize()` or `redact()` did work and none of the rows it writes reached the audit log), **findings nobody acted on** (entities the last PHI scan left `IDENTIFIED`; condition 7 under [How the grade is decided](#how-the-grade-is-decided)), and **entities edited after their scan** (condition 8).

!!! warning "Which losses move the Validation Status"

    A dropped **private** (odd-group) element grades the session
    `REVIEW_REQUIRED`. It may be a vendor block `remove_private_tags: false`
    was set specifically to keep ([The 65534-byte limit](configuration.md#the-65534-byte-limit)), and nobody outside the vendor can size
    or identify what went missing.

    A dropped **standard** element -- a large Overlay Data plane
    `(60xx,3000)`, say -- does not. Those come off ordinary images by the thousand,
    so a grade that moved on them would read `REVIEW_REQUIRED` for most
    cohorts and stop carrying information.

    Read the Data Loss section on its own terms either way: its **Scope**
    column says which rows were graded -- `PRIVATE` and `SIGNAL` rows are,
    `STANDARD` rows are not -- and `unrecorded` means a row written by an
    older release, before the scope was recorded. `SIGNAL` is the
    standard-group loss that grades: acquired content that was in the
    source and is not in the export -- a discarded waveform multiplex group
    (see [Waveforms](waveforms.md)), or a nested icon image dropped because
    pixel data is redacted.

!!! warning "Generate the report after `export()`"

    `generate_report()` grades the audit log as it stands when you call
    it. Losses recorded at ingest are already in it; losses recorded on
    the way *out* -- an element that could not be encoded, a waveform
    with no samples -- are written during `export()`. Call
    `generate_report()` **after** `export()`, or those losses cannot
    reach the Validation Status no matter what group they are in.

    A report generated before any export says so itself: when the
    audit trail holds no `EXPORT` row, the Executive Summary carries a
    boundary note under the grade and a warning is logged. The note
    states the boundary without moving the grade. (An audit-only session
    grades `REVIEW_REQUIRED` anyway, for a different reason: `audit()`
    writes no audit row, and an empty trail attests nothing -- see
    section 5's Grade Basis.)

!!! tip "Format"
    The report is Markdown. `generate_report(output_path, format="markdown")` is the default and the only accepted spelling; any other `format` raises `ValueError` naming it, and no file is written.

## Manifests

`generate_manifest(output_path, format="html")` writes one row per instance the session holds: every instance in `session.store`, whatever an export selected. `format` is `"html"` (the default) or `"json"`, exactly; any other spelling, a case variant included, raises `ValueError` and writes no file.

```python
session.generate_manifest("manifest.html")
session.generate_manifest("manifest.json", format="json")
```

Each row holds the instance's Patient ID, its Study, Series and SOP Instance UIDs, its modality, the manufacturer and model name of its series' equipment, and the path of the file it was ingested from, unless redaction detached it (below). The values are the session's, as they stand when the manifest is written:

- **After `anonymize()`**, the Patient ID is the replacement and the UIDs are the replacement UIDs, next to the source file path. The manifest is then a crosswalk from each source file to the identifiers its export carries. Keep it with the store, not with an export.
- **A subject whose files carried no Patient ID** is listed under its key, the value `get_cohort_report()` shows in its `PatientID` column: `\no-patient-id\` followed by the subject's *source* Study Instance UID. No export writes that key.
- **The file path** is the path as `ingest()` walked it, so it is relative when the directory you passed was relative. **After `redact()` changes an instance's pixels it is the string `"None"`**, in both formats: the instance no longer matches its source file, so the session stops pointing at it (`Instance.regenerate_uid()` does the same). The manifest then cannot say which file such an instance came from.

The **HTML** manifest is one page: the store file's name, when the manifest was generated, the number of instances, and a table with the columns Patient ID, Study UID, Series UID, Modality, Manufacturer, Model, SOP Instance UID and File Path. It has no column for `anonymized`.

The **JSON** manifest is one object. This one was written after `ingest()`, `audit()`, `anonymize()` and `export()` into a store named `my_project.db`, over pydicom's `CT_small.dcm` and a copy of its `MR_small.dcm` with the Patient ID deleted:

```json
{
  "generated_at": "2026-09-23T21:03:55.218090",
  "project_name": "my_project.db",
  "total_files": 2,
  "total_size_bytes": 0,
  "items": [
    {
      "patient_id": "ANON_28e5c87cd169c80d0dd05bfb",
      "study_instance_uid": "2.25.297990125461744647168780076193196812719",
      "series_instance_uid": "2.25.188531152787603764149085731129800765062",
      "sop_instance_uid": "2.25.102433379052112046212620522432915392421",
      "file_path": "input/CT_small.dcm",
      "file_size_bytes": 0,
      "modality": "CT",
      "manufacturer": "GE MEDICAL SYSTEMS",
      "model_name": "RHAPSODE",
      "anonymized": true
    },
    {
      "patient_id": "\\no-patient-id\\1.3.6.1.4.1.5962.1.2.4.20040826185059.5457",
      "study_instance_uid": "2.25.276106521748935728200388598927172523634",
      "series_instance_uid": "2.25.163640163259226614858004570992412354281",
      "sop_instance_uid": "2.25.261339294768423179199607825024297936589",
      "file_path": "input/MR_noid.dcm",
      "file_size_bytes": 0,
      "modality": "MR",
      "manufacturer": "TOSHIBA_MEC",
      "model_name": "MRT50H1",
      "anonymized": true
    }
  ]
}
```

The pseudonym and the replacement UIDs come from the store's project secret, so another store gives other values.

`generated_at` is local time with no UTC offset, `project_name` is the store file's name, and `total_files` is the number of items. `file_size_bytes` and `total_size_bytes` are always `0`: nothing measures a file for the manifest.

An item's **`anonymized`** is `true` when the last tag-policy PHI scan left no identifier unremediated on the instance's patient, its study or the instance itself, and none of the three has been edited since. It is `false` when the scan never ran on one of them, when one was edited after it, or when a remediation on it was declined in its last pass. It does not mean "`anonymize()` ran": a file the scan found clean reads `true` after `audit()` alone. It says nothing about burned-in text in the pixels, which the tag scan does not read. The series is not consulted, because the scan records no status on a series. `session.phi_status_summary()` gives the same statuses as counts.

`generate_manifest()` and its two formats are frozen at 1.0. The JSON keys, the meaning of `anonymized` and the HTML layout are documented but internal: a 1.x release may change them, with a CHANGELOG entry ([API stability](api/stability.md)).

---

## Cohort Analysis (EDA)

`get_cohort_report()` returns the cohort as a pandas DataFrame, one row per instance, for analysis with pandas, Jupyter or a BI tool. It reads the session's in-memory graph and writes nothing.

### 1. Load into pandas

```python
# expand_metadata=True adds one column per DICOM attribute
df = session.get_cohort_report(expand_metadata=True)

# Inspect the data
print(df.head())
print(df.groupby('Modality').size())
```

**How the columns are named.** The base columns are keywords: `PatientID`, `PatientName`, `StudyInstanceUID`, `StudyDate`, `SeriesInstanceUID`, `Modality`, `SOPInstanceUID`, `Manufacturer`, `Model` and `DeviceSerial`. The columns `expand_metadata=True` adds are lowercase `gggg,eeee` tag keys: Slice Thickness is `df["0018,0050"]`, not `df["SliceThickness"]`. The values are the session's as they stand, so before `anonymize()` the frame holds the original identifiers.

### 2. Write to CSV or Parquet

`export_dataframe()` builds the frame `get_cohort_report()` builds, writes it to a file and returns it. It takes the same `expand_metadata` argument, also `False` by default, so pass `expand_metadata=True` for the tag columns. Write the expanded frame as CSV: Parquet cannot yet hold a multi-valued tag such as Image Type, and a cohort that has one makes the call raise ([#816](https://github.com/kvnlng/Isocenter/issues/816)). The format follows the extension: Parquet for `.parquet`, CSV for anything else. With no path it writes `export_metadata.csv` in the current directory. Called before `anonymize()`, the file holds the original identifiers, so write it where PHI may live, or use `get_cohort_report()` when you only want the frame.

For large cohorts (100k+ images), Parquet is smaller and faster to read, and BI tools (PowerBI, Tableau, Apache Spark) read it directly.

```python
# Export the full cohort's base columns to Parquet
session.export_dataframe("cohort_inventory.parquet")

# Every tag column too, as CSV
session.export_dataframe("cohort_full.csv", expand_metadata=True)

# Or just part of it
session.export_dataframe("arm_a.parquet", patient_ids=["P001", "P002"])
```

---

## Query-Based Export

Instead of exporting the entire session, you can pass `export()` a subset: a filtered cohort DataFrame, or a list of IDs and UIDs.

Worked example: [Select part of a cohort](tutorials/select-part-of-a-cohort.md).

### Use Case: "Export only thick-slice CTs"

```python
# 1. Get the inventory
df = session.get_cohort_report(expand_metadata=True)

# 2. Define your criteria (Standard Pandas syntax)
# e.g., Keep only CT scans with Slice Thickness (0018,0050) > 2.5 mm
subset = df[
    (df['Modality'] == 'CT') &
    (df['0018,0050'].astype(float) > 2.5)
]

print(f"Filtering: {len(df)} -> {len(subset)} instances.")

# 3. Feed the subset back into the exporter
session.export("export_thick_cts", subset=subset)
```

### Use Case: "Export a list of series"

You can also filter by a list of identifiers if you have an external manifest.

```python
# Filter by Series Instance UIDs
target_series = ["1.2.840...", "1.2.840..."]

# Filter the dataframe
subset = df[df['SeriesInstanceUID'].isin(target_series)]

session.export("export_selected_series", subset=subset)
```

`subset` also takes the UIDs themselves, as any iterable -- a list, a
tuple, a set -- at any level: Patient ID, Study, Series or SOP Instance
UID. A UID that names nothing in the session is not silently dropped:
the export writes what the rest selects, and one `WARNING` audit row
counts the others by position, so the report grades `REVIEW_REQUIRED`.

```python
session.export("export_selected_series", subset=target_series)
```

# Analytics & Reporting

Isocenter de-identifies data, and it also helps you understand your data. It includes built-in tools for compliance verification, cohort analysis, and data exploration.

## Compliance Reports

For regulatory audits (HIPAA/GDPR), Isocenter can generate a formal **Compliance Report**. This one document summarizes the entire session and records your de-identification process.

```python
# Generate a Markdown report
session.generate_report("compliance_report.md")
```

The report includes:

1. **Executive Summary**: the grade (`PASS` / `REVIEW_REQUIRED`), how many patients and instances the session holds, the privacy profile and de-identification method, and the pre-export boundary note described below. An **Instances Written** row (`191 of 192 requested`) appears only when the last `export()` call in this session was a DICOM export that ran. The row describes that export alone. A WFDB export, or an export that raised, removes the row. A session that has not exported has no such row, and that includes a session on a reopened store.
2. **Processing Audit**: aggregated counts of actions taken (e.g., tags remediated, pixels redacted, export runs completed).
3. **Data Loss & Unscanned Content**: 3.1 lists every element that was
   present in the source and is not in the exported data, named with its
   VR. 3.2 lists content that the PHI scan could not open: a private
   value whose bytes look like a sequence and do not parse as one. Its
   **Disposition** column is resolved against the object graph at report
   time:
    - `removed before export`: the private-tag sweep deleted the bytes.
      This grades `PASS`, exactly as a swept parseable sequence does.
    - `retained for export`
    - `unresolved`: the instance's UID changed after ingest, e.g. by
      `redact()`, so the row cannot be matched back.

   The latter two grade the session `REVIEW_REQUIRED`, with no scope
   test. A run that exports bytes it could not read does not get to
   call itself PASS. 3.3 lists remediations that were **proposed and
   did not run**, each with its reason:
    - a finding whose entity could not be resolved against the live graph
    - a target the action has no arm for
    - a date with no PatientID to seed its shift
    - a value that is not a date
    - a remediation that raised (its value may be unchanged or partly
      written)

   The element each row names is still in the object graph and reached
   the exported files. So one row grades the session `REVIEW_REQUIRED`,
   on the same argument that 3.2 makes. Unlike 3.1 and 3.2, 3.3 is
   omitted entirely from a run that declined nothing.
4. **Exceptions & Errors**: every `ERROR` and `WARNING` audit row, plus report-time checks (`COMPLIANCE_CHECK`, `AUDIT_DROP`). Any row here grades the run `REVIEW_REQUIRED`.
    - An `ERROR` means something requested failed: a file refused at ingest, an instance that failed to write.
    - A `WARNING` means that the run did what it should. But something about the source data could not be honoured or read, or was deliberately held back. Among them:
        - a file declined because its SOP Instance UID is already held, or because its Study Instance UID belongs to a patient with a different Patient ID
        - an instance `export(check_burned_in=True)` withheld because it still carries an identifier (it is not written, and nothing failed)
        - an instance OCR could not read
        - a store an older release de-identified, where what that release did cannot be vouched for
        - a Photometric Interpretation the written transfer syntax does not admit, written as declared because correcting it would invent a claim
        - an ambiguous value representation whose deciding attribute the source omits or contradicts:
            - no Waveform Bits Allocated above a waveform element
            - no LUT Descriptor in a LUT
            - a value the Pixel Representation names cannot hold
            - a value the unsigned default cannot hold where no Pixel Representation is declared
        - a WFDB record in which two signals would share a lead name. Each of those signals that got its name from the lead table is then written as its Code Value ([Lead names](waveforms.md#lead-names)).
5. **Validation & Verification**: the **Grade Basis** comes first. It lists every reason this run is not `PASS`, one line each, or it states that nothing costs the run its `PASS`. Then the section gives:
    - how many `REMEDIATION_*` rows the audit trail holds
    - what each `scan_pixel_content()` call in this session read and could not read
    - what each [`burned_in_text`](configuration.md#burned-in-text) pass of `redact()` in this session read under its scope, left out of scope, found and redacted, reported or left to be withheld
    - what each `export(check_burned_in=True)` under that policy read, left out of scope, found and withheld or wrote with a row
    - how many images, and how many icons, were read with an inferred Planar Configuration (0028,0006)
    - on a line of its own, how many were read as 0 over a declared Planar Configuration under a compressed syntax
    - the configured method

    A scope that selected nothing costs no `PASS`. A session that ran a `burned_in_text` pass or an `export(check_burned_in=True)` read, and no `scan_pixel_content()`, is not told that no OCR ran. The two Planar Configuration counts are each counted over the store, and each line appears only when any were. Neither count costs a `PASS`.

    When the grade surprises you, read the Grade Basis first. It names the section that holds the row. Every condition behind it is listed under [How the grade is decided](#how-the-grade-is-decided).

A per-instance manifest is not part of the report. It is a separate document, written by `generate_manifest()`. See [Manifests](#manifests).

### How the grade is decided

Worked example: [De-identify a cohort and read the grade](tutorials/deidentify-and-read-the-grade.md).

The report grades a run `PASS` or `REVIEW_REQUIRED`. There is no `FAIL`. A failure that the run records costs it its `PASS` (condition 2). A failure that the run cannot record raises.

**The grade is `PASS` exactly when none of the conditions below holds.** Section 5's *Grade Basis* lists each one that does, one line per condition, naming the section that holds the evidence. The grade and that list are computed from one list, so they cannot disagree.

The conditions are read from the store's audit log and from the store itself. The audit log holds everything any session ever recorded in this store, not only the session generating the report. A row, once written, stays. Only a new store starts clean.

1. **Nothing is attested.** The audit log holds no rows at all. A clean ingest followed by `audit()` alone writes none, for example.
2. **Something failed, or the source could not be honoured.** Any `ERROR` or `WARNING` row, or a report-time check (section 4):
    - `COMPLIANCE_CHECK`: an instance stored with Burned In Annotation `YES`, or stored pixel descriptors that cannot describe its frame.
    - `AUDIT_DROP`: audit rows that failed to write.
3. **Graded data was lost.** A `DATA_LOSS` row scoped `PRIVATE` or `SIGNAL` (section 3.1). A `STANDARD` loss is listed but does not grade.
4. **The PHI scan could not read something the export still carries.** A `SCAN_GAP` row whose element is retained for export, or whose disposition cannot be resolved (section 3.2).
5. **A remediation was proposed and did not run.** A `REMEDIATION_DECLINED` row, including a proposal that raised (section 3.3).
6. **A verb left no evidence.** `anonymize()` or `redact()` ran in the session generating the report, and none of the rows it writes is in the audit log.
7. **A finding raised under your policy was not acted on.** This counts a patient, study or instance when both of these are true:
    - its last PHI scan found a value that a rule acts on
    - no `anonymize()` pass since that scan acted on the value

    The last PHI scan is `audit()`, or the scan that `export(check_burned_in=True)` runs. The rule is one of the policy that the scan ran with. "Acted on" means the value was replaced, shifted or removed, or was already what the rule asks.

    A remediation that declined did not act. So its entity reads `IDENTIFIED` after the pass, and counts here as well as under condition 5. The exception is an entity that was edited after its scan, when the pass changed nothing else on it: that entity reads `UNSCANNED`. This condition counts the entities that read `IDENTIFIED` in `session.phi_status_summary()`, over the whole store. Section 5's line gives the count per level. Series are never scanned and never counted. An entity edited after its scan reads `UNSCANNED`, and grades under condition 8 instead.
8. **A patient, study or instance was edited after its PHI status was recorded.** Its content changed after a scan or a pass recorded its status, and no scan has read the change since. For an instance, that change is one of these:
    - a change through its methods: `set_attr`, a sequence added, filled or cleared, `set_pixel_data`
    - an assignment of `instance.sop_instance_uid`, which also sets its `(0008,0018)` element
    - a change to an item nested in it, at any depth
    - an assignment of `series.series_instance_uid`, which the export writes into it, or of `series.modality` or `series.equipment`

    An assignment of `series.series_number` is not counted. The export writes each instance's own Series Number, so an assignment of the series' field changes no file. A series is counted through its instances, never by itself. The scan records nothing on a series, so no scan could ever clear a status that the series held by itself.

    For a patient or a study, that change is an assignment of a field that the export writes from it into every file beneath it, or that the scan reads on it. Those fields are `patient.patient_name`, `patient.patient_id`, `study.study_date` and `study.study_instance_uid`. An assignment of `study.study_time` is counted too, though the export writes each instance's own Study Time and not the study's field.

    Such an entity reads `UNSCANNED`, and it grades until a scan reads it. `audit()` reads it, and the line goes. A save and a reopen keep it, because the store records the status that the edit left behind. An entity never scanned is not counted here. Condition 9 counts it.

    What the library itself writes after a scan is not counted either:
    - A pass records its status after what it writes.
    - Redaction carries the status it found. It writes the pixels, the new SOP Instance UID and its flags, but no attribute that a scan read.
    - The reversible lock (`lock_identities`) also carries the status it found. Its token is not PHI.

    Each of them carries the status only when that status still applied, so an edit made before them is not hidden by them. That includes `anonymize()`. An entity edited after its scan and before the pass is still counted after the pass. The pass does not read the edit, so the pass keeps the status that the scan left, and does not stamp its own (#752). So `audit()`, then `redact()`, then `anonymize()` over that audit's report settles a redacted instance as the documented order does. An edit made before the redaction is still counted after both.

    Assigning the value a field already holds is not an edit. A nested item's own status is not counted. A write that goes around the entity -- straight into `instance.attributes`, into a sequence's `items` list, or `instance.pixel_array = ...` -- is not seen. Section 5's line gives the count per level.
9. **A patient, study or instance was never scanned, in a store that holds a recorded PHI status.** At least one patient, study or instance in the store holds a recorded status, and this one holds none, from a scan or from a pass. The usual way there is a file ingested after the store's `audit()` or `anonymize()`.

    Until the next scan, the store holds such an entity as ingested, and an export writes it as the store holds it. Until then, nothing that your policy names has been looked for in it. For example, a late series of a study already passed gets its owners' pseudonym, Patient ID, shifted Study Date and replaced Study Instance UID stamped on it. That series keeps every other value of the source, its Series and SOP Instance UIDs included. `audit()` reads the entity, and the line goes. What that scan finds then grades under condition 7 until a pass acts on it.

    This condition is counted over the whole store, as conditions 7 and 8 are. So it holds before any export, and for an export whose `subset` or `patient_ids` left the entity out. "Holds a recorded status" means a status of any value on any patient, study or instance, including one that an edit left stale (condition 8). The condition reads the statuses that the store holds, so a save and a reopen keep it. A store in which no status is recorded anywhere is not counted. See "Unscanned data does not grade in a store that holds no PHI status", below.

    An entity is counted here or under condition 8, never under both. Condition 8 is a status that an edit left behind, and this condition is no status at all. A series and a nested item are never counted and never read, since no scan records a status on either. A patient that holds no studies is counted. The condition does not say that an entity with a status was scanned, because a pass over findings built by hand also records a status. Section 5's line gives the count per level.

**What `PASS` does not mean.** The grade is about what the run recorded doing, and what its own scan found and its own passes left. It does not say the exported data holds no identifiers:

- **Unscanned data does not grade in a store that holds no PHI status.** This applies while no patient, study or instance in the store holds a recorded PHI status -- `ingest()` then `export()` with no `audit()`, a format conversion for example. In that case, the run grades `PASS` if nothing else is recorded. An unscanned instance is the absence of a measurement, not a finding. That stops once the store holds a recorded status anywhere. From then on, each patient, study or instance that holds none costs the `PASS` (condition 9). So a file ingested after a pass grades `REVIEW_REQUIRED` until `audit()` reads it, and the same file in a store nobody audited does not.

  Either way, section 5 says how many instances have no PHI scan at their current revision, and whether the Grade Basis counts them. So a `PASS` over data that no scan has read does not pass for a `PASS` over data that a scan cleared. The per-entity answer is `session.phi_status_summary()` and the manifest's `anonymized`.
- **The scan finds what your policy names, plus Patient's Name, Patient ID and Study Date, which it always checks.** With `remove_private_tags` on, it also finds every private tag. An identifier anywhere else is not a finding, and the grade does not see it.
- **An edit made between `audit()` and `anonymize()`** can be stamped `REMEDIATED` by the pass without any scan having read it. Such an edit then does not grade ([#752](https://github.com/kvnlng/Isocenter/issues/752)).
- **Burned-in pixel text is not graded.** Text that `scan_pixel_content()` finds is counted in section 5 and costs no `PASS`. An instance that `scan_pixel_content()` could not read does cost the `PASS` (condition 2). So does a stored Burned In Annotation `YES`, in any letter case (a `COMPLIANCE_CHECK` row). A zone that `redact()` applied writes Burned In Annotation `NO`, so a zone-redacted image no longer costs the `PASS`. A zone applied only by `export()` leaves the attribute as the source had it, so the image still costs it. The check reads the store, not the files written, so it also counts a `YES` instance that an export's `subset` left out.

  Under the [`burned_in_text`](configuration.md#burned-in-text) policy, a `redact` pass that blanked text costs no `PASS` by itself. These `WARNING` rows do cost it, under condition 2:
    - text still found when a redacted image is read again
    - an image the pass could not read
    - an image the export withheld or wrote because the policy never checked it
    - an image with text the export withheld (`withhold`, or `redact` before a redaction landed)
    - an image with text the export wrote (`report`), besides the row the `report` pass writes for it
    - under `export(check_burned_in=True)`, an image its read found text on or could not read in full, written or withheld

  Under a narrowed `target` ([#1121](https://github.com/kvnlng/Isocenter/issues/1121)), the text found is only text of a named kind. Text left in place writes one `RISK` row per image, which is listed and does not grade. Text that OCR does not find is not graded at all.
- **An inferred or overridden Planar Configuration is not graded.** An image or icon whose source stated no Planar Configuration is read as 0, counted in section 5, and named per instance at INFO in the export's log. It costs no `PASS` ([#1134](https://github.com/kvnlng/Isocenter/issues/1134)). The same is true of an image or icon that declared a value other than 0 under a compressed syntax whose stream fixes the layout. That one is counted on a line of its own ([#1137](https://github.com/kvnlng/Isocenter/issues/1137)).

The grade describes a run. Whether the result meets a protocol or a regulation is the data steward's determination.

The conditions are a 1.x promise. No 1.x release removes or narrows a condition, and a 1.x release may add one, with a CHANGELOG entry. The promise covers the conditions, not the direction a grade can move. A 1.x fix that stops writing a wrong row can move a run from `REVIEW_REQUIRED` to `PASS`, and the CHANGELOG entry for that fix says so. The wording of the report and of each Grade Basis line is not part of that promise ([API stability](api/stability.md)).

:::{admonition} A `WARNING` row needs a person; a correction is not reported
:class: note

You will **not** find in section 4 that Isocenter corrected a descriptor of its own making. Such a correction is PixelRepresentation, BitsStored, Rows or Columns rewritten to match the pixels actually written. It can also be Encapsulated Document Length `(0042,0015)` rewritten to the length of the document written. Those corrections are exact, lose nothing, and say nothing about your data. So they are logged at `INFO`, and they are neither recorded in the audit log nor graded. The default console handler shows `WARNING` and above, so they do not appear on screen either.

Four more lines go out on that same `INFO` channel, with no row, and are not corrections of that kind:
- When a colour image or icon held no Planar Configuration in its source, the export says that it wrote the 0 that ingest inferred ([#1134](https://github.com/kvnlng/Isocenter/issues/1134)).
- When a colour image or icon declared a value other than 0 under a compressed syntax whose stream fixes the layout, the export says that it read and wrote 0 ([#1137](https://github.com/kvnlng/Isocenter/issues/1137)).
- When a file carries no Encapsulated Document `(0042,0011)`, its Encapsulated Document Length is not written, and the line says so. The Encapsulated Document is absent because a rule removed it, or because it was dropped at ingest with its own `DATA_LOSS` row.
- When the export writes a private value that this library's own `ingest()` would drop (over 65534 bytes, under a binary VR or `UN`), the line names the element. The file is written as asked, but a re-ingest here will lose that value with a `DATA_LOSS` row.

To see these lines, attach a handler at `INFO` to the `isocenter` logger.

A `WARNING` row, by contrast, needs a person to read it. Most `WARNING` rows say something about the source dataset. Some say something about the run itself:
- statuses recorded under another policy
- a `patient_ids` that named a patient the session does not hold
- a `subset` that named a UID the session does not hold

Five grade reasons have no row anywhere else, so the Grade Basis is the only place they appear:
- **an empty audit trail**: a clean ingest followed by `audit()` alone writes no row, and grades `REVIEW_REQUIRED` because nothing the run did is attested.
- **a verb with no evidence**: `anonymize()` or `redact()` did work, and none of the rows it writes reached the audit log.
- **findings nobody acted on**: entities that the last PHI scan left `IDENTIFIED` (condition 7 under [How the grade is decided](#how-the-grade-is-decided)).
- **entities edited after their scan** (condition 8).
- **entities never scanned in a store that holds a recorded status** (condition 9).
:::

:::{admonition} Which losses move the Validation Status
:class: warning

A dropped **private** (odd-group) element grades the session
`REVIEW_REQUIRED`. It may be a vendor block that `remove_private_tags: false`
was set specifically to keep ([The 65534-byte limit](configuration.md#the-65534-byte-limit)).
Nobody outside the vendor can size or identify what went missing.

A dropped **standard** element -- a large Overlay Data plane
`(60xx,3000)`, say -- does not. Those come off ordinary images by the thousand.
A grade that moved on them would read `REVIEW_REQUIRED` for most
cohorts, and would stop carrying information.

Read the Data Loss section on its own terms either way. Its **Scope**
column says which rows were graded: `PRIVATE` and `SIGNAL` rows are,
and `STANDARD` rows are not. `unrecorded` means a row that an older
release wrote, before the scope was recorded. `SIGNAL` is the
standard-group loss that grades. It is acquired content that was in the
source and is not in the export. That content is a discarded waveform multiplex group
(see [Waveforms](waveforms.md)), or a nested icon image dropped because
pixel data is redacted.
:::

:::{admonition} Generate the report after `export()`
:class: warning

`generate_report()` grades the audit log as it stands when you call
it. Losses recorded at ingest are already in it. Losses recorded on
the way *out* -- an element that could not be encoded, a waveform
with no samples -- are written during `export()`. Call
`generate_report()` **after** `export()`, or those losses cannot
reach the Validation Status no matter what group they are in.

A report generated before any export says so itself. When the
audit trail holds no `EXPORT` row, the Executive Summary carries a
boundary note under the grade, and a warning is logged. The note
states the boundary without moving the grade. (An audit-only session
grades `REVIEW_REQUIRED` anyway, for a different reason. `audit()`
writes no audit row, and an empty trail attests nothing -- see
section 5's Grade Basis.)
:::

:::{admonition} Format
:class: tip

The report is Markdown. `generate_report(output_path, format="markdown")` is the default and the only accepted spelling. Any other `format` raises `ValueError` naming it, and no file is written.
:::

## Manifests

`generate_manifest(output_path, format="html")` writes one row per instance the session holds: every instance in `session.store`, whatever an export selected. `format` is `"html"` (the default) or `"json"`, exactly. Any other spelling, a case variant included, raises `ValueError` and writes no file.

```python
session.generate_manifest("manifest.html")
session.generate_manifest("manifest.json", format="json")
```

Each row holds these values:

- the instance's Patient ID
- its Study, Series and SOP Instance UIDs
- its modality
- the manufacturer and model name of its series' equipment
- the path of the file it was ingested from

The values are the session's, as they stand when the manifest is written:

- **After `anonymize()`**, the Patient ID is the replacement and the UIDs are the replacement UIDs, next to the source file path. The manifest is then a crosswalk from each source file to the identifiers its export carries. Keep it with the store, not with an export.
- **A subject whose files carried no Patient ID** is listed under its key. That key is the value that `get_cohort_report()` shows in its `PatientID` column: `\no-patient-id\` followed by the subject's *source* Study Instance UID. No export writes that key.
- **The file path** is the path as `ingest()` walked it, so it is relative when the directory you passed was relative. It is still that path after `redact()` changes an instance's pixels. The instance then no longer matches its source file, but the manifest names the file that the instance was read from. The store keeps that path, so it survives a reopen. An instance with no source file (one built in code) has `null` in JSON and an empty cell in HTML.

The **HTML** manifest is one page. It holds the store file's name, when the manifest was generated, the number of instances, and a table. The table has the columns Patient ID, Study UID, Series UID, Modality, Manufacturer, Model, SOP Instance UID and File Path. It has no column for `anonymized`.

The **JSON** manifest is one object. The example below was written into a store named `my_project.db`, after `ingest()`, `audit()`, `anonymize()` and `export()`. Its input was pydicom's `CT_small.dcm`, and a copy of its `MR_small.dcm` with the Patient ID deleted:

```json
{
  "generated_at": "2026-09-23T21:03:55.218090",
  "project_name": "my_project.db",
  "total_files": 2,
  "items": [
    {
      "patient_id": "ANON_28e5c87cd169c80d0dd05bfb",
      "study_instance_uid": "2.25.297990125461744647168780076193196812719",
      "series_instance_uid": "2.25.188531152787603764149085731129800765062",
      "sop_instance_uid": "2.25.102433379052112046212620522432915392421",
      "file_path": "input/CT_small.dcm",
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
      "modality": "MR",
      "manufacturer": "TOSHIBA_MEC",
      "model_name": "MRT50H1",
      "anonymized": true
    }
  ]
}
```

The pseudonym and the replacement UIDs come from the store's project secret, so another store gives other values.

`generated_at` is local time with no UTC offset, `project_name` is the store file's name, and `total_files` is the number of items. The manifest carries no file sizes.

An item's **`anonymized`** is `true` when two things are true. First, the last tag-policy PHI scan left no identifier unremediated on the instance's patient, its study or the instance itself. Second, none of the three has been edited since that scan. It is `false` in each of these cases:

- the scan never ran on one of the three
- one of them was edited after the scan
- a remediation on one of them was declined in its last pass

`anonymized` does not mean "`anonymize()` ran": a file the scan found clean reads `true` after `audit()` alone. It says nothing about burned-in text in the pixels, which the tag scan does not read. The series is not consulted, because the scan records no status on a series. `session.phi_status_summary()` gives the same statuses as counts.

`generate_manifest()` and its two formats are frozen at 1.0. The JSON keys, the meaning of `anonymized` and the HTML layout are documented but internal. A 1.x release may change them, with a CHANGELOG entry ([API stability](api/stability.md)).

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

**How the columns are named.** The base columns are keywords: `PatientID`, `PatientName`, `StudyInstanceUID`, `StudyDate`, `SeriesInstanceUID`, `Modality`, `SOPInstanceUID`, `Manufacturer`, `Model` and `DeviceSerial`. The columns that `expand_metadata=True` adds are lowercase `gggg,eeee` tag keys. Slice Thickness is `df["0018,0050"]`, not `df["SliceThickness"]`. The values are the session's as they stand, so before `anonymize()` the frame holds the original identifiers.

### 2. Write to CSV or Parquet

`export_dataframe()` builds the same frame as `get_cohort_report()`, writes it to a file and returns it. It takes the same `expand_metadata` argument, also `False` by default, so pass `expand_metadata=True` for the tag columns. Either format holds the expanded frame. The format follows the extension: Parquet for `.parquet`, CSV for anything else.

In Parquet, a multi-valued tag such as Image Type is a list column when its values are uniform. Uniform means that every value present is a list whose elements are all of one kind (all text, all numbers, all dates, and so on). A column that is not uniform is written as text, each value as the CSV writes it. Two examples are a tag that is single-valued in one file and multi-valued in another, and a tag that is text in one file and a number in another. Missing values are null in both formats, never the text `nan`, and every other column keeps its type.

The path is required. If you call `export_dataframe()` before `anonymize()`, the file holds the original identifiers. So name a place where PHI may live, or use `get_cohort_report()` when you only want the frame. A call with no path raises `TypeError` and writes nothing.

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
UID. A UID that names nothing in the session is not silently dropped.
The export writes what the rest selects, and one `WARNING` audit row
counts the UIDs that name nothing, by position. So the report grades
`REVIEW_REQUIRED`.

```python
session.export("export_selected_series", subset=target_series)
```

# Analytics & Reporting

Isocenter is designed not just for de-identification, but for understanding your data. It includes built-in tools for compliance verification, cohort analysis, and data exploration.

## Compliance Reports

For regulatory audits (HIPAA/GDPR), Isocenter can generate a formal **Compliance Report**. This single-document artifact summarizes the entire session, ensuring transparent documentation of your de-identification process.

```python
# Generate a Markdown report
session.generate_report("compliance_report.md")
```

The report includes:

1. **Executive Summary**: the grade (`PASS` / `REVIEW_REQUIRED`), how many patients and instances the session holds, how many instances were written of those requested when an export ran, the privacy profile and de-identification method, and the pre-export boundary note described below.
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
   with no PatientID to seed its jitter, a value that is not a date --
   each with its reason. The element each row names is still in the
   object graph and reached the exported files, so one row grades the
   session `REVIEW_REQUIRED` on the same argument 3.2 makes. Unlike 3.1
   and 3.2, 3.3 is omitted entirely from a run that declined nothing.
4. **Exceptions & Errors**: every `ERROR` and `WARNING` audit row, plus report-time checks (`COMPLIANCE_CHECK`, `AUDIT_DROP`). Any row here grades the run `REVIEW_REQUIRED`. An `ERROR` means something requested failed -- a file refused at ingest, an instance that failed to write. A `WARNING` means the run did what it should but something about the *source data* could not be honoured or read, or was deliberately held back: a file declined because its SOP Instance UID is already held, an instance `export(check_burned_in=True)` withheld because it still carries an identifier (not written, and nothing failed -- the pipeline declined it; [#536](https://github.com/kvnlng/Isocenter/issues/536)), an instance OCR could not read, a store de-identified before 0.9.6 (see [Migration Tools](migration.md)), or a Photometric Interpretation the written transfer syntax does not admit, written as declared because correcting it would invent a claim.
5. **Validation & Verification**: the **Grade Basis** -- every reason this run is not `PASS`, one line each, or a statement that nothing costs it its `PASS` -- then how many `REMEDIATION_*` rows the audit trail holds, what each `scan_pixel_content()` call in this session read and could not read, and the configured method. When the grade surprises you, read the Grade Basis first: it names the section that holds the row.

A per-instance manifest is not part of the report; it is a separate document written by `generate_manifest()`.

!!! note "A `WARNING` row is about your data; a correction is not reported"

    What you will **not** find in section 4 is Isocenter correcting a descriptor of its own making -- PixelRepresentation or BitsStored rewritten to match the pixels actually written. Those corrections are exact, lose nothing, and say nothing about your data, so they are logged at `INFO` and are neither recorded in the audit log nor graded. The default console handler shows `WARNING` and above, so they do not appear on screen either. A `WARNING` row, by contrast, always says something about the source dataset and needs a person to read it.

    Two grade reasons have no row anywhere else, so the Grade Basis is the only place they appear: **an empty audit trail** (a clean ingest followed by `audit()` alone writes no row, and grades `REVIEW_REQUIRED` because nothing the run did is attested), and **a verb with no evidence** (`anonymize()` or `redact()` did work and none of the rows it writes reached the audit log).

!!! warning "Which losses move the Validation Status"

    A dropped **private** (odd-group) element grades the session
    `REVIEW_REQUIRED`. It may be a vendor block `remove_private_tags=False`
    was set specifically to keep, and nobody outside the vendor can size
    or identify what went missing.

    A dropped **standard** element -- a large Overlay Data plane
    `(60xx,3000)`, say -- does not. Those come off ordinary images by the thousand,
    so a grade that moved on them would read `REVIEW_REQUIRED` for most
    cohorts and stop carrying information.

    Read the Data Loss section on its own terms either way: its **Scope**
    column says which rows were graded -- `PRIVATE` and `SIGNAL` rows are,
    `STANDARD` rows are not -- and `unrecorded` means a row written by a
    version that predated the distinction. `SIGNAL` is the
    standard-group loss that grades: acquired content that was in the
    source and is not in the export -- a discarded waveform multiplex group
    ([#150](https://github.com/kvnlng/Isocenter/issues/150); see
    [Waveforms](waveforms.md)), or a nested icon image dropped because
    pixel data is redacted
    ([#542](https://github.com/kvnlng/Isocenter/issues/542)).

!!! warning "Generate the report after `export()`"

    `generate_report()` grades the audit log as it stands when you call
    it. Losses recorded at ingest are already in it; losses recorded on
    the way *out* -- an element that could not be encoded, a waveform
    with no samples -- are written during `export()`. Call
    `generate_report()` **after** `export()`, or those losses cannot
    reach the Validation Status no matter what group they are in.

    A report generated before any export says so itself
    ([#153](https://github.com/kvnlng/Isocenter/issues/153)): when the
    audit trail holds no `EXPORT` row, the Executive Summary carries a
    boundary note under the grade and a warning is logged. The note
    states the boundary without moving the grade. (An audit-only session
    grades `REVIEW_REQUIRED` anyway, for a different reason: `audit()`
    writes no audit row, and an empty trail attests nothing -- see
    section 5's Grade Basis.)

!!! tip "Format Options"
    Currently, Isocenter supports Markdown (`.md`) reports. PDF support is planned for future releases via Pandoc integration.

---

## Cohort Analysis (EDA)

Isocenter treats your DICOM data as a **structured database**, not just a pile of files. You can leverage the `export_dataframe` method to extract a flattened inventory of your cohort for analysis with Pandas, Jupyter, or Tableau.

### 1. Export to Pandas

```python
# Export inventory to a Pandas DataFrame
# expand_metadata=True parses the JSON attributes into columns
df = session.export_dataframe(expand_metadata=True)

# Inspect the data
print(df.head())
print(df.groupby('Modality').size())
```

### 2. Parquet Export

For massive datasets (100k+ images), exporting to Parquet is recommended for performance and compatibility with external BI tools (PowerBI, Tableau, Apache Spark).

The format follows the extension -- `export_dataframe` writes Parquet for
`.parquet` and CSV for anything else. It is the same method, and the same
columns, either way.

```python
# Export full cohort to Parquet
session.export_dataframe("cohort_inventory.parquet")

# Or just part of it
session.export_dataframe("arm_a.parquet", patient_ids=["P001", "P002"])
```

!!! warning "`export_to_parquet` was removed in #55"
    A second Parquet writer, `session.export_to_parquet(...)`, existed
    until 0.9. It read from the **database** rather than the in-memory
    graph, so the two methods could disagree about the cohort, and it
    emitted SQL column names (`patient_id`, `sop_instance_uid`) where
    `export_dataframe` emits DICOM keywords (`PatientID`,
    `SOPInstanceUID`). Calling it now raises `AttributeError`. Code
    reading its output needs the column names updated, not just the call.

---

## Query-Based Export

One of Isocenter's most powerful features is **Query-Based Export**. Instead of exporting the entire session, you can filter the export using Pandas-style queries or a subset DataFrame.

### Use Case: "Export only thick-slice CTs"

```python
# 1. Get the inventory
df = session.export_dataframe(expand_metadata=True)

# 2. Define your criteria (Standard Pandas syntax)
# e.g., Keep only CT scans with SliceThickness > 2.5mm
subset = df[ 
    (df['Modality'] == 'CT') & 
    (df['SliceThickness'].astype(float) > 2.5) 
]

print(f"Filtering: {len(df)} -> {len(subset)} instances.")

# 3. Feed the subset back into the exporter
session.export("export_thick_cts", subset=subset)
```

### Use Case: "Export List of Accession Numbers"

You can also filter by a list of strict identifiers if you have an external manifest.

```python
# Filter by Series Instance UIDs
target_series = ["1.2.840...", "1.2.840..."]

# Filter the dataframe
subset = df[df['SeriesInstanceUID'].isin(target_series)]

session.export("export_selected_series", subset=subset)
```

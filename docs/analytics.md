# Analytics & Reporting

Isocenter is designed not just for de-identification, but for understanding your data. It includes built-in tools for compliance verification, cohort analysis, and data exploration.

## Compliance Reports

For regulatory audits (HIPAA/GDPR), Isocenter can generate a formal **Compliance Report**. This single-document artifact summarizes the entire session, ensuring transparent documentation of your de-identification process.

```python
# Generate a Markdown report
session.generate_report("compliance_report.md")
```

The report includes:

1. **Validation Status**: Uses Isocenter's internal audit logic to grade the session (PASS / REVIEW_REQUIRED).
2. **Audit Trail**: Aggregated counts of actions taken (e.g., number of patients anonymized, pixels redacted).
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
   call itself PASS.
4. **Exceptions**: A detailed list of any warnings or errors encountered (e.g., "Corrupt pixel data in File X", "Burned-In Annotation found").
5. **Manifest**: A summary of the processed cohort (Top studies by size).

!!! warning "Which losses move the Validation Status"

    A dropped **private** (odd-group) element grades the session
    `REVIEW_REQUIRED`. It may be a vendor block `remove_private_tags=False`
    was set specifically to keep, and nobody outside the vendor can size
    or identify what went missing.

    A dropped **standard** element -- Overlay Data `(60xx,3000)`, the
    palette color LUTs `(0028,120x)` -- does not. Those come off ordinary
    images by the thousand, so a grade that moved on them would read
    `REVIEW_REQUIRED` for most cohorts and stop carrying information.

    Read the Data Loss section on its own terms either way: its **Scope**
    column says which rows were graded, and `unrecorded` means a row
    written by a version that predated the distinction. One case sits on
    the line: a discarded waveform multiplex group is standard-group and
    so does not move the grade -- see
    [#150](https://github.com/kvnlng/Isocenter/issues/150).

!!! warning "Generate the report after `export()`"

    `generate_report()` grades the audit log as it stands when you call
    it. Losses recorded at ingest are already in it; losses recorded on
    the way *out* -- an element that could not be encoded, a waveform
    with no samples -- are written during `export()`. Call
    `generate_report()` **after** `export()`, as the README's pipeline
    does, or those losses cannot reach the Validation Status no matter
    what group they are in. Which order the pipeline should document is
    [#153](https://github.com/kvnlng/Isocenter/issues/153).

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

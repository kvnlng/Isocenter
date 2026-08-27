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
3. **Exceptions**: A detailed list of any warnings or errors encountered (e.g., "Corrupt pixel data in File X", "Burned-In Annotation found").
4. **Manifest**: A summary of the processed cohort (Top studies by size).

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

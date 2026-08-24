# Quick Start

## 1. Initialize a Session

Gantry uses a **persistent session** to manage your workflow. Unlike scripts that run once and forget, a Session creates a local SQLite database (`gantry.db`) to index your data. This allows you to pause, resume, and audit your work without re-scanning thousands of files.

```python
from gantry import Session

# Initialize a new session (creates 'gantry.db' by default)
session = Session("my_project.db")
```

## 2. Ingest & Examine

Ingestion builds a lightweight **metadata index** of your DICOM files. Gantry scans your folders recursively, extracting patient/study/series information into the database *without moving or modifying your original files*. It is resilient to nested directories and non-DICOM clutter.

```python
session.ingest("/path/to/dicom/data")
session.save() # Persist the index to disk

# Print a summary of the cohort and equipment
session.examine()
```

## 3. Configure & Audit

Before changing anything, define your privacy rules. Use `create_config` to generate a scaffolding based on your inventory, then `audit` to scan that inventory against your rules. This "Measure Twice, Cut Once" approach lets you identify all PHI risks before applying any irreversible changes.

```python
# Create a default configuration file (v2.0 YAML)
session.create_config("config.yaml")

# Load the configuration (rules, tags, jitter)
session.load_config("config.yaml")

# Run an audit to find PHI
report = session.audit() 
session.save_analysis(report)

print(f"Found {len(report)} potential PHI issues.")
```

## 4. Backup Identity (Optional)

To enable reversible anonymization, generate a cryptographic key and "lock" the original patient identities into a secure, encrypted DICOM tag. This must be done *before* anonymization.

```python
# Enable encryption (generates 'gantry.key')
session.enable_reversible_anonymization()

# cryptographically lock identities for all patients found in the audit
# Optional: Specify custom tags to preserve (defaults to Name, ID, DOB, Sex, Accession)
session.lock_identities(report, tags_to_lock=["0010,0010", "0010,0020", "0010,0030"])
session.save()
```

## 5. Anonymize, Redact & Export

Remediation is a multi-stage process performed in-memory:

1. **Anonymize**: Strips or replaces metadata tags (PatientID, Names, Dates) based on your config.
2. **Redact**: Loads pixel data and scrubs burned-in PHI from defined regions.
3. **Export**: The final "Gatekeeper". Writes clean files to a new directory. Setting `check_burned_in=True` ensures the export halts if any verification checks fail (e.g., corrupt images or missing codecs).

```python
# Apply metadata remediation (anonymization) using the findings
session.anonymize(report)

# Apply pixel redaction rules (requires config to be loaded)
session.redact()

# Export only safe (clean) data to a new folder
# use_compression=True optionally compresses output to JPEG 2000
session.export("/path/to/export_clean", check_burned_in=True, use_compression=True)
```

Progress for the save, memory release, and export phases will be displayed:

```text
Preparing for export (Auto-Save & Memory Release)...
Releasing Memory: 100%|██████████| 5000/5000 [00:02<00:00, 2000.00img/s]
Memory Cleanup: Released 5000 images from RAM.
Executing Redaction Rules...
Redacting: 100%|██████████| 150/150 [00:05<00:00, 28.00img/s]
Exporting session to: /path/to/export_clean
Exporting:  15%|██▌       | 15/100 [00:05<00:30,  2.80patient/s]
```

## 6. Recover Identity (Optional)

If you have a valid key (`gantry.key`) and need to retrieve the original identity of an anonymized patient:

```python
# Load the session containing anonymized data
session = Session("my_project.db")
session.enable_reversible_anonymization("gantry.key")

# Recover the original PatientName and PatientID
# Recover the original identity and restore attributes in-memory
# restore=True (default) automatically updates all instances with original values
session.recover_patient_identity("ANON_12345", restore=True)

# Now, accessing p.patient_name or instance attributes returns original data
print(f"Restored: {session.store.patients[0].patient_name}")
```

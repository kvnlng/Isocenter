# Quick Start

## 1. Initialize a Session

Isocenter uses a **persistent session** to manage your workflow. Unlike scripts that run once and forget, a Session creates a local SQLite database (`isocenter.db`) to index your data. This allows you to pause, resume, and audit your work without re-scanning thousands of files.

```python
from isocenter import Session

# Initialize a new session (creates 'isocenter.db' by default)
session = Session("my_project.db")
```

!!! tip "Context Manager"
    `Session` supports the `with` statement: `with Session("my_project.db") as session:`. On exit it calls `session.close()` for you, releasing the background threads and worker pool the session holds -- steps 2-6 below work the same way indented inside that block. Step 7 ("Recover Identity") opens a *separate* `Session`, so it needs its own `with` block (or its own `close()` call) rather than being nested inside the first one.

    Leaving the block does **not** save: edits made since the last `save()` or `export()` are dropped, and `close()` warns naming the instances. Call `session.save(sync=True)` first to keep them. `export()` saves the session itself before it writes, so after an export the store holds the de-identified graph, and the rows of patients whose identifier was replaced are removed.

!!! warning "Scripts need a main guard"
    Isocenter starts its worker processes by *spawn* on every platform and every Python build, and a spawned worker re-imports the script that launched it. In a `.py` file, put everything that uses the session under `if __name__ == "__main__":`. Without it the first `ingest()` fails with `BrokenProcessPool` and a `RuntimeError` about the bootstrapping phase. Notebooks and the interactive interpreter need no guard.

## 2. Ingest & Examine

Ingestion builds a lightweight **metadata index** of your DICOM files. Isocenter scans your folders recursively, extracting patient/study/series information into the database *without moving or modifying your original files*. It is resilient to nested directories and non-DICOM clutter.

```python
session.ingest("/path/to/dicom/data")
session.save() # Persist the index to disk

# Print a summary of the cohort and equipment
session.examine()
```

## 3. Configure & Audit

Before changing anything, define your privacy rules. Use `create_config` to generate a scaffolding based on your inventory, then `audit` to scan that inventory against your rules. This "Measure Twice, Cut Once" approach lets you identify all PHI risks before applying any irreversible changes. Skipping this step does not skip de-identification: a session that has loaded no configuration still applies a **floor policy** of 620 tag rules (the PS3.15 Annex E Basic Profile table, 2026c, with UIDs not yet replaced, plus Study Date jittered, Sex and Age kept), and removes private tags. The config file is where you record the policy you actually want; see [Configuration](configuration.md#privacy-profile).

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

To enable reversible anonymization, generate a cryptographic key and "lock" the original patient identities into a secure, encrypted DICOM tag. This must be done *before* anonymization: locking after `anonymize()` raises `RuntimeError`, because there is no original value left to stash, and so does locking before `enable_reversible_anonymization()`. Locking again before anonymizing replaces the stored token, and the lock replaces any Encrypted Attributes Sequence `(0400,0500)` the source file already carried.

```python
# Enable encryption (generates 'isocenter.key')
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
3. **Export**: The final "Gatekeeper". Writes clean files to a new directory. With `check_burned_in=True` the export runs `audit()` first and skips every instance that still carries an identifier, on itself or a parent, under the policy in force -- so on a session that has not run `anonymize()`, that is every instance carrying a value any rule of the policy would act on. The skip is a logged warning only: it writes no audit row and is not counted in the report, so a run that withheld every instance can still grade `PASS` ([#536](https://github.com/kvnlng/Isocenter/issues/536)). Compare the returned `ExportSummary` against the cohort to see what was held back.

```python
# Apply metadata remediation (anonymization) using the findings
session.anonymize(report)

# Apply pixel redaction rules (requires config to be loaded)
session.redact()

# Export only safe (clean) data to a new folder
# Compression is on by default (lossless JPEG 2000); use_compression=False writes uncompressed
session.export("/path/to/export_clean", check_burned_in=True, use_compression=True)
```

`session.redact()` returns how many instances had at least one configured
zone applied to their pixels. An instance a rule matched but whose every
zone fell outside the image is not counted, and nothing is written onto
it. A zone with no area (end not greater than start on either axis) is
not a skip: it fails the instance, which is left as it was found, and the
pass raises `RedactionError`.

Each redacted instance is a new, derived image: it takes a **new SOP
Instance UID**, so its exported filename is not its source's, and it no
longer points at the file it was ingested from. This happens on every
redaction, not only under `force=True` below.

### What the export writes

Files land at
`<folder>/Subject_<PatientID>/Study_<date>_<description>_<uid>/Series_<number>_<modality>_<description>_<uid>/<SOPInstanceUID>.dcm`.
The directory names are built from the values being exported, so **run
`anonymize()` first**; otherwise the real Patient ID and descriptions
appear in the paths. Each file is written under a temporary name and
renamed when complete, so a crash never leaves a partial file under a
real name; a stray `*.tmp` left by a killed worker is safe to delete.

**Compression is on by default** and lossless, but it changes how colour
images are labelled. An `RGB` image is encoded with JPEG 2000's reversible
colour transform and declared `YBR_RCT`, as the standard requires; a YBR
source that decodes to RGB is already stored as `RGB`. 32- and 64-bit
images cannot be compressed and fail export with a message naming
`use_compression=False`. If a recipient's reader cannot handle JPEG 2000,
export with `use_compression=False`.

**`verify_readback=True`** decodes every file it writes through the same
decoder `ingest()` uses and compares every pixel sample with what it meant
to write. It also refuses a Photometric Interpretation the file's transfer
syntax does not admit (for example `YBR_ICT` on an uncompressed file). An
instance that fails is not delivered: it gets an `ERROR` audit row, appears
in `ExportSummary.failures`, and grades the run `REVIEW_REQUIRED`. Without
verification that same inadmissible label is written as declared with a
`WARNING` row, which also grades `REVIEW_REQUIRED` -- so turning
verification on can cost you a file the default export would have
delivered, by design.

Once any instance in the store has been redacted, or any loaded rule has
redaction zones, the export removes every small preview image (Icon Image
Sequence) from every file it writes, redacted or not, because nothing
scans or redacts icons. Each removal is a `DATA_LOSS` row and does not
change the grade ([#542](https://github.com/kvnlng/Isocenter/issues/542)).

!!! warning "Redacted on 0.9.0 or earlier with a multi-zone rule?"

    Releases up to and including 0.9.0 applied only the last applicable
    zone of a multi-zone rule to an instance loaded from a saved store,
    and still recorded a full redaction. Because that record is a hash of
    the *configuration* rather than of the pixels, the corrected code
    agrees with it and skips the instance: `session.redact()` returns `0`
    and the burned-in identifier stays where it is.

    If you redacted with a rule carrying two or more zones, against a
    store that had been saved and reopened, on 0.9.0 or earlier, repair it
    with:

    ```python
    session.redact(force=True)
    session.save()
    ```

    No source file is needed -- the identifier is still in the store's own
    pixels. The cost: every instance the rules match is redacted again,
    and each takes a **new SOP Instance UID**, so its exported filename
    changes and it stops matching the source file it was ingested from.

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

## 6. Report

Generate the compliance report **last, after `export()`**: export is where the final data-loss, error and warning rows are written, and the report grades only what the audit log holds when you call it. A report generated before any export says so in its own Executive Summary.

```python
session.generate_report("compliance_report.md")
```

See [Analytics & Reporting](analytics.md) for what each section means and how the grade is decided.

## 7. Recover Identity (Optional)

If you have a valid key (`isocenter.key`) and need to retrieve the original identity of an anonymized patient, load the session under that key. `enable_reversible_anonymization()` **creates a new key** when none exists at the path you give it, so point it at the key the data was locked with. Under any other key recovery finds nothing: `recover_patient_identity()` prints `No encrypted identity token found or decryption failed.` and returns `None` ([#539](https://github.com/kvnlng/Isocenter/issues/539)):

```python
# Load the session containing anonymized data
session = Session("my_project.db")
session.enable_reversible_anonymization("isocenter.key")

# Recover the original PatientName and PatientID
# Recover the original identity and restore attributes in-memory
# restore=True (default) automatically updates all instances with original values
session.recover_patient_identity("ANON_5b5ce7b47f254ef3a0d90c0f", restore=True)

# Now, accessing p.patient_name or instance attributes returns original data
print(f"Restored: {session.store.patients[0].patient_name}")
```

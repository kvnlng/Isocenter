# Quick Start

This page walks the pipeline once, step by step, on your own data. Each step is one or two calls. For a walkthrough that runs as written on files bundled with pydicom, and shows each step's output, start with the tutorial [De-identify a cohort and read the grade](tutorials/deidentify-and-read-the-grade.md).

## 1. Initialize a Session

A `Session` keeps its work in a **session store**, so you can pause, resume and audit a job without re-scanning thousands of files.

```python
from isocenter import Session

session = Session("my_project.db")
```

The store is the file you name (with no name, `$ISOCENTER_DB_PATH`, else `isocenter.db`) plus a sidecar beside it. `Session("my_project.db")` creates:

- `my_project.db`, the SQLite index, with its `-wal` and `-shm` files;
- `my_project_pixels.bin`, the pixel and waveform data (two `.lock` files appear beside it at the first `ingest()`);
- `isocenter.log` in the current directory.

!!! warning "The session store holds PHI"
    `my_project.db` and `my_project_pixels.bin` keep the original identifiers and pixels. Until `export()`, the session writes only the store, `isocenter.log` and files you name (a configuration, a key). Keep the store where PHI may live, and do not hand it out with the export.

!!! tip "Context manager"
    `Session` supports the `with` statement: `with Session("my_project.db") as session:`. On exit it calls `session.close()`, which releases the session's background threads and worker pool. Steps 2 to 6 work the same way inside that block. Step 7 opens a separate `Session`, with its own `with` block.

    Leaving the block does **not** save: edits made since the last `save()` or `export()` are dropped, and `close()` warns naming the instances. Call `session.save(sync=True)` first to keep them. `export()` saves the session itself before it writes.

!!! warning "Scripts need a main guard"
    Isocenter starts its worker processes by *spawn*, and a spawned worker re-imports the script that launched it. In a `.py` file, put everything that uses the session under `if __name__ == "__main__":`. Without it the first `ingest()` fails with `BrokenProcessPool`. Notebooks and the interactive interpreter need no guard.

## 2. Ingest & Examine

Ingest reads your folders recursively and indexes every DICOM file into the store. It never moves or modifies the source files. It tries every file it finds except hidden ones (names starting with `.`): a file that is not DICOM is rejected, not skipped (see below).

```python
summary = session.ingest("/path/to/dicom/data")
session.save()

print(summary)      # IngestSummary(ingested=..., failures=[...], declined=..., skipped=...)
session.examine()   # the cohort and its equipment
```

`ingest()` does not raise for a file it cannot read. It returns an `IngestSummary` that puts every file in one of four places: `ingested`, `failures` (one `(path, reason)` pair per rejected file), `declined` (its SOP Instance UID is already in the session) and `skipped` (an earlier `ingest()` already read it). A rejected or declined file also writes an audit row, so the report grades `REVIEW_REQUIRED` and names it. Check the summary; the [`ingest()` reference](api/session.md) has the details.

## 3. Configure & Audit

Write down the policy you want, then measure the cohort against it before anything changes.

```python
session.create_config("config.yaml")   # a scaffold built from the inventory
# edit config.yaml for your protocol
session.load_config("config.yaml")

report = session.audit()
session.save_analysis(report)
print(f"{len(report)} findings")
```

`audit()` checks the tags against the policy. Each finding is one value a rule acts on, so a small cohort can have hundreds. It does not read the pixels.

With no configuration loaded, a **floor policy** of 646 tag rules still applies: the PS3.15 Annex E Basic Profile table (2026c), with Study Date jittered and Patient's Sex and Age kept, and private tags removed. The config file is where you record the policy you actually want; see [Configuration](configuration.md#privacy-profile).

The scaffold lists each machine in the cohort that has a Device Serial Number, with empty `redaction_zones`. `redact()` changes nothing until you fill them in: see [Pixel Redaction (Machines)](configuration.md#pixel-redaction-machines), and [Burned-in text (OCR)](ocr.md) to find where a machine writes text.

## 4. Backup Identity (Optional)

Reversible anonymization encrypts each patient's original identifiers into the Encrypted Attributes Sequence `(0400,0500)` of their files, so whoever holds the key can recover them later. Lock **before** `anonymize()`.

```python
# Keep the key outside the project: whoever holds the key and an export
# can read the identities in it. The first lock creates the key file.
session.enable_reversible_anonymization("/secure/keys/my_project.key")

# Lock every patient the audit found, by the Patient ID in the findings.
# tags_to_lock is optional (default: Name, ID, Birth Date, Sex, Accession Number).
locked = session.lock_identities(report, tags_to_lock=["0010,0010", "0010,0020", "0010,0030"])
print(locked)   # <LockingResult: N instances secured>
session.save()
```

The lock replaces any Encrypted Attributes Sequence `(0400,0500)` the source file already carried, unless that sequence holds an Isocenter identity token whose values the new one would change: then the lock refuses with `RuntimeError`. `lock_identities()` before `enable_reversible_anonymization()` raises `RuntimeError`. Locking again before anonymizing replaces the stored token, unless the new token would lose a value the existing one holds: then it raises `RuntimeError` and writes nothing. Encryption is Fernet (AES-128-CBC with HMAC-SHA256) from the `cryptography` package.

Locking after `anonymize()` secures nothing. Given the report, `lock_identities()` finds none of its Patient IDs (they have been replaced), logs one `ERROR`, returns an empty result and still creates the key file. Given a patient's new ID, it raises `RuntimeError`. Check the count it returns.

`Session()` loads a key by itself only from `./isocenter.key` in the current working directory. A key kept anywhere else is named with `enable_reversible_anonymization(path)`, as above. [What to keep](configuration.md#what-to-keep) lists the key, the store and the configuration, and what each is for.

Worked example: [Reversible anonymization: keep a way back](tutorials/reversible-anonymization.md) locks, exports, and recovers one patient.

## 5. Anonymize, Redact & Export

`anonymize()` and `redact()` change the in-memory graph. `export()` writes the result to a new directory.

```python
session.anonymize(report)   # remove, replace or shift tags, as the policy says
session.redact()            # blank the configured zones on matching machines
```

`anonymize()` acts on the findings it is given. `redact()` returns how many instances had at least one zone applied, and each redacted instance takes a new SOP Instance UID.

### Verify

Audit again before exporting. A clean run leaves no findings, and every patient, study and instance reads `CLEARED` or `REMEDIATED`.

```python
remaining = session.audit()
print(len(remaining))                 # 0 when nothing is left to change
print(session.phi_status_summary())
```

### Export

```python
# Lossless JPEG 2000 by default; use_compression=False writes uncompressed.
session.export("/path/to/export_clean", check_burned_in=True)
```

Files land at `<folder>/Subject_<PatientID>/Study_…/Series_…/<SOPInstanceUID>.dcm`. The folder names are built from the exported values, so run `anonymize()` first.

`check_burned_in=True` runs `audit()` again and withholds every instance that still carries an identifier in its tags, with a `WARNING` row for each. It checks tags, not pixels. Burned-in text is handled by redaction zones and found by `scan_pixel_content()`.

### What the export writes

[What the export writes](export-output.md) covers the layout, compression and colour images, the de-identification markers, and what `check_burned_in=True` and `verify_readback=True` add.

## 6. Report

Generate the compliance report **last, after `export()`**. Export writes the final data-loss, error and warning rows, and the report grades only what the audit log holds when you call it. A report generated before any export says so.

```python
session.generate_report("compliance_report.md")
```

See [Analytics & Reporting](analytics.md) for what each section means and how the grade is decided.

## 7. Recover Identity (Optional)

To recover a patient's original identity, open the session under the key the data was locked with, and name the patient by the ID it was exported under: the `ANON_…` value in the exported files' Patient ID `(0010,0020)`, which is also in the `Subject_ANON_…` folder name.

```python
with Session("my_project.db") as session:
    session.enable_reversible_anonymization("/secure/keys/my_project.key")

    # Read the original identity without writing it back.
    identity = session.recover_patient_identity("ANON_5b5ce7b47f254ef3a0d90c0f", restore=False)
    first = next(iter(identity.values()))   # the patient-level answer
    print(first["0010,0020"], first["0010,0010"])   # original Patient ID and name

    # Or write the original values back onto the instances, in memory.
    session.recover_patient_identity("ANON_5b5ce7b47f254ef3a0d90c0f", restore=True)
    session.save(sync=True)
```

`recover_patient_identity()` returns a `dict` mapping the SOP Instance UID of each instance that carries an identity token to the values that token holds, in study, series and instance order. The first entry speaks for the patient. `restore=False` only reads, which also checks that the patient is recoverable under the key.

It prints nothing, and raises when it cannot recover: `FileNotFoundError` when no key file exists at that path, `ValueError` when no patient in the session holds the ID, and `RuntimeError` when the patient has no identity token or the key does not open it. No message names the Patient ID.

Restore puts back the locked tags only. Every other date stays shifted by the patient's offset, so intervals are intact. [Reversible anonymization: keep a way back](tutorials/reversible-anonymization.md) works through recovery and what happens without the key.

## Next: the tutorials

Each tutorial follows one question end to end over files bundled with pydicom, and every code block runs in the test suite:

- [De-identify a cohort and read the grade](tutorials/deidentify-and-read-the-grade.md)
- [Select part of a cohort](tutorials/select-part-of-a-cohort.md)
- [Reversible anonymization: keep a way back](tutorials/reversible-anonymization.md)
- [Redact burned-in text for one machine](tutorials/redact-burned-in-pixels.md)
- [Write your own export format](tutorials/write-an-exporter.md)

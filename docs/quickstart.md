# Quick Start

This page walks the pipeline once, step by step, on your own data. Each step is one or two calls. For a walkthrough on files bundled with pydicom, start with the tutorial [De-identify a cohort and read the grade](tutorials/deidentify-and-read-the-grade.md). That tutorial runs as written and shows the output of each step.

## 1. Initialize a Session

A `Session` keeps its work in a **session store**. So you can pause, resume and audit a job without re-scanning thousands of files.

```python
from isocenter import Session

session = Session("my_project.db")
```

The store is the file you name, plus a sidecar beside it. With no name, the store is `$ISOCENTER_DB_PATH`, else `isocenter.db`. `Session("my_project.db")` creates these files:

- `my_project.db`, the store's SQLite file, with its `-wal` and `-shm` files.
- `my_project_pixels.bin`, the pixel and waveform data. Two `.lock` files appear beside it at the first `ingest()`.
- `isocenter.log` in the current directory.

:::{admonition} Keep the store on local disk
:class: warning

The store is the `.db` with its `-wal` and `-shm` files, and the `_pixels.bin` with its two `.lock` files. Keep the store on a filesystem that is local to the machine that runs the session. Open a store from one host at a time.

The store uses SQLite's WAL mode. SQLite documents that WAL mode does not work over a network filesystem. The store's locks are `flock`. The behaviour of `flock` on NFS, SMB, Lustre and GPFS depends on the mount. Isocenter has not been tested on one.

`Session()` recognises some network filesystems: NFS, SMB, AFP, WebDAV, Lustre, GPFS, Ceph, BeeGFS, GlusterFS, SSHFS and a few others. If the store is on one of them, `Session()` logs one `WARNING` that says so. Then it opens the store anyway. `Session()` cannot recognise every network filesystem. So no warning is not proof that the disk is local.

With `Session(":memory:")`, the pixel file goes in the temporary directory (`$TMPDIR`). That directory should be local too.

The source files that `ingest()` reads, and the folder that `export()` writes, may be on network storage. Ingest only reads. Export writes each file under a temporary name beside its target, then renames it, with no lock.
:::

:::{admonition} The session store holds PHI
:class: warning

`my_project.db` and `my_project_pixels.bin` keep the original identifiers and pixels. Until `export()`, the session writes only the store, `isocenter.log` and files you ask for. Those files are a configuration, a key, a report, a manifest or a cohort table. Keep the store where PHI may live. Do not send the store with the export.
:::

:::{admonition} Context manager
:class: tip

`Session` supports the `with` statement: `with Session("my_project.db") as session:`. On exit, it calls `session.close()`. `close()` releases the session's background threads and worker pool. Steps 2 to 6 work the same way inside that block. Step 7 opens a separate `Session`, with its own `with` block.

Leaving the block does **not** save. Edits made since the last `save()` or `export()` are dropped, and `close()` warns with the names of the instances. To keep the edits, call `session.save(sync=True)` first. `export()` saves the session itself before it writes.
:::

:::{admonition} Scripts need a main guard
:class: warning

Isocenter starts its worker processes by *spawn*. A spawned worker re-imports the script that started it. In a `.py` file, put everything that uses the session under `if __name__ == "__main__":`. Without the guard, the first `ingest()` fails with `BrokenProcessPool`. Notebooks and the interactive interpreter need no guard.
:::

## 2. Ingest & Examine

Ingest reads your folders recursively and adds every DICOM file to the store. It never moves or modifies the source files. It tries every file it finds, except hidden ones. A hidden file is one whose name starts with `.`. Ingest still searches a hidden folder. A file that is not DICOM is refused, not skipped (see below).

```python
summary = session.ingest("/path/to/dicom/data")
session.save()

print(summary)      # IngestSummary(ingested=..., failures=[...], declined=..., skipped=..., hidden=...)
session.examine()   # the cohort and its equipment
```

`ingest()` does not raise for a file it cannot read. It returns an `IngestSummary` that puts every file in one of five places:

- `ingested`.
- `failures`: one `(path, reason)` pair per refused file.
- `declined`: the file's SOP Instance UID is already in the session. Or the file's Study Instance UID belongs to a patient with a different Patient ID.
- `skipped`: an earlier `ingest()` already read the file.
- `hidden`: the file's name starts with `.`, such as `.DS_Store`, so it was not read. A directory whose name starts with `.` is still walked.

A refused or declined file also writes an audit row, so the report grades `REVIEW_REQUIRED` and names the file. Check the summary. The [`ingest()` reference](api/session.md) has the details.

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

`audit()` scans the tags against the policy. Each finding is one value that a rule acts on. So a small cohort can have hundreds of findings. `audit()` does not read the pixels.

With no configuration loaded, a **floor policy** of 649 tag rules still applies. It is the PS3.15 Annex E Basic Profile table (2026c), with these settings:

- Study Date is shifted.
- Patient's Sex and Age are kept.
- Private tags are removed.

The configuration is where you record the policy you actually want. See [Configuration](configuration.md#privacy-profile).

The scaffold lists each machine in the cohort that has a Device Serial Number, with empty `redaction_zones`. `redact()` changes nothing until you add zones to them. See [Pixel Redaction (Machines)](configuration.md#pixel-redaction-machines). To find where a machine writes text, see [Burned-in text (OCR)](ocr.md).

## 4. Backup Identity (Optional)

Reversible anonymization encrypts each patient's original identifiers into the Encrypted Attributes Sequence `(0400,0500)` of their files. Whoever holds the key can recover the identifiers later. Lock **before** `anonymize()`.

```python
# Keep the key outside the project: whoever holds the key and an export
# can read the identities in it. The first lock that writes a token creates the key file.
session.enable_reversible_anonymization("/secure/keys/my_project.key")

# Lock every patient the audit found, by the Patient ID in the findings.
# tags_to_lock is optional (default: Name, ID, Birth Date, Sex, Accession Number).
locked = session.lock_identities(report, tags_to_lock=["0010,0010", "0010,0020", "0010,0030"])
print(locked)   # <LockingResult: N instances secured>
session.save()
```

The lock replaces any Encrypted Attributes Sequence `(0400,0500)` that the source file already carried. There is one exception. If that sequence holds an Isocenter identity token that the lock cannot read or would change, the lock refuses with `RuntimeError`.

`lock_identities()` before `enable_reversible_anonymization()` raises `RuntimeError`. If you lock again before anonymizing, the new token replaces the stored token. But if the new token would lose a value that the existing token holds, the lock raises `RuntimeError` and writes nothing. Encryption is Fernet (AES-128-CBC with HMAC-SHA256) from the `cryptography` package.

Locking after `anonymize()` secures nothing:

- Given the report, `lock_identities()` finds none of its Patient IDs, because `anonymize()` replaced them. It logs one `ERROR` and returns an empty result.
- Given a patient's new ID, `lock_identities()` raises `RuntimeError`.

Neither call creates the key file. Only a lock that writes a token creates it. Check the count that `lock_identities()` returns.

`Session()` loads a key by itself only from `./isocenter.key` in the current working directory. For a key kept anywhere else, name it with `enable_reversible_anonymization(path)`, as above. [What to keep](configuration.md#what-to-keep) lists the key, the store and the configuration, and what each is for.

Worked example: [Reversible anonymization: keep a way back](tutorials/reversible-anonymization.md) locks, exports, and recovers one patient.

## 5. Anonymize, Redact & Export

`anonymize()` and `redact()` change the in-memory graph. `export()` writes the result to a new directory.

```python
session.anonymize(report)   # remove, replace or shift tags, as the policy says
session.redact()            # blank the configured zones on matching machines
```

`anonymize()` acts on the findings that you give it. `redact()` returns the number of instances that had at least one zone applied. Each redacted instance takes a new SOP Instance UID.

### Verify

Audit again before you export. A clean run leaves no findings, and every patient, study and instance reads `CLEARED` or `REMEDIATED`.

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

Files land at `<folder>/Subject_<PatientID>/Study_…/Series_…/<SOPInstanceUID>.dcm`. The folder names are built from the exported values. So run `anonymize()` first.

`check_burned_in=True` runs `audit()` again. The export then withholds every instance that still carries an identifier in its tags, with a `WARNING` row for each.

- Without a [`burned_in_text`](configuration.md#burned-in-text) policy, it checks tags, not pixels. Redaction zones handle burned-in text, and `scan_pixel_content()` finds it.
- With a `burned_in_text` policy, it also reads with OCR the pixels that it is about to write. It does this for every image in the policy's [`scope`](configuration.md#burned-in-text).

### What the export writes

[What the export writes](export-output.md) covers the layout, compression and colour images, the de-identification markers, and what `check_burned_in=True` and `verify_readback=True` add.

## 6. Report

Generate the compliance report **last, after `export()`**. Export writes the final data-loss, error and warning rows. The report grades only what the audit log holds when you call it. A report generated before any export says so.

```python
session.generate_report("compliance_report.md")
```

See [Analytics & Reporting](analytics.md) for what each section means and how the grade is decided.

## 7. Recover Identity (Optional)

To recover a patient's original identity, do these steps:

1. Open the session under the key that the data was locked with.
2. Name the patient by the ID that it was exported under. This ID is the `ANON_…` value in the Patient ID `(0010,0020)` of the exported files. It is also in the `Subject_ANON_…` folder name.

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

`recover_patient_identity()` returns a `dict`. The dict maps the SOP Instance UID of each instance that carries an identity token to the values that token holds. The entries are in study, series and instance order. The first entry speaks for the patient. `restore=False` only reads. This read also checks that the patient is recoverable under the key.

`recover_patient_identity()` prints nothing. It raises an exception when it cannot recover:

- `FileNotFoundError` when no key file exists at that path.
- `ValueError` when no patient in the session holds the ID.
- `RuntimeError` when the patient has no identity token, or the key does not open it.

No message names the Patient ID.

Restore sets only the locked tags back to their original values. Every other date stays shifted by the patient's offset, so intervals are intact. [Reversible anonymization: keep a way back](tutorials/reversible-anonymization.md) works through recovery and what happens without the key.

## Next: the tutorials

Each tutorial follows one question end to end over files bundled with pydicom. Every code block runs in the test suite:

- [De-identify a cohort and read the grade](tutorials/deidentify-and-read-the-grade.md)
- [Select part of a cohort](tutorials/select-part-of-a-cohort.md)
- [Reversible anonymization: keep a way back](tutorials/reversible-anonymization.md)
- [Redact burned-in text for one machine](tutorials/redact-burned-in-pixels.md)
- [Write your own export format](tutorials/write-an-exporter.md)
- [Prepare your own ECGs for a PhysioNet Challenge](tutorials/physionet-challenge-ecgs.md)

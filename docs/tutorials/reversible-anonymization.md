# Keep a way back to the identities

<!-- tutorial: inputs=CT_small.dcm -->

Some studies need a way back. A collaborator finds something in patient
`ANON_...` that the treating team must hear about, and someone has to be
able to say who that patient is. **Reversible anonymization** gives you
that: before de-identifying, Isocenter encrypts each patient's original
identity under a key you hold and writes it into the files.

That way back travels with the data, so this tutorial also covers what
you are handing out. It runs a real session over one CT file: lock, then
de-identify, export and read the disclosure. It then recovers the identity
from the pseudonym, and shows what happens without the key.

!!! tip "Run it yourself"

    Every Python block on this page runs, in order, as part of
    Isocenter's test suite, and every output shown is checked. To follow
    along, make a folder called `input` holding pydicom's bundled test
    file `CT_small.dcm` (`pydicom.data.get_testdata_file("CT_small.dcm")`
    returns where it is), then paste the blocks into a Python prompt or a
    notebook. In a `.py` script, put them under
    `if __name__ == "__main__":`
    ([why](../quickstart.md#1-initialize-a-session)).

## 1. Ingest one patient

```python
from isocenter import Session

session = Session("tutorial.db")
summary = session.ingest("input")
```

```python
>>> summary
IngestSummary(ingested=1, failures=[], declined=0, skipped=0)
>>> [(p.patient_id, p.patient_name) for p in session.store.patients]
[('1CT1', 'CompressedSamples^CT1')]
```

These are the identifiers the export will remove, and the ones you want a
way back to.

## 2. Load a configuration

This page keeps the policy simple, the DICOM standard's Basic Profile
(PS3.15 Annex E Table E.1-1, 2026c edition) with no changes. Save it as
`config.yaml`:

<!-- tutorial: file=config.yaml -->
```yaml
privacy_profile: "basic@2026c"
```

```python
session.load_config("config.yaml")
```

[De-identify a cohort and read the grade](deidentify-and-read-the-grade.md)
shows how to change the rules and read the grade they give.

## 3. Lock the identities, then anonymize

Locking has to come **before** `anonymize()`. The lock copies each
file's original values into an encrypted token, and after `anonymize()`
there is no original value left to copy, so a lock then raises
`RuntimeError`.

```python
session.enable_reversible_anonymization("isocenter.key")
report = session.audit()
locked = session.lock_identities(report)
```

- **`enable_reversible_anonymization("isocenter.key")`** names the key
  file. It does not create one.
- **`audit()`** scans the patients against the policy. Its report is also
  the list of patients to lock.
- **`lock_identities(report)`** writes an identity token into every
  instance of those patients: Patient's Name, Patient ID, Birth Date, Sex
  and Accession Number, encrypted, in the Encrypted Attributes Sequence
  `(0400,0500)`. The first lock creates the key file, readable only by
  you (mode 0600).

```python
>>> locked
<LockingResult: 1 instances secured>
>>> import os
>>> os.path.exists("isocenter.key")
True
```

The key is a file of its own, next to the store but not inside it. Keep
its contents to yourself: anyone who holds it can read every locked
identity. This page never prints it.

Now de-identify and export as usual, with the report last:

```python
session.anonymize(report)
session.export("export", use_compression=False)
session.generate_report("report.md")
```

`use_compression=False` writes the pixels as ingested, so this page does
not depend on the JPEG 2000 encoder.

## 4. What you are handing out

The exported file looks de-identified:

```python
import pydicom
from pathlib import Path

exported = pydicom.dcmread(next(Path("export").rglob("*.dcm")))
```

```python
>>> exported.PatientID
'ANON_...'
>>> exported.PatientName
'ANONYMIZED'
```

It still carries the original identity, encrypted:

```python
>>> "EncryptedAttributesSequence" in exported
True
```

That is the point of the lock, and it is also a disclosure. Anyone who
holds this file **and** the key can recover who the patient is. So
`export()` says so twice.

**First, a warning**, printed to the console as the export runs and
written to `isocenter.log` in the working directory:

```python
def logged_warning(text):
    """The first WARNING line in isocenter.log that contains `text`."""
    with open("isocenter.log", encoding="utf-8") as log:
        return next(line.split(" - ", 1)[1].strip() for line in log
                    if " - WARNING - " in line and text in line)
```

```python
>>> print(logged_warning("re-identifiable"))
WARNING - 1 of 1 exported instances carry encrypted original identities (0400,0500). They are recoverable with the session key; treat the export as re-identifiable by any holder of it.
```

The log does not last: the next `Session` you open overwrites it.

**Second, a `REVERSIBLE_EXPORT` row** in the store's audit log. The row
is kept in the store for good, and the report counts it in section 2.
These helpers print one row of section 2, and the grade:

```python
def audit_row(path, action):
    with open(path, encoding="utf-8") as report_file:
        return next(line.strip() for line in report_file
                    if line.startswith(f"| {action} |"))

def grade_line(path):
    with open(path, encoding="utf-8") as report_file:
        return next(line.strip() for line in report_file
                    if "Validation Status" in line)
```

```python
>>> print(audit_row("report.md", "REVERSIBLE_EXPORT"))
| REVERSIBLE_EXPORT | 1 |
>>> print(grade_line("report.md"))
| **Validation Status** | **PASS** |
```

The grade is `PASS`, and it does not contradict the disclosure. The grade
says the run did what your policy asked. The `REVERSIBLE_EXPORT` row says
who can undo it. Read both before this export leaves. If the recipient
must not be able to re-identify, the key must never reach them.

!!! note "These helpers read the report's layout, which can change"

    The action name `REVERSIBLE_EXPORT` and the grade values `PASS` and
    `REVIEW_REQUIRED` are frozen for 1.x. The report's layout, the log's
    format and the warning's wording are not
    ([API stability](../api/stability.md)). If a 1.x release changes it,
    this page goes red in Isocenter's own tests and is updated with it.

Keep the pseudonym, as a collaborator would quote it back to you, and
close the session:

```python
pseudonym = exported.PatientID
session.close()
```

## 5. Recover the identity

Weeks later, the collaborator asks about `ANON_...`. Reopen the store,
enable reversible anonymization with the key the data was locked with,
and recover:

```python
session = Session("tutorial.db")
session.load_config("config.yaml")
session.enable_reversible_anonymization("isocenter.key")
identity = session.recover_patient_identity(pseudonym, restore=False)
```

A `Session` opened in a folder that holds a file named `isocenter.key`
enables reversible anonymization with it on its own. The explicit call
says which key you mean, and it is the call you need when the key lives
anywhere else (step 6).

`recover_patient_identity()` returns a dict. Each key is the SOP Instance
UID of an instance that carries a token, and each value holds what that
token holds. The first entry speaks for the patient:

```python
>>> list(identity)
['2.25...']
>>> first = next(iter(identity.values()))
>>> sorted(first)
['0008,0050', '0010,0010', '0010,0020', '0010,0030', '0010,0040']
>>> first["0010,0010"], first["0010,0020"]
('CompressedSamples^CT1', '1CT1')
```

`restore=False` only reads. Nothing in the store changes, and the patient
still carries the pseudonym:

```python
>>> session.store.patients[0].patient_id == pseudonym
True
```

With `restore=True` (the default) the call returns the same dict and
also writes the original values back onto the patient in memory. A later
`save()` stores them. Use it when you need the identified data back in
the store, not just the answer. The
[Quick Start](../quickstart.md#7-recover-identity-optional) covers what a
restore puts back and what it leaves shifted.

```python
session.close()
```

## 6. Without the key, there is no way back

Store the key apart from the store, for example on another machine or
in a password manager. Here, a `vault` folder stands in for that place:

```python
Path("vault").mkdir()
Path("isocenter.key").rename("vault/isocenter.key")
```

Reopen the store and try to recover with no key at the path you give:

```python
session = Session("tutorial.db")
session.load_config("config.yaml")
session.enable_reversible_anonymization("isocenter.key")
```

```python
>>> session.recover_patient_identity(pseudonym, restore=False)
Traceback (most recent call last):
    ...
FileNotFoundError: no key file at ...isocenter.key; recovery needs the key the identities were locked with, and does not create one
```

The store holds the token but not the key, so it cannot open the token.
Recovery never creates a key, because a new key could not open a token
locked under the old one: a mistyped path would only make a useless key.
A key file that exists but is the wrong key raises `RuntimeError`
instead. Either way, nothing is written.

Point the session at the key's real place, and recovery works again:

```python
session.enable_reversible_anonymization("vault/isocenter.key")
identity = session.recover_patient_identity(pseudonym, restore=False)
```

```python
>>> next(iter(identity.values()))["0010,0020"]
'1CT1'
```

```python
session.close()
```

## What to keep

Reversible anonymization adds a third thing to keep. The three are
separate files, and each one does a different job:

| Keep | Because | If you lose it |
| :--- | :--- | :--- |
| **`isocenter.key`**, apart from the store and never with an export | It is the only thing that opens the identity tokens. | Nobody can recover any identity locked under it, and no new key helps. |
| **The store**: `tutorial.db` *and* `tutorial_pixels.bin`, together | It holds the project secret that made the pseudonyms and date offsets, and the patients you recover from. Never send it with an export. | Later exports of the same patients get new pseudonyms and will not link to this one. The tokens in files already exported can still be opened with the key. |
| **`config.yaml`**, under version control | It is your policy. Reload it every time you reopen the store. | Nothing you cannot write again, but a rewritten file must be the same policy, or the next report grades `REVIEW_REQUIRED` until you audit again. |

Keep them apart. The key opens the tokens in the exported files, so an
export shipped with its key is not de-identified at all. The store
holds the secret behind every date shift, so it must stay with you as
well. The configuration holds no secret, so it is the one of the three
you can show a reviewer.
The Configuration guide's [What to keep](../configuration.md#what-to-keep)
is the full table.

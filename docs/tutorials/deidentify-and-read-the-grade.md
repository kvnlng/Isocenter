# De-identify a cohort and read the grade

<!-- tutorial: inputs=CT_small.dcm,MR_small.dcm,rtdose.dcm -->

You have de-identified a cohort. Two questions come next: **what does the
compliance report's grade mean**, and **what do you need to keep so that
a later export matches this one**? This tutorial answers both by running
a real session over three small files and reading what it produces.

It follows one question end to end. For a tour of every step in the
pipeline, read the [Quick Start](../quickstart.md) first.

!!! tip "Run it yourself"

    Every Python block on this page runs, in order, as part of
    Isocenter's test suite, and every output shown is checked. To follow along, make
    a folder called `input` holding pydicom's bundled test files
    `CT_small.dcm`, `MR_small.dcm` and `rtdose.dcm`
    (`pydicom.data.get_testdata_file("CT_small.dcm")` returns where each
    one is), then paste the blocks into a Python prompt or a notebook.
    In a `.py` script, put them under `if __name__ == "__main__":`
    ([why](../quickstart.md#1-initialize-a-session)).

## 1. Ingest three patients

The three files are a CT, an MR and an RT dose grid, one patient each.
Ingest reads them into a *store*: a SQLite file plus a pixel sidecar
beside it. The source files are never modified.

```python
from isocenter import Session

session = Session("tutorial.db")
summary = session.ingest("input")
```

`ingest()` tells you what it read, and what it could not:

```python
>>> summary
IngestSummary(ingested=3, failures=[], declined=0, skipped=0)
```

## 2. Write a configuration

The configuration is your protocol, written down. This one starts from
the DICOM standard's Basic Profile (PS3.15 Annex E Table E.1-1, 2026c
edition) and changes one rule. Save it as `config.yaml`:

<!-- tutorial: file=config.yaml -->
```yaml
privacy_profile: "basic@2026c"

# Every patient's dates move back by the same number of days,
# somewhere from 10 to 30, so intervals between them survive.
date_jitter:
  min_days: -30
  max_days: -10

phi_tags:
  # The Basic Profile empties Study Date. This protocol keeps it,
  # shifted by the patient's offset.
  "0008,0020":
    action: "SHIFT"
    name: "StudyDate"
```

Why the override: the Basic Profile empties Study Date, so without it
`date_jitter` would have nothing to shift. With it, each exported Study
Date is real time moved by a per-patient offset. The
[Configuration](../configuration.md) guide lists every key and action.

```python
session.load_config("config.yaml")
```

## 3. Audit before changing anything

`audit()` scans every patient, study and instance against the policy and
returns a report of findings. Nothing changes yet.

```python
report = session.audit()
```

Suppose you are not yet sure about one tag. Your protocol may allow
Institution Name, and you want to ask before removing it. So you hold
those findings back and pass the rest to `anonymize()`:

```python
held = [f for f in report.findings if f.tag == "0008,0080"]
rest = [f for f in report.findings if f.tag != "0008,0080"]
```

```python
>>> sorted(f.value for f in held)
['JFK IMAGING CENTER', 'TOSHIBA']
```

## 4. Anonymize, export, and read the grade

```python
session.anonymize(rest)
session.export("export-draft", use_compression=False)
session.generate_report("report-draft.md")
```

Two choices here are deliberate:

- **`use_compression=False`.** `rtdose.dcm` stores 32-bit dose values,
  and lossless JPEG 2000 cannot carry 32-bit samples exactly, so with
  compression on
  [that instance fails to export](../quickstart.md#what-the-export-writes).
  A failure is an `ERROR` row in the audit log, and a row, once written,
  stays in the store for good.
- **The report comes last,** after `export()`. Export writes rows of its
  own (anything it lost or could not write), and the report grades only
  the rows that exist when you call it.

The report is a Markdown file. Its first table holds the grade. This
small helper prints that line:

```python
def grade_line(path):
    with open(path, encoding="utf-8") as report_file:
        return next(line.strip() for line in report_file
                    if "Validation Status" in line)
```

```python
>>> print(grade_line("report-draft.md"))
| **Validation Status** | **REVIEW_REQUIRED** |
```

The grade is `PASS` or `REVIEW_REQUIRED`, never `FAIL`.
`REVIEW_REQUIRED` means a person has to look before this data leaves.
Section 5 of the report, the **Grade Basis**, gives one line per reason:

```python
def grade_basis(path):
    with open(path, encoding="utf-8") as report_file:
        lines = report_file.read().splitlines()
    start = next(i for i, line in enumerate(lines)
                 if "**Grade Basis:**" in line)
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("    "):
            break
        block.append(line)
    print("\n".join(block))
```

```python
>>> grade_basis("report-draft.md")
*   **Grade Basis:** REVIEW_REQUIRED, for 1 reason(s):
    *   2 entities read IDENTIFIED: ...
```

The run did what it was asked. But the scan found two values your policy
acts on, and nothing acted on them, so the report will not call the
result clean. This is condition 7 of
[How the grade is decided](../analytics.md#how-the-grade-is-decided),
which lists every condition. The grade reads the store, not your
intentions: holding a finding back looks, to the report, exactly like
forgetting it.

The exported files say the same thing. Each file that is fully
de-identified carries Patient Identity Removed `(0012,0062)`. The CT and
the MR, whose Institution Name is still there, do not:

```python
import pydicom
from pathlib import Path

def exported(folder):
    """Each exported file, keyed by its modality."""
    return {ds.Modality: ds for ds in
            (pydicom.dcmread(path) for path in Path(folder).rglob("*.dcm"))}
```

```python
>>> draft = exported("export-draft")
>>> draft["CT"].InstitutionName
'JFK IMAGING CENTER'
>>> "PatientIdentityRemoved" in draft["CT"], "PatientIdentityRemoved" in draft["MR"]
(False, False)
>>> draft["RTDOSE"].PatientIdentityRemoved
'YES'
```

`export-draft` still holds an institution's name, so it is not an export
to hand out. Delete it:

```python
import shutil
shutil.rmtree("export-draft")
```

## 5. Act on the held findings, and read PASS

Your protocol says Institution Name goes. Pass the held findings to
`anonymize()`, export again, and generate a new report. The pass writes
the Basic Profile's replacement for that tag, the word `ANONYMIZED`.

```python
session.anonymize(held)
session.export("export", use_compression=False)
session.generate_report("report.md")
```

```python
>>> print(grade_line("report.md"))
| **Validation Status** | **PASS** |
>>> grade_basis("report.md")
*   **Grade Basis:** PASS...
```

`PASS` means none of the report's conditions holds: nothing failed,
nothing graded was lost, and no finding is left open. It does not mean the
files hold no identifiers. It means the run did what your policy asked
and recorded doing it. Anything your policy does not name is outside the
scan. Whether the result meets your protocol is still the data steward's
call. [What PASS does not mean](../analytics.md#how-the-grade-is-decided)
spells out the limits.

## 6. What an exported file says about itself

Every file the export writes records how it was de-identified, so a
recipient can check without your report:

```python
>>> ct = exported("export")["CT"]
>>> ct.PatientIdentityRemoved
'YES'
>>> ct.DeidentificationMethod
'isocenter/...; basic@2026c; v1:...'
>>> ct.LongitudinalTemporalInformationModified
'MODIFIED'
>>> ct.InstitutionName
'ANONYMIZED'
>>> ct.PatientID
'ANON_...'
```

- **Patient Identity Removed `(0012,0062)`** is `YES` only when every
  rule of the policy was applied to that file.
- **De-identification Method `(0012,0063)`** names the release, the
  profile, and a short fingerprint of the exact rules (`v1:` and 8 hex
  digits). Two configurations that differ by one rule have different
  fingerprints.
- **Longitudinal Temporal Information Modified `(0028,0303)`** is
  `MODIFIED` because the file's dates were shifted rather than removed.
- The Patient ID is a pseudonym, `ANON_` and 24 hex digits.

The shift is between 10 and 30 days, as `date_jitter` asked:

```python
from datetime import datetime

def study_date(ds):
    return datetime.strptime(ds.StudyDate, "%Y%m%d").date()

source = pydicom.dcmread("input/CT_small.dcm")
shift = study_date(source) - study_date(ct)
```

```python
>>> 10 <= shift.days <= 30
True
```

[What an exported file says about itself](../configuration.md#what-an-exported-file-says-about-itself)
covers every case in which these markers are withheld.

## 7. Come back later: the store is the project

Close the session. Weeks later you reopen the same store to export again,
perhaps for a second recipient:

```python
session.close()

session = Session("tutorial.db")
session.load_config("config.yaml")
session.export("export-again", use_compression=False)
session.generate_report("report-again.md")
```

Load the configuration again after reopening. The store remembers which
policy each finding was scanned under, and no config at all means the
default floor policy, which is a different one. Exporting under a
different policy
[costs the run its `PASS`](../configuration.md#what-to-keep) until the
next `audit()`.

Every patient gets the same pseudonym and the same date offset as before:

```python
def identities(folder):
    return {modality: (ds.PatientID, ds.StudyDate)
            for modality, ds in exported(folder).items()}
```

```python
>>> identities("export-again") == identities("export")
True
>>> print(grade_line("report-again.md"))
| **Validation Status** | **PASS** |
```

This works because the pseudonyms, the date offsets and the replacement
UIDs are all derived from a secret the store generated for itself. It is
not the configuration that reproduces them. The same configuration over
a **new** store gives every patient a different pseudonym:

```python
other = Session("other.db")
other.ingest("input")
other.load_config("config.yaml")
other.anonymize()
other.export("export-other", use_compression=False)
other.close()
```

```python
>>> first = {pid for pid, _ in identities("export").values()}
>>> second = {pid for pid, _ in identities("export-other").values()}
>>> first & second
set()
```

Data exported from `other.db` will never link to data exported from
`tutorial.db`. (`anonymize()` with no argument audits and then acts on
every finding. That is the shortcut when you are holding nothing back.)

```python
session.close()
```

## What to keep

The run depends on three things. Each one gives you something different:

| Keep | Because |
| :--- | :--- |
| **`config.yaml`**, under version control | It is your policy. Reload it every time you reopen the store. `basic@2026c` names one fixed table in every 1.x release. |
| **The store**: `tutorial.db` *and* `tutorial_pixels.bin`, together | It holds the project secret. Lose it and the next export of the same patients gets new pseudonyms, new offsets and new UIDs, which will not link to anything exported before. Never send it with an export: whoever holds it can undo the date shifts. |
| **The report**, beside the export it describes | It is the record of what this run did. Generate it after `export()`, and read the grade *and* its Grade Basis. |

With [reversible anonymization](../quickstart.md#4-backup-identity-optional)
there is a fourth thing to keep, `isocenter.key`. The Configuration
guide's [What to keep](../configuration.md#what-to-keep) is the full
table.

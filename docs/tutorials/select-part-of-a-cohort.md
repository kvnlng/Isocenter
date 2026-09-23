# Select part of a cohort

<!-- tutorial: inputs=CT_small.dcm,MR_small.dcm,rtdose.dcm -->

You rarely hand out everything you ingested. One recipient gets the
images, another gets one patient. Two questions come with that: **how do
you export only the patients or series you chose**, and **how do you
find out when you asked for something the session does not hold**? This
tutorial answers both over the three files
[the first tutorial](deidentify-and-read-the-grade.md) used.

!!! tip "Run it yourself"

    Every Python block on this page runs, in order, as part of
    Isocenter's test suite, and every output shown is checked. To follow
    along, make a folder called `input` holding pydicom's bundled test
    files `CT_small.dcm`, `MR_small.dcm` and `rtdose.dcm`
    (`pydicom.data.get_testdata_file("CT_small.dcm")` returns where each
    one is), then paste the blocks into a Python prompt or a notebook.
    In a `.py` script, put them under `if __name__ == "__main__":`
    ([why](../quickstart.md#1-initialize-a-session)).

## 1. Look at the cohort

Ingest the three files. Each is one patient: a CT, an MR and an RT dose
grid.

```python
from isocenter import Session

session = Session("tutorial.db")
session.ingest("input")
early = session.get_cohort_report()
```

`get_cohort_report()` returns a pandas DataFrame with one row per
instance: its patient, study, series and instance, and a few columns you
can select on.

```python
>>> early[["PatientID", "Modality"]]
  PatientID Modality
0      1CT1       CT
1      4MR1       MR
2   id11111   RTDOSE
>>> list(early.columns)
['PatientID', 'PatientName', 'StudyInstanceUID', 'StudyDate', 'SeriesInstanceUID', 'Modality', 'SOPInstanceUID', 'Manufacturer', 'Model', 'DeviceSerial']
```

These are still the source values. That matters later: this report was
taken before anything was de-identified.

## 2. Choose rows with a pandas query

The report is an ordinary DataFrame, so you choose with ordinary pandas.
This recipient gets the images and not the dose grid:

```python
images = early.query("Modality in ['CT', 'MR']")
```

```python
>>> images[["PatientID", "Modality"]]
  PatientID Modality
0      1CT1       CT
1      4MR1       MR
```

`get_cohort_report(expand_metadata=True)` adds a column for every
attribute in the files, if the column you want to select on is not among
the ten above.

## 3. De-identify, then export the selection

On this page, nothing leaves before it is de-identified. No configuration is loaded
here, so `anonymize()` applies the session's default policy (the first
tutorial shows how to write your own). With no argument it audits and
then acts on every finding.

```python
session.anonymize()
```

Now pass the rows you chose as `subset=`. `rtdose.dcm` stores 32-bit
dose values, which Isocenter's JPEG 2000 encoder cannot write exactly,
so every export on this page uses `use_compression=False`
([#771](https://github.com/kvnlng/Isocenter/issues/771) plans to write
such an instance uncompressed on its own).

```python
session.export("export-images", subset=images, use_compression=False)
session.generate_report("report-images.md")
```

Two small helpers read the result. `exported` opens every file an export
wrote, keyed by modality. `grade_line` prints the line of the report
that holds its grade, as in the first tutorial:

```python
import pydicom
from pathlib import Path

def exported(folder):
    """Each exported file, keyed by its modality."""
    return {ds.Modality: ds for ds in
            (pydicom.dcmread(path) for path in Path(folder).rglob("*.dcm"))}

def grade_line(path):
    with open(path, encoding="utf-8") as report_file:
        return next(line.strip() for line in report_file
                    if "Validation Status" in line)
```

```python
>>> sorted(exported("export-images"))
['CT', 'MR']
>>> print(grade_line("report-images.md"))
| **Validation Status** | **PASS** |
```

The CT and the MR were written; the dose grid was not.

**Why a report from before `anonymize()` still selects.** A DataFrame is
read by the most precise of four columns it carries: `SOPInstanceUID`,
then `SeriesInstanceUID`, `StudyInstanceUID`, `PatientID`. `anonymize()`
replaced every one of those UIDs in the graph, so the exported CT no
longer carries the UID the report shows:

```python
>>> ct = exported("export-images")["CT"]
>>> ct.SOPInstanceUID == images.SOPInstanceUID[0]
False
>>> ct.SOPInstanceUID
'2.25...'
```

The session remembers each instance's source UIDs, so a Study, Series or
SOP Instance UID taken before `anonymize()` still names its entity. A
**Patient ID does not**, as section 5 shows.

## 4. Select patients by ID

To export whole patients, pass `patient_ids=`. After `anonymize()` a
patient's ID is its replacement, so read the IDs from a report taken
now, not from `early`:

```python
now = session.get_cohort_report()
dose_patient = now.query("Modality == 'RTDOSE'").PatientID.tolist()
```

```python
>>> all(pid.startswith("ANON_") for pid in now.PatientID)
True
>>> len(dose_patient)
1
```

```python
session.export("export-dose", patient_ids=dose_patient,
               use_compression=False)
session.generate_report("report-dose.md")
```

```python
>>> sorted(exported("export-dose"))
['RTDOSE']
>>> print(grade_line("report-dose.md"))
| **Validation Status** | **PASS** |
```

`patient_ids` takes a list, or any other iterable, of Patient IDs, and
each one must match exactly. A bare string is refused with a
`TypeError`, because a string is itself an iterable of characters: write
`patient_ids=["ANON_..."]`, not `patient_ids="ANON_..."`.

## 5. Check a list before you export it

A Patient ID that no patient holds selects nothing. Isocenter never
drops it silently: it is **counted**, by its position in your list, and
never named. The common way to get one after `anonymize()` is not a typo.
It is a list built from a report taken earlier, which holds the source
IDs:

```python
source_ids = early.PatientID.tolist()
check = session.get_cohort_report(patient_ids=source_ids)
```

```python
>>> source_ids
['1CT1', '4MR1', 'id11111']
>>> len(check)
0
```

No row came back. The call also logged one `WARNING` line: no patient
matches 3 of the 3 IDs given, and after `anonymize()` a patient is
selected by its replacement Patient ID. The same holds for `subset=` with
a frame that has only a `PatientID` column.

`get_cohort_report()` is a read. It logs the count and writes nothing to
the store, so it is the place to try a list. `export()` counts in the
same way, and also writes a row into the store's audit log, as the next
section shows.

## 6. Export with a mistyped ID, and read the grade

The steps above ended at `PASS`, and this one will not. A row, once
written, stays in the store for good, so this page keeps its one mistake
for last.

Say your list holds the dose patient's ID and a second ID with a typo in it:

```python
wanted = dose_patient + ["ANON_TYPO"]
session.export("export-typo", patient_ids=wanted, use_compression=False)
session.generate_report("report-typo.md")
```

The patient that matched is exported as usual. The one that did not is
counted, and the report says a person has to look:

```python
>>> sorted(exported("export-typo"))
['RTDOSE']
>>> print(grade_line("report-typo.md"))
| **Validation Status** | **REVIEW_REQUIRED** |
```

The reason is in section 4 of the report, **Exceptions & Errors**. This
helper counts the rows there and prints the details of each:

```python
def exceptions(path):
    with open(path, encoding="utf-8") as report_file:
        text = report_file.read()
    section = text.split("## 4. Exceptions & Errors")[1].split("\n## ")[0]
    rows = [line.split(" | ")[-1].rstrip(" |")
            for line in section.splitlines() if line.startswith("| 20")]
    print(len(rows), "row(s)")
    for row in rows:
        print(row)
```

```python
>>> exceptions("report-typo.md")
1 row(s)
DICOM export to export-typo: patient_ids: no patient in the session matches 1 of the 2 ids given (position 2, in the order given); it selects nothing. After anonymize(), a patient is selected by its replacement Patient ID.
```

Position 2 is `"ANON_TYPO"`. The row names the export folder and the
position, never the value, because an ID in a list you pass may be a
source identifier.

!!! note "These helpers read the report's layout, which can change"

    The grade values `PASS` and `REVIEW_REQUIRED` are frozen for 1.x. The
    report's layout and the wording of each row are not
    ([API stability](../api/stability.md)). `grade_line` and
    `exceptions` are for reading a report, not for gating automation on
    one.

The report grades every row the store holds: every row any session has
ever written into this store, not only the rows of the last export.
Exporting again with the right list writes the right files, and the
grade stays `REVIEW_REQUIRED`, because the typo's row is still there:

```python
session.export("export-dose-again", patient_ids=dose_patient,
               use_compression=False)
session.generate_report("report-dose-again.md")
```

```python
>>> sorted(exported("export-dose-again"))
['RTDOSE']
>>> print(grade_line("report-dose-again.md"))
| **Validation Status** | **REVIEW_REQUIRED** |
>>> exceptions("report-dose-again.md")
1 row(s)
DICOM export to export-typo: patient_ids: no patient in the session matches 1 of the 2 ids given (position 2, in the order given); it selects nothing. After anonymize(), a patient is selected by its replacement Patient ID.
```

The one row is still the typo's, naming `export-typo`; the correct
export added none. That is deliberate. The report is the record of every
session run over this store, and an export that asked for someone who was
not there is part of it. The reviewer reads section 4, sees that the row
names `export-typo`, and decides. Nothing removes a row: only a new store
starts clean ([How the grade is decided](../analytics.md#how-the-grade-is-decided)).
Section 5 is the way to avoid the row: try the list with
`get_cohort_report()` first.

```python
session.close()
```

## What each selection reads

| You pass | It selects | After `anonymize()` |
| :--- | :--- | :--- |
| `subset=<DataFrame>` | Every instance under the value in its most precise UID column: `SOPInstanceUID`, then `SeriesInstanceUID`, `StudyInstanceUID`, `PatientID` | A UID column taken earlier still selects. A `PatientID` column taken earlier selects nothing. |
| `subset=[uid, ...]` | Every instance under each UID, at any of the four levels | As for a DataFrame |
| `subset="<pandas query>"` | The rows of `get_cohort_report(expand_metadata=True)` that match | The query runs against the report as it is now |
| `patient_ids=[id, ...]` | Every instance of each patient whose ID matches exactly | Use the replacement IDs |

A value that names nothing is counted, never named, in one `WARNING`
line; `export()` also writes one `WARNING` row, which grades the report
`REVIEW_REQUIRED`. `subset=` is for the `dicom` format; `patient_ids=`
works for both `dicom` and `wfdb`.
[API stability](../api/stability.md) states these rules as 1.x promises,
and [Query-Based Export](../analytics.md#query-based-export) has more
examples.

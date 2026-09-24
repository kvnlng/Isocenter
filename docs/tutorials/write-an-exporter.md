# Write your own export format

<!-- tutorial: inputs=CT_small.dcm -->

Isocenter writes DICOM and WFDB. If a recipient needs something else, you
can register an **exporter** of your own and select it with
`session.export(folder, format=...)`. This tutorial writes the smallest
useful one, a CSV listing each exported Patient ID, runs it over
pydicom's bundled `CT_small.dcm`, and reads what the report says about
it.

!!! warning "Provisional until 1.1"

    The exporter registry (`Exporter`, `register()`, `get_exporter()`,
    `available_formats()`) is documented but internal, and 1.1 may
    replace it rather than extend it. Pin `isocenter>=1.0,<1.1` in a
    plugin written against 1.0. The terms are on
    [Exporter registry](../api/exporters.md) and
    [API stability](../api/stability.md#the-exporter-registry-provisional-until-11).

!!! tip "Run it yourself"

    Every Python block on this page runs, in order, as part of
    Isocenter's test suite, and every output shown is checked. To follow
    along, make a folder called `input` holding pydicom's bundled test
    file `CT_small.dcm` (`pydicom.data.get_testdata_file("CT_small.dcm")`
    returns where it is), then paste the blocks into a Python prompt or a
    notebook. In a `.py` script, put them under
    `if __name__ == "__main__":`
    ([why](../quickstart.md#1-initialize-a-session)).

## 1. Write the exporter

An exporter is a class with an `export(self, session, folder, **options)`
method. It reads the session's graph, writes files under `folder`, and
returns what it wrote:

```python
import csv
import os

from isocenter import exporters
from isocenter.entities import exported_patient_id


class PatientIdCsv(exporters.Exporter):
    """Write one CSV row per patient: the Patient ID the export carries."""

    def export(self, session, folder, **options):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "patients.csv")
        with open(path, "w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            writer.writerow(["PatientID"])
            for patient in session.store.patients:
                writer.writerow([exported_patient_id(patient)])
        return [path]
```

Three choices in it follow the
[rules for an exporter author](../api/exporters.md#rules-for-an-exporter-author):

- **It reads and never writes the graph.** Export is a read.
- **It writes `exported_patient_id(patient)`, not `patient.patient_id`.**
  A patient whose files carry no Patient ID is held under a key that must
  never reach an output, and `exported_patient_id()` returns the value
  the patient's files should carry: its ID, or `''` for that key.
- **It returns the paths it wrote.** A caller can then tell an export
  that wrote nothing from one that worked.

`export()` does not create `folder` before calling an exporter, so the
exporter does.

## 2. Register it

`register(name, cls)` makes the class selectable by name:

```python
exporters.register("patient-ids", PatientIdCsv)
```

```python
>>> exporters.available_formats()
['dicom', 'patient-ids', 'wfdb']
```

Registering under a name that is already taken replaces what was there.

## 3. De-identify, export, and read the CSV

A plugin runs on the graph as it stands, so de-identify first. No
configuration is loaded here, so `anonymize()` applies the session's
default policy:

```python
from isocenter import Session

session = Session("tutorial.db")
session.ingest("input")
session.anonymize()
written = session.export("export", format="patient-ids")
```

`export()` returns what the exporter returned:

```python
>>> written
['export/patients.csv']
>>> with open("export/patients.csv", encoding="utf-8") as rows:
...     rows.read().splitlines()
['PatientID', 'ANON_...']
```

The CSV holds the patient's pseudonym, not the source ID `1CT1`.

## 4. Read the report

```python
session.generate_report("report.md")
```

The helpers below print the grade line and the rows of section 4,
**Exceptions & Errors**, as the
[cohort-selection tutorial](select-part-of-a-cohort.md) does:

```python
def grade_line(path):
    with open(path, encoding="utf-8") as report_file:
        return next(line.strip() for line in report_file
                    if "Validation Status" in line)

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
>>> print(grade_line("report.md"))
| **Validation Status** | **REVIEW_REQUIRED** |
>>> exceptions("report.md")
1 row(s)
Export to export in format 'patient-ids' ran ...PatientIdCsv, an exporter Isocenter does not ship: its output is not attested by Isocenter. None of the export gates ran for it (...), so this report does not know what it wrote (#527).
```

This is by design, and it is the same for every exporter Isocenter does
not ship, however well it behaves. The export gates (the burned-in
re-audit, the redaction zones, the de-identification markers and the
rest) live inside the two built-in formats, so none of them ran for
`PatientIdCsv`. Before calling it, `export()` wrote one `WARNING` row
saying so, and a `WARNING` row grades the report `REVIEW_REQUIRED`
(condition 2 of
[How the grade is decided](../analytics.md#how-the-grade-is-decided)).
In 1.0, no third-party export grades `PASS`.
[What a third-party exporter receives](../api/exporters.md#what-a-third-party-exporter-receives)
lists every gate it runs without.

The report also carries the note that it was generated before any
export, because only the built-in formats record an `EXPORT` row.

## 5. Save before you close

The built-in formats save the session before they write. A plugin's
export does not, so `anonymize()`'s changes are still only in memory.
Save them, then close:

```python
session.save(sync=True)
session.close()
```

Without the `save()`, `close()` warns that instances hold unsaved
changes, and the store keeps the source values.

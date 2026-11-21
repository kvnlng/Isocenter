# Write your own export format

<!-- tutorial: inputs=CT_small.dcm -->

Isocenter writes DICOM and WFDB. If a recipient needs something else, you
can register an **exporter** of your own and select it with
`session.export(folder, format=...)`. This tutorial writes a small one:
one JSON file per instance, which holds the Patient ID and the modality
that the export carries. It runs the exporter over pydicom's bundled
`CT_small.dcm`. Then it reads what the report says about the export.

:::{admonition} Documented but internal
:class: note

The exporter contract (`Exporter`, `ExportItem`, `register()`,
`get_exporter()`, `available_formats()`) can change in a minor release,
with a `Breaking` CHANGELOG entry. 1.1 replaced the 1.0 registry: an
exporter written for 1.0, with `export(session, folder, **options)`,
does not register on 1.1. The terms are on
[Exporter registry](../api/exporters.md) and
[API stability](../api/stability.md#the-exporter-contract).
:::

:::{admonition} Run it yourself
:class: tip

Every Python block on this page runs, in order, as part of
Isocenter's test suite, and every output shown is checked.

- **Start in a new, empty folder.** Each tutorial creates its own
  `tutorial.db` and export folders, and running one in another
  tutorial's folder changes what it prints. The first block below
  copies the input file from pydicom into `input/`.
- Paste the blocks into a Python prompt or a notebook. In a `.py`
  script, put them under `if __name__ == "__main__":`
  ([why](../quickstart.md#1-initialize-a-session)). The exporter class
  is the exception: it stays in its own file, `patient_id_json.py`,
  never under that line. A worker process does not run the block under
  it, so a class defined there is missing in the worker, and
  `register()` refuses it.
- In a block with `>>>`, type what follows each `>>>`. The lines
  under it are what Python prints. A `...` inside a printed value
  stands for a part that differs on every run, such as a pseudonym or
  a UID.
- The session also prints progress bars, status lines and `WARNING`
  lines as it works. They are not shown here.
  `ISOCENTER_SHOW_PROGRESS=0` turns the bars off.
:::

```python
import shutil
from pathlib import Path

import pydicom.data

Path("input").mkdir(exist_ok=True)
shutil.copy(pydicom.data.get_testdata_file("CT_small.dcm"), "input")
```

## 1. Write the exporter in a file

An exporter is a class with a `write(self, dataset, item, folder,
**options)` method. The export calls it once for each instance, in an
export worker process. A worker process imports the class by its module
and name, so the class must be in a file, not in a notebook cell or at
the prompt. Save this as `patient_id_json.py` in your folder:

<!-- tutorial: file=patient_id_json.py -->
```py
import json
import os

from isocenter import exporters


class PatientIdJson(exporters.Exporter):
    """Write one JSON file per instance: its Patient ID and modality."""

    def write(self, dataset, item, folder, **options):
        out = os.path.join(folder, *item.folder_names)
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, f"{item.sop_instance_uid}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"PatientID": item.patient_id,
                       "Modality": str(dataset.Modality)}, handle)
        return path
```

Three choices in it follow the
[rules for an exporter author](../api/exporters.md#rules-for-an-exporter-author):

- **It writes from `dataset` and `item` only.** `dataset` is the
  instance as the DICOM export would write it, after every gate:
  configured redaction zones, the drop of nested icons, the owner
  stamps and the de-identification markers. The exporter never gets the
  session.
- **It writes `item.patient_id`.** That is the Patient ID the export
  carries, `''` for a subject with none.
- **It gives each instance its own file and returns its path.** A caller
  can then tell an export that wrote nothing from one that worked.

## 2. Import it and register it

The worker processes start with the `sys.path` of your session. So the
folder that holds `patient_id_json.py` goes on `sys.path` before you
import the class:

```python
import os
import sys

sys.path.insert(0, os.getcwd())  # the folder that holds patient_id_json.py

from isocenter import exporters
from patient_id_json import PatientIdJson

exporters.register("patient-ids", PatientIdJson)
```

```python
>>> exporters.available_formats()
['dicom', 'patient-ids', 'wfdb']
```

`register()` refuses the names `dicom` and `wfdb`, which are the
built-in formats'. It also refuses a class that a worker cannot import.

## 3. De-identify, export, and read the files

No configuration is loaded here. So `audit()` and `anonymize()` apply
the session's default policy:

```python
from isocenter import Session

session = Session("tutorial.db")
session.ingest("input")
session.audit()
session.anonymize()
summary = session.export("export", format="patient-ids")
```

`export()` returns an `ExportSummary`, as it does for `dicom`:

```python
>>> summary.written, summary.failures
(1, [])
>>> import glob, json
>>> [path] = glob.glob("export/**/*.json", recursive=True)
>>> with open(path, encoding="utf-8") as handle:
...     json.load(handle)
{'PatientID': 'ANON_...', 'Modality': 'CT'}
```

The file holds the patient's pseudonym, not the source ID `1CT1`.

## 4. Read the report

```python
session.generate_report("report.md")
```

The helper below prints the grade line, as the
[cohort-selection tutorial](select-part-of-a-cohort.md) does:

```python
def grade_line(path):
    with open(path, encoding="utf-8") as report_file:
        return next(line.strip() for line in report_file
                    if "Validation Status" in line)
```

```python
>>> print(grade_line("report.md"))
| **Validation Status** | **PASS** |
```

The export ran behind the same gates as a `dicom` export, so it can
grade `PASS`. Its `EXPORT` audit row names your class: `patient-ids
export to export by patient_id_json.PatientIdJson: wrote 1 of 1 planned
instances from 1 patients.`

Isocenter vouches for what it handed `write()`. It does not read back
what `write()` wrote, so a class that adds an identifier of its own is
not caught.

## 5. Close

`export()` saved the session before it wrote, as it does for every
format. Close it:

```python
session.close()
```

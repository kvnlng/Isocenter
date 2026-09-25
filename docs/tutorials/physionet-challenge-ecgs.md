# Prepare your own ECGs for a PhysioNet Challenge

<!-- tutorial: inputs=waveform_ecg.dcm -->

The [George B. Moody PhysioNet Challenge](https://moody-challenge.physionet.org/)
publishes its data as WFDB records: a `.hea` header and a `.dat` signal
file for each ECG. The organizers have already de-identified that data.
This tutorial is for a team that also holds **its own ECGs as DICOM** and
wants to use them alongside the Challenge's: as extra training data, or to
test a model locally on a population the public sets do not cover.

That raises three questions. **How do you turn a folder of DICOM ECGs
into de-identified WFDB records**, **how do you make each header read the
way the Challenge's code expects**, and **how do you look at the records
before you train on them**? This tutorial answers them over the one
12-lead ECG bundled with pydicom. Your folder will hold many. Every step
loops over every record, and the notes after each step say what changes
when your files differ from this one.

The Challenge changes its question every year. The header lines below are
the 2025 Challenge's (detecting Chagas disease), taken from its
[data description](https://moody-challenge.physionet.org/2025/). Check the
page for your year before you copy them.

!!! tip "Run it yourself"

    Every Python block on this page runs, in order, as part of
    Isocenter's test suite, and every output shown is checked.

    - **Start in a new, empty folder.** Each tutorial creates its own
      `tutorial.db` and export folders, and running one in another
      tutorial's folder changes what it prints. The first block below
      copies the input file from pydicom into `input/`.
    - This page also needs the `wfdb` package: `pip install wfdb`.
    - Paste the blocks into a Python prompt or a notebook. In a `.py`
      script, put them under `if __name__ == "__main__":`
      ([why](../quickstart.md#1-initialize-a-session)).
    - In a block with `>>>`, type what follows each `>>>`; the lines
      under it are what Python prints. A `...` inside a printed value
      stands for a part that differs on every run, such as a pseudonym or
      a date.
    - The session also prints progress bars, status lines and `WARNING`
      lines as it works. They are not shown here.
      `ISOCENTER_SHOW_PROGRESS=0` turns the bars off.

```python
import shutil
from pathlib import Path

import pydicom.data

Path("input").mkdir(exist_ok=True)
shutil.copy(pydicom.data.get_testdata_file("waveform_ecg.dcm"), "input")
```

Your labels live outside the DICOM files, in whatever your study keeps
them in. Here they are a small table keyed by each file's path under
`input/`. Save it as `labels.csv`:

<!-- tutorial: file=labels.csv -->
```csv
file,chagas
waveform_ecg.dcm,False
```

## 1. De-identify and export as WFDB

The Challenge's own data has already been through this step; yours has
not. Write and load a configuration, audit, anonymize, then export with
`format="wfdb"`:

```python
from isocenter import Session

session = Session("tutorial.db")
session.ingest("input")
session.create_config("config.yaml")
session.load_config("config.yaml")
session.audit()
session.anonymize()
records = session.export("challenge", format="wfdb")
session.save(sync=True)
```

`create_config()` writes a configuration built on the DICOM Basic Profile
([Configuration](../configuration.md)). A WFDB export does not save the
session, so `save(sync=True)` keeps the de-identified graph in the store.

`export()` returns the path of each record's header:

```python
>>> records
['challenge/Subject_ANON_.../Study_..._..._.../Series_NoNumber_ECG_Series_.../ANON_..._0_0.hea']
```

Each record sits in a folder per patient, study and series, and is named
after the patient's pseudonym. The Challenge's own code finds records in
subfolders too: its `find_records()` walks the whole folder you give it.
[Waveforms and WFDB export](../waveforms.md) describes the layout and
every field the exporter writes.

## 2. Read what was written

Open the header with `wfdb`, the package the Challenge's example code
uses:

```python
import wfdb

record_name = records[0].removesuffix(".hea")
header = wfdb.rdheader(record_name)
```

```python
>>> header.fs, header.n_sig, header.sig_len
(1000, 12, 10000)
>>> header.units
['uV', 'uV', 'uV', 'uV', 'uV', 'uV', 'uV', 'uV', 'uV', 'uV', 'uV', 'uV']
>>> header.sig_name
['5.6.3-9-1', '5.6.3-9-2', '5.6.3-9-61', '5.6.3-9-62', '5.6.3-9-63', '5.6.3-9-64', '5.6.3-9-3', '5.6.3-9-4', '5.6.3-9-5', '5.6.3-9-6', '5.6.3-9-7', '5.6.3-9-8']
>>> header.comments
['de-identified start date: ...']
```

Twelve leads, ten seconds at 1000 Hz. Four things differ from a
Challenge header:

- **The lead names are codes.** This file names each lead by a coded
  Channel Source, from the SCP-ECG vocabulary, and Isocenter 1.0 writes
  the code value: `5.6.3-9-1` is lead I. The Challenge's headers say `I`,
  `II`, `III`, `AVR`, `AVL`, `AVF`, `V1` to `V6`, and its helper code
  reorders leads by name.
  [#828](https://github.com/kvnlng/Isocenter/issues/828) proposes writing
  the lead name instead.
- **The units are microvolts.** The Challenge's headers use millivolts.
- **There is no label.** The Challenge's headers end with comment lines
  such as `# Chagas label: False` and `# Source: CODE-15%`. Isocenter
  writes no comment line except the start date, because WFDB readers show
  comments verbatim and MIT-BIH convention puts age, sex and diagnosis
  there ([what is and isn't de-identified](../waveforms.md#what-is-and-isnt-de-identified)).
  The label is yours to add.
- **The start-date comment comes before the signal lines.** WFDB readers
  accept that, but the Challenge's `get_signal_names()` and
  `get_signal_files()` read lines 1 to 12 as the signal lines. `wrheader()`
  in the next step writes every comment after the signal lines, so rewrite
  the header even if your leads and units already match.

The sampling rate differs too: the Challenge's records are 400 Hz or
500 Hz. That is a question for your model's preprocessing, not for the
header, so this page leaves it alone.

## 3. Make each header read like the Challenge's

The next block rewrites only the header. The samples in the `.dat` file
are not touched.

**Lead names.** A map from each SCP-ECG code to the lead's name:

```python
SCP_ECG_LEADS = {
    "5.6.3-9-1": "I", "5.6.3-9-2": "II", "5.6.3-9-61": "III",
    "5.6.3-9-62": "aVR", "5.6.3-9-63": "aVL", "5.6.3-9-64": "aVF",
    "5.6.3-9-3": "V1", "5.6.3-9-4": "V2", "5.6.3-9-5": "V3",
    "5.6.3-9-6": "V4", "5.6.3-9-7": "V5", "5.6.3-9-8": "V6",
}
```

The Challenge's code matches lead names without regard to case, so `aVR`
and `AVR` are the same lead to it.

**Labels.** Your table is keyed by source file, and the export is named
by pseudonym. The link between them is the session's manifest: one row
per instance, holding the file it was read from and, after `anonymize()`,
the Patient ID its export carries.

```python
import csv
import json

session.generate_manifest("manifest.json", format="json")
with open("manifest.json", encoding="utf-8") as manifest_file:
    manifest = json.load(manifest_file)["items"]
with open("labels.csv", encoding="utf-8") as labels_file:
    label_of_file = {row["file"]: row["chagas"]
                     for row in csv.DictReader(labels_file)}

label_of_patient = {}
for item in manifest:
    label = label_of_file[Path(item["file_path"]).relative_to("input").as_posix()]
    if label_of_patient.setdefault(item["patient_id"], label) != label:
        raise ValueError(f"{item['patient_id']}: files disagree on the label")
```

The table is keyed by the path under `input/` rather than the bare file
name, because archives often reuse a file name in different folders. The
loop refuses a patient whose files disagree on the label, rather than
keeping whichever came last.

The manifest pairs each source file with its pseudonym, so it is a
crosswalk back to your source data. Keep it with the store, never with
the records you share ([Manifests](../analytics.md#manifests)).

The Chagas label belongs to the patient, so this page labels every record
of a patient the same. Each record sits in a folder named
`Subject_<pseudonym>`, which says whose record it is.

Two cases this join does not reach
([#830](https://github.com/kvnlng/Isocenter/issues/830)). If your labels
belong to each recording rather than each patient, the manifest cannot
join them yet: a record's name is the pseudonym, the Series Number and the
Instance Number, and the manifest carries neither number. And a file with
no Patient ID exports under `Subject_UnknownPatient`, while the manifest
lists it under a key of its own, so the lookup below fails for it.

**The rewrite.** For each record: rename the leads, restate the gain in
millivolts (the gain is ADC units per physical unit, so a thousandfold
larger unit needs a thousandfold larger gain), and add the two lines:

```python
for hea in records:
    name = hea.removesuffix(".hea")
    patient_id = Path(hea).parents[2].name.removeprefix("Subject_")
    header = wfdb.rdheader(name)
    header.sig_name = [SCP_ECG_LEADS.get(lead, lead) for lead in header.sig_name]
    header.adc_gain = [gain * 1000 if unit == "uV" else gain
                       for gain, unit in zip(header.adc_gain, header.units)]
    header.units = ["mV" if unit == "uV" else unit for unit in header.units]
    header.comments += [f"Chagas label: {label_of_patient[patient_id]}",
                        "Source: Local ECG archive"]
    header.wrheader(write_dir=str(Path(hea).parent))
```

A record whose signals are already in millivolts keeps its gain; only
microvolt signals are restated.

A lead name the map does not know passes through unchanged, so a file
that named its leads another way shows up rather than being renamed
wrongly. Two cases the map does not cover: a cart that codes its leads in
MDC (IEEE 11073) exports those code values, which need their own entries;
and a lead with no coded source is written `ch0`, `ch1` and so on, because
the Basic Profile removes Channel Label `(003A,0203)` (give it
`action: KEEP` to keep lead names). Check every record's names, not just
the first:

```python
>>> {lead for hea in records
...  for lead in wfdb.rdheader(hea.removesuffix(".hea")).sig_name} - set(SCP_ECG_LEADS.values())
set()
```

## 4. Read it back the way the Challenge does

The Challenge's helper code loads each record's signal with
`wfdb.rdsamp()` and reads the label from the header line that starts
`# Chagas label:`. Do
the same:

```python
signal, fields = wfdb.rdsamp(record_name)
with open(records[0], encoding="utf-8") as hea_file:
    label_line = next(line.strip() for line in hea_file
                      if line.startswith("# Chagas label:"))
```

```python
>>> fields["sig_name"]
['I', 'II', 'III', 'aVR', 'aVL', 'aVF', 'V1', 'V2', 'V3', 'V4', 'V5', 'V6']
>>> fields["units"][0], signal.shape
('mV', (10000, 12))
>>> label_line
'# Chagas label: False'
>>> round(float(abs(signal).max()), 3)
1.962
```

The largest deflection is just under 2 mV, an ordinary ECG amplitude,
which says the gain was restated correctly. Before the rewrite `rdsamp`
reported the same deflection as 1962.5, in microvolts.

The Challenge's headers also carry `# Age:` and `# Sex:`. The Basic
Profile removes both from the DICOM, and this page does not add them
back. Whether your protocol lets you release age and sex, and at what
precision, is a decision for your study, not for this tutorial.

## 5. Read the grade

Generate the report last, after every export:

```python
session.generate_report("report.md")

def grade_line(path):
    with open(path, encoding="utf-8") as report_file:
        return next(line.strip() for line in report_file
                    if "Validation Status" in line)
```

```python
>>> print(grade_line("report.md"))
| **Validation Status** | **REVIEW_REQUIRED** |
```

This file is why. A DICOM waveform can hold several multiplex groups,
each with its own channels and sampling rate. This one holds two at the
same rate: the ten-second rhythm and a median beat the cart derived from
it. Isocenter keeps the first group, the rhythm, and discards the rest at
ingest, with a warning and a `DATA_LOSS` row
([Limitations](../waveforms.md#limitations)). Signal that was in
the source and is not in the export is a loss a person should look at,
so the report asks for review rather than grading `PASS`. Your own files
may hold one group and grade `PASS`. Either way, read the report before
you train on the records.

## 6. Look at the records in Murmur Studio

[Murmur Studio](https://kvnlng.github.io/Murmur/) is a macOS viewer for
WFDB recordings. A folder with no `RECORDS` index is scanned flat, and the
export puts each record three folders down, so first write the index
PhysioNet corpora carry: one record path per line, relative to the folder.

```python
Path("challenge", "RECORDS").write_text(
    "".join(Path(hea).relative_to("challenge").with_suffix("").as_posix() + "\n"
            for hea in records))
```

Then open the `challenge` folder with **File ▸ Open Record…**. Murmur
follows the `RECORDS` index down the tree and lists each record by its
path, with its signal count, sample rate and duration. It is a quick way
to see that each record holds twelve leads and, opening each, that none
looks flat or clipped, before a model sees them. Murmur's guide to
[reviewing a PhysioNet corpus](https://kvnlng.github.io/Murmur/reviewing-a-corpus)
covers folders of many thousands of records.

Beside each record, the export writes the cart's own findings as
`<record>.annotations.json`, the file Murmur reads as an annotation
layer:

```python
with open(record_name + ".annotations.json", encoding="utf-8") as notes_file:
    findings = json.load(notes_file)["findings"]
```

```python
>>> len(findings)
66
>>> sorted({finding["category"] for finding in findings})
['uncoded']
```

The cart marked six fiducial points on each of eleven beats; each finding
keeps its kind and sample position, so Murmur draws it where the cart put
it. The
names are withheld: the exporter writes a finding's name only when its
code comes from a vocabulary it recognises, and 1.0 does not recognise
SCP-ECG, the vocabulary this cart used
([#828](https://github.com/kvnlng/Isocenter/issues/828)). Passing
`include_annotation_text=True` to `export()` writes the names. It would
also write an annotation's free text, but the Basic Profile has already
replaced that text with a dummy, so none comes out here. Pass it only when
your protocol allows the cart's text out
([what is and isn't de-identified](../waveforms.md#what-is-and-isnt-de-identified)).

```python
session.close()
```

## Where each Challenge header field comes from

| Challenge header | From Isocenter | Added by you |
| :--- | :--- | :--- |
| Record line: name, 12 signals, rate, length | Written by `export(format="wfdb")` | Resampling, if your model needs 400 or 500 Hz |
| Signal lines: format, gain, baseline | Written, in microvolts | Restating the gain in millivolts |
| Lead names | Written as the file's codes | Renaming SCP-ECG codes (until [#828](https://github.com/kvnlng/Isocenter/issues/828)) |
| `# Chagas label:`, `# Source:` | Never written | From your own labels, through the manifest |
| `# Age:`, `# Sex:` | Never written; the Basic Profile removes both | Only if your protocol allows it |

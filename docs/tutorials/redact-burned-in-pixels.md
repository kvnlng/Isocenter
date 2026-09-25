# Redact burned-in text for one machine

<!-- tutorial: inputs=MR_small.dcm -->

Some scanners draw the patient's name, an accession number or a date
into the image itself. That text is pixels, not an attribute, so no
`phi_tags` rule reaches it. What reaches it is a **redaction zone**: a
rectangle that `redact()` sets to zero in every image from a given
machine. This tutorial writes one zone for the machine that made
pydicom's bundled `MR_small.dcm`, redacts, exports, and reads the
exported pixels back to show which ones changed.

!!! tip "Run it yourself"

    Every Python block on this page runs, in order, as part of
    Isocenter's test suite, and every output shown is checked.

    - **Start in a new, empty folder.** Each tutorial creates its own
      `tutorial.db` and export folders, and running one in another
      tutorial's folder changes what it prints. The first block below
      copies the input file from pydicom into `input/`.
    - Paste the blocks into a Python prompt or a notebook. In a `.py`
      script, put them under `if __name__ == "__main__":`
      ([why](../quickstart.md#1-initialize-a-session)).
    - In a block with `>>>`, type what follows each `>>>`; the lines
      under it are what Python prints. A `...` in that output stands for
      a value that differs on every run, such as a pseudonym or a UID.
    - The session also prints progress bars, status lines and `WARNING`
      lines as it works. They are not shown here.
      `ISOCENTER_SHOW_PROGRESS=0` turns the bars off.

```python
import shutil
from pathlib import Path

import pydicom.data

Path("input").mkdir(exist_ok=True)
shutil.copy(pydicom.data.get_testdata_file("MR_small.dcm"), "input")
```

## 1. Find the machine

A zone belongs to a machine, and Isocenter tells machines apart by their
Device Serial Number `(0018,1000)`. Ingest the file and read the serial
from the cohort report:

```python
from isocenter import Session

session = Session("tutorial.db")
session.ingest("input")
cohort = session.get_cohort_report()
```

```python
>>> cohort[["PatientID", "Modality", "DeviceSerial"]]
  PatientID Modality DeviceSerial
0      4MR1       MR     -0000200
```

A series with no Device Serial Number matches no machine rule, so a zone
never reaches it. Section 5 says what that means for the grade.

## 2. Write the zone

The image is 64 by 64 pixels. Suppose this machine prints its text in
the top-left corner: the first ten rows, the left half of the width. A
zone is `[row_start, row_end, col_start,
col_end]`, counted from 0, with each end one past the last row or column
it covers:

<!-- tutorial: file=config.yaml -->
```yaml
privacy_profile: "basic@2026c"
machines:
  - serial_number: "-0000200"
    redaction_zones:
      - [0, 10, 0, 32]
```

Quote the serial number. Unquoted, YAML reads `-0000200` as the octal
number -128, and `load_config()` refuses it
([Schema](../configuration.md#schema-version-2)).

```python
session.load_config("config.yaml")
```

## 3. De-identify, then redact

`anonymize()` handles the attributes under the Basic Profile, and
`redact()` handles the pixels. They are separate passes:

```python
session.anonymize()
redacted = session.redact()
```

`redact()` returns how many images it changed:

```python
>>> redacted
1
```

It changes the pixels in the session's store, never the file in
`input/`. The Basic Profile replaced the Device Serial Number in the
attributes, and the rule still matched: a rule is matched on the serial
the series was ingested with.

## 4. Export and read the pixels back

```python
session.export("export", use_compression=False)
session.generate_report("report.md")
```

`use_compression=False` writes the pixels uncompressed, so this page
does not depend on the JPEG 2000 codec to read them back. Open the
exported file and the source file side by side:

```python
import pydicom
from pathlib import Path

exported_path = next(Path("export").rglob("*.dcm"))
exported = pydicom.dcmread(exported_path)
source = pydicom.dcmread("input/MR_small.dcm")

pixels = exported.pixel_array
original = source.pixel_array
```

Rows 0 to 9, columns 0 to 31, are zero. Before redaction, no pixel in
them was:

```python
>>> int(original[:10, :32].min())
239
>>> int(pixels[:10, :32].max())
0
```

The zone's bounds are rows first, then columns. In the same ten rows,
the columns past the zone are exactly what the source held, and so is
every row below it:

```python
>>> bool((pixels[:10, 32:] == original[:10, 32:]).all())
True
>>> bool((pixels[10:] == original[10:]).all())
True
```

The exported file also says what was done to it: Burned In Annotation
`(0028,0301)` reads `NO`, because `redact()` cleared the region the rule
names.

```python
>>> exported.BurnedInAnnotation
'NO'
```

## 5. Read the grade

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

```python
session.close()
```

`PASS` says the zones you wrote were applied. It does not say the rest
of each image is free of text: nothing on this page read the pixels for
text. `scan_pixel_content()` does, with OCR, over the images of every
machine your configuration names by serial with zones, and reports text
no zone covers ([Intelligent Verification](../ocr.md#intelligent-verification)).
A series with no Device Serial Number is reached by neither a zone nor
that scan, so check such a series yourself.

A zone is applied whether or not you call `redact()`: `export()` looks up
every matching rule and zeroes a copy of each frame it writes. Calling
`redact()` first is what puts the redacted pixels in the store, so the
store and the export agree. Only `redact()` writes Burned In Annotation
`NO`: an export that applies a zone by itself zeroes the same pixels and
leaves that attribute as the source had it.

!!! note "Finding the zones for a new machine"

    This page wrote its zone by hand. For a machine you have not seen,
    `session.discover_redaction_zones(serial_number)` reads a sample of
    that machine's images with OCR and returns the text it found, with
    `to_zones()` to group it into zones for a rule. It needs the `ocr`
    extra and the `tesseract` program, so this page does not run it.
    [Zone Discovery](../ocr.md#setting-up-new-machines-zone-discovery)
    shows how.

## What a rule matches

- **`serial_number`** is compared exactly with Device Serial Number
  `(0018,1000)`. `"*"` matches every series that has one.
- **Every matching rule applies.** An exact rule and a `"*"` rule both
  zero their zones.
- **A zone's end must be greater than its start** on both axes. A zone
  with no area is refused at `redact()` with `RedactionError`.

[Pixel Redaction](../configuration.md#pixel-redaction-machines) is the
full reference for the `machines:` section.

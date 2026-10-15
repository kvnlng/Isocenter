# Burned-in text (OCR)

Some equipment draws the patient's name, a date or an accession number into the image itself. That text is pixels, so no `phi_tags` rule reaches it. What reaches it is a **redaction zone**: a rectangle that is set to zero in every image from one machine (see [Pixel Redaction](configuration.md#pixel-redaction-machines), and the worked example [Redact burned-in text for one machine](tutorials/redact-burned-in-pixels.md)).

Isocenter uses **Tesseract** optical character recognition to help you write and check those zones:

1. **Find** where a machine draws its text, from a sample of its images: `discover_redaction_zones()`.
2. **Check** that the zones you configured cover the text actually present: `scan_pixel_content()`.
3. **Fix** the zones from what the check found: `auto_remediate_config()`.

And, for text that moves from image to image, or images whose series carry no Device Serial Number, which no zone reaches:

4. **Redact, report or withhold** the text found on each image: the [`burned_in_text`](configuration.md#burned-in-text) configuration key, applied by `redact()` ([below](#redacting-the-text-ocr-finds-on-each-image)).

OCR runs only when you call one of the first three, or, with `burned_in_text` configured, `redact()` or `export(check_burned_in=True)`. Plain `export()` and the compliance grade never run OCR: without `burned_in_text`, a `PASS` says the zones you wrote were applied, not that the rest of each image is free of text. Without `burned_in_text`, `export(check_burned_in=True)` re-runs the tag scan (`audit()`) and does not look for text in the pixels; with it, it also reads every image in the policy's scope it is about to write ([`check_burned_in=True`](export-output.md#check_burned_intrue)).

## Prerequisites

OCR needs two things. The `ocr` extra, which brings `pytesseract` (quoted,
because zsh expands unquoted brackets):

```bash
pip install "isocenter[ocr]"
```

and the Tesseract binary, which pip cannot install:

=== "macOS"
    ```bash
    brew install tesseract
    ```

=== "Linux (Ubuntu/Debian)"
    ```bash
    sudo apt-get install tesseract-ocr
    ```

Without either, `scan_pixel_content()` and `discover_redaction_zones()` raise
`OcrUnavailableError`, a `RuntimeError`, naming what is missing, before they scan
anything. So does `redact()` when the configuration holds `burned_in_text`,
before it redacts anything, the zones included, and
`export(check_burned_in=True)`, before its tag scan; `load_config()` still loads
such a file, so one file serves machines with and without OCR. `pixel_analysis.HAS_OCR` says only whether `pytesseract` imported; it
does not check the binary. If you point `pytesseract.pytesseract.tesseract_cmd`
at a tesseract that is not on `PATH`, the scan uses that one.

A `burned_in_text` target naming `names` ([below](#redacting-only-some-kinds-of-text)) needs one thing more: the `nlp` extra and spaCy's English model, which pip cannot install from the extra:

```bash
pip install "isocenter[nlp]"
python -m spacy download en_core_web_sm
```

Without them, `redact()` and `export(check_burned_in=True)` raise
`NlpUnavailableError`, an `OcrUnavailableError`, in the same places and before
anything. spaCy publishes no free-threaded wheel, so on the free-threaded build
(3.14t) `names` cannot be used; `dates` and `ids` need no model.

**When an image cannot be read.** `scan_pixel_content()` lists each instance
whose pixels could not be loaded, or whose OCR failed on any frame, in
`report.failures` and warns with the count. `discover_redaction_zones()` warns the
same way and counts only the instances it read in `n_sources`. Both write one
`WARNING` audit row per instance they could not read, naming it and the reason, so
the compliance report grades the run `REVIEW_REQUIRED` and lists each one under
"Exceptions & Errors". The rows stay in the store's audit log: a rescan that reads
everything after you fix the cause reports no failures but does not remove them,
so the store's reports still grade `REVIEW_REQUIRED`. If either call could read
none of the instances it tried, it raises `PixelScanError`, also a
`RuntimeError`, after the pass and the audit rows.

`scan_pixel_content()` runs in worker processes (threads on a free-threaded
build). `discover_redaction_zones()` runs in threads, but in processes when
`ISOCENTER_MAX_TASKS_PER_CHILD` is set. [Environment Variables](environment.md)
lists the variables that change either choice.
Run either from a script whose top level is guarded by
`if __name__ == "__main__":`, as the examples below are.

## Setting Up New Machines (Zone Discovery)

A new machine in your configuration has no zones: `create_config()` writes an empty `redaction_zones` list for each machine it does not recognise. The same model in the same room tends to draw its text in the same place every time. `discover_redaction_zones()` runs OCR over a random sample of one machine's instances and reports where text was found, so you can write zones from what the data does rather than from one screenshot. `serial_number=""` samples every series with no Device Serial Number, a series with no equipment at all included; the comparison is exact, so `"*"` is a literal here, not every series.

```python
import isocenter

if __name__ == "__main__":
    session = isocenter.Session("my_project.db")
    session.ingest("dicom_data/")

    # One machine at a time: zones are a property of the device, not the cohort.
    result = session.discover_redaction_zones(
        serial_number="SN-12345",
        sample_size=50,
        min_confidence=60.0,
    )
    print(len(result))                  # candidate text regions found

    # Group the candidates into zones. Raise pad_x to merge words on one line.
    zones = result.to_zones(pad_x=100, pad_y=10)
    for z in zones:
        print(f"Type: {z['type']}")         # LIKELY_NAME, PROPER_NOUN, or TEXT
        print(f"Zone: {z['zone']}")         # [y1, y2, x1, x2]
        print(f"Examples: {z['examples']}") # ['SMITH^JOHN', 'MERCY', ...]
        print("-" * 20)
    session.close()
```

`sample_size` is the number of instances read (default 50); `min_confidence` is the lowest Tesseract confidence, 0 to 100, a word needs to be kept (default 80).

### What discovery returns

A `DiscoveryResult` holds `DiscoveryCandidate` records: `text`, `confidence`,
`box` (`[x, y, w, h]`, OCR box space), `source_index` (which sampled instance it
came from) and `classification`. It is iterable and sized, and `n_sources` is the
number of instances it read.

`to_zones()` clusters the candidates, unions each cluster's boxes, drops any
merged box narrower or shorter than 6 pixels, and keeps only clusters seen in at
least `min_occurrence` of the sampled instances. **The default `min_occurrence`
is 0.1, so text seen in fewer than 10% of the sampled images is dropped**: a name
that appears in one frame out of fifty is treated as noise. To see everything
that was found, pass `min_occurrence=0` or read `to_dataframe()`. Each zone's
`zone` is `[y1, y2, x1, x2]`, the form a rule stores, and its `type` is
`LIKELY_NAME` if any member matched the name pattern (text with a `^`),
`PROPER_NOUN` if any was a named person or organisation (with the `nlp` extra)
or holds a word of two or more characters (punctuation removed) that starts
with a capital letter, such as `T1` or `L5`, and `TEXT` otherwise.

`filter()`, `to_zones()` and `to_dataframe()` are frozen for 1.x.
`get_density_matrix()`, `visualize_heatmap()`, `analyze_temporal_stability()`
and `n_sources` are documented but internal: they may change in a 1.x release,
with a changelog entry ([API stability](api/stability.md)).

### Inspecting the candidates

`to_dataframe()` needs only pandas, which Isocenter already depends on.

```python
import re

# One row per candidate
df = result.to_dataframe()
high_conf = df[df['confidence'] > 90.0]
print(high_conf['text'].value_counts().head())

# Keep candidates that look like years OR come from the first 10 sampled images
filtered = result.filter(lambda c:
    re.match(r"\d{4}", c.text) or c.source_index < 10
)
zones = filtered.to_zones()

# A number keeps candidates at or above that confidence
confident = result.filter(90.0)
```

**Static or transient.** `analyze_temporal_stability()` groups the candidates with no occurrence floor and labels each zone by the share of sampled images it appears in: `STATIC_ALWAYS` above 90%, `STATIC_FREQUENT` above 50%, `TRANSIENT` otherwise.

```python
for item in result.analyze_temporal_stability():
    print(f"Zone: {item['zone']} | Status: {item['status']} ({item['occurrence']*100:.1f}%)")
# Zone: [476, 496, 302, 400] | Status: STATIC_ALWAYS (100.0%)
# Zone: [16, 36, 21, 184] | Status: TRANSIENT (40.0%)
```

**Where the hits fell.** `visualize_heatmap()` prints an ASCII sketch, and `get_density_matrix()` returns the counts as a list of lists for `matplotlib`'s `imshow`:

```python
print(result.visualize_heatmap(bins=(20, 20)))
matrix = result.get_density_matrix(bins=(100, 100))
```

**`get_density_matrix()` is not an image-space heatmap.** It bins each candidate's
box centre on a grid scaled to the largest box *origin* among the candidates, not
to the image's Rows and Columns, so the grid stretches to fit whatever was found,
and two scans are not comparable to each other or to the image. `visualize_heatmap()`
uses the same grid. Take coordinates from `to_zones()` or from each candidate's `box`.

### Entity Detection Modes

Discovery classifies each word in one of two ways:

1. **Regex heuristics (default)**: detects DICOM name patterns (e.g., `Smith^John`) and capitalized phrases.
2. **NLP (optional)**: with the `nlp` extra and its language model, `discover_redaction_zones()` uses **spaCy** named entity recognition, which also finds names written without carets (e.g., "John Smith").

    ```bash
    pip install "isocenter[nlp]"
    python -m spacy download en_core_web_sm
    ```

    The extra installs spaCy; the language model is a separate download
    (see [Installation](installation.md)). Without either, discovery logs a
    warning and uses the regex tier rather than failing.

### Applying Zones

Add the `zone` values to your `isocenter_config.yaml`.
Take them from `zone["zone"]` in `to_zones()`, which is `[y1, y2, x1, x2]`,
not from a candidate's `box`, which is `[x, y, w, h]`.

```yaml
machines:
  - serial_number: "SN-NEW"
    redaction_zones:
      # Found: LIKELY_NAME ['Smith^John'] (candidate box [20, 50, 200, 30])
      - [50, 80, 20, 220]
```

Or in code, with `session.configuration.add_rule()` or `update_rule()` (see [Programmatic Configuration](configuration.md#programmatic-configuration)), then `session.configuration.save()`.

## Redacting the text OCR finds on each image

A zone is a rectangle on every image of one machine. Text that moves (a technologist's initials, a name block placed wherever the operator clicked), and images whose series carry no Device Serial Number, need a reading of each image instead. That is the `burned_in_text` key:

```yaml
burned_in_text:
  action: redact        # report | redact | withhold
  scope: modalities     # flagged | modalities | all
  modalities: [CR, DX, MG]
  min_confidence: 80
```

`scope` is required ([#1121](https://github.com/kvnlng/Isocenter/issues/1121)). `flagged` reads the images whose Burned In Annotation says `YES`, plus any image this store has redacted or found text on before; `modalities` reads those and every image of the listed modalities; `all` reads every image that holds pixels. Reading everything is slow on a large cohort: on a 66-file MIDI-B sample `redact()` under `all` took 24 s against 0.1 s without the key, about 2.5 hours projected over the Validation set's ~24,000 images, nearly all CT, MR and PET slices that rarely carry burned-in text. `modalities: [CR, DX, MG, US]` reads 258 of them. No MIDI-B file says Burned In Annotation `YES`, so `flagged` alone reads nothing there.

With it loaded, `redact()` reads every image in scope with OCR, after zeroing (on a copy) the zones its machine rules hold, keeps every box tesseract reports at `min_confidence` or more whose text holds at least two letters or digits (a single `a` or `4` is not a box; `JT` is), and records the boxes on the image, never the text. Under `redact` it blanks each box on its own frame, widened by 15% of the box's height on every side (at least 2 pixels), gives the image a new SOP Instance UID derived from the boxes, and reads it once more. Under `report` and `withhold` it changes no pixel, and the export writes the image with a row, or leaves it out with a row. [Configuration](configuration.md#burned-in-text) has the whole contract: the export gate, the rows, the grade.

Nothing OCR read is kept: the workers hand back box coordinates and drop the text, so no audit row, log line, report, store or exception holds it. The rows give counts, frame numbers and the tesseract version.

The boxes depend on the tesseract build and its language data. The same image, threshold and zones under the same tesseract give the same boxes, and so the same UID and the same pixels; another tesseract may find other boxes, and then the image takes another UID, which is right, since its pixels differ. The version is in each image's record and in the pass's `REDACTION` row.

### Redacting only some kinds of text

Under `target: any` (the default) every box is acted on, labels included. A list of kinds acts only on the text read as one of them ([#1121](https://github.com/kvnlng/Isocenter/issues/1121)):

```yaml
burned_in_text:
  action: redact
  scope: modalities
  modalities: [CR, DX, MG, US]
  target: [names, dates, ids]
```

The words tesseract reads at `min_confidence` or more with two letters or digits (the boxes `any` would take) are grouped into the lines tesseract reports. Each line is read for the named kinds, and a word is acted on when a match overlaps it: `DOE, JANE  F  01/01/1970` under `[names, dates]` loses the name and the date, and a `PORTABLE` on the line below stays. Nothing is compared with the patient's own values, which a burned-in name may misspell; the source file is not read again.

* **`names`**: what spaCy's `en_core_web_sm` reads as a person, on the line as read and title-cased (the small model misses most upper-case names), plus a word holding `^` between letters. An institution is not a name. It needs the [`nlp` extra and the model](#prerequisites); there is no fallback when they are missing, and on the free-threaded build (3.14t) it cannot be used. The model runs in no CI job: this arm is tested with a stand-in model only, and its reading of short upper-case OCR lines was measured only on the MIDI-B crops below.
* **`dates`**: day, month and year with one separator, either order or year first, 2- or 4-digit years from 1800 to 2199; `YYYYMMDD`; an English month name beside a day or a year. `O`, `I`, `l`, `|`, `S` and `B` inside a candidate are read as digits, so `0I/O2/197O` is a date. Times of day (`12:34`) are not dates, and `12.5` or `3/4` are not.
* **`ids`**: a run of letters and digits, `-` or `_` allowed inside, of 6 or more characters holding 5 or more digits. `MRN0012345` and `00123456` are IDs; `120kV`, `5.0MHz`, `1024`, `10cm` and `L5-S1` are not.

A narrowed target never redacts a box `target: any` would not: the kinds are read over the boxes `any` keeps, so `min_confidence` has one meaning under every target, and a name read below it is missed under both. Each image with text left in place gets one `RISK` audit row naming the target's kinds and the frames (it does not grade), and the pass's `REDACTION` row and section 5 count the regions read as each kind and the regions left. The read after redaction counts only text of a named kind, so a label left on purpose writes no `WARNING` row. `export(check_burned_in=True)` reads the same way, and needs no identifier, so it reads as well after `anonymize()`. Nothing read is kept: the record holds counts, and under `names` the spaCy and model versions; a record another model read is read again by the next `redact()`, and plain `export()` needs no model.

What it misses: a name the model does not read as a person (on MIDI-B crops it missed one upper-case surname and given name pair, even title-cased); a date in a shape not listed, including two OCR artefacts measured on MIDI-B crops: a space read inside a year (two digits, a space, two digits), and a colon read as the separator between day, month and year (a colon is never a date separator, because a time of day is not a date); an ID shorter than six characters or with fewer than five digits; any word tesseract read below `min_confidence`.

Measured on synthetic MIDI-B Validation crops: under `[names, dates]` all 10 patient blocks were redacted, and `dates` and `ids` matched none of 52 other text boxes. What it takes that is not what you meant: three numbers on one line that read as a date (`10 12 20`, or `10 IS 2020` once `IS` is read as `15`), so a technique or measurement label can be blanked under `[dates]`; and a matrix size such as `1024x1024` under `[ids]`. Both are the safe direction.

### What OCR misses

Measured on bundled images with text drawn on them, and on 35 MIDI-B validation images ([#1108](https://github.com/kvnlng/Isocenter/issues/1108)):

* **Faint or short text.** Text drawn at a third of the image's range above its background was never found; two- and three-letter initials were found at confidence 86 to 96, and one (`ICG`) only at 44 to 72, below the default. In the owner's MIDI-B measurement OCR found text in 31 of 35 answer-key boxes.
* **Text over anatomy is read in fragments.** On a uniform border every word found had all its ink inside its box. Over tissue, a name block was often read as pieces (`E`, `1970-`), and blanking the pieces left the rest readable; the read after redaction catches some of it, as a `WARNING` row. This is why an OCR-only redaction leaves Burned In Annotation as the source had it rather than writing `NO`.
* **Labels you may want are blanked too.** Technique labels (`PA`, `PORTABLE`, `ERECT`) and ultrasound measurement text were read at 95 and above, and `redact` blanks them. Single drawn `L`/`R` side markers were not detected. On 23 bundled anatomy-only images nothing was found at 80 or above; ten boxes were at 60 or above.
* **Side markers carry laterality.** A radiograph's lead `L`/`R` marker is imaged with the patient, and technologist initials are often written on it. On MIDI-B, 25 of the 35 answer-key boxes were such initials; tilted 5 to 25 degrees and small in a 2500 to 3400 pixel image, they are what OCR misses most ([#1121](https://github.com/kvnlng/Isocenter/issues/1121)). When it does find them, `redact` blanks the box, and the `L` or `R` beside the initials usually goes with it: that is clinical information, and nothing in the row says a box sat on a marker, because the text is never kept. A lone `L` or `R` is one letter and is not a box.
* **One stray character is not a box.** Every false box measured on 82 text-free MIDI-B frames was one character read over anatomy (`a` at 86 to 91 on an ultrasound frame), so a region needs two letters or digits; the rule lost no answer-key text.
* **Palette-colour images** are read as their palette indices.

A `PASS` after a `redact` pass says what OCR found was blanked, not that each image is free of text. Use `report` first on a cohort you have not looked at.

## Checking Zones (Verification)

`scan_pixel_content()` runs OCR over the images of the machines you configured and reports the text your zones do not cover. It does not report text inside a zone, so labels your zones already black out do not appear.

### How it works

1. **Match**: each instance is matched to every rule that covers its series, exactly by serial, by `""` (no serial), by `"*"` or by `match:`, as `redact()` reads them.
2. **Scan**: OCR finds every text region in the image.
3. **Filter**: each region is compared with the `redaction_zones` of every matching rule:
    * **Covered**: at least 80% of the region lies in one zone. Not reported.
    * **`PARTIAL_LEAK`**: more than 0% and less than 80% covered. Reported.
    * **`NEW_LEAK`**: not covered at all. Reported.

Text of two characters or fewer is skipped as noise.

**Zones the scan reads.** The zones `redact()` and `export()` apply: both forms a rule accepts, `[y1, y2, x1, x2]` and `{roi: [y1, y2, x1, x2]}`, from every rule that covers the instance's machine, an exact rule, a `""` rule, a `match:` rule and a `"*"` rule alike. A zone that does not hold exactly four values is not a zone here either.

**What is scanned.** Only instances of a machine a rule covers, by exact serial, `""`, `"*"` or `match:`, and only when at least one of the rules covering it has a zone. So:

* a session that has loaded no configuration, or whose rules are all fresh from `create_config()` with empty `redaction_zones`, scans nothing: it prints "No matching configured instances found to scan." with how many instances it skipped, counting those of machines no rule names apart from those whose rules have no zone, and returns an empty report;
* a series with no Device Serial Number is scanned under a `""`, `"*"` or `match:` rule with a zone, as `redact()` redacts it. `scan_pixel_content(serial_number="")` is still no filter: it scans every configured series, not only the serial-less ones.

Load a configuration whose rules have zones first:

```yaml
machines:
  - serial_number: "SN-12345"
    model_name: "Sono1"
    redaction_zones:
      - [0, 30, 0, 200]   # name banner, top left
```

```python
import isocenter

if __name__ == "__main__":
    session = isocenter.Session("my_project.db")
    session.ingest("dicom_data/")
    session.load_config("isocenter_config.yaml")

    # Every configured machine that has zones
    report = session.scan_pixel_content()

    # OR: one machine
    report = session.scan_pixel_content(serial_number="SN-12345")

    print(f"Found {len(report)} leaks.")
    for finding in report:
        print(f"{finding.metadata['leak_type']}: {finding.value} in {finding.entity_uid}")
    session.close()
```

`finding.value` is the text OCR read, which may be misread (`SMITH4JOHN` for `SMITH^JOHN`), and `finding.entity_uid` is the SOP Instance UID. Text the scan finds is counted in section 5 of the compliance report and does not change the grade; an instance it could not read does.

After changing the zones, scan again: a report with no findings means every text region OCR found is at least 80% covered.

## Automated Remediation

`auto_remediate_config()` turns a scan's findings into zone changes: a `NEW_LEAK` becomes a new zone around its text, and a `PARTIAL_LEAK` grows the zone that covers most of it, in place (a `{roi: [...]}` zone keeps its `note`). A `NEW_LEAK`'s zone goes to the narrowest rule covering the machine: its exact serial rule, else the first `match:` rule, else, for a series with no serial, its `""` rule. A `NEW_LEAK` on a machine that only a `"*"` rule covers becomes a new rule for that machine's serial (`serial_number: ""` for a series with none), with its manufacturer, model and the new zone, rather than a zone on `"*"`, which would redact that region on every machine's images. A suggested `""` rule covers every series with no serial, whatever its device; to confine the zone to one device, write a `match:` rule for its manufacturer and model instead. A `PARTIAL_LEAK` whose zone is the `"*"` rule's does the same with the grown zone: `"*"` is never widened. A zone the rule already holds, in either form, is not added again. It changes the configuration in memory and returns the number of changes.

```python
import isocenter

if __name__ == "__main__":
    session = isocenter.Session("my_project.db")
    session.load_config("isocenter_config.yaml")

    # 1. Scan
    report = session.scan_pixel_content()

    # 2. Apply suggestions to the in-memory configuration
    count = session.auto_remediate_config(report)

    if count > 0:
        print(f"Applied {count} fixes.")

        # 3. Scan again to confirm
        report_v2 = session.scan_pixel_content()
        print(f"{len(report_v2)} leaks left.")  # 0 when the new zones cover everything

        # 4. Write the file load_config() read. To write another file,
        #    set session.configuration.config_path first.
        session.configuration.save()
    session.close()
```

Read the zones it added before you keep them: a zone around a word OCR misread, or around text that is not an identifier, blacks out those pixels in every image from that machine.

## Configuration Reference

Your `isocenter_config.yaml` defines the zones used for verification. See [Pixel Redaction](configuration.md#pixel-redaction-machines) for the full rule format.

```yaml
machines:
  - serial_number: "SN-12345"
    model_name: "CT-Scanner-X"
    redaction_zones:
      # [y1, y2, x1, x2] (row start, row end, column start, column end)
      - [0, 100, 0, 200]       # Top-Left Info Box
      - [400, 450, 400, 500]   # Bottom-Right Label
```

## API Reference

`scan_pixel_content()`, `discover_redaction_zones()` and `auto_remediate_config()` are documented on the [Session API](api/session.md) page, and `DiscoveryResult` and the verification classes on the [OCR API](api/ocr.md) page.

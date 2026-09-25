# Burned-in text (OCR)

Some equipment draws the patient's name, a date or an accession number into the image itself. That text is pixels, so no `phi_tags` rule reaches it. What reaches it is a **redaction zone**: a rectangle that is set to zero in every image from one machine (see [Pixel Redaction](configuration.md#pixel-redaction-machines), and the worked example [Redact burned-in text for one machine](tutorials/redact-burned-in-pixels.md)).

Isocenter uses **Tesseract** optical character recognition to help you write and check those zones:

1. **Find** where a machine draws its text, from a sample of its images: `discover_redaction_zones()`.
2. **Check** that the zones you configured cover the text actually present: `scan_pixel_content()`.
3. **Fix** the zones from what the check found: `auto_remediate_config()`.

OCR runs only when you call one of these. None of `redact()`, `export()` or the compliance grade runs OCR: a `PASS` says the zones you wrote were applied, not that the rest of each image is free of text. `export(check_burned_in=True)` re-runs the tag scan (`audit()`); it does not look for text in the pixels.

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
anything. `pixel_analysis.HAS_OCR` says only whether `pytesseract` imported; it
does not check the binary. If you point `pytesseract.pytesseract.tesseract_cmd`
at a tesseract that is not on `PATH`, the scan uses that one.

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

A new machine in your configuration has no zones: `create_config()` writes an empty `redaction_zones` list for each machine it does not recognise. The same model in the same room tends to draw its text in the same place every time. `discover_redaction_zones()` runs OCR over a random sample of one machine's instances and reports where text was found, so you can write zones from what the data does rather than from one screenshot.

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

## Checking Zones (Verification)

`scan_pixel_content()` runs OCR over the images of the machines you configured and reports the text your zones do not cover. It does not report text inside a zone, so labels your zones already black out do not appear.

### How it works

1. **Match**: each instance is matched to a rule by its series' Device Serial Number.
2. **Scan**: OCR finds every text region in the image.
3. **Filter**: each region is compared with the rule's `redaction_zones`:
    * **Covered**: at least 80% of the region lies in one zone. Not reported.
    * **`PARTIAL_LEAK`**: more than 0% and less than 80% covered. Reported.
    * **`NEW_LEAK`**: not covered at all. Reported.

Text of two characters or fewer is skipped as noise.

**Zones the scan reads.** The scan reads only zones written as a list,
`[y1, y2, x1, x2]`. A zone written as `{roi: [...]}`, the other form a rule
accepts, counts as no zone: text inside it is reported as a leak
([#814](https://github.com/kvnlng/Isocenter/issues/814)). And when two rules
share a serial, the scan reads only the first rule's zones, while `redact()`
applies the zones of every matching rule.

**What is scanned.** Only instances whose Device Serial Number equals a rule's `serial_number` exactly, and only when that rule has at least one zone. So:

* a session that has loaded no configuration, or whose rules are all fresh from `create_config()` with empty `redaction_zones`, scans nothing: it prints "No matching configured instances found to scan." and returns an empty report;
* a `"*"` rule is applied by `redact()` and `export()`, but the scan never selects instances by it;
* a series with no Device Serial Number is never scanned.

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

`auto_remediate_config()` turns a scan's findings into zone changes: a `NEW_LEAK` becomes a new zone around its text, and a `PARTIAL_LEAK` grows the zone that covers most of it. It changes the configuration in memory and returns the number of changes.

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

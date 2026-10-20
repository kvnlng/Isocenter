# Burned-in text (OCR)

Some equipment draws the patient's name, a date or an accession number into the image itself. That text is pixels, so no `phi_tags` rule reaches it. What reaches it is a **redaction zone**: a rectangle that is set to zero in every image from one machine. See [Pixel Redaction](configuration.md#pixel-redaction-machines), and the worked example [Redact burned-in text for one machine](tutorials/redact-burned-in-pixels.md).

Isocenter uses **Tesseract** optical character recognition to help you write and check those zones:

1. **Find** where a machine draws its text, from a sample of its images: `discover_redaction_zones()`.
2. **Check** that the zones you configured cover the text actually present: `scan_pixel_content()`.
3. **Fix** the zones from what the check found: `auto_remediate_config()`.

And, for text that moves from image to image, or images whose series carry no Device Serial Number, which no zone reaches:

4. **Redact, report or withhold** the text found on each image: the [`burned_in_text`](configuration.md#burned-in-text) configuration key, applied by `redact()` ([below](#redacting-the-text-ocr-finds-on-each-image)).

OCR runs only when you call one of the first three, or, with `burned_in_text` configured, `redact()` or `export(check_burned_in=True)`. Plain `export()` and the compliance grade never run OCR. Without `burned_in_text`, a `PASS` says that the zones you wrote were applied, not that the rest of each image is free of text. Without `burned_in_text`, `export(check_burned_in=True)` re-runs the tag scan (`audit()`) and does not look for text in the pixels. With `burned_in_text`, it also reads every image in the policy's scope that it is about to write ([`check_burned_in=True`](export-output.md#check_burned_intrue)).

## Prerequisites

OCR needs two things. The first is the `ocr` extra, which brings
`pytesseract`. Quote the argument, because zsh expands unquoted brackets:

```bash
pip install "isocenter[ocr]"
```

The second is the Tesseract binary, which pip cannot install:

::::{tab-set}
:::{tab-item} macOS
:sync: macos

```bash
brew install tesseract
```
:::
:::{tab-item} Linux (Ubuntu/Debian)
:sync: linux-ubuntu-debian

```bash
sudo apt-get install tesseract-ocr
```
:::
::::

Without either, `scan_pixel_content()` and `discover_redaction_zones()` raise
`OcrUnavailableError`, a `RuntimeError`, naming what is missing, before they scan
anything. Without either, and when the configuration holds
`burned_in_text`, `redact()` and `export(check_burned_in=True)` raise it too. `redact()` raises before it
redacts anything, the zones included. `export(check_burned_in=True)` raises
before its tag scan. `load_config()` still loads such a file, so one file
serves machines with and without OCR. `pixel_analysis.HAS_OCR` says only
whether `pytesseract` imported. It does not check the binary. If you point `pytesseract.pytesseract.tesseract_cmd`
at a tesseract that is not on `PATH`, the scan uses that one.

A `burned_in_text` target that names `names` ([below](#redacting-only-some-kinds-of-text)) needs one more thing: the `nlp` extra and spaCy's English model. pip cannot install the model from the extra:

```bash
pip install "isocenter[nlp]"
python -m spacy download en_core_web_sm
```

Without them, `redact()` and `export(check_burned_in=True)` raise
`NlpUnavailableError`, an `OcrUnavailableError`, in the same places and before
anything. spaCy publishes no free-threaded wheel, so on the free-threaded build
(3.14t) `names` cannot be used. `dates` and `ids` need no model.
`burned_in_text` with `detector: db` needs the `textdetect` extra (OpenCV),
which cannot be installed on the free-threaded build
([below](#detecting-text-with-a-model-detector-db)). Under `target: any`,
`detector: db` needs no tesseract. Under a narrowed target, it needs
tesseract too, and the names model under `names`.

**When an image cannot be read.** `scan_pixel_content()` lists each instance
whose pixels could not be loaded, or whose OCR failed on any frame, in
`report.failures` and warns with the count. `discover_redaction_zones()` warns the
same way and counts only the instances it read in `n_sources`. Both write one
`WARNING` audit row per instance they could not read, which names the instance
and the reason. So the compliance report grades the run `REVIEW_REQUIRED`, and
lists each one under "Exceptions & Errors". The rows stay in the store's audit
log. A rescan that reads everything after you fix the cause reports no
failures, but does not remove the rows. So the store's reports still grade
`REVIEW_REQUIRED`. If either call could read
none of the instances it tried, it raises `PixelScanError`, also a
`RuntimeError`, after the pass and the audit rows.

`scan_pixel_content()` runs in worker processes (threads on a free-threaded
build). `discover_redaction_zones()` runs in threads, but in processes when
`ISOCENTER_MAX_TASKS_PER_CHILD` is set. [Environment Variables](environment.md)
lists the variables that change either choice.
Run either from a script whose top level is guarded by
`if __name__ == "__main__":`, as the examples below are.

## Setting Up New Machines (Zone Discovery)

A new machine in your configuration has no zones. `create_config()` writes an empty `redaction_zones` list for each machine it does not recognise. The same model in the same room tends to draw its text in the same place every time. `discover_redaction_zones()` runs OCR over a random sample of one machine's instances and reports where text was found. So you can write zones from what the data does, not from one screenshot. `serial_number=""` samples every series with no Device Serial Number, a series with no equipment at all included. The comparison is exact, so `"*"` is a literal here, not every series.

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

`sample_size` is the number of instances read (default 50). `min_confidence` is the lowest Tesseract confidence, 0 to 100, that a word needs to be kept (default 80).

### What discovery returns

A `DiscoveryResult` holds `DiscoveryCandidate` records: `text`, `confidence`,
`box` (`[x, y, w, h]`, OCR box space), `source_index` (which sampled instance it
came from) and `classification`. It is iterable and sized, and `n_sources` is the
number of instances it read.

`to_zones()` clusters the candidates, unions each cluster's boxes, drops any
merged box narrower or shorter than 6 pixels, and keeps only clusters seen in at
least `min_occurrence` of the sampled instances. **The default `min_occurrence`
is 0.1, so text seen in fewer than 10% of the sampled images is dropped**. A name
that appears in one frame out of fifty is treated as noise. To see everything
that was found, pass `min_occurrence=0` or read `to_dataframe()`. Each zone's
`zone` is `[y1, y2, x1, x2]`, the form a rule stores. Its `type` is one of
these:

- `LIKELY_NAME` if any member matched the name pattern (text with a `^`)
- `PROPER_NOUN` if any member was a named person or organisation (with the
  `nlp` extra), or holds a word of two or more characters (punctuation
  removed) that starts with a capital letter, such as `T1` or `L5`
- `TEXT` otherwise

`filter()`, `to_zones()` and `to_dataframe()` are frozen for 1.x.
`get_density_matrix()`, `visualize_heatmap()`, `analyze_temporal_stability()`
and `n_sources` are documented but internal. They may change in a 1.x release,
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
to the image's Rows and Columns. So the grid stretches to fit whatever was found,
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

    The extra installs spaCy. The language model is a separate download
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

`scope` is required ([#1121](https://github.com/kvnlng/Isocenter/issues/1121)). Its values are these:

- `flagged` reads the images whose Burned In Annotation says `YES`, plus any image this store has redacted or found text on before.
- `modalities` reads those and every image of the listed modalities.
- `all` reads every image that holds pixels.

Reading everything is slow on a large cohort. On a 66-file MIDI-B sample, `redact()` under `all` took 24 s, against 0.1 s without the key. That projects to about 2.5 hours over the Validation set's ~24,000 images, nearly all CT, MR and PET slices that rarely carry burned-in text. `modalities: [CR, DX, MG, US]` reads 258 of them. No MIDI-B file says Burned In Annotation `YES`, so `flagged` alone reads nothing there.

With it loaded, `redact()` does these things for every image in scope:

- It reads the image with OCR, after zeroing (on a copy) the zones its machine rules hold.
- It keeps every box that tesseract reports at `min_confidence` or more whose text holds at least two letters or digits. A single `a` or `4` is not a box. `JT` is.
- It records the boxes on the image, never the text.

Under `redact`, it blanks each box on its own frame, widened by 15% of the box's height on every side (at least 2 pixels). It gives the image a new SOP Instance UID derived from the boxes, and reads the image once more. Under `report` and `withhold`, it changes no pixel, and the export writes the image with a row, or leaves it out with a row. [Configuration](configuration.md#burned-in-text) has the whole contract: the export gate, the rows, the grade.

Nothing OCR read is kept. The workers return box coordinates and drop the text, so no audit row, log line, report, store or exception holds it. The rows give counts, frame numbers and the tesseract version.

The boxes depend on the tesseract build and its language data. The same image, threshold and zones under the same tesseract give the same boxes, and so the same UID and the same pixels. Another tesseract may find other boxes, and then the image takes another UID, which is right, since its pixels differ. The version is in each image's record and in the pass's `REDACTION` row.

### Redacting only some kinds of text

Under `target: any` (the default) every box is acted on, labels included. A list of kinds acts only on the text read as one of them ([#1121](https://github.com/kvnlng/Isocenter/issues/1121)):

```yaml
burned_in_text:
  action: redact
  scope: modalities
  modalities: [CR, DX, MG, US]
  target: [names, dates, ids]
```

The words that tesseract reads at `min_confidence` or more with two letters or digits (the boxes `any` would take) are grouped into lines by where they sit. On one frame, a word joins a line when both of these are true:

- its height overlaps the line's by at least half the shorter of its own and the line's first word's
- it sits beside the line's words, not under one of them

Each line is read left to right. A line's extent grows with each word it takes, so a slightly tilted line is followed. A word that sits in a gap of another line, under none of its words, still joins that line and is read between its words. That can break a match. Tesseract's own line numbers are not used. Its sparse-text mode can put two words of one line in different blocks, and a name read word by word is no name.

Each line is read for the named kinds, and a word is acted on when a match overlaps it. For example, `DOE, JANE  F  01/01/1970` under `[names, dates]` loses the name and the date, and a `PORTABLE` on the line below stays. Nothing is compared with the patient's own values, which a burned-in name may misspell. The source file is not read again.

* **`names`**: what spaCy's `en_core_web_sm` reads as a person, on the line as read and title-cased (the small model misses most upper-case names), plus a word holding `^` between letters. An institution is not a name. It needs the [`nlp` extra and the model](#prerequisites). There is no fallback when they are missing, and on the free-threaded build (3.14t) it cannot be used. Once the model has loaded, Isocenter ignores two spaCy warnings, W030 and W102, for the rest of the process. Both quote the text they were given, and that text is what the image says. This also applies in `discover_redaction_zones()` and in your own use of spaCy. Every other spaCy and thinc warning reaches your application as usual. This arm is tested with a stand-in model, except for a test that CI runs with the real model on every job that is not free-threaded. The model's reading of short upper-case OCR lines was measured only on the MIDI-B crops below.
* **`dates`**: day, month and year with one separator, in either order or year first, with 2- or 4-digit years from 1800 to 2199. Also `YYYYMMDD`, and an English month name beside a day or a year. `O`, `I`, `l`, `|`, `S` and `B` inside a candidate are read as digits, so `0I/O2/197O` is a date. Times of day (`12:34`) are not dates, and `12.5` or `3/4` are not.
* **`ids`**: a run of letters and digits, `-` or `_` allowed inside, of 6 or more characters holding 5 or more digits. `MRN0012345` and `00123456` are IDs. `120kV`, `5.0MHz`, `1024`, `10cm` and `L5-S1` are not.

A narrowed target never redacts a box that `target: any` would not. The kinds are read over the boxes that `any` keeps. So `min_confidence` has one meaning under every target, and a name read below it is missed under both. Each image with text left in place gets one `RISK` audit row that names the target's kinds and the frames. That row does not grade. The pass's `REDACTION` row and section 5 count the regions read as each kind and the regions left. The read after redaction counts only text of a named kind, so a label left on purpose writes no `WARNING` row. `export(check_burned_in=True)` reads the same way. It needs no identifier, so it reads as well after `anonymize()`. Nothing read is kept. The record holds counts, and under `names` the spaCy and model versions. A record that another model read is read again by the next `redact()`. Plain `export()` needs no model.

What it misses:

- a name that the model does not read as a person (on MIDI-B crops it missed one upper-case surname and given name pair, even title-cased)
- a date in a shape not listed, including two OCR artefacts measured on MIDI-B crops:
    - a space read inside a year (two digits, a space, two digits)
    - a colon read as the separator between day, month and year (a colon is never a date separator, because a time of day is not a date)
- an ID shorter than six characters or with fewer than five digits
- any word that tesseract read below `min_confidence`

Measured on synthetic MIDI-B Validation crops: under `[names, dates]` all 10 patient blocks were redacted, and `dates` and `ids` matched none of 52 other text boxes. It also takes text that is not what you meant:

- three numbers on one line that read as a date (`10 12 20`, or `10 IS 2020` once `IS` is read as `15`), so a technique or measurement label can be blanked under `[dates]`
- a matrix size such as `1024x1024` under `[ids]`

Both are the safe direction.

### What OCR misses

Measured on bundled images with text drawn on them, and on 35 MIDI-B validation images ([#1108](https://github.com/kvnlng/Isocenter/issues/1108)):

* **Faint or short text.** Text drawn at a third of the image's range above its background was never found. Two- and three-letter initials were found at confidence 86 to 96, and one (`ICG`) only at 44 to 72, below the default. In the owner's MIDI-B measurement, OCR found text in 31 of 35 answer-key boxes.
* **Text over anatomy is read in fragments.** On a uniform border, every word found had all its ink inside its box. Over tissue, a name block was often read as pieces (`E`, `1970-`), and blanking the pieces left the rest readable. The read after redaction catches some of it, as a `WARNING` row. This is why an OCR-only redaction leaves Burned In Annotation as the source had it, and does not write `NO`.
* **Labels you may want are blanked too.** Technique labels (`PA`, `PORTABLE`, `ERECT`) and ultrasound measurement text were read at 95 and above, and `redact` blanks them. Single drawn `L`/`R` side markers were not detected. On 23 bundled anatomy-only images, nothing was found at 80 or above. Ten boxes were at 60 or above.
* **Side markers carry laterality.** A radiograph's lead `L`/`R` marker is imaged with the patient, and technologist initials are often written on it. On MIDI-B, 25 of the 35 answer-key boxes were such initials. They are tilted 5 to 25 degrees and small in a 2500 to 3400 pixel image, and they are what OCR misses most ([#1121](https://github.com/kvnlng/Isocenter/issues/1121)). When OCR does find them, `redact` blanks the box, and the `L` or `R` beside the initials usually goes with it. That is clinical information. Nothing in the row says that a box sat on a marker, because the text is never kept. Under tesseract, a lone `L` or `R` is one letter and is not a box. Under [`detector: db`](#detecting-text-with-a-model-detector-db), which reads no letters, it is a box.
* **One stray character is not a box.** Every false box measured on 82 text-free MIDI-B frames was one character read over anatomy (`a` at 86 to 91 on an ultrasound frame). So a region needs two letters or digits. The rule lost no answer-key text.
* **Palette-colour images** are read as their palette indices.

A `PASS` after a `redact` pass says that what OCR found was blanked, not that each image is free of text. Use `report` first on a cohort you have not looked at.

### Detecting text with a model (`detector: db`)

Tesseract decides "is this text?" by reading it, and misses what it cannot read, such as the tilted, small initials on side markers above. `detector: db` gives that decision to a text-detection model instead ([#1121](https://github.com/kvnlng/Isocenter/issues/1121)):

```yaml
burned_in_text:
  action: redact
  scope: modalities
  modalities: [CR, DX, MG]
  detector: db           # tesseract (the default) | db
  min_confidence: 80     # under db: the detector's box score x 100
```

The model is PP-OCRv3 English text detection (DB), from the OpenCV Model Zoo. It is shipped inside the package under Apache-2.0 (`isocenter/resources/text_detection_en_ppocrv3_2023may.onnx`, sha256 `03f550c6...6587`, with a NOTICE beside it giving its source, licence and training data). Nothing is downloaded. It runs under OpenCV, the optional `textdetect` extra:

```bash
pip install "isocenter[textdetect]"
```

**Not on the free-threaded build.** OpenCV publishes no free-threaded (3.14t) wheel, so the extra cannot be installed there. Under `detector: db`, `redact()` and `export(check_burned_in=True)` raise `TextDetectorUnavailableError` there (an `OcrUnavailableError`, so a `RuntimeError`) before anything, zones included. They raise it too where the extra is not installed, or where the model file is not the pinned one. `load_config()`, `save()` and a plain `export()` need neither OpenCV nor the model. A file is portable, and a plain export reads each image's record.

How it reads:

* **The whole frame, once.** A frame whose long side is over 1,280 pixels is read with that side brought to 1,280. A smaller frame is read at its own size, never enlarged. Enlarging small PET frames is what put boxes on text-free frames when this was measured.
* **Inside a border.** Each frame is read inside a 16-pixel border of black (0 on the frame scaled to 8 bits), and the boxes are moved back onto the frame. Without the border, text touching the frame's edge was missed. On the MIDI-B sample, the border found the same 35 of 35 answer-key boxes and put a box on none of the 82 text-free frames, at the same time per image. A border of the frame's median value put a box on an ultrasound frame, so black is used.
* **At most 1,999 regions per frame.** OpenCV is asked for every region on a frame. It is given no limit of its own, because a limit there cuts the list before small regions are dropped, so the cut cannot be seen in what comes back. A frame on which OpenCV returns 2,000 or more regions is treated as a frame that could not be read. The image gets no record, the row says `text detection failed on frame N: 2000 text regions, the detector's limit of 2000 per frame`, and the export withholds the image under `redact` and `withhold`. It is never redacted part-way. The count includes regions below `min_confidence`. 2,000 is not measured on real dense frames. No image in the sample came near 200.
* **One OpenCV thread.** Each worker runs OpenCV on one thread (`cv2.setNumThreads(1)`). That setting is process-wide. When the pass runs in threads (the free-threaded build, a `:memory:` store, or `ISOCENTER_FORCE_THREADS`), the setting also reaches any OpenCV that your own code uses in the same process.
* **`min_confidence` is the detector's box score times 100**, default 80, inclusive. It is not tesseract's word confidence, and the two numbers do not mean the same thing.
* **No tesseract under `target: any`.** The detector reads no text, so tesseract need not be installed. The two-letters-or-digits rule does not apply, because there is nothing to count. **A lone `L` or `R` side marker the detector finds is blanked**, like any other text.
* **Under a [narrowed target](#redacting-only-some-kinds-of-text), each box is read.** The detector says where text is. To say what kind it is, each detected region is cut out with padding of 0.30 of its height, levelled by its angle, enlarged 2×, and read by tesseract as one line (`--psm 7`). The reading is classified as a tesseract line is, and the region is redacted when a named kind is found on it. **A region whose reading holds fewer than two letters or digits cannot be classified and is redacted.** It is counted as `unreadable redacted` in the pass's row and section 5. This is the direction that fails safe. So tesseract is needed here (`OcrUnavailableError` before anything without it), and the names model too under `names`. A narrowed target never redacts a box that `any` would not.

  On the MIDI-B sample below, under `[names, dates]`:

    - every line of all 10 patient blocks was redacted
    - 12 of the 25 sets of initials were redacted, 11 of them as unreadable. The other 13 read as two or more characters that no kind matched, and were left.
    - `dates` and `ids` matched none of the 52 other regions
    - reading added about 0.085 s per box, about 0.25 s per radiograph

  The padding, the enlargement and the levelling were the measurement's pick among variants whose results differed by 1 to 4 counts. That difference is noise, not an optimum.
* **A box larger than a quarter of the frame is not text.** A box whose short side is more than 25% of the frame's short side is dropped, and counted. The box measured is the one that would be blanked: the region's straight box, padded and clipped to the frame. So a tilted region is measured by its straight box in the pass's row and in section 5 (`N box(es) over a quarter of the frame not redacted`). On ultrasound frames, the detector scored whole-frame regions up to 0.98, and no threshold removes those. This rule does. Under `db`, large text such as a title on a small secondary capture is dropped and counted, not redacted. DB widens each region to about 1.5 to 2 times the text's height before it is padded, so the rule reaches smaller text than a quarter of the frame. Measured with the shipped model on synthetic text, these are dropped and counted:

    - horizontal text from about an eighth of the frame's short side (11 to 15%)
    - a long line tilted about 15 to 25 degrees or more, on frames of 512 px or less
* **Each box** is the detected region's straight bounding box, widened by 15% of the region's height on every side (at least 2 pixels). A tilted marker is blanked as its whole straight box, which is the safe direction.
* **Nothing it found is kept but the boxes**, as under tesseract. The image's record names the model (its sha256) and the OpenCV version. The pass's `REDACTION` row says `detection by PP-OCRv3 (sha256 03f550c6b406fda8) on OpenCV V`. A record read by another OpenCV is read again by the next `redact()`. A plain `export()` still trusts it.

Measured before it shipped, on a synthetic MIDI-B Validation sample with OpenCV 5.0.0, the detector did these things:

- It found all 35 answer-key boxes: all 10 patient blocks and all 25 sets of initials, against 14 for tesseract at 80.
- It put a box on none of 82 text-free frames.
- It took about 0.21 s per radiograph on one thread.
- It also found 55 other regions on those 35 radiographs: mostly overlay text outside the answer key (`PORTABLE`, `SUPINE`, counters), 3 lone laterality markers, 7 other marker glyphs, and 5 that were not text, 2 of which score above 80.

Not measured: initials on frames far larger than 5,800 pixels, and images outside the sample on which the box-size rule and the no-enlarging rule were chosen. The boxes come from floating-point inference and may differ between CPUs or OpenCV builds, as tesseract's differ between tesseract builds. Another box set takes another UID, which is right.

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

**Zones the scan reads.** The scan reads the zones that `redact()` and `export()` apply. It reads both forms that a rule accepts, `[y1, y2, x1, x2]` and `{roi: [y1, y2, x1, x2]}`. It reads them from every rule that covers the instance's machine: an exact rule, a `""` rule, a `match:` rule and a `"*"` rule alike. A zone that does not hold exactly four values is not a zone here either.

**What is scanned.** Only instances of a machine a rule covers, by exact serial, `""`, `"*"` or `match:`, and only when at least one of the rules covering it has a zone. So:

* A session that has loaded no configuration, or whose rules are all fresh from `create_config()` with empty `redaction_zones`, scans nothing. It prints "No matching configured instances found to scan." with how many instances it skipped. It counts the instances of machines that no rule names, and separately the instances whose rules have no zone. `scan_pixel_content()` then returns an empty report.
* A series with no Device Serial Number is scanned under a `""`, `"*"` or `match:` rule with a zone, as `redact()` redacts it. `scan_pixel_content(serial_number="")` is still no filter. It scans every configured series, not only the serial-less ones.

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

`finding.value` is the text that OCR read, which may be misread (`SMITH4JOHN` for `SMITH^JOHN`). `finding.entity_uid` is the SOP Instance UID. Text that the scan finds is counted in section 5 of the compliance report and does not change the grade. An instance that the scan could not read does change the grade.

After you change the zones, scan again. A report with no findings means that every text region OCR found is at least 80% covered.

## Automated Remediation

`auto_remediate_config()` turns a scan's findings into zone changes:

- A `NEW_LEAK` becomes a new zone around its text.
- A `PARTIAL_LEAK` grows the zone that covers most of it, in place. A `{roi: [...]}` zone keeps its `note`.

A `NEW_LEAK`'s zone goes to the narrowest rule that covers the machine: its exact serial rule, else the first `match:` rule, else, for a series with no serial, its `""` rule. A `NEW_LEAK` on a machine that only a `"*"` rule covers becomes a new rule for that machine's serial (`serial_number: ""` for a series with none). The new rule holds the machine's manufacturer, model and the new zone. The zone does not go on `"*"`, because there it would redact that region on every machine's images. A suggested `""` rule covers every series with no serial, whatever its device. To confine the zone to one device, write a `match:` rule for its manufacturer and model instead. A `PARTIAL_LEAK` whose zone is the `"*"` rule's does the same with the grown zone, so `"*"` is never widened. A zone that the rule already holds, in either form, is not added again. `auto_remediate_config()` changes the configuration in memory and returns the number of changes.

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

Read the zones that it added before you keep them. A zone around a word that OCR misread, or around text that is not an identifier, blacks out those pixels in every image from that machine.

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

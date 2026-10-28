# MIDI-B benchmark

[MIDI-B](https://www.cancerimagingarchive.net/collection/midi-b-test-midi-b-validation/) is a de-identification benchmark from The Cancer Imaging Archive (TCIA). It provides two synthetic DICOM datasets seeded with realistic identifiers. It also provides an answer key that says what a correct de-identification does to every element of every instance. A [validation script](https://github.com/CBIIT/MIDI_validation_script) scores an output against the key.

This page shows how to configure Isocenter to match that answer key, and why each setting is there. The whole configuration is one YAML file, given in full in the [appendix](#appendix-the-configuration). Every setting is a configuration rule except one, an export flag.

| | Validation | Test |
| :--- | :--- | :--- |
| Isocenter's default configuration | 72.51% | 68.45% |
| **The configuration on this page** | **99.22%** | **99.23%** |

**Measured 2026-10-28 with Isocenter 1.0.1 from PyPI (tag `v1.0.1`, commit `895dbb3f`). The scorer was the MIDI-B validation script at commit `ccc0939`. The machine was an Apple M5 Max (18 cores, 64 GB, macOS 27.2).** The script's own automatic check scored the pixel checks. No manual pixel review was applied. That is the measurement.

The configuration was tuned against both sets. Neither score is therefore a held-out estimate. Each score is what this release and this configuration achieve on this data.

In both datasets:

- Study Date is shifted by each patient's offset, the other dates the profile names are emptied, replaced or removed, and every date check passes.
- Every UID is replaced, consistently across instances, and every UID check passes.
- Every Patient ID check passes.
- 99.15% (Validation) and 97.80% (Test) of the identifiers the key expects removed are removed. Almost all of the rest are 2- or 3-letter site acronyms (see [Why not 100%](#why-not-100)).

The configuration follows MIDI-B's answer key, which is one de-identification policy among several. Where the key leaves identifying text in place, or removes more than a study might want, the section says so. [Choices a site may want to change](#choices-a-site-may-want-to-change) collects these cases.

Example values on this page are synthetic. Some are MIDI-B's own synthetic values, already quoted in this project's public issues. The rest are invented values of the same shape.

## How MIDI-B scores

| Set | Patients | Series | Instances | Checks |
| :--- | :--- | :--- | :--- | :--- |
| Validation | 216 | 280 | 23,921 | 6,173,204 |
| Test | 322 | 428 | 29,660 | 8,035,948 |

Each check is one expectation about one element of one instance:

- An identifier must be gone.
- A date must be shifted.
- A UID must be replaced, and consistently.
- An element must be kept as written.
- The pixels must be unchanged, or burned-in text hidden.

The score is checks passed divided by all checks. A check fails either way: an identifier left in, or a useful element removed. Most of what the default configuration loses is the second kind. The DICOM Basic Profile removes, as a precaution, much that the key wants kept.

Free text is checked twice. Take a description, in a file whose Patient's Name is `WHITLOCK^DANA` (an illustrative example):

| | Value |
| :--- | :--- |
| Source | `PET-CT STUDY for Dana Whitlock` |
| Check 1: identifier gone | `Dana Whitlock` must not appear |
| Check 2: clinical text kept | `PET-CT STUDY` must appear, as written |
| Basic Profile (empty or remove the element) | passes 1, fails 2 |
| This configuration (scrub the element) | `PET-CT STUDY`: passes both |

## What each part of the configuration is worth

Each part below overrides the default for one group of elements. The table shows the checks each part wins, measured against a run of the default configuration:

| Part | Validation | Test |
| :--- | :--- | :--- |
| Start: Isocenter's default configuration | 72.51% | 68.45% |
| [1. Keep private elements, remove the identifying ones](#1-keep-private-elements-remove-the-identifying-ones) | +21.53 | +25.36 |
| [2. Keep times of day](#2-keep-times-of-day) | +2.17 | +2.09 |
| [6. Keep free text, scrub the identifiers out of it](#6-keep-free-text-scrub-the-identifiers-out-of-it) | +0.95 | +0.98 |
| [7. Keep structured report content, scrub its text](#7-keep-structured-report-content-scrub-its-text) | +0.67 | +0.80 |
| [3. Keep patient characteristics](#3-keep-patient-characteristics) | +0.61 | +0.55 |
| [4. Keep coded and technical elements](#4-keep-coded-and-technical-elements) | +0.47 | +0.71 |
| [5. Empty Series Number](#5-empty-series-number) | +0.30 | +0.28 |
| [8. Replace registry UIDs](#8-replace-registry-uids) | +0.00 | +0.00 |
| [9. Remove burned-in text](#9-remove-burned-in-text) | −0.00 | −0.00 |
| **This configuration** | **99.22%** | **99.23%** |

Points are percentage points of all checks. Each part is credited with the checks on the elements it owns, so the parts add up (to rounding). Parts 8 and 9 are worth almost nothing to the score. Part 8 is a handful of checks. Part 9 hides nearly all burned-in text but costs about as much as it wins (see part 9). Both runs export uncompressed ([part 10](#10-export-uncompressed)).

The default configuration is what Isocenter's scaffold writes. It is the DICOM PS3.15 Annex E Basic Profile (`basic@2026c`, edition 2026c), with Study Date shifted per patient, and with sex and age kept. It also removes every private element (`remove_private_tags: true`).

## The configuration, part by part

### 1. Keep private elements, remove the identifying ones

```yaml
remove_private_tags: false
phi_tags:
  '0009,"GEMS_PETD_01",05': {action: REMOVE, name: GEMS_PETD_01 PET patient_datetime}
  '0009,"GEMS_IDEN_01",27': {action: REMOVE, name: GEMS_IDEN_01 Image actual date}
  '0029,"SIEMENS MEDCOM HEADER",31': {action: REMOVE, name: SIEMENS MEDCOM HEADER PMTF Information 1}
  # ... 64 rules in all
```

| Private element | Source | Output |
| :--- | :--- | :--- |
| GE PET landmark name | `Umbilicus` | `Umbilicus` (kept) |
| GE PET exam UID | `2.5.731.4.2.5508176…` | a new UID, the same in every instance |
| GE PET patient datetime | a date and time | removed |

Vendor private elements carry acquisition and reconstruction parameters, such as GE PET landmarks, filters and iterations, or Siemens and Philips sequence details. MIDI-B expects them kept, and they are the largest part of the score. The 64 rules remove the private elements that hold identifiers, dates or UIDs, and keep everything else. That covers GE PET exam, scan and calibration IDs and datetimes, and GE suite and equipment IDs. It also covers prescribing-series UIDs, a UCSF free-text note, and four Siemens header fields.

PS3.15's Retain Safe Private Option keeps only private attributes listed as safe. This configuration takes the inverse route. It keeps every private element and removes the ones known to hold identifiers, dates or UIDs.

Each rule names its private creator, not a concrete tag ([Private elements by creator](configuration.md#private-elements-by-creator)). A vendor's element lands in whichever block a file assigns its creator: `0029,10xx` in some files, `0029,11xx` in others. Two vendors can also use the same concrete tag for different things. A creator key matches that creator's element wherever it is, and nothing else. It matches the creator in any case and spacing. This data writes one creator both as `UCSF BIRP PRIVATE CREATOR 011710XX` and as `UCSF BIRP PRIVATE CREATOR 011710xx`.

### 2. Keep times of day

```yaml
  0008,0030: {action: KEEP, name: Study Time}
  0008,0032: {action: KEEP, name: Acquisition Time}
  0040,a122: {action: KEEP, name: Time}
  # ... 13 time elements
```

| Element | Source | Basic Profile | This configuration |
| :--- | :--- | :--- | :--- |
| Acquisition Time | `094517` | emptied | `094517` |
| Study Date | `20180913` | emptied (the default configuration shifts it) | shifted by the patient's offset |

The Basic Profile empties, replaces or removes every time. But the dates are shifted or removed, so a time of day no longer places a study in time. Acquisition and injection times are needed for timing, decay and dose analysis. MIDI-B keeps them.

### 3. Keep patient characteristics

```yaml
  0010,1020: {action: KEEP, name: Patient's Size}
  0010,1030: {action: KEEP, name: Patient's Weight}
  0010,2160: {action: KEEP, name: Ethnic Group}
  0010,21a0: {action: KEEP, name: Smoking Status}
  0010,21c0: {action: KEEP, name: Pregnancy Status}
```

These describe the patient without identifying them, and size and weight are needed for dose and SUV. They are among the elements PS3.15's Retain Patient Characteristics Option keeps.

### 4. Keep coded and technical elements

```yaml
  0040,0555: {action: KEEP, name: Acquisition Context Sequence}
  0040,0275: {action: KEEP, name: Request Attributes Sequence}
  0008,2111: {action: KEEP, name: Derivation Description}
  0018,1010: {action: KEEP, name: Secondary Capture Device ID}
  0008,1110: {action: KEEP, name: Referenced Study Sequence}
  # ... and the trial sponsor, other references, processing description
```

These carry acquisition codes, how an image was processed or derived, and references between objects. Keeping a sequence does not switch off the profile inside it:

| Inside a kept sequence | Output |
| :--- | :--- |
| Referenced SOP Instance UID | replaced, consistently with the referenced instance |
| Requested Procedure ID, Scheduled Procedure Step ID | removed |
| Scheduled Procedure Step Description | scrubbed (part 6) |

### 5. Empty Series Number

```yaml
  0020,0011: {action: EMPTY, name: Series Number}
```

| Source Series Number | Key expects | Output |
| :--- | :--- | :--- |
| `20160811` (a date) | removed | empty: passes |
| `412` (a number) | kept | empty: fails |

In most series of this data, Series Number holds an acquisition date, a quasi-identifier the key expects removed. Emptying keeps the element, which the General Series Module requires (Type 2). It costs the series whose number is an ordinary number (2,470 Validation checks). `SCRUB` cannot pick out the dates, because it applies only to text elements and Series Number is an Integer String.

### 6. Keep free text, scrub the identifiers out of it

```yaml
  0008,103e:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Series Description
  # ... 15 free-text elements: descriptions, protocol name, reasons,
  # comments, occupation, contrast agent
```

The Basic Profile empties, replaces or removes descriptions, protocol names and comments whole. MIDI-B expects the clinical text kept and the injected identifier removed. `SCRUB` does that: it is PS3.15's Clean Descriptors Option as a rule ([Cleaning descriptions](configuration.md#cleaning-descriptions)). Each kind named in `target` finds one sort of identifier. Each span found is removed together with a connecting word directly before it (`for`, `at`, `by`, `per`, `from`).

| Kind | Finds | Source | Output |
| :--- | :--- | :--- | :--- |
| `names` | the words of the instance's own Person Names | `Standard Breast Imaging Report for Amanda Hoover`, in a file whose Patient's Name holds those two names | `Standard Breast Imaging Report` |
| `dates` | dates | `MA SURGICAL SPECIMEN 20170707` | `MA SURGICAL SPECIMEN` |
| `ids` | a run of 6 or more characters holding 5 or more digits | `MR BREAS UNIT for 736-69-8140` | `MR BREAS UNIT` |
| `phones` | telephone numbers, with extensions | `PRONE : Dana Whitlock : 312.555.0186 ext 204`, Patient's Name `WHITLOCK^DANA` (illustrative) | `PRONE` |
| `addresses` | a street address with city, state and ZIP | `AP Routine : Dana Whitlock : 42 Larkspur Ct Fairhaven, OR 97000`, Patient's Name `WHITLOCK^DANA` (illustrative) | `AP Routine` |
| `sites` | a 4 to 8 capital acronym after a lower-case `at` | `PET-CT STUDY at CHAJH` | `PET-CT STUDY` |
| `initials` | a lower-case `by` and 1 to 3 capitals, directly after a removed span | `Ordered for 2675888487 by SS` | `Ordered` |

Clinical text is left as written, spacing included. Protocol tokens and decimals are not mistaken for IDs: `ep2d_diff_b50_400_800_tra_p3 for 310-50-9582` becomes `ep2d_diff_b50_400_800_tra_p3`. A name cut off at the element's maximum length (`… for Christopher Powe`, for a patient `POWERS^CHRISTOPHER`) is still recognised.

`orgs`, an eighth kind, is not used. It is spaCy's entity recogniser (the `nlp` extra). On this data it found no site acronym, and it erased clinical text: `MR BRAIN W/WO CONTRAST` became `MR`.

**Additional Patient History is left to the profile, which removes it.** In this data it holds facility names and addresses more often than history. A scrub cannot find facility names, so removing the element scores better on the Test set.

### 7. Keep structured report content, scrub its text

```yaml
  0040,a730: {action: KEEP, name: Content Sequence}
  0040,a160:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Text Value
```

A structured report (SR) is a tree of content items: measurements, codes, references and text. The Basic Profile removes the whole Content Sequence, because its free text could not be cleaned, and leaves a report with no content. With `SCRUB` on Text Value the tree can be kept ([Keeping a structured report's content](configuration.md#keeping-a-structured-reports-content)). This is not PS3.15's Clean Structured Content Option, which cleans content items by their concept. The profile's rules still apply inside the tree: names, dates and UIDs in content items are removed, emptied or replaced, and every such check passes.

### 8. Replace registry UIDs

```yaml
  0008,0118: {action: REPLACE, name: Mapping Resource UID}
  0008,010c: {action: REPLACE, name: Coding Scheme UID}
```

The Basic Profile has no row for either. MIDI-B's synthetic data gives them random values and expects them changed like any UID. A `REPLACE` with no value on a UID replaces it as every other UID is replaced. In real data they name a template or coding registry, not a patient, and replacing them loses that meaning.

### 9. Remove burned-in text

```yaml
burned_in_text:
  action: redact
  scope: modalities
  modalities: [CR, DX, MG]
  detector: db
  target: any
```

Some radiographs in MIDI-B have text burned into the pixels. It is either a patient block (name, sex, study date, date of birth) or technologist initials on a lead side marker. CR, DX and MG are the only modalities that carry it. No file declares Burned In Annotation `YES`, so the scope is by modality (`scope: modalities` also reads any image flagged, and none is).

The `db` detector, a text-detection model run by OpenCV, finds text regions without reading them, so under `target: any` no OCR is needed. `target: any` redacts every region found, as MIDI-B expects of all burned-in text. Fixed redaction zones do not fit this data, because the text moves from image to image. [Burned-in Text](configuration.md#burned-in-text) describes every key.

| | Validation | Test |
| :--- | :--- | :--- |
| Burned-in text boxes hidden, without this part | 5 of 35 | 3 of 27 |
| Burned-in text boxes hidden, with it | 34 of 35 | 27 of 27 |

The score barely moves, for two reasons. Other text on the same images (a portable marker, side markers) is removed too, which the key counts against. And the key marks a redacted image's Image Type `DERIVED` as wrong (see [Why not 100%](#why-not-100)).

### 10. Export uncompressed

```python
session.export("export/", use_compression=False)
```

The default export re-encodes pixel data as lossless JPEG 2000. MIDI-B compares pixel bytes, so a re-encoded image fails its pixels-unchanged check even though no pixel value changed. That costs about 0.4 points. An uncompressed export of a native little-endian source carries its Pixel Data bytes unchanged when its samples need no decoding. An encapsulated or big-endian source is decoded ([Compression](export-output.md#compression)).

## Why not 100%

| Remaining Validation checks | Failed | Points | Why |
| :--- | :--- | :--- | :--- |
| GE PET private elements empty in the source, a number in the key | 39,338 | 0.64 | answer key |
| Short site acronyms left in free text (`at FGH`) | 2,925 | 0.05 | not recognised |
| Series Number emptied where it was an ordinary number | 2,470 | 0.04 | trade-off |
| Elements the key expects that the source never held | 1,594 | 0.03 | answer key |
| Additional Patient History removed | 1,065 | 0.02 | trade-off |
| Text differing from the key, other | 759 | 0.01 | mixed |
| Burned-in redaction side effects, one box not found | 66 | <0.01 | answer key, detection |
| **Total** | **48,217** | **0.78** | |

**About 85% of what is left cannot be won by any tool, because the answer key disagrees with the source files:**

- **GE PET private elements** (reconstruction iterations, filter parameters) are empty in the source, and the key expects a number there (`0` to `4`). That is 0.64 points on Validation and 0.55 on Test.
- **Elements the source never held** are expected present. These are Position Reference Indicator, Acquisition Number, and Presentation Intent Type on some DX images. On Test, the key also expects Philips private creators the source does not have.
- **Values written differently from the source.** The key expects Smallest Image Pixel Value as a signed number where the source writes the same 16 bits unsigned. It expects a structured-report Numeric Value at full precision where the source writes it rounded to seven significant digits. Isocenter exports both as the source has them.
- **Image Type after redaction.** The key expects burned-in text hidden and Image Type still `ORIGINAL` on the same images. DICOM marks an image whose pixels were changed `DERIVED`, and Isocenter writes `DERIVED`.

**What a configuration could still win:**

- **Short site acronyms.** The acronyms `sites` leaves are 2 or 3 letters (`at FGH`, `at MG`, `at JCH`): 2,883 Validation and 9,440 Test checks. `sites` starts at 4 so that it never takes a modality or a common word. `MG` is mammography as well as a site here. A configurable floor is proposed in [#1162](https://github.com/kvnlng/Isocenter/issues/1162).
- **A sequence label.** `P4^P114^S301^I00004, Gated, 10.0%A` loses its last token, `I00004`, to `ids`.
- **Series Number.** Kept, it would pass 2,470 checks and fail about 21,000.

## Choices a site may want to change

MIDI-B's key is one policy, and this configuration follows it. Under a different protocol:

- **Free text.** A scrub removes what it recognises, and leaves short site acronyms, facility names and bare initials. To leave none of it, `REMOVE` the comment elements (Image Comments, Patient Comments, Requested Procedure Comments). Then `REMOVE` the descriptions and Protocol Name, or keep them only under a protocol that retains institution identity.
- **Structured report text.** Use `REPLACE` instead of `SCRUB` on Text Value `(0040,A160)` to remove all report text. That includes bare reader initials, such as a Text Value that is only `DL`. It costs a few hundred checks.
- **Registry UIDs.** `KEEP` Mapping Resource UID and Coding Scheme UID to keep their meaning.
- **Burned-in text.** `target: [names, dates, ids]` redacts only regions whose reading looks like a name, a date or an ID. That leaves side markers and staff initials. On this data it also left some patient blocks that the detector found but OCR read poorly. A narrowed target reads each region with tesseract, so it needs the `ocr` extra and the `tesseract` binary. `names` needs the `nlp` extra and spaCy's `en_core_web_sm` model, with no fallback.

## Reproducing these results

- **Software:** Isocenter 1.0.1 from PyPI, with the `textdetect` extra (`pip install "isocenter[textdetect]==1.0.1"`). Use a Python build with the GIL (3.12 to 3.14), because OpenCV publishes no free-threaded wheel. This configuration needs no other extra. For a narrowed burned-in `target` or for `detector: tesseract`, add `ocr`. If a `SCRUB` target names `orgs` or a burned-in target names `names`, add `nlp`. Scoring uses the MIDI-B validation script at commit `ccc0939`.
- **Run:**

```python
import isocenter

with isocenter.Session("midi-b.db") as session:
    session.load_config("isocenter.yaml")
    session.ingest("MIDI-B-Synthetic-Validation/")
    session.anonymize()
    session.redact()
    session.export("export/", use_compression=False)
    session.generate_manifest("manifest.json", format="json")
```

- **Scoring:** Score `export/` with the validation script. The script also needs mapping files that link each source UID and Patient ID to its replacement. The Study, Series and SOP Instance UIDs and the Patient ID come from the manifest above. Every other UID is found by comparing source and exported headers. The manifest maps source files to exported UIDs, so keep it with the store, never with an export.
- **Time:** De-identifying the Validation set (23,921 instances) took about 12 minutes on the machine above, and scoring it about 5 more.

## Appendix: the configuration

The complete policy section. The scaffold also writes a `machines:` list of the devices found in the dataset, with no zones. This page omits that list.

```yaml
version: '2.0'
privacy_profile: basic@2026c
phi_tags:
  # Research defaults (what Isocenter's scaffold writes): Study Date is
  # shifted by the per-patient offset, and sex and age are kept because
  # analyses stratify on them.
  0008,0020:
    action: JITTER
    name: Study Date
  0010,0040:
    action: KEEP
    name: Patient's Sex
  0010,1010:
    action: KEEP
    name: Patient's Age
  # Times of day are kept. Study Date is shifted and the profile's other dates
  # are emptied, replaced or removed, so a time no longer places a study in
  # time, and acquisition and injection times are needed for timing
  # analysis. Time (0040,A122) is the time of a structured-report content
  # item.
  0008,0013:
    action: KEEP
    name: Instance Creation Time
  0008,0030:
    action: KEEP
    name: Study Time
  0008,0031:
    action: KEEP
    name: Series Time
  0008,0032:
    action: KEEP
    name: Acquisition Time
  0008,0033:
    action: KEEP
    name: Content Time
  0018,1014:
    action: KEEP
    name: Time of Secondary Capture
  0018,1072:
    action: KEEP
    name: Radiopharmaceutical Start Time
  0018,1073:
    action: KEEP
    name: Radiopharmaceutical Stop Time
  0018,1201:
    action: KEEP
    name: Time of Last Calibration
  0018,700e:
    action: KEEP
    name: Time of Last Detector Calibration
  0040,0245:
    action: KEEP
    name: Performed Procedure Step Start Time
  0040,0251:
    action: KEEP
    name: Performed Procedure Step End Time
  0040,a122:
    action: KEEP
    name: Time
  # Patient characteristics are kept: body size and weight (needed for dose and
  # SUV), ethnic group, pregnancy and smoking status. They describe the patient
  # without identifying them (among the elements PS3.15's Retain Patient
  # Characteristics Option keeps).
  0010,1020:
    action: KEEP
    name: Patient's Size
  0010,1030:
    action: KEEP
    name: Patient's Weight
  0010,2160:
    action: KEEP
    name: Ethnic Group
  0010,21c0:
    action: KEEP
    name: Pregnancy Status
  0010,21a0:
    action: KEEP
    name: Smoking Status
  # Coded and technical elements are kept: acquisition context codes, how an
  # image was derived and processed, the device that made a secondary capture,
  # the trial sponsor, the request attributes, and the study, procedure-step
  # and patient references.
  # The profile's rows still apply inside them: their UIDs are replaced,
  # consistently with the rest of the data, and the procedure IDs in the
  # request attributes are removed.
  0008,1110:
    action: KEEP
    name: Referenced Study Sequence
  0008,1111:
    action: KEEP
    name: Referenced Performed Procedure Step Sequence
  0012,0010:
    action: KEEP
    name: Clinical Trial Sponsor Name
  0040,0555:
    action: KEEP
    name: Acquisition Context Sequence
  0008,2111:
    action: KEEP
    name: Derivation Description
  0040,0275:
    action: KEEP
    name: Request Attributes Sequence
  0008,1120:
    action: KEEP
    name: Referenced Patient Sequence
  0018,1010:
    action: KEEP
    name: Secondary Capture Device ID
  0018,1400:
    action: KEEP
    name: Acquisition Device Processing Description
  0008,0001:
    action: KEEP
    name: Length to End
  # Series Number holds an acquisition date (2010 to 2019) in most series of
  # this data, so it is emptied; the element stays present, as the General
  # Series Module requires (Type 2). This costs the series whose number is
  # an ordinary number.
  # SCRUB cannot help: it applies to text elements, and this one is an
  # integer string.
  0020,0011:
    action: EMPTY
    name: Series Number
  # Free text is kept and the identifiers inside it are removed (SCRUB, PS3.15
  # Clean Descriptors Option). MIDI-B expects the clinical text kept and the
  # injected identifier removed. Each kind removes its span together with a
  # connecting word directly before it ("for", "at", "by", "per", "from"):
  #   names      the words of the instance's own Person Names
  #   dates      date shapes
  #   ids        a run of 6 or more characters holding 5 or more
  #              digits (SSNs, MRNs, accession numbers); decimals and
  #              protocol tokens (t2_tse_tra_320_p2) are spared unless they
  #              hold an SSN shape or 7 or more digits in a row
  #   phones     telephone numbers, with extensions
  #   addresses  street addresses with city, state and ZIP
  #   sites      a 4-8 capital acronym after a lower-case "at" ("at CHAJH")
  #   initials   "by SS" directly after another removed span
  # What no kind finds stays: 2-3 letter site acronyms ("at FGH"), facility
  # names ("Admitted to ... Memorial") and bare initials. Additional Patient
  # History is left to the profile, which removes it: in this data it holds
  # facility names and addresses more often than history, and removing it
  # scores better on the Test set. A site whose protocol does not allow what
  # a scrub leaves should REMOVE the comment elements.
  0008,1030:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Study Description
  0008,103e:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Series Description
  0018,1030:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Protocol Name
  0032,1060:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Requested Procedure Description
  0040,0007:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Scheduled Procedure Step Description
  0040,0254:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Performed Procedure Step Description
  0040,1400:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Requested Procedure Comments
  0040,1002:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Reason for the Requested Procedure
  0032,1030:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Reason for Study
  0008,1080:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Admitting Diagnoses Description
  0010,4000:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Patient Comments
  0010,2180:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Occupation
  0020,4000:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Image Comments
  0040,0310:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Comments on Radiation Dose
  0018,0010:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Contrast/Bolus Agent
  # Structured reports keep their content tree (measurements, codes,
  # references) and their free text is scrubbed. This is not PS3.15's Clean
  # Structured Content Option, which cleans content items by their concept.
  # The Basic Profile removes the whole Content Sequence; the rows for names,
  # dates and UIDs nested in content items still apply. A
  # scrub leaves reader initials in a few Text Values; REPLACE removes all
  # report text instead, at the cost of algorithm names and versions.
  0040,a730:
    action: KEEP
    name: Content Sequence
  0040,a160:
    action: SCRUB
    target: [names, dates, ids, phones, addresses, sites, initials]
    name: Text Value
  # Registry UIDs in structured-report templates and code sequences are
  # replaced like any other UID. MIDI-B's synthetic data gives them random
  # values and expects them changed; in real data they name a template or
  # coding registry, and a site may prefer to KEEP them.
  0008,0118:
    action: REPLACE
    name: Mapping Resource UID
  0008,010c:
    action: REPLACE
    name: Coding Scheme UID
  # Private elements are kept (remove_private_tags: false, below), because
  # vendor private data carries acquisition and reconstruction parameters
  # research needs, and the ones that hold identifiers, dates or UIDs are
  # removed. Each rule names its private creator, so it matches that creator's
  # element in whichever block a file assigns it, and never another vendor's
  # element at the same tag. A creator key matches the creator in any case
  # and spacing (the UCSF creator is written 011710XX and 011710xx here).
  '0009,"GEMS_IDEN_01",02':
    action: REMOVE
    name: GEMS_IDEN_01 Suite id
  '0009,"GEMS_PETD_01",05':
    action: REMOVE
    name: GEMS_PETD_01 PET patient_datetime
  '0009,"GEMS_PETD_01",07':
    action: REMOVE
    name: GEMS_PETD_01 PET exam_id
  '0009,"GEMS_PETD_01",0a':
    action: REMOVE
    name: GEMS_PETD_01 PET scan_id
  '0009,"GEMS_PETD_01",0d':
    action: REMOVE
    name: GEMS_PETD_01 PET scan_datetime
  '0009,"GEMS_PETD_01",0e':
    action: REMOVE
    name: GEMS_PETD_01 PET scan_ready
  '0009,"GEMS_PETD_01",13':
    action: REMOVE
    name: GEMS_PETD_01 PET for_identifier
  '0009,"GEMS_IDEN_01",30':
    action: REMOVE
    name: GEMS_IDEN_01 Service id
  '0009,"GEMS_PETD_01",37':
    action: REMOVE
    name: GEMS_PETD_01 PET batch_description
  '0009,"GEMS_PETD_01",39':
    action: REMOVE
    name: GEMS_PETD_01 PET meas_datetime
  '0009,"GEMS_PETD_01",3b':
    action: REMOVE
    name: GEMS_PETD_01 PET admin_datetime
  '0009,"GEMS_PETD_01",3d':
    action: REMOVE
    name: GEMS_PETD_01 PET post_inj_datetime
  '0009,"GEMS_PETD_01",56':
    action: REMOVE
    name: GEMS_PETD_01 PET normal_cal_id
  '0009,"GEMS_PETD_01",57':
    action: REMOVE
    name: GEMS_PETD_01 PET normal_2d_cal_id
  '0009,"GEMS_PETD_01",59':
    action: REMOVE
    name: GEMS_PETD_01 PET wc_cal_id
  '0009,"GEMS_PETD_01",5c':
    action: REMOVE
    name: GEMS_PETD_01 PET frame_id
  '0009,"GEMS_PETD_01",5d':
    action: REMOVE
    name: GEMS_PETD_01 PET scan_id
  '0009,"GEMS_PETD_01",5e':
    action: REMOVE
    name: GEMS_PETD_01 PET exam_id
  '0009,"GEMS_PETD_01",68':
    action: REMOVE
    name: GEMS_PETD_01 PET landmark_datetime
  '0009,"GEMS_PETD_01",6c':
    action: REMOVE
    name: GEMS_PETD_01 PET acq_start
  '0009,"GEMS_PETD_01",7b':
    action: REMOVE
    name: GEMS_PETD_01 PET is_datetime
  '0009,"GEMS_PETD_01",97':
    action: REMOVE
    name: GEMS_PETD_01 PET trans_scan_id
  '0009,"GEMS_PETD_01",98':
    action: REMOVE
    name: GEMS_PETD_01 PET norm_cal_id
  '0009,"GEMS_PETD_01",ad':
    action: REMOVE
    name: GEMS_PETD_01 PET trans_frame_id
  '0009,"GEMS_IDEN_01",e3':
    action: REMOVE
    name: GEMS_IDEN_01 Equipment UID
  '0009,"GEMS_IDEN_01",e9':
    action: REMOVE
    name: GEMS_IDEN_01 Actual series data time stamp
  '0017,"GEMS_PETD_01",04':
    action: REMOVE
    name: GEMS_PETD_01 PET cal_datetime
  '0019,"GEMS_PETD_01",04':
    action: REMOVE
    name: GEMS_PETD_01 PET cal_datetime
  '0019,"GEMS_ACQU_01",9d':
    action: REMOVE
    name: GEMS_ACQU_01 Pulse Sequence Date
  '0021,"GEMS_RELA_01",03':
    action: REMOVE
    name: GEMS_RELA_01 Series from which Prescribed
  '0021,"GEMS_RELA_01",35':
    action: REMOVE
    name: GEMS_RELA_01 Series from which prescribed
  '0029,"SIEMENS MEDCOM HEADER",31':
    action: REMOVE
    name: SIEMENS MEDCOM HEADER PMTF Information 1
  '0029,"SIEMENS MEDCOM HEADER",34':
    action: REMOVE
    name: SIEMENS MEDCOM HEADER PMTF Information 4
  '0043,"GEMS_PARM_01",05':
    action: REMOVE
    name: GEMS_PARM_01 Img is original or unoriginal
  '0043,"GEMS_PARM_01",29':
    action: REMOVE
    name: GEMS_PARM_01 Histogram tables
  '0043,"GEMS_PARM_01",60':
    action: REMOVE
    name: GEMS_PARM_01 Slop_int_10...slop_int_17
  '0043,"GEMS_PARM_01",80':
    action: REMOVE
    name: GEMS_PARM_01 Coil ID Data
  '0043,"GEMS_PARM_01",88':
    action: REMOVE
    name: GEMS_PARM_01 PURE Acquisition Calibration Series UID
  '0043,"GEMS_PARM_01",98':
    action: REMOVE
    name: GEMS_PARM_01 ASSET Acquisition Calibration Series UID
  '0045,"GEMS_SENO_02",50':
    action: REMOVE
    name: GEMS_SENO_02 SC Instance UID
  '0045,"GEMS_SENO_02",51':
    action: REMOVE
    name: GEMS_SENO_02 SC series UID
  '0117,"UCSF BIRP PRIVATE CREATOR 011710XX",c4':
    action: REMOVE
    name: UCSF BIRP Private tag data
  '0009,"GEMS_PETD_01",06':
    action: REMOVE
    name: GEMS_PETD_01 PET type
  '0009,"GEMS_IDEN_01",27':
    action: REMOVE
    name: GEMS_IDEN_01 Image actual date
  '0009,"GEMS_IDEN_01",31':
    action: REMOVE
    name: GEMS_IDEN_01 Mobile location number
  '0009,"GEMS_PETD_01",62':
    action: REMOVE
    name: GEMS_PETD_01 PET where_is_frame
  '0009,"GEMS_PETD_01",63':
    action: REMOVE
    name: GEMS_PETD_01 PET frame_size
  '0009,"GEMS_PETD_01",64':
    action: REMOVE
    name: GEMS_PETD_01 PET file_exists
  '0009,"GEMS_PETD_01",71':
    action: REMOVE
    name: GEMS_PETD_01 PET total_prompts
  '0009,"GEMS_PETD_01",72':
    action: REMOVE
    name: GEMS_PETD_01 PET total_delays
  '0009,"GEMS_PETD_01",96':
    action: REMOVE
    name: GEMS_PETD_01 PET wellcountercal_id
  '0009,"GEMS_PETD_01",99':
    action: REMOVE
    name: GEMS_PETD_01 PET blnk_cal_id
  '0009,"GEMS_PETD_01",9c':
    action: REMOVE
    name: GEMS_PETD_01 PET emiss_sub_id
  '0009,"GEMS_PETD_01",ae':
    action: REMOVE
    name: GEMS_PETD_01 PET tpluse_frame_id
  '0009,"GEMS_PETD_01",e4':
    action: REMOVE
    name: GEMS_PETD_01 Recon Protocol
  '0009,"GEMS_IDEN_01",e7':
    action: REMOVE
    name: GEMS_IDEN_01 Exam Record checksum
  '0009,"GEMS_PETD_01",ea':
    action: REMOVE
    name: GEMS_PETD_01 3D Filter flag
  '0009,"GEMS_PETD_01",eb':
    action: REMOVE
    name: GEMS_PETD_01 3D Filter cutoff
  '0009,"GEMS_PETD_01",ec':
    action: REMOVE
    name: GEMS_PETD_01 3D Filter order
  '0019,"GEMS_ACQU_01",10':
    action: REMOVE
    name: GEMS_ACQU_01 Private tag data
  '0023,"GEMS_STDY_01",70':
    action: REMOVE
    name: GEMS_STDY_01 Start time(secs) in first axial
  '0027,"GEMS_IMAG_01",06':
    action: REMOVE
    name: GEMS_IMAG_01 Image archive flag
  '0029,"SIEMENS MEDCOM HEADER",32':
    action: REMOVE
    name: SIEMENS MEDCOM HEADER PMTF Information 2
  '0029,"SIEMENS MEDCOM HEADER",33':
    action: REMOVE
    name: SIEMENS MEDCOM HEADER PMTF Information 3
date_jitter:
  min_days: -365
  max_days: -1
remove_private_tags: false
# Burned-in text: CR, DX and MG are the only modalities that carry it here.
# The DB text detector finds it, and every region found is redacted, as
# MIDI-B expects of all burned-in text.
burned_in_text:
  action: redact
  scope: modalities
  modalities: [CR, DX, MG]
  detector: db
  target: any
```

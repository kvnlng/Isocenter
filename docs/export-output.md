# What the export writes

`session.export(folder)` writes de-identified copies of the session's instances to a new directory. Until `export()`, the session writes only the store, `isocenter.log` and files you ask for (a configuration, a key, a report, a manifest or a cohort table). This page describes the files: where they land, how they are encoded, what the export changes on the way out, and what the two checking options add.

## Where files land

```text
<folder>/Subject_<PatientID>/Study_<date>_<description>_<uid>/Series_<number>_<modality>_<description>_<uid>/<SOPInstanceUID>.dcm
```

The directory names are built from the values being exported, so **run `anonymize()` first**. The Series number and modality are the first instance's own Series Number and Modality after the configuration's rules: a removed number reads `0`, an empty one `NoNumber`, and a removed or empty modality `OT`. Otherwise the real Patient ID and descriptions appear in the paths. After `anonymize()` the Patient ID is the `ANON_…` pseudonym, so the folder reads `Subject_ANON_…`. A patient whose files carried no Patient ID is written under `Subject_UnknownPatient`.

The filename is the SOP Instance UID, because InstanceNumber is not unique within a series. A redacted instance takes a new SOP Instance UID, so its filename is not its source's.

Each file is written under a temporary name and renamed when complete, so a crash never leaves a partial file under a real name. A stray `*.tmp` left by a killed worker is safe to delete.

`export()` returns an `ExportSummary` of what it wrote and raises `ExportError` if it planned files and delivered none. A partial export returns what it wrote, with an `ERROR` audit row for each failure.

## Compression

The export writes two transfer syntaxes and no others: JPEG 2000 Lossless (`1.2.840.10008.1.2.4.90`) with `use_compression=True`, the default, and Implicit VR Little Endian with `use_compression=False`. A file with no pixel data is always Implicit VR Little Endian. A source in any other syntax is decoded at ingest and re-encoded into one of those two, and nothing records the source syntax. [Codec support](codecs.md) lists which decoder reads each source syntax.

Compression is lossless, but it changes how colour images are labelled:

- An `RGB` image is encoded with JPEG 2000's reversible colour transform and declared `YBR_RCT`, as the standard requires. A YBR source that decodes to RGB is already stored as `RGB`.
- 32- and 64-bit images cannot be compressed. Their export fails with a message naming `use_compression=False`.
- A 16-bit colour image is compressed too, and pydicom cannot read it with Pillow alone; with `pylibjpeg-openjpeg` installed, pydicom reads it exactly. The export names each such instance at INFO.

If a recipient's reader cannot handle JPEG 2000, export with `use_compression=False`.

A source that was compressed lossily is re-encoded losslessly from its decoded samples. Its `LossyImageCompression (0028,2110)` is carried only if the source declared it, so a near-lossless JPEG-LS source that did not declare it exports with no record that it was lossy.

## What the export changes on the way out

- **De-identification markers.** An instance whose patient, study and own status are de-identified under the policy in force gets up to three markers: Patient Identity Removed `(0012,0062)` `YES`, a De-identification Method `(0012,0063)` value naming Isocenter and the policy, and Longitudinal Temporal Information Modified `(0028,0303)` read from the file's own dates. They describe the attributes, not the pixels. [What an exported file says about itself](configuration.md#what-an-exported-file-says-about-itself) says when each is written.
- **Patient, study and series tags** are written onto each file from the patient, study and series that own them. Series Number `(0020,0011)`, Modality `(0008,0060)` and the equipment tags come from the instance, which is what `anonymize()` edits, so a configuration's rule on any of them reaches the file. `Series.series_number` and `Series.modality` are the source's values as ingested and are not written. A study with no Study Time is written with an empty one.
- **A file with no Modality**, because the configuration removed or emptied it or the source carried none, is written as it is, with one `WARNING` row saying it is not conformant: Modality is Type 1. The row grades the run `REVIEW_REQUIRED`.
- **A CT Image file missing a Type 1 element other than Modality is not written.** For CT Image Storage (`1.2.840.10008.5.1.4.1.1.2`) the export checks six Type 1 elements: SOP Class UID `(0008,0016)`, SOP Instance UID `(0008,0018)`, Series Instance UID `(0020,000E)`, Image Position (Patient) `(0020,0032)`, Image Orientation (Patient) `(0020,0037)` and Pixel Spacing `(0028,0030)`. An instance that reaches the write with one of them absent or empty, typically because a configuration rule removed or emptied it, is withheld: no file is written for it, and one `ERROR` row names it, for example `Export failed for instance 2.25.…: ValueError: Validation Errors: ['[Type 1 Error] Missing 0020,0032 in CTImage']`. The row grades the run `REVIEW_REQUIRED`, and an export that wrote nothing raises `ExportError`, as [above](#where-files-land). Series Instance UID never trips it: the series that owns it stamps it on every file. Because of that stamp, a `REMOVE` or `EMPTY` rule on Series Instance UID `(0020,000E)` or Study Instance UID `(0020,000D)` is refused when the configuration is loaded, with a `ValueError` naming `REPLACE` ([#877](https://github.com/kvnlng/Isocenter/issues/877)). Releases before that fix loaded the rule, and the export wrote the **source** UID: under `REMOVE` with a `PASS` grade and `(0012,0062) YES`. Keep the profile's `REPLACE` on both tags, which writes a [replacement UID](configuration.md#what-basic2026c-contains). This differs from Modality on purpose ([#874](https://github.com/kvnlng/Isocenter/issues/874)): Modality is metadata you may choose to hide, while the geometry is what makes the pixels usable, so a file without it is not written. No other SOP class is checked: on an MR image, say, a rule that removes Image Position (Patient) is applied and the file is written without it, with no row. [Configuration](configuration.md#a-rule-the-export-refuses) says which rules this is.
- **Photometric Interpretation** spelled in lower case or with a leading space (`' rgb '`) is written upper-cased and stripped (`RGB`), with an INFO line.
- **A private element whose value no longer fits the VR recorded for it at ingest** (after a `REPLACE`, typically) is written under a VR that holds it, with one `WARNING` row per instance naming the tags and both VRs.
- **Icon images.** Nothing scans or redacts a small preview image (an Icon Image Sequence item), so the export removes the ones that could show redacted pixels. An instance's own icon is removed from that instance's file when the instance was redacted or has redaction zones applied by this export. Every other nested icon (one under Referenced Image Sequence, for instance, which is a thumbnail of a different image) is removed from every file once any instance in the store was redacted, or a loaded rule's zones match a series in the store. A `"*"` rule matches every series with a Device Serial Number; a rule for a scanner the store does not hold removes nothing. Each removal writes a `DATA_LOSS` row and grades the run `REVIEW_REQUIRED`.
- **Recoverable identities.** When reversible anonymization locked identities into the Encrypted Attributes Sequence `(0400,0500)`, the export keeps them and logs a `WARNING` with the count: anyone who holds the key can read the identities from the exported files.

`DicomExporter.write_tree()` writes a graph as it stands. It applies the same owner tags and the same CT Type 1 check, raising `RuntimeError` for an instance that fails it, but none of the export's other checks, redaction zones or markers; `session.export()` is the way to write de-identified output.

## `check_burned_in=True`

`export(folder, check_burned_in=True)` runs `audit()` first and withholds every instance that still carries an identifier, on itself or on its patient, study or series, under the policy in force. On a session that has not run `anonymize()`, that is every instance carrying a value any rule acts on.

It checks tags, not pixels. Burned-in text is handled by redaction zones, and found by `scan_pixel_content()` ([Burned-in text (OCR)](ocr.md)).

Each withheld instance writes one `WARNING` audit row naming the instance and the level that carried the identifier, never the value, so the report grades `REVIEW_REQUIRED`. "Instances Written" counts withheld instances as requested ("1 of 2 requested"). An export that withheld everything returns an empty `ExportSummary` and does not raise. An instance outside `subset` is not withheld; it was never asked for.

## `verify_readback=True`

`export(folder, verify_readback=True)` decodes every file it writes through the same decoder `ingest()` uses, and compares every pixel sample with what it meant to write. It also refuses:

- a Photometric Interpretation the file's transfer syntax does not admit, for example `YBR_ICT` on an uncompressed file, or `YBR_PARTIAL_422` and `YBR_PARTIAL_420` under either syntax the export writes. The label is judged on a file with no pixel data too;
- a `YBR_FULL` image whose samples are 16-bit or signed 8-bit, which `ingest()` cannot read back, because pydicom's colour conversion takes unsigned 8-bit samples only.

An instance that fails is not delivered: it gets an `ERROR` audit row, appears in `ExportSummary.failures`, and grades the run `REVIEW_REQUIRED`.

Without verification, an inadmissible Photometric Interpretation is written as declared with a `WARNING` row, which also grades `REVIEW_REQUIRED`. So turning verification on can cost you a file the default export would have delivered. The 16-bit or signed 8-bit `YBR_FULL` file is conformant DICOM, so the default export writes it, with an INFO line saying Isocenter cannot read it back and no audit row.

## WFDB

`session.export(folder, format="wfdb")` writes waveform instances as PhysioNet WFDB records instead. [Waveforms & WFDB](waveforms.md) describes that output.

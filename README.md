# Isocenter

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22104298.svg)](https://doi.org/10.5281/zenodo.22104298)
[![PyPI](https://img.shields.io/pypi/v/isocenter.svg)](https://pypi.org/project/isocenter/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/kvnlng/Isocenter/blob/main/LICENSE)

**De-identify a DICOM cohort without touching the source files, and hand compliance a report that names anything the run could not do.**

Isocenter is a Python library for indexing, de-identifying, and exporting DICOM datasets at cohort scale. It builds a SQLite metadata index and a pixel/waveform sidecar beside a read-only source tree, applies your de-identification profile and pixel redaction rules to an in-memory object graph, grades its own output, and writes clean copies to a new directory as DICOM or PhysioNet WFDB.

You have a cohort of studies and a protocol an IRB approved. You need to hand a de-identified copy to a collaborator, a registry, or a model, and to hand your compliance reviewer a record of what was removed, what stayed because the protocol allowed it, and anything the run lost on the way. Isocenter is the library your script imports to do that. There is no command-line tool: the Python API is the whole interface.

**Documentation: <https://kvnlng.github.io/Isocenter/>**

## What it refuses to do

- **Modify a source file.** Ingest reads; anonymize and redact change an in-memory graph. Until `export()`, the session writes only the store, `isocenter.log` and files you name (a configuration, a key). `export()` writes copies to a directory you name, so a crashed or abandoned run leaves the originals as they were. The store (`<name>.db` and `<name>_pixels.bin`) holds the original identifiers and pixels, so keep it where PHI may live.
- **Grade a lossy export `PASS`.** Every step that can lose data writes an audit row, and the compliance report reads those rows. A cohort that lost a file, a private tag, a waveform group, or a pixel frame grades `REVIEW_REQUIRED` and names the loss. An export that attempted instances and wrote none of them raises `ExportError`; a partial export returns what it wrote, with an `ERROR` row for each failure.
- **Pass through pixels it could not decode.** If a compressed frame cannot be decompressed, because of a missing codec or a stream the decoder rejects, the export fails on that instance rather than copying bytes it never inspected.
- **Advertise a Python version it does not test.** The suite runs on all four supported versions, including the free-threaded 3.14t build, at every release, and 3.12 and 3.14t must pass before anything is uploaded. The PyPI classifiers list only those.

## What it does

- **De-identification against the DICOM PS3.15 Annex E Basic Profile table**, edition 2026c. A profile decides which tags go, are emptied, are replaced, or are date-shifted; a field your protocol permits stays. Nested sequences are scanned, not only the top level. Date jitter is deterministic per patient within a project, so intervals survive. UIDs are replaced consistently, so references between exported files still resolve (a UID in a private tag is not replaced yet: [#765](https://github.com/kvnlng/Isocenter/issues/765)).
- **Machine-specific pixel redaction.** Redaction zones are keyed by device, because the same model burns identifiers into the same place every time. An optional OCR pass finds where the text lands, and CTP `DicomPixelAnonymizer.script` rules import directly.
- **Reversible anonymization, if you choose it.** Original identities can be encrypted under a key into the Encrypted Attributes Sequence `(0400,0500)` before anonymization, and recovered later by whoever holds the key.
- **A persistent session.** A `Patient → Study → Series → Instance` graph over a SQLite index and a pixel sidecar, so a 10,000-instance cohort reopens without rescanning and a job can be paused and resumed. Every action is written to an audit log.
- **A compliance report** with a `PASS` or `REVIEW_REQUIRED` grade, every exception listed, and a signature block for the reviewer.
- **Waveforms.** DICOM waveform IODs (ECG, hemodynamic) export as PhysioNet WFDB records, with an annotation bridge to [Murmur Studio](https://github.com/kvnlng/Murmur).

## Install

Isocenter needs Python 3.12 or later on Linux or macOS. The storage layer's locks use `fcntl`, so it does not import on Windows; there, use WSL or a Linux container.

```bash
pip install isocenter
```

Release candidates: `pip install --pre isocenter`.

```bash
python -c "import isocenter; print(isocenter.__version__)"
```

The OCR and NLP extras, and what each needs, are on the [installation page](https://kvnlng.github.io/Isocenter/installation/).

## Example

Put some DICOM files in `input/` and run:

```python
from isocenter import Session

# Worker processes are started by spawn, so a script needs this guard.
if __name__ == "__main__":
    with Session("my_project.db") as session:   # the session store
        session.ingest("input")                  # reads; never writes the source
        report = session.audit()                 # findings under the default policy
        session.anonymize(report)                # in memory
        summary = session.export("export_clean")
        print(len(summary.written_uids), "files written")
        session.generate_report("compliance_report.md")
```

With no configuration loaded, the default policy is the Basic Profile table (646 tag rules), with Study Date jittered, Patient's Sex and Age kept, and private tags removed. `export_clean/` holds one `Subject_ANON_…` folder per patient written. An image the default lossless JPEG 2000 cannot hold (32-bit samples, for example) is not written: its `ERROR` row says so and names `use_compression=False`. The Executive Summary of `compliance_report.md` opens with the grade. The [Quick Start](https://kvnlng.github.io/Isocenter/quickstart/) adds a configuration, redaction, reversible anonymization and a verify step, and the [tutorials](https://kvnlng.github.io/Isocenter/tutorials/deidentify-and-read-the-grade/) run each step on files bundled with pydicom.

## Burned-in text

Machines burn identifiers into the pixels, and the same model tends to burn them in the same place every time. Isocenter finds that text by OCR and blanks it with redaction zones you write per device. The [OCR guide](https://kvnlng.github.io/Isocenter/ocr/) covers both.

### 5b. Zone Discovery

`discover_redaction_zones()` OCRs a random sample of one machine's instances and returns a `DiscoveryResult` of where text was found, so you can write zones from what the data does rather than from one screenshot. The scan needs the `ocr` extra (`pip install "isocenter[ocr]"`) and the `tesseract` binary; without either it raises `OcrUnavailableError` naming what is missing.

```python
result = session.discover_redaction_zones("SN-12345", sample_size=50, min_confidence=80.0)

# Suggested zones, in the [y1, y2, x1, x2] form the redaction config takes.
for zone in result.to_zones(min_occurrence=0.25):
    print(zone["type"], zone["zone"], zone["examples"])
```

`to_zones()` clusters the candidates, and keeps only clusters seen in at least `min_occurrence` of the sampled instances: a name in one frame out of fifty is noise, one in forty is the overlay. Working on a `DiscoveryResult` needs no extra:

<!-- runnable: none -->
```python
>>> from isocenter.discovery import DiscoveryCandidate, DiscoveryResult
>>> result = DiscoveryResult([
...     DiscoveryCandidate("SMITH^JOHN", 92.0, [10, 8, 60, 12], 0, "NAME_PATTERN"),
...     DiscoveryCandidate("MERCY GENERAL", 88.0, [200, 180, 50, 10], 1, "PROPER_NOUN"),
... ], n_sources=2)
>>> list(result.to_dataframe().columns)
['text', 'confidence', 'box', 'source_index', 'classification']
>>> result.get_density_matrix(bins=(2, 2))
[[1, 0], [0, 1]]
>>> result.to_zones(min_occurrence=0.5)
[{'zone': [8, 20, 10, 70], 'type': 'LIKELY_NAME', 'occurrence': 0.5, 'confidence': 92.0, 'examples': ['SMITH^JOHN']}, {'zone': [180, 190, 200, 250], 'type': 'PROPER_NOUN', 'occurrence': 0.5, 'confidence': 88.0, 'examples': ['MERCY GENERAL']}]
```

`filter()`, `to_zones()` and `to_dataframe()` are frozen for 1.x; `get_density_matrix()` is documented but internal ([API stability](https://kvnlng.github.io/Isocenter/api/stability/)). **`get_density_matrix()` is not an image-space heatmap.** It bins each box's centre into a grid normalised by the largest box origin among the candidates, not by the image's Rows and Columns, so two scans are not comparable to each other or to the image. Take coordinates from `to_zones()` or from each candidate's `box`.

### Writing the zones

Zones go in the configuration file, under the machine they belong to:

```yaml
machines:
  - serial_number: "DEV12345"
    model_name: "UltraSound Pro"
    redaction_zones:
      - [0, 50, 0, 800] # [row_start, row_end, col_start, col_end]
```

## Documentation

- [Installation](https://kvnlng.github.io/Isocenter/installation/) and [Quick Start](https://kvnlng.github.io/Isocenter/quickstart/)
- [Tutorials](https://kvnlng.github.io/Isocenter/tutorials/deidentify-and-read-the-grade/): de-identify and read the grade, select part of a cohort, reversible anonymization, redaction, a custom export format
- [Configuration](https://kvnlng.github.io/Isocenter/configuration/), [Analytics & Reporting](https://kvnlng.github.io/Isocenter/analytics/) and [how the grade is decided](https://kvnlng.github.io/Isocenter/analytics/#how-the-grade-is-decided)
- [What the export writes](https://kvnlng.github.io/Isocenter/export-output/) and [Codec support](https://kvnlng.github.io/Isocenter/codecs/)
- [Import CTP rules](https://kvnlng.github.io/Isocenter/ctp-import/), for a CTP `DicomPixelAnonymizer.script`
- [Upgrading from 0.9.x](https://kvnlng.github.io/Isocenter/migration/)
- [API reference](https://kvnlng.github.io/Isocenter/api/session/) and [API stability](https://kvnlng.github.io/Isocenter/api/stability/)
- [Architecture](https://kvnlng.github.io/Isocenter/architecture/) and [Performance](https://kvnlng.github.io/Isocenter/performance/)
- [For institutions](https://kvnlng.github.io/Isocenter/for-institutions/) and the [changelog](https://github.com/kvnlng/Isocenter/blob/main/CHANGELOG.md)

## Citing Isocenter

If Isocenter's de-identification is part of how a dataset was prepared, it belongs in the methods section rather than the acknowledgements. Use GitHub's **Cite this repository** button, which reads `CITATION.cff`.

Each release is archived on Zenodo. Cite the concept DOI, [10.5281/zenodo.22104298](https://doi.org/10.5281/zenodo.22104298), which always resolves to the latest version, so the citation follows the work. If you need to record the exact version used, name it in the text (`Isocenter v1.0.0`) and leave the DOI pointing at the concept record.

Isocenter is the upstream half of a pair: it builds and de-identifies the corpus that [Murmur Studio](https://github.com/kvnlng/Murmur) ([10.5281/zenodo.21077528](https://doi.org/10.5281/zenodo.21077528)) reviews. Work that used both should cite both.

## License

Apache-2.0. See [`LICENSE`](https://github.com/kvnlng/Isocenter/blob/main/LICENSE) and [`NOTICE`](https://github.com/kvnlng/Isocenter/blob/main/NOTICE).

Releases up to and including 0.9.2 were published under the GNU Affero General Public License v3.0 or later and remain available under it; the change is not retroactive.

## Contact

Bug reports and questions about documented behaviour go to [GitHub Issues](https://github.com/kvnlng/Isocenter/issues); they are answered there, in public, for free.

Help beyond that is available as paid consulting: configuring a de-identification profile for a protocol, integrating Isocenter into a pipeline, reviewing a run's report before it goes to a reviewer, or a feature your study needs sooner than the roadmap. Write to <support@isocenter.net> with what you need, and use the same address for anything that should not be public.

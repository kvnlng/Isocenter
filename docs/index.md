---
title: Isocenter
template: home.html
hide:
  - navigation
  - toc
---

<!-- The hero (the one-line claim, the install command, the report
     excerpt) and the pipeline band are rendered by docs/overrides/home.html
     above this file's content. This file carries the rest of the landing
     page as ordinary markdown; extra.css lays the "What it refuses to do"
     and "What it does" lists out as the ledger's clause grid. -->

## The situation

You have a cohort of studies and a protocol an IRB approved. You need to hand a de-identified copy to a collaborator, a registry, or a model. You also need to hand your compliance reviewer a record: what was removed, what stayed because the protocol allowed it, and anything the run lost on the way.

Isocenter is the Python library your script imports to do that. It builds a SQLite metadata index and a pixel/waveform sidecar beside a read-only source tree, applies your de-identification profile and pixel redaction rules to an in-memory object graph, grades its own output, and writes clean copies to a new directory as DICOM or PhysioNet WFDB.

There is no command-line tool. The Python API is the whole interface, because a library that lives in your script can be paused, resumed, inspected, and audited in ways a batch command cannot.

## What it refuses to do

- **Modify a source file.** Ingest reads. Anonymize and redact change an in-memory graph. Nothing reaches disk until `export()` writes copies to a directory you name, so a crashed or abandoned run leaves the originals as they were.
- **Grade a lossy export `PASS`.** Every step that can lose data writes an audit row, and the report reads those rows. A cohort that lost a file, a private tag, a waveform group, or a pixel frame grades `REVIEW_REQUIRED` and names the loss. A DICOM export that wrote nothing raises rather than returning quietly.
- **Pass through pixels it could not decode.** A frame that cannot be decompressed fails its instance's export rather than being copied uninspected.
- **Advertise a Python version it does not test.** The suite runs on Python 3.12 and on the free-threaded 3.14t build on every pull request, and on all four supported versions at release. The PyPI classifiers list only those.

## What it does

- **An object model over pydicom.** `Patient`, `Study`, `Series`, and `Instance`, with attributes keyed by tag. Pixel and waveform data load lazily from the sidecar and can be released. See [Architecture](architecture.md).
- **A persistent session.** Reopen a 10,000-instance cohort without rescanning, pause and resume a job, and read the audit log of every action. See the [Quick Start](quickstart.md).
- **De-identification against the DICOM PS3.15 Annex E Basic Profile table.** A profile decides which tags go, are emptied, are replaced, or are date-shifted. A field the protocol permits stays. PHI detection walks nested sequences structurally. With no configuration a 620-rule floor policy applies: the PS3.15 Annex E Basic Profile table (2026c; UIDs not yet replaced) plus three research defaults. UIDs are not replaced. See [Configuration](configuration.md).
- **Machine-specific pixel redaction.** Zones are keyed by device. An optional OCR pass finds where burned-in text actually lands, and CTP `DicomPixelAnonymizer.script` rules import directly. See [Intelligent OCR](ocr.md) and [Migration Tools](migration.md).
- **Reversible anonymization, if you choose it.** Original identities encrypted under a Fernet key, stored in a private tag, recoverable by whoever holds the key. The export discloses when recoverable identities are present.
- **Waveforms.** DICOM waveform IODs in, PhysioNet WFDB records out, with an annotation bridge to Murmur Studio. See [Waveforms & WFDB](waveforms.md).
- **A compliance report.** Cohort summary, audit trail, every exception listed, a `PASS` or `REVIEW_REQUIRED` grade, and a signature block for the reviewer who accepts it. See [Analytics & Reporting](analytics.md).

## Start here

```bash
pip install isocenter
```

Then the [Quick Start](quickstart.md) walks the pipeline end to end: ingest, examine, configure, audit, anonymize, redact, export, report. The [Configuration](configuration.md) guide is where your protocol becomes a profile.

If you are evaluating Isocenter for an institution rather than running it, [For institutions](for-institutions.md) is the page to read: the license, the citation, and what the report gives a reviewer.

## Measured, not promised

One benchmark has a recorded run behind it: 100 multi-frame files, about 50 GB raw, ingest through export, January 2026. The numbers and the machine are on the [Performance](performance.md) page, and larger runs will appear there when they exist.

## License and citation

Apache License 2.0 from the release after 0.9.2; earlier releases remain under AGPL-3.0-or-later. Each release is archived on Zenodo under the concept DOI [10.5281/zenodo.22104298](https://doi.org/10.5281/zenodo.22104298), and the repository carries a `CITATION.cff`. If Isocenter's de-identification is part of how a dataset was prepared, it belongs in the methods section.

Bug reports and questions about documented behaviour go to [GitHub Issues](https://github.com/kvnlng/Isocenter/issues), in public and for free. Help beyond that, such as configuring a profile for your protocol, integrating Isocenter into a pipeline, or reviewing a run's report, is available as paid consulting: write to <support@isocenter.net> with what you need, and use the same address for anything that should not be public.

# For institutions

This page is for the people who decide whether Isocenter may be used at a site and never run it: compliance officers, IRB and privacy-office staff, and imaging-informatics leads. It answers the questions that review usually asks, in the order they are usually asked, without code.

## What it is

Isocenter is an open-source Python library that a research team's own script imports to de-identify a cohort of DICOM studies. It reads the source files, builds an index and a working copy of the pixel data beside them, applies the de-identification rules the team configured, and writes de-identified copies to a new directory. It also writes a report of what it did.

It is a library, not a service. It runs on the machine the team runs it on, inside the institution's network, and sends nothing anywhere. There is no account, no telemetry, and no cloud component.

## What it never does

- **It never modifies the source files.** The originals are read and left as they were. A crashed or abandoned run leaves them untouched.
- **It never certifies compliance.** De-identification under Isocenter is whatever the team's configured profile says it is. Whether that profile satisfies a protocol, an IRB determination, or a regulation is the institution's judgement, and the report exists to make that judgement possible.
- **It never grades a run that lost identifiable or acquired data as passing.** If a file, a private tag, a pixel frame, or a waveform group could not be carried through, the report says so and grades the run `REVIEW_REQUIRED`. Routine losses of standard elements -- a large overlay plane, say -- are listed in the report's Data Loss section but do not change the grade.

## What the report contains

The team generates a Markdown report at the end of a run. It contains:

- **A cohort summary**: how many patients and instances the session holds and, after an export, how many instances were written of those requested. A per-instance manifest can be written as a separate document.
- **An audit trail**: counts of every action taken (tags removed or replaced, dates shifted, pixel regions redacted, files exported) and every loss recorded.
- **Exceptions**: every warning and error the run raised, listed individually.
- **A grade**: `PASS` or `REVIEW_REQUIRED`. There is deliberately no `FAIL`. A run that lost something is a run a person must look at, and the report's *Grade Basis* lists every reason the run is not `PASS`.
- **A signature block** for the reviewer who accepts the report.

The report is evidence for whatever review the institution runs. It is not itself a determination.

## How de-identification is configured

The team writes a configuration file that names the de-identification profile, the tags to remove, replace, or date-shift, and the pixel regions to redact on each make and model of equipment. Isocenter does not start from nothing. With no configuration, or a file that names no profile, a floor of 620 tag rules applies -- the PS3.15 Annex E Basic Profile table (2026c; UIDs not yet replaced) plus three research defaults -- and private tags are removed unless the file says otherwise. A file removes less than that floor only by saying so explicitly. The configuration plus that floor is what a reviewer reads to see what will happen to the data; two caveats a reviewer should know are that Patient's Name, Patient ID and Study Date are always replaced whatever the file says, and that certain rule options are accepted but not applied ([#537](https://github.com/kvnlng/Isocenter/issues/537), [#538](https://github.com/kvnlng/Isocenter/issues/538)).

Two options bear on review:

- **Date shifting** is deterministic per patient within a project, so intervals between a patient's studies survive. The offset is derived from a secret held in the team's working store and is not recoverable from the de-identified files alone. It is not a guarantee that absolute dates cannot be recovered: anyone holding that store, or the secret written out of it, can recover exact dates, so neither should leave the team; a date a whole-day shift moves keeps its weekday, and the configured range bounds the guess. Releases before 0.9.7 derived both the offset and the `ANON_` pseudonym without a secret, so dates in files exported by those releases can be recovered from the file itself, and the pseudonym can be reversed by trying candidate IDs (GHSA-phg9-vcvc-j4r7).
- **UIDs are kept.** Study, Series and SOP Instance UIDs are not replaced, so exported files remain linkable to the originals by anyone who can see the source UIDs ([#544](https://github.com/kvnlng/Isocenter/issues/544)).
- **Reversible anonymization** is optional and off by default. When a team turns it on, original identities are encrypted under a key the team holds and stored inside the de-identified files, recoverable only with that key. The export warns and records in the audit trail when it has written files that carry recoverable identities, so a cohort cannot be shared under the impression that it does not.

## Burned-in text

Some equipment writes patient identifiers into the image pixels themselves. Isocenter redacts rectangular regions of the pixels for each make and model the team configures, and can use optical character recognition on a sample of one machine's images to find where text actually lands before the team writes those regions. Images the source marks as carrying burned-in annotations are flagged for manual review rather than silently exported.

## License

Apache License 2.0 from the release after version 0.9.2. It is a permissive license with an explicit patent grant; it permits internal use, modification, and redistribution, and does not require an institution to publish its own code. Releases up to and including 0.9.2 were published under the GNU Affero General Public License v3.0 or later and remain available under it.

The full text is in the repository's `LICENSE` file.

## Citation and provenance

Each release is archived on Zenodo, a repository operated by CERN for research outputs, under the concept DOI [10.5281/zenodo.22104298](https://doi.org/10.5281/zenodo.22104298), which always resolves to the latest version. The repository carries a `CITATION.cff` file that reference managers read. Work that used Isocenter to prepare a dataset should cite it in the methods section.

The source, the issue tracker, and the full change history are public at [github.com/kvnlng/Isocenter](https://github.com/kvnlng/Isocenter). Every release runs its test suite on the supported Python versions before it is published.

## Where it runs

Python 3.12 or later, on Linux or macOS, on the team's own hardware. Windows is not supported. Sizing guidance for large cohorts is on the [Performance](performance.md) page.

## Contact

Bug reports and questions about documented behaviour go to [GitHub Issues](https://github.com/kvnlng/Isocenter/issues), where they are answered in public and for free.

Support beyond that is offered as paid consulting, under a written engagement: configuring de-identification to a protocol, integrating Isocenter into an institution's pipeline, reviewing a run's report ahead of a compliance review, or building a feature a study needs. Write to <support@isocenter.net> with what you need. The same address is for anything that should not be public.

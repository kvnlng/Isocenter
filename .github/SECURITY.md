# Security policy

Isocenter's promise is the data it exports. If an export can reveal who a patient is, or a real date the export was meant to hide, that is a security problem, and it is reported privately.

## Report privately

Use GitHub's [private vulnerability reporting](https://github.com/kvnlng/Isocenter/security/advisories/new), or write to <support@isocenter.net>. Do not open a public issue or discussion.

Never send patient data. Describe the file by its shape instead: the modality, manufacturer and model, the transfer syntax, the tag or sequence path that carries the value, the configuration the session ran with, and the Isocenter version. A synthetic file that reproduces the problem is the most useful report of all.

## What happens next

You will get a reply. A confirmed problem is fixed in a release, and a GitHub Security Advisory is published with that release. It credits you unless you ask not to be named.

## What counts

- An exported DICOM or WFDB file still carries something that the session's de-identification policy says to remove or replace. That covers an attribute, a nested sequence, a private element, or the pixels of a configured redaction region.
- An export lets someone recover an identity or a real date, or confirm a guess about one, without the project's secret or `isocenter.key`. [GHSA-phg9-vcvc-j4r7](https://github.com/kvnlng/Isocenter/security/advisories/GHSA-phg9-vcvc-j4r7) was this kind: the pseudonym and the date shift were derived from an unkeyed hash.
- The identity token that reversible anonymization writes can be opened without `isocenter.key`, or the project secret reaches an export.

## What does not

The following are not security problems. If they are bugs, they belong in public [issues](https://github.com/kvnlng/Isocenter/issues):

- What the session store (the `.db` file and its `_pixels.bin`), the compliance report, a manifest or `isocenter.log` contains. These are for the operator, who already holds the source data, and they are not de-identified. The store keeps the original identifiers by design.
- A value that the configuration keeps. The built-in profile, `basic`, follows DICOM PS3.15 Annex E, Table E.1-1 (2026c). Whether a value outside that table identifies anyone is for the configuration to decide.
- Burned-in text in a region that no redaction rule covers. Neither the export nor the grade detects it, and optical character recognition runs only when asked ([Burned-in text](https://kvnlng.github.io/Isocenter/ocr/)).

## Supported versions

Security fixes are made on the 1.0 line: the latest 1.0 release candidate until 1.0.0 is final, then the latest 1.0.x release. The 0.9 series gets none.

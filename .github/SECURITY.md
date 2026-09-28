# Security policy

Isocenter's promise is the data it exports. An export can still carry something that its de-identification policy says to remove or replace, or can let someone recover an identity or a real date that it was meant to hide. Either is a security problem, and it is reported privately.

## Report privately

Use GitHub's [private vulnerability reporting](https://github.com/kvnlng/Isocenter/security/advisories/new), or write to <support@isocenter.net>. Do not open a public issue or discussion.

Never send patient data, the key file, or a file made from your data. Describe the problem by its shape instead: the modality, manufacturer and model, the transfer syntax, the tag or sequence path that carries the value, the configuration the session ran with (with device serial numbers replaced), and the Isocenter version. A synthetic file that reproduces the problem is the most useful report of all.

## What happens next

You will get a reply. A confirmed problem is fixed in a release, and a GitHub Security Advisory is published with that release. It credits you unless you ask not to be named.

## What counts

- An exported DICOM or WFDB file still carries something that the session's de-identification policy says to remove or replace. That covers an attribute, a nested sequence, a private element, or the pixels of a configured redaction region.
- Under the built-in `basic` profile, or the floor built on it, an export keeps a value that DICOM PS3.15 Annex E, Table E.1-1 (2026c) says to remove or replace, and [Configuration](https://kvnlng.github.io/Isocenter/configuration/#privacy-profile) lists no departure for it.
- An export lets someone recover an identity or a real date that it was meant to hide, or confirm a guess about one, without the project's secret or the reversible-anonymization key (`isocenter.key` by default). [GHSA-phg9-vcvc-j4r7](https://github.com/kvnlng/Isocenter/security/advisories/GHSA-phg9-vcvc-j4r7) was this kind: the pseudonym and the date shift were derived from an unkeyed hash.
- The identity token that reversible anonymization writes can be opened without that key, or the project secret reaches an export.

## What does not

The following are not security problems. If they are bugs, report them in public [issues](https://github.com/kvnlng/Isocenter/issues), but describe them by their shape, never by their content: no value, screenshot or log line from your data, and nothing from a report beyond its two summary tables. If you cannot describe one without its content, write to <support@isocenter.net> instead.

- What the session store contains (the `.db` file with its `-wal` and `-shm`, and its `_pixels.bin`), and likewise the compliance report, a manifest, the cohort table that `export_dataframe()` writes, or `isocenter.log`. These are for the operator, who already holds the source data. They are not de-identification outputs: they can carry source UIDs and file paths, and the store holds the original data.
- A value that the configuration keeps on purpose, either through a rule of your own or through one of the departures from Table E.1-1 that [Configuration](https://kvnlng.github.io/Isocenter/configuration/#privacy-profile) lists.
- A value outside Table E.1-1 that no rule removes. Whether it identifies anyone is for your configuration to decide.
- Burned-in text in a region that no redaction rule covers. Neither the export nor the grade detects it, and optical character recognition runs only when asked ([Burned-in text](https://kvnlng.github.io/Isocenter/ocr/)).

## Supported versions

Security fixes are made on the 1.0 line: the latest 1.0 release candidate until 1.0.0 is final, then the latest 1.0.x release. The 0.9 series gets none.

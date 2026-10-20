# Glossary

This page gives the one term that the documentation uses for each concept. The terms apply to prose only. An API name stays as it is wherever it names the API, for example `anonymize()`, `date_jitter` or `config`.

The procedure pages and the tutorials use these terms every time. The other pages use them as a guide.

## Terms

| Term | Meaning | Not used for this concept |
| :--- | :--- | :--- |
| **de-identify**, **de-identification** | The whole process that removes identifiers. | anonymize, scrub, pseudonymize |
| **anonymize** | The metadata step that applies the tag rules: `anonymize()`, or "the anonymize step". | — |
| **reversible anonymization** | The optional feature that lets the holder of a key recover the original identities. It is the name of the feature and of `enable_reversible_anonymization()`. | reversible de-identification, re-identification |
| **scrub** | Remove identifiers from free text with the `SCRUB` action. Used only for that action. | clean, sanitize |
| **the store** | `<name>.db` and `<name>_pixels.bin` together. The first mention on a page is "the session store". "SQLite" names the engine only where the engine matters, for example for WAL or `flock`. | database, SQLite file, index, persistence file |
| **the sidecar** | The append-only file that holds pixel and waveform data, `<name>_pixels.bin`. | blob file, pixel file |
| **the audit log** | The record of actions that the store keeps. | log, trail, history |
| **audit row** | One record in the audit log. "A `WARNING` row" where the level matters. | log entry, audit entry, event |
| **Audit trail** | The title of the [Audit trail](api/persistence.md) API page. Used only as that name. | — |
| **log line** | One line in `isocenter.log`. A log line is not an audit row: the two are in different places. | log message, entry |
| **finding** | A possible identifier that the scan detected (`PhiFinding`). | hit, detection, match |
| **grade** | `PASS` or `REVIEW_REQUIRED`. Also a verb: "the report grades the run". | verdict, status, score |
| **grade condition** | One reason why a run is not `PASS`. [How the grade is decided](analytics.md#how-the-grade-is-decided) lists them. The report shows them in its **Grade Basis**. | reason, criterion |
| **PHI status** | `REMEDIATED`, `CLEARED`, `IDENTIFIED` or `UNSCANNED`. "Status" alone means this. | dirty, state, flag |
| **scan**, **audit** | To scan is to read the graph for PHI. The audit is the step that scans, `audit()`. `examine()` is a different step. | inspect, examine, check |
| **remediate**, **remediation** | Change a tag as a rule says. The procedures say "remove", "replace" or "shift" instead. | fix, clean, apply |
| **decline** | Not apply a rule, and write a `REMEDIATION_DECLINED` row. A decline is not an error. `IngestSummary.declined` is ingest's count of files it did not add. That use is the API's own name. | skip, refuse |
| **refuse** | Turn an input or a call away with an error or an `ERROR` row. For example, ingest refuses a file. | reject, deny |
| **withhold** | Not write an instance, on purpose. | skip, drop, exclude |
| **data loss** | Data that the export could not carry, recorded in a `DATA_LOSS` row. A verb is fine: "the export drops X". | "dropped data", "lost data" |
| **de-identification markers** | The elements `(0012,0062)`, `(0012,0063)` and `(0028,0303)` that the export writes. | stamps, flags |
| **owner stamp** | A Patient, Study or Series field that the export writes into each file. | — |
| **export** | Write de-identified copies to a folder. Used as a verb and as the name of the step. "Write" is the act at the level of one file: "the worker writes the file". | write out, save, output |
| **export folder** | The folder that the export writes into. | output directory, destination |
| **ingest** | Read source files into the store. | import, load, index |
| **configuration** | The YAML policy file and the object that holds it. `config` appears only in code. | config file, settings |
| **profile** | A pinned rule table, such as `basic@2026c`. Also "privacy profile". | template, preset |
| **policy** | What a PHI status is recorded under: the tag rules, `remove_private_tags` and `CONFIG_VERSION`. A policy is not a configuration. | — |
| **redaction zone** | A rectangle of pixels that a configuration tells `redact()` to blank. "Region" and "box" are used only for what OCR or the text detector finds. | region, mask, box |
| **redact** | Blank pixels. | mask, black out, burn |
| **burned-in text** | Text drawn into the pixels. The hyphen is used when the term comes before a noun. The verb phrase is fine: "text burned in by the modality". | burnt-in, burned in (before a noun) |
| **date shift**, **shift** | The per-patient offset that moves dates. "Jitter" appears only in the key `date_jitter` and the action `JITTER`. | jitter |
| **pseudonym** | An `ANON_…` value. | alias, token |
| **key file** | The file that holds the key for reversible anonymization, for example `isocenter.key`. "Fernet key" is used only where the format matters. | secret |
| **project secret** | The per-project HMAC secret that the store holds. It is not the key file, and it is never called a key. | key, salt |
| **identity token** | The encrypted identity in `(0400,0500)`. The first mention on a page is "identity token", not "token" alone. | token (first mention) |
| **cohort** | A set of patients that are processed together. "Dataset" is used for a pydicom `Dataset` and for MIDI-B's own datasets. | dataset, collection |
| **the graph** | `Patient → Study → Series → Instance` in memory. The first mention on a page is "the in-memory object graph". | model, tree |
| **instance** | The object for one DICOM file. "File" means the bytes on disk. | file (for the object), object |
| **tag**, **element** | A tag is the identifier, `gggg,eeee`. An element (data element) is the item in a file. "Attribute" is used for `attributes` and in quotations from the standard. | attribute |

# Exporter registry

!!! warning "Provisional"
    The exporter registry (`Exporter`, `register()`, `get_exporter()`,
    `available_formats()`) is **provisional until 1.1**. It is documented
    but internal (see [API stability](stability.md#the-exporter-registry-provisional-until-11)):
    1.1 may replace these four names rather than extend them, with a
    CHANGELOG entry naming both spellings. A plugin written against 1.0
    should pin `isocenter>=1.0,<1.1`.

`session.export(folder, format=...)` looks `format` up in this registry
and calls the exporter it finds. The two built-in formats, `dicom` and
`wfdb`, register themselves when `isocenter` is imported. Anything else
can be registered too:

```python
from isocenter import exporters


class AcmeExporter(exporters.Exporter):
    def export(self, session, folder, **options):
        written = []
        # ... write files under `folder`, reading the graph, never changing it
        return written


exporters.register("acme", AcmeExporter)
print(exporters.available_formats())  # ['acme', 'dicom', 'wfdb']
```

## What a third-party exporter receives

**The session's graph and nothing else.** That graph is not what the
built-in formats write. Every export-time gate lives inside the built-in
formats, and some of them change pixels and sequences on the way out.
`export()` itself only resolves the format and dispatches, so for any
exporter other than the two built-in classes, none of the following runs:

- the burned-in re-audit that withholds an instance still carrying an
  identifier (`check_burned_in`);
- the configured redaction zones. The DICOM export looks up each series'
  zones in the configuration in force and applies them to a copy of every
  frame it writes, whether or not `redact()` ran. For a plugin,
  `instance.get_pixel_data()` holds only what `redact()` changed: with
  zones configured and `redact()` not run, the pixels are unredacted;
- the drop of nested icons. An Icon Image
  Sequence `(0088,0200)` item is a downsampled copy of a frame, and nothing
  scans or redacts one. The DICOM export drops an instance's own icon when
  its pixels are redacted or have zones configured, and every other nested
  icon when any instance in the store is redacted or any configured zone
  matches a series in it. A plugin that copies `instance.sequences` ships a
  thumbnail of what redaction removed, **even after `redact()` ran**;
- the recoverable-identity disclosure (`check_reversibility`). After
  `lock_identities()`, the graph still carries the encrypted identity token
  at `(0400,0500)`;
- the de-identification markers `(0012,0062)`, `(0012,0063)` and
  `(0028,0303)`, which Isocenter decides per instance at export time and
  never puts in the graph;
- the filter on `attributes` keys. The DICOM writer writes only keys shaped
  `gggg,eeee` and drops every `_`-prefixed bookkeeping key. One of those,
  `_ISOCENTER_SOURCE_SOP_UID` (`entities.SOURCE_SOP_UID_ATTR`), holds the
  source SOP Instance UID that UID replacement removed. A plugin that
  walks `instance.attributes` writes it;
- the owner stamps: `(0010,0010)`, `(0010,0020)`, `(0008,0020)`,
  `(0020,000D)` and `(0020,000E)` written from the patient, study and
  series rather than from an instance's own copy;
- the rule for a subject with no Patient ID: `patient.patient_id` can be
  the synthetic key `"\no-patient-id\<Study Instance UID>"`, and
  `exported_patient_id()` is the only reader for output;
- the `patient_ids`/`subset` counting and the stale-policy notice, and the
  `EXPORT` and export-time `DATA_LOSS` audit rows.

## How the report treats a third-party export

Each run of an exporter that is not one of the two built-in classes writes
one `WARNING` audit row before the exporter is called, naming the format
and the class and saying that its output is not attested by Isocenter. The
row is written even when the exporter then raises.

**So in 1.0, no third-party export grades `PASS`.** A `WARNING` row grades
the report `REVIEW_REQUIRED` (condition 2 in
[How the grade is decided](../analytics.md#how-the-grade-is-decided)),
however the plugin behaves. The same report still carries the "generated
before any export" note, because no `EXPORT` row was written.

The class is what decides, not the format name or the module: a subclass
of a built-in, or a different class registered as `dicom`, is third-party.
`DicomFormatExporter` registered again, under any name, is not.

Running the format-independent gates in `export()` before dispatch, so a
plugin can grade `PASS`, is planned for 1.1
([#783](https://github.com/kvnlng/Isocenter/issues/783)).

## Rules for an exporter author

1. **Do not change the session's graph or store.** Export is a read.
   Nothing checks this for a third-party exporter.
2. **Write `exported_patient_id(patient)`, never `patient.patient_id`.**
   Import it with `from isocenter.entities import exported_patient_id`.
   Take a patient's, study's or series' identifiers from the owner object,
   not from an instance's copy of them. Write only `gggg,eeee` keys from
   `attributes`, never a `_`-prefixed one: those are Isocenter's
   bookkeeping, and one holds the source SOP Instance UID.
3. **Do not write the de-identification markers.** Isocenter decides them
   per instance from the PHI statuses it recorded, and a plugin cannot
   reproduce that decision.
4. **Decide deliberately what to do with `(0400,0500)`–`(0400,0520)`.**
   They hold the recoverable identity.
5. **Say what you wrote.** Your return value must let a caller detect that
   nothing was written: an empty list, a zero count, or a raise.
6. **`register()` checks only that the class has an `export` attribute.**
   1.1 may check more.
7. **Call `redact()` before exporting, and write no nested pixel payload.**
   `export()` does not apply the configured zones for you. An Icon Image
   Sequence `(0088,0200)` item, or any other pixel data nested in a
   sequence, is never scanned or redacted, so leave it out.

There is no reader or codec plugin point: ingest reads through pydicom and
Isocenter's own codec dispatch. The transfer syntaxes it reads and the two
it writes are in the
[Quick Start](../quickstart.md#5-anonymize-redact-export).

## Writing DICOM without the pipeline

`DicomExporter.write_tree()` is the serializer alone. It writes a graph as
it stands. It applies the owner stamps and the no-Patient-ID rule (both
write paths share those), and it drops nested icons by the redaction
already recorded on the graph:
an instance's own icon when that instance was redacted, and every other one
when any instance it writes was. This half of the gate needs no
configuration. It applies none of the rest: no burned-in
re-audit, no redaction zones, no `patient_ids` or `subset` selection, no
recoverable-identity disclosure, no de-identification markers, no notices,
and no `EXPORT` row. It writes `DATA_LOSS` rows only
to a `store_backend` you pass. Use `session.export()` to de-identify a
cohort; use `write_tree()` for a graph you built by hand, with no session
behind it.

## Reference

::: isocenter.exporters
    handler: python
    options:
      members:
        - Exporter
        - register
        - get_exporter
        - available_formats

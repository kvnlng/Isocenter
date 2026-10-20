# Exporter registry

:::{admonition} Provisional
:class: warning

The exporter registry (`Exporter`, `register()`, `get_exporter()`,
`available_formats()`) is **provisional until 1.1**. It is documented
but internal (see [API stability](stability.md#the-exporter-registry-provisional-until-11)).
Version 1.1 may replace these four names rather than extend them, with a
CHANGELOG entry that names both spellings. A plugin written against 1.0
should pin `isocenter>=1.0,<1.1`.
:::

`session.export(folder, format=...)` finds the exporter registered for
`format` in this registry, and calls it. The two built-in formats, `dicom` and
`wfdb`, register themselves when `isocenter.exporters` is imported, which
`session.export()` does. Anything else
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
`export()` itself only resolves the format and dispatches. So for any
exporter other than the two built-in classes, none of the following runs:

- The burned-in re-audit that withholds an instance still carrying an
  identifier (`check_burned_in`).
- The configured redaction zones. The DICOM export reads each series'
  zones from the configuration in force. It applies them to a copy of
  every frame it writes, whether or not `redact()` ran. For a plugin,
  `instance.get_pixel_data()` holds only what `redact()` changed. With
  zones configured and `redact()` not run, the pixels are unredacted.
- The drop of nested icons. An Icon Image
  Sequence `(0088,0200)` item is a downsampled copy of a frame, and nothing
  scans or redacts one. The DICOM export drops an instance's own icon when
  its pixels are redacted or have zones configured. It drops every other
  nested icon when any instance in the store is redacted, or any
  configured zone matches a series in the store. A plugin that copies
  `instance.sequences` ships a thumbnail of what redaction removed,
  **even after `redact()` ran**.
- The recoverable-identity disclosure (`check_reversibility`). After
  `lock_identities()`, the graph still carries the encrypted identity token
  at `(0400,0500)`.
- The de-identification markers `(0012,0062)`, `(0012,0063)` and
  `(0028,0303)`, which Isocenter decides per instance at export time and
  never puts in the graph.
- The filter on `attributes` keys. The DICOM writer writes only keys shaped
  `gggg,eeee` and drops every `_`-prefixed bookkeeping key. One of those,
  `_ISOCENTER_SOURCE_SOP_UID` (`entities.SOURCE_SOP_UID_ATTR`), holds the
  source SOP Instance UID that UID replacement removed. A plugin that
  walks `instance.attributes` writes it.
- The owner stamps: `(0010,0010)`, `(0010,0020)`, `(0008,0020)`,
  `(0020,000D)` and `(0020,000E)` written from the patient, study and
  series rather than from an instance's own copy.
- The rule for a subject with no Patient ID. `patient.patient_id` can be
  the synthetic key `"\no-patient-id\<Study Instance UID>"`, and
  `exported_patient_id()` is the only reader for output.
- The `patient_ids`/`subset` counting and the stale-policy notice, and the
  `EXPORT` and export-time `DATA_LOSS` audit rows.

## How the report treats a third-party export

Each run of an exporter that is not one of the two built-in classes writes
one `WARNING` audit row before `export()` calls the exporter. The row
names the format and the class, and says that Isocenter does not attest
the exporter's output. The row is written even when the exporter then
raises.

**So in 1.0, no third-party export grades `PASS`.** A `WARNING` row grades
the report `REVIEW_REQUIRED` (condition 2 in
[How the grade is decided](../analytics.md#how-the-grade-is-decided)),
however the plugin behaves. Isocenter writes no `EXPORT` row for a
third-party export. So the same report also carries the "generated
before any export" note, unless the store already holds an `EXPORT` row.
An earlier `dicom` or `wfdb` export on the same store, in any session,
can have written one.

The class decides, not the format name or the module. A subclass of a
built-in, or a different class registered as `dicom`, is third-party.
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
   `attributes`, never a `_`-prefixed one. Those keys are Isocenter's
   bookkeeping, and one of them holds the source SOP Instance UID.
3. **Do not write the de-identification markers.** Isocenter decides them
   per instance from the PHI statuses it recorded. A plugin cannot
   reproduce that decision.
4. **Decide deliberately what to do with `(0400,0500)`–`(0400,0520)`.**
   They hold the recoverable identity.
5. **Say what you wrote.** Your return value must let a caller detect that
   nothing was written. Return an empty list or a zero count, or raise.
6. **`register()` checks only that the class has an `export` attribute.**
   Version 1.1 may check more.
7. **Call `redact()` before exporting, and write no nested pixel payload.**
   `export()` does not apply the configured zones for you. An Icon Image
   Sequence `(0088,0200)` item, or any other pixel data nested in a
   sequence, is never scanned or redacted. Leave it out.

There is no reader or codec plugin point. Ingest reads through pydicom and
Isocenter's own codec dispatch. The transfer syntaxes that Isocenter reads,
and the two that it writes, are in
[Codec support](../codecs.md).

## Writing DICOM without the pipeline

`DicomExporter.write_tree()` is the serializer alone. It writes a graph as
it stands. It applies the owner stamps and the no-Patient-ID rule, which
both write paths share. It also drops nested icons by the redaction
already recorded on the graph. It drops an instance's own icon when that
instance was redacted, and every other icon when any instance that it
writes was redacted. This half of the gate needs no configuration.
`write_tree()` also runs the export's Type 1 check, which covers CT, MR
and PET Image Storage only. It raises `RuntimeError` for such an
instance that is missing one of the elements the check covers, or holds
it empty
([What the export changes on the way out](../export-output.md#what-the-export-changes-on-the-way-out)).
It applies none of the rest: no burned-in
re-audit, no redaction zones, no `patient_ids` or `subset` selection, no
recoverable-identity disclosure, no de-identification markers, no notices,
and no `EXPORT` row. It writes `DATA_LOSS` rows only
to a `store_backend` you pass. Use `session.export()` to de-identify a
cohort. Use `write_tree()` for a graph you built by hand, with no session
behind it.

## Reference

```{eval-rst}
.. automodule:: isocenter.exporters
   :members: Exporter, register, get_exporter, available_formats
```

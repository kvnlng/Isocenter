# Exporter registry

`session.export(folder, format=...)` finds the exporter registered for
`format` in this registry. The two built-in formats, `dicom` and `wfdb`,
register themselves when `isocenter.exporters` is imported, which
`session.export()` does. You can register a format of your own: a class
whose `write()` method writes one instance.

The contract (`Exporter`, `ExportItem`, `register()`, `get_exporter()`,
`available_formats()`) is documented but internal
([API stability](stability.md#the-exporter-contract)). A minor release
can change it, with a `Breaking` CHANGELOG entry that names both
spellings. A patch release never does. In 1.0 it was provisional, and
1.1 replaced it ([#783](https://github.com/kvnlng/Isocenter/issues/783)).
`register()` refuses a class written for 1.0, which implements
`export(session, folder, **options)`, with `TypeError`.

## An exporter is a class in a module

```python
# acme_exporter.py
import json
import os

from isocenter import exporters


class AcmeExporter(exporters.Exporter):
    options = frozenset({"indent"})

    def write(self, dataset, item, folder, **options):
        out = os.path.join(folder, *item.folder_names)
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, f"{item.sop_instance_uid}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"PatientID": item.patient_id,
                       "Modality": str(dataset.get("Modality", ""))},
                      handle, indent=options.get("indent"))
        return path
```

```python
from isocenter import exporters
from acme_exporter import AcmeExporter

exporters.register("acme", AcmeExporter)
print(exporters.available_formats())  # ['acme', 'dicom', 'wfdb']
```

`write()` runs once per instance, in an export worker process, in
parallel, as the DICOM export's own writer does. The worker imports your
class by its module and name. So the class must be defined at the top
level of a module that a new Python process can import. A class defined
in a notebook cell, in an interactive prompt or inside a function
cannot be imported there, and `register()` refuses it with `TypeError`.
If the module is a file in your working directory, put that directory
on `sys.path` before the export: a worker starts with the `sys.path` of
the process that started it.

## What `write()` receives

- **`dataset`**: a `pydicom.dataset.FileDataset`, the instance as the
  DICOM export would write it, before compression. The same worker code
  builds it, so every write-side gate has applied:
  - the configured redaction zones, applied to a copy of the pixels,
    whether or not `redact()` ran;
  - the drop of nested icons. An Icon Image Sequence `(0088,0200)` item
    is a downsampled copy of a frame, and nothing scans or redacts one.
    The instance's own icon is dropped when its pixels are redacted or
    zones are configured for it. Every other nested icon is dropped when
    any instance in the store is redacted, or a configured zone matches
    a series in the store;
  - only `gggg,eeee` keys. Isocenter's `_`-prefixed bookkeeping keys,
    one of which holds the source SOP Instance UID that UID replacement
    removed (`_ISOCENTER_SOURCE_SOP_UID`), are never in it;
  - the owner stamps: `(0010,0010)`, `(0010,0020)`, `(0008,0020)`,
    `(0020,000D)` and `(0020,000E)` from the patient, study and series;
  - the rule for a subject with no Patient ID: Patient ID `''`;
  - the de-identification markers `(0012,0062)`, `(0012,0063)` and
    `(0028,0303)`, where the DICOM export would write them;
  - the Type 2 fills, and native pixels with descriptors that describe
    them.
- **`item`**: an `ExportItem` with `sop_instance_uid`, `folder_names`
  (the `Subject`/`Study`/`Series` folder names that both built-in
  formats write under) and `patient_id` (the Patient ID the export
  carries, `''` for a subject with none).
- **`folder`**: the export folder that `export()` was given.
- **`**options`**: the options that your class names in `options`, as
  the caller passed them. An option name that is neither yours nor the
  session's raises `TypeError` before anything runs. Each value must
  pickle, because it goes to the worker.

`write()` never receives the session, its store, its configuration or
its project secret. Changing `dataset` changes nothing in the session,
because the worker holds a copy.

The rows about the source data are written for a plugin export too,
in the DICOM export's words: the ambiguous value representations (which
are resolved on the dataset you get), the label of a file with no
pixels, and the label fallback. Where the IOD check finds a gap, the
DICOM export fails the file; a plugin export hands you the dataset and
writes a `WARNING` row that names the gap.

The DICOM-only rewrites are not applied to the dataset: the relabelling
of long values as `UN`, Encapsulated Document Length, `use_compression`
and `verify_readback`.

## What `export()` does around it

`session.export(folder, format="acme")` runs every export gate that does
not depend on the format, as it does for `dicom`:

- the options `patient_ids`, `subset`, `check_burned_in`,
  `check_reversibility` and `show_progress`, read and refused as for
  `dicom`. Under a `burned_in_text` policy, `check_burned_in=True` also
  reads the pixels it will hand over;
- the save, and the withheld, unmatched and stale-policy rows;
- the refusal of a SOP Instance UID that cannot name a file. That
  instance fails, with an `ERROR` row, before `write()` sees it;
- one `ERROR` row for each instance whose `write()` raised, one
  `DATA_LOSS` row for each element the dataset could not carry, and the
  collision report;
- the recoverable-identity disclosure, the delivery counters, one
  `EXPORT` row and `ExportError` last, when no instance was written and
  at least one failed.

`export()` returns an `ExportSummary`, in plan order. The `EXPORT` row
names your class, for example `acme export to out by
acme_exporter.AcmeExporter: wrote 3 of 3 planned instances from 1
patients.`

An export through a plugin can grade `PASS`. Isocenter vouches for what
it handed `write()`, as it vouches for what it hands pydicom's writer.
It does not read back what `write()` wrote.

## Rules for an exporter author

1. **Write from `dataset` and `item`, and import nothing from the
   session or the store.** They are what the gates produced.
2. **Return the path of the file that holds this instance**, as a
   `str`. Return `None` when you write nothing for it: it is then
   neither written nor failed, and the `EXPORT` row counts it apart.
   Raise to fail the instance.
3. **Give each instance its own file.** Instances that return one path
   are reported as one file, with one `ERROR` row. Isocenter reads the
   file's `(0008,0018)` back: the instance it names stays written, and
   the others fail. A file that is not DICOM names none, so all of them
   fail.
4. **Decide deliberately what to do with `(0400,0500)`–`(0400,0520)`.**
   After `lock_identities()` they hold the recoverable identity. The
   disclosure counts every instance whose dataset `write()` was handed,
   whatever `write()` did with it: returned a path, returned `None` or
   raised.
5. **Define your class in an importable module**, at its top level.
   Not in a function, a notebook cell or the prompt, and not under a
   script's `if __name__ == "__main__":`. A worker process does not run
   that block, so `register()` refuses a class defined in it.
6. **Name your options in `options`.** A name that `export()` itself
   takes (`patient_ids`, `subset`, `check_burned_in`,
   `check_reversibility`, `show_progress`) is refused by `register()`,
   and so is a name of `write()`'s own arguments (`dataset`, `item`,
   `folder`).

`register()` raises `ValueError` for the names `dicom` and `wfdb`, which
are the built-in formats'. It raises `TypeError` for a class that does
not implement `write()` itself, for a class written for 1.0, for a class
that a worker cannot import, and for an `options` that is not a
`frozenset` of `str` or that names an option the session owns.
Registering under another existing name replaces the class there.

The built-in `wfdb` format is not a plugin. It writes records from the
session's waveforms and stays behind the same session gates.

There is no reader or codec plugin point, and none is planned. Ingest
reads through pydicom and Isocenter's own codec dispatch. The transfer
syntaxes that Isocenter reads, and the two that it writes, are in
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
   :members: Exporter, ExportItem, register, get_exporter, available_formats
```

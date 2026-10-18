# Results and errors

What the frozen `Session` methods return, and the two exceptions a caller
is expected to catch. The names and fields on this page are **frozen at
1.0** ([API stability](stability.md#frozen-at-10)) unless the stability
page lists them as tier 2.

| Method | Returns |
| --- | --- |
| `ingest()` | [`IngestSummary`][isocenter.io_handlers.IngestSummary] |
| `audit()`, `scan_pixel_content()` | [`PhiReport`][isocenter.privacy.PhiReport] of [`PhiFinding`][isocenter.privacy.PhiFinding] |
| `lock_identities()`, `lock_identities_batch()` | [`LockingResult`][isocenter.session.LockingResult], a `list` of `Instance` |
| `recover_patient_identity()` | `Dict[str, Dict[str, Any]]`: SOP Instance UID to the values its token holds |
| `export(format="dicom")` | [`ExportSummary`][isocenter.io_handlers.ExportSummary] |
| `export(format="wfdb")` | `List[str]`, the paths written |
| `discover_redaction_zones()` | [`DiscoveryResult`][isocenter.discovery.DiscoveryResult] |
| `get_cohort_report()` | `pandas.DataFrame` |
| `phi_status_summary()` | `Dict[str, Counter]` of [`PhiStatus`][isocenter.entities.PhiStatus] |
| `redact()`, `reconcile_private_tags()`, `auto_remediate_config()` | `int` |

Import the two exceptions from the package:
`from isocenter import RedactionError, ExportError`. Both subclass
`RuntimeError`.

## Summaries

```{eval-rst}
.. autoclass:: isocenter.io_handlers.IngestSummary
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.io_handlers.ExportSummary
   :members:
```

## Findings

```{eval-rst}
.. autoclass:: isocenter.privacy.PhiReport
   :members: to_dataframe
   :class-doc-from: both
```

```{eval-rst}
.. autoclass:: isocenter.privacy.PhiFinding
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.privacy.PhiRemediation
   :members:
```

## Identity locks

```{eval-rst}
.. autoclass:: isocenter.session.LockingResult
```

## Exceptions

```{eval-rst}
.. autoclass:: isocenter.services.RedactionError
   :members:
   :class-doc-from: both
```

```{eval-rst}
.. autoclass:: isocenter.io_handlers.ExportError
   :members:
   :class-doc-from: both
```

## Building a graph by hand

`isocenter.Builder` is the frozen name of the class below; only
`start_patient()` is frozen, and the chain it starts is tier 2.

## Builder

```{eval-rst}
.. autoclass:: isocenter.builders.DicomBuilder
   :members: start_patient
```

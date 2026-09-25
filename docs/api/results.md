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

::: isocenter.io_handlers.IngestSummary
    handler: python
    options:
      show_root_full_path: false

::: isocenter.io_handlers.ExportSummary
    handler: python
    options:
      show_root_full_path: false

## Findings

::: isocenter.privacy.PhiReport
    handler: python
    options:
      show_root_full_path: false
      merge_init_into_class: true
      members:
        - to_dataframe

::: isocenter.privacy.PhiFinding
    handler: python
    options:
      show_root_full_path: false

::: isocenter.privacy.PhiRemediation
    handler: python
    options:
      show_root_full_path: false

## Identity locks

::: isocenter.session.LockingResult
    handler: python
    options:
      show_root_full_path: false
      members: false

## Exceptions

::: isocenter.services.RedactionError
    handler: python
    options:
      show_root_full_path: false
      merge_init_into_class: true

::: isocenter.io_handlers.ExportError
    handler: python
    options:
      show_root_full_path: false
      merge_init_into_class: true

## Building a graph by hand

`isocenter.Builder` is the frozen name of the class below; only
`start_patient()` is frozen, and the chain it starts is tier 2.

::: isocenter.builders.DicomBuilder
    handler: python
    options:
      heading: Builder
      members:
        - start_patient

# Session API

`Session` is the entry point: `from isocenter import Session`. It is the
class `isocenter.session.DicomSession`, rendered below. Its constructor
and every method on this page are **frozen at 1.0**
([API stability](stability.md)), and so are the attributes in its table
except `store_backend`, which is tier 2. The methods are listed in pipeline
order, ingest → examine → config → audit → anonymize → redact → verify →
export → report, with `compact`, `release_memory` and `close` last. What
the methods return and raise is on [Results and errors](results.md).

<!-- tests/test_frozen_surface.py asserts the `:members:` line below
     renders every method stability.md freezes. `configuration` is not a
     member here: the class docstring's Attributes table describes it with
     the other three attributes. -->

## Session

```{eval-rst}
.. autoclass:: isocenter.session.DicomSession
   :class-doc-from: both

   .. automethod:: ingest
   .. automethod:: save
   .. automethod:: examine
   .. automethod:: create_config
   .. automethod:: load_config
   .. automethod:: preview_config
   .. automethod:: audit
   .. automethod:: auto_remediate_config
   .. automethod:: anonymize
   .. automethod:: enable_reversible_anonymization
   .. automethod:: lock_identities
   .. automethod:: lock_identities_batch
   .. automethod:: recover_patient_identity
   .. automethod:: redact
   .. automethod:: redact_by_machine
   .. automethod:: scan_pixel_content
   .. automethod:: discover_redaction_zones
   .. automethod:: reconcile_private_tags
   .. automethod:: export
   .. automethod:: export_dataframe
   .. automethod:: get_cohort_report
   .. automethod:: phi_status_summary
   .. automethod:: generate_report
   .. automethod:: generate_manifest
   .. automethod:: save_analysis
   .. automethod:: compact
   .. automethod:: release_memory
   .. automethod:: close
```

## DICOM export options

`export(folder, format="dicom", **options)` takes the options below for
the `dicom` format. Their names and defaults are frozen with `export()`
([API stability](stability.md#frozen-at-10)). An option name the format
does not take raises `TypeError`, and nothing is written. The table is
read from the format's own method, which is private: pass the options to
`export()`, never call that method directly.

<!-- `_export_dicom` is private (leading underscore) and rendered here on
     purpose (#27): its docstring is the one definition of the `dicom`
     format's options, and rendering it keeps a single copy of that text
     rather than a second one in `export()`'s docstring that could drift
     from it. The name stays tier 3; the option names and defaults are
     tier 1 through `export()` (stability.md, and FROZEN_DICOM_EXPORT_OPTIONS
     in tests/test_frozen_surface.py). Named as the target itself, so autodoc's
     rule that leaves out `_` names does not apply, and
     tests/test_api_docstrings_render_cleanly.py grades its docstring. -->

```{eval-rst}
.. automethod:: isocenter.session.DicomSession._export_dicom
```

# Session API

Every method on this page is **frozen at 1.0**
([API stability](stability.md)). They are listed in pipeline order:
lifecycle first, then ingest → examine → config → audit → anonymize →
redact → verify → export → report.

<!-- tests/test_frozen_surface.py asserts the `members:` block below
     renders every method stability.md freezes. -->

::: isocenter.session.DicomSession
    handler: python
    options:
      members:
        - ingest
        - save
        - examine
        - create_config
        - load_config
        - preview_config
        - configuration
        - audit
        - auto_remediate_config
        - anonymize
        - enable_reversible_anonymization
        - lock_identities
        - lock_identities_batch
        - recover_patient_identity
        - redact
        - redact_by_machine
        - scan_pixel_content
        - discover_redaction_zones
        - reconcile_private_tags
        - export
        - export_dataframe
        - get_cohort_report
        - phi_status_summary
        - generate_report
        - generate_manifest
        - save_analysis
        - compact
        - release_memory
        - close

## DICOM export options

`export(folder, format="dicom", **options)` hands `options` to the DICOM
format, whose parameters after `folder` are the options below. The option names and
defaults are frozen with `export()` ([API stability](stability.md#frozen-at-10));
an option name the format does not take raises `TypeError`, and nothing is
written. Pass them to `export()`. The method rendered here is where they
are defined, and its own name is private (tier 3): calling it directly
skips what `export()` does before dispatch.

<!-- `_export_dicom` is private (leading underscore) and rendered here on
     purpose (#27): its docstring is the one definition of the `dicom`
     format's options, and rendering it keeps a single copy of that text
     rather than a second one in `export()`'s docstring that could drift
     from it. The name stays tier 3; the option names and defaults are
     tier 1 through `export()` (stability.md, and FROZEN_DICOM_EXPORT_OPTIONS
     in tests/test_frozen_surface.py). Named as the target itself, so the
     default filter that hides `_` names does not apply, and
     tests/test_api_docstrings_render_cleanly.py grades its docstring. -->

::: isocenter.session.DicomSession._export_dicom
    handler: python
    options:
      show_root_full_path: false

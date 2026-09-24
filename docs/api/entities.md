# Entities API

The object graph `session.store` holds: `Patient` → `Study` → `Series` →
`Instance`. This page renders the names of `isocenter.entities` that
[API stability](stability.md) places in a tier, and no others. Every other
name in the module is private (tier 3).

<!-- The two `members:` lists below are stability.md's classification of
     this module, copied by hand: tier 1 from "Frozen at 1.0", tier 2 from
     "Documented but internal". A name added to either tier there is added
     here too; a name in neither is not rendered, because rendering it
     would make it tier 2 by the page's own definition ("rendered on this
     site"). The page rendered the whole module until #27. -->

## Frozen (tier 1)

The entity classes as reached from `session.store`, and the key a subject
with no Patient ID is held under, with its test. A class being here does
not make every method rendered under it tier 1. The stability page names
the frozen ones (`Instance.get_pixel_data()`, `set_pixel_data()`,
`unload_pixel_data()`, `discard_pixel_data()`, `get_waveform_data()`,
`set_attr()`), and lists the rest as tier 2: `Instance.regenerate_uid()`,
the waveform helpers, the sequence methods, the recording helpers and the
`TrackedEntity` bookkeeping the classes inherit.

::: isocenter.entities
    handler: python
    options:
      show_root_heading: false
      show_root_toc_entry: false
      filters: ["!^_"]
      members:
        - Patient
        - Study
        - Series
        - Instance
        - DicomItem
        - Equipment
        - NO_PATIENT_ID_PREFIX
        - is_synthetic_patient_id

## Documented but internal (tier 2)

Safe to call, and may change in a 1.x release with a CHANGELOG entry that
names the old spelling and the new one.

::: isocenter.entities
    handler: python
    options:
      show_root_heading: false
      show_root_toc_entry: false
      filters: ["!^_"]
      members:
        - TrackedEntity
        - PhiStatus
        - ScanPolicy
        - DicomSequence
        - clone_sequences
        - exported_patient_id
        - iter_item_tree
        - normalize_study_date
        - resolve_item_path

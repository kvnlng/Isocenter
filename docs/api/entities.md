# Entities API

The object graph `session.store` holds: `session.store.patients` is a
list of `Patient`, and each holds `Study` → `Series` → `Instance`.
`attributes` on every entity is keyed by lowercase `"gggg,eeee"` tag
strings, not keywords. This page renders the names of
`isocenter.entities` that [API stability](stability.md) places in tier 1
or tier 2.

<!-- The blocks below follow stability.md's classification of this
     module, copied by hand: tier 1 from "Frozen at 1.0", tier 2 from
     "Documented but internal". A name added to either tier there is added
     here too. The filters on the tier-1 classes hide the recording
     helpers and persistence bookkeeping (tier 2, listed on stability.md
     and not rendered): they are what the scan, remediation and the lock
     call, not what a user calls. -->

## Frozen (tier 1)

The entity classes as reached from `session.store`, and the key a subject
with no Patient ID is held under, with its test. A class being here does
not make every method rendered under it tier 1: the frozen ones are
`Instance.get_pixel_data()`, `set_pixel_data()`, `unload_pixel_data()`,
`discard_pixel_data()`, `get_waveform_data()` and `set_attr()`. The other
methods shown, such as `Instance.regenerate_uid()` and the sequence
methods, are tier 2, and so are `pixel_array` and `waveform_array` in
`Instance`'s attribute table.

**What `set_attr()` stores.** The value it is given, with one exception:
a numpy bool, integer or float (a scalar or a 0-d array), and a `list`
or `tuple` directly holding one, is stored as the Python number it
equals, so
`set_attr("0018,1150", np.int64(7))` leaves the `int` 7 in `attributes`
and a live export writes what a reopened session writes. An array of one
or more dimensions is not converted; pass `.tolist()` or `.tobytes()`.
Nor is any other numpy value: a `datetime64`, a `timedelta64`, a complex
number and a masked value stay as given, and the save refuses them.
A value written straight into `attributes` is not seen by the entity
(call `mark_modified()`), and a numpy number written that way is
converted when it is saved. The store holds `None`, `str`, `bytes`,
`bool`, `int`, `float`, pydicom's DS and IS values, and lists of those;
`set_attr()` raises nothing for anything else, and the next
`save(sync=True)`, `export()` or `compact()` raises `TypeError` naming
the instance, the tag and the type ([API stability](stability.md),
Exceptions).

**What assigning `Instance.sop_instance_uid` does.** It moves the field
and the `(0008,0018)` element together, and marks the instance edited:
`instance.sop_instance_uid = "1.2.3.4"` leaves `"1.2.3.4"` in
`instance.attributes["0008,0018"]`, and the export writes the file as
`1.2.3.4.dcm` with that UID inside. An instance assigned `''` is not
written; the export reports it as a failure keyed `UNKNOWN`. Assigning
the value the field already holds changes nothing. `series.series_instance_uid`,
`study.study_instance_uid` and `patient.patient_id` need no such step:
the export stamps each file's copy from the owner.

::: isocenter.entities
    handler: python
    options:
      show_root_heading: false
      show_root_toc_entry: false
      heading_level: 3
      members:
        - NO_PATIENT_ID_PREFIX
        - is_synthetic_patient_id

`NO_PATIENT_ID_PREFIX` is the start of the key a subject whose files
carry no Patient ID is held under. The key never reaches an exported
file.

::: isocenter.entities.Patient
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false
      filters: ["!^_", "!^record_", "!_vouches_for$", "!^mark_", "!^identity_token_is_this_stores$", "!^clear_sequence_items$"]

::: isocenter.entities.Study
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false
      filters: ["!^_", "!^record_", "!_vouches_for$", "!^mark_", "!^identity_token_is_this_stores$", "!^clear_sequence_items$"]

::: isocenter.entities.Series
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false
      filters: ["!^_", "!^record_", "!_vouches_for$", "!^mark_", "!^identity_token_is_this_stores$", "!^clear_sequence_items$"]

::: isocenter.entities.Instance
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false
      filters: ["!^_", "!^record_", "!_vouches_for$", "!^mark_", "!^identity_token_is_this_stores$", "!^clear_sequence_items$"]

::: isocenter.entities.DicomItem
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false
      filters: ["!^_", "!^record_", "!_vouches_for$", "!^mark_", "!^identity_token_is_this_stores$", "!^clear_sequence_items$"]

::: isocenter.entities.Equipment
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false
      filters: ["!^_", "!^record_", "!_vouches_for$", "!^mark_", "!^identity_token_is_this_stores$", "!^clear_sequence_items$"]

## Documented but internal (tier 2)

Safe to call, and may change in a 1.x release with a CHANGELOG entry that
names the old spelling and the new one.

::: isocenter.entities.TrackedEntity
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.PhiStatus
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.ScanPolicy
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.DicomSequence
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.clone_sequences
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.exported_patient_id
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.iter_item_tree
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.normalize_study_date
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.resolve_item_path
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

::: isocenter.entities.SOURCE_SOP_UID_ATTR
    handler: python
    options:
      heading_level: 3
      show_root_full_path: false

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
with no Patient ID is held under, with its test. A class in this section
does not make every method rendered under it tier 1. The frozen methods
are `Instance.get_pixel_data()`, `set_pixel_data()`, `unload_pixel_data()`,
`discard_pixel_data()`, `get_waveform_data()` and `set_attr()`. The other
methods shown, such as `Instance.regenerate_uid()` and the sequence
methods, are tier 2. So are `pixel_array` and `waveform_array` in
`Instance`'s attribute table.

**What `set_attr()` stores.** It stores the value it is given, with one
exception. It stores a numpy bool, integer or float (a scalar or a 0-d
array) as the Python number it equals. It stores a `list` or `tuple`
that directly holds a numpy value as a new `list`, with each numpy
number in it replaced in the same way. So `set_attr("0018,1150",
np.int64(7))` leaves the `int` 7 in `attributes`, and
`set_attr("0018,1150", (np.int64(7),))` leaves the `list` `[7]`. A live
export writes what a reopened session writes. An array of one or more
dimensions is not converted. Pass `.tolist()` or `.tobytes()` instead. No other numpy value is converted
either. A `datetime64`, a `timedelta64`, a complex number and a masked
value stay as given, and the save refuses them.

The entity does not see a value written straight into `attributes`
(call `mark_modified()`). A numpy number written that way is converted
when it is saved. `export()` writes such a number as the Python number
it equals, whether or not it was saved. So the file is the same before
and after a save and a reopen. `export()` also writes a `tuple` as the
list it equals, and a finite `Decimal` that a float can hold as the
`float` it equals. Under a standard element whose VR is `AT`, there is
one exception. `export()` writes a `tuple` of exactly two integers, or
of exactly two `str`, as one tag, as pydicom reads it (#1009). The pair
must still be a `tuple` when the export reads it. After `set_attr()`, a
`tuple` that holds a numpy number is a `list` (above), and a `list` of
two integers is two tags. A store that was saved and opened again also
holds the pair as a `list`
([#1016](https://github.com/kvnlng/Isocenter/issues/1016), open). The
save refuses a `Decimal`, and only a value written straight into
`attributes`, or a graph built by hand, can hold one. `export()` changes
none of the three in `attributes`.

A number under a standard element whose VR holds text
(`set_attr("0008,1090", 7)`) is not written. The file is exported
without that element, under one `DATA_LOSS` row that names the tag, the
type and the VR. Pass the text instead. The store holds `None`, `str`,
`bytes`, `bool`, `int`, `float`, pydicom's DS and IS values, and lists
of those. `set_attr()` does not raise for a value of another type. The
next `save(sync=True)`, `export()` or `compact()` raises `TypeError`
that names the instance, the tag and the type
([API stability](stability.md), Exceptions). `Instance.set_attr()` can
raise `ValueError` for an edit to a pixel descriptor, which is a
different check ([API stability](stability.md)).

**What assigning `Instance.sop_instance_uid` does.** It moves the field
and the `(0008,0018)` element together, and marks the instance edited.
For example, `instance.sop_instance_uid = "1.2.3.4"` leaves `"1.2.3.4"`
in `instance.attributes["0008,0018"]`, and the export writes the file as
`1.2.3.4.dcm` with that UID inside. The export does not write an
instance assigned `''`. Its file-name rule refuses a UID that cannot
name a file, and reports the instance as a failure keyed `UNKNOWN`.
Assigning the value that the field already holds changes nothing.
`series.series_instance_uid`, `study.study_instance_uid` and
`patient.patient_id` need no such step. The export stamps each file's
copy from the owner.

```{eval-rst}
.. automodule:: isocenter.entities
   :members: NO_PATIENT_ID_PREFIX, is_synthetic_patient_id
```

`NO_PATIENT_ID_PREFIX` is the start of the key a subject whose files
carry no Patient ID is held under. The key never reaches an exported
file.

```{eval-rst}
.. autoclass:: isocenter.entities.Patient
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.entities.Study
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.entities.Series
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.entities.Instance
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.entities.DicomItem
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.entities.Equipment
   :members:
```

## Documented but internal (tier 2)

These names are safe to call. They may change in a 1.x release, with a
CHANGELOG entry that names the old spelling and the new one.

```{eval-rst}
.. autoclass:: isocenter.entities.TrackedEntity
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.entities.PhiStatus
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.entities.ScanPolicy
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.entities.DicomSequence
   :members:
```

```{eval-rst}
.. autofunction:: isocenter.entities.clone_sequences
```

```{eval-rst}
.. autofunction:: isocenter.entities.exported_patient_id
```

```{eval-rst}
.. autofunction:: isocenter.entities.iter_item_tree
```

```{eval-rst}
.. autofunction:: isocenter.entities.normalize_study_date
```

```{eval-rst}
.. autofunction:: isocenter.entities.resolve_item_path
```

```{eval-rst}
.. autodata:: isocenter.entities.SOURCE_SOP_UID_ATTR
```

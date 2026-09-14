"""`write_tree` stamps what `session.export()` stamps, and nothing else (#570).

`DicomExporter.write_tree()` built its own patient, study and series
attributes instead of the session's, and the two sets disagreed.
Measured at c8b4f15 and again at 220a20f, 3.12.14:

- **Study Time.** `write_tree` wrote the literal `120000` on every file:
  over CT_small's real `072730`, and in place of a Study Time the source
  never had. `session.export()` kept the instance's own value -- and
  refused a CT with no Study Time at all, `ValueError: Validation Errors:
  ['[Type 2 Error] Missing 0008,0030 in Common']`, before and after
  `anonymize()`.
- **Equipment, PHI-visible.** `write_tree` re-stamped Manufacturer, Model
  Name and Device Serial Number from `Series.equipment`. After
  `anonymize()` the instance's `(0018,1000)` is empty, while
  `Series.equipment` keeps the serial because `redact()` matches rules
  on it -- so `write_tree` put the scanner's real serial `SN-570` back
  into a de-identified file that `session.export()` wrote empty.
- **Series Number None.** The session stamped `str(None)`, which IS
  refuses, so the element was dropped with a `DATA_LOSS` row;
  `write_tree` wrote it zero-length. (Measured on the same base.)

One helper, `export_stamp_attributes`, now builds the stamps for both
doors, with no literal time and no equipment. A study with no time is
written with a zero-length Study Time -- Type 2's "unknown" -- by the
shared worker, on both doors, and only where nothing else supplied one.
`SeriesBuilder` writes its equipment onto the instances, so a hand-built
graph's files still carry it and the graph says what the file says.
"""
import itertools
import os
from datetime import date

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter import Builder
from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import DicomExporter
from isocenter.session import DicomSession

OT_STORAGE = "1.2.840.10008.5.1.4.1.1.7"
_serial = itertools.count(1)


def _files(root):
    return sorted(os.path.relpath(os.path.join(d, f), root)
                  for d, _, names in os.walk(root)
                  for f in names if f.endswith(".dcm"))


def _read_one(root):
    names = _files(root)
    assert len(names) == 1, names
    return pydicom.dcmread(os.path.join(root, names[0]))


def _ct_source(directory, *, serial="SN-570", study_time=True):
    """CT_small with a device serial, optionally without its Study Time."""
    directory.mkdir(parents=True, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    if not study_time:
        del ds.StudyTime
    ds.DeviceSerialNumber = serial
    ds.save_as(str(directory / "ct.dcm"))
    return directory


def _image(extra=()):
    inst = Instance(f"1.2.826.0.1.570.{next(_serial)}", OT_STORAGE, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230102"), ("0008,0060", "OT"),
                       ("0028,0002", 1), ("0028,0004", "MONOCHROME2"),
                       *extra):
        inst.set_attr(tag, value)
    inst.set_pixel_data(np.arange(64, dtype=np.uint16).reshape(8, 8))
    return inst


def _hand_built(instance, *, study_time=None, study_date=date(2023, 1, 2),
                series_number=3):
    patient = Patient("PAT570", "Doe^Jane")
    study = Study("1.2.826.0.2.570", study_date)
    study.study_time = study_time
    series = Series("1.2.826.0.3.570", "OT", series_number)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _session_export(tmp_path, patient, name="via_session"):
    """`session.export()` over a hand-built graph; the summary and root."""
    out = tmp_path / name
    with DicomSession(str(tmp_path / f"{name}.db")) as session:
        session.store.patients.append(patient)
        session.save()
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
    return summary, out


def _tree_export(tmp_path, patient, name="via_tree"):
    out = tmp_path / name
    DicomExporter.write_tree(patient, str(out), show_progress=False)
    return out


# ---------------------------------------------------------------------------
# Study Time
# ---------------------------------------------------------------------------

def test_write_tree_keeps_a_real_study_time(tmp_path):
    """CT_small's `072730` reaches the `write_tree` file. Killing mutation:
    the `120000` literal restored in `_generate_export_contexts`."""
    source = _ct_source(tmp_path / "src")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        out = _tree_export(tmp_path, session.store.patients[0])

    assert _read_one(out).StudyTime == "072730"


def test_an_absent_study_time_is_written_empty_on_both_doors(tmp_path):
    """No time on the study and none on the instance: both doors write a
    **present, zero-length** Study Time (Type 2 "unknown"), and the
    session delivers the file. Killing mutations: the fill deleted (the
    session refuses `Missing 0008,0030`); the fill writing `120000`."""
    summary, via_session = _session_export(tmp_path, _hand_built(_image()))
    via_tree = _tree_export(tmp_path, _hand_built(_image()))

    assert summary.written == 1, summary.failures
    for root in (via_session, via_tree):
        written = _read_one(root)
        assert "StudyTime" in written
        assert written["StudyTime"].VR == "TM"
        assert written.StudyTime == ""


def test_a_ct_without_study_time_exports_through_the_session(tmp_path):
    """The pre-existing half of #570: an ingested CT with no Study Time
    used to fail `session.export()` 0 of 1 on the Type 2 check. Killing
    mutation: the fill deleted."""
    source = _ct_source(tmp_path / "src", study_time=False)
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)

    assert summary.written == 1, summary.failures
    written = _read_one(out)
    assert "StudyTime" in written and written.StudyTime == ""


def test_the_fill_does_not_overwrite_the_instance_time(tmp_path):
    """The instance's own `072730` and no time on the study: both doors
    keep `072730`. Killing mutation: the fill made unconditional."""
    summary, via_session = _session_export(
        tmp_path, _hand_built(_image([("0008,0030", "072730")])))
    via_tree = _tree_export(
        tmp_path, _hand_built(_image([("0008,0030", "072730")])))

    assert summary.written == 1, summary.failures
    assert _read_one(via_session).StudyTime == "072730"
    assert _read_one(via_tree).StudyTime == "072730"


def test_a_study_time_on_the_study_is_stamped_on_both_doors(tmp_path):
    """`Study.study_time` is stamped over the instance's value on both
    doors -- the session's rule, which `write_tree` now shares. Killing
    mutation: the helper not stamping `study_time`."""
    image = [("0008,0030", "072730")]
    summary, via_session = _session_export(
        tmp_path, _hand_built(_image(image), study_time="101010"))
    via_tree = _tree_export(
        tmp_path, _hand_built(_image(image), study_time="101010"))

    assert summary.written == 1, summary.failures
    assert _read_one(via_session).StudyTime == "101010"
    assert _read_one(via_tree).StudyTime == "101010"


# ---------------------------------------------------------------------------
# One helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("study_date", [date(2023, 1, 2), "20230102", None])
def test_a_study_date_of_any_shape_is_the_same_on_both_doors(
        tmp_path, study_date):
    """The session passed `study_date` raw and `write_tree` formatted it;
    the helper formats. A `date`, a string and None each reach both files
    identically. Killing mutation: one door keeping its own date stamp."""
    summary, via_session = _session_export(
        tmp_path, _hand_built(_image(), study_date=study_date))
    via_tree = _tree_export(
        tmp_path, _hand_built(_image(), study_date=study_date))

    assert summary.written == 1, summary.failures
    expected = "" if study_date is None else "20230102"
    assert _read_one(via_session).StudyDate == expected
    assert _read_one(via_tree).StudyDate == expected


def test_a_series_with_no_number_writes_an_empty_series_number(tmp_path):
    """Series Number is Type 2. The session stamped `str(None)`, which IS
    refuses, so the element was dropped with a `DATA_LOSS` row. Both doors
    now write it present and empty. Killing mutation: `str(None)`
    restored in the helper."""
    summary, via_session = _session_export(
        tmp_path, _hand_built(_image(), series_number=None))
    via_tree = _tree_export(tmp_path, _hand_built(_image(), series_number=None))

    assert summary.written == 1, summary.failures
    for root in (via_session, via_tree):
        written = _read_one(root)
        assert "SeriesNumber" in written
        assert written["SeriesNumber"].value in (None, "")


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------

def test_write_tree_after_anonymize_writes_no_series_serial(tmp_path):
    """The PHI half. After `anonymize()` the instance's serial is empty
    and `Series.equipment` still holds `SN-570` (redaction matches on it).
    `write_tree` writes the instance's value, and the serial appears
    nowhere in the file's bytes. Killing mutation: the equipment block
    restored in `_generate_export_contexts`."""
    source = _ct_source(tmp_path / "src")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        session.anonymize()
        patient = session.store.patients[0]
        series = patient.studies[0].series[0]
        assert series.equipment.device_serial_number == "SN-570"
        out = _tree_export(tmp_path, patient)

    names = _files(out)
    assert len(names) == 1, names
    raw = (out / names[0]).read_bytes()
    assert b"SN-570" not in raw
    assert _read_one(out).get("DeviceSerialNumber", "") == ""


def test_the_builder_puts_equipment_on_its_instances(tmp_path):
    """`SeriesBuilder` writes the three tags onto each instance, whether
    `set_equipment` comes before or after `add_instance`, and the
    `write_tree` file carries them from the instance. Killing mutations:
    the copy in `add_instance` deleted; the copy in `set_equipment`
    deleted (the instance added first loses them)."""
    series = (Builder.start_patient("PAT570", "Doe^Jane")
              .add_study("1.2.826.0.2.571", "20230102")
              .add_series("1.2.826.0.3.571", "OT", 1))
    first = series.add_instance("1.2.826.0.1.571.1", OT_STORAGE, 1).instance
    series.set_equipment("ACME", "Model-9", "SN-9")
    second = series.add_instance("1.2.826.0.1.571.2", OT_STORAGE, 2).instance

    for inst in (first, second):
        assert inst.attributes["0008,0070"] == "ACME"
        assert inst.attributes["0008,1090"] == "Model-9"
        assert inst.attributes["0018,1000"] == "SN-9"

    for inst in (first, second):
        for tag, value in (("0028,0002", 1), ("0028,0004", "MONOCHROME2")):
            inst.set_attr(tag, value)
        inst.set_pixel_data(np.zeros((4, 4), np.uint8))
    patient = series.end_series().end_study().build()
    out = _tree_export(tmp_path, patient)

    names = _files(out)
    assert len(names) == 2, names
    for name in names:
        written = pydicom.dcmread(os.path.join(out, name))
        assert written.Manufacturer == "ACME"
        assert written.ManufacturerModelName == "Model-9"
        assert written.DeviceSerialNumber == "SN-9"


def test_the_builder_writes_only_the_parts_it_was_given(tmp_path):
    """An empty part is not written, and a call with neither manufacturer
    nor model builds no `Equipment` (#290) and writes nothing. The latest
    call wins over an earlier `set_attribute`, and a later
    `set_attribute` wins over it. Killing mutation: empty parts written
    as zero-length elements."""
    series = (Builder.start_patient("PAT570", "Doe^Jane")
              .add_study("1.2.826.0.2.572", "20230102")
              .add_series("1.2.826.0.3.572", "OT", 1))
    context = series.add_instance("1.2.826.0.1.572.1", OT_STORAGE, 1)
    inst = context.instance

    series.set_equipment("ACME", "", "")
    assert inst.attributes["0008,0070"] == "ACME"
    assert "0008,1090" not in inst.attributes
    assert "0018,1000" not in inst.attributes

    other = (Builder.start_patient("PAT571", "Doe^John")
             .add_study("1.2.826.0.2.573", "20230102")
             .add_series("1.2.826.0.3.573", "OT", 1))
    lone = other.add_instance("1.2.826.0.1.573.1", OT_STORAGE, 1).instance
    other.set_equipment("", "", "SN-ONLY")
    assert other.series.equipment is None
    assert not {"0008,0070", "0008,1090", "0018,1000"} & set(lone.attributes)

    context.set_attribute("0018,1000", "SN-EARLIER")
    series.set_equipment("ACME", "Model-9", "SN-LATER")
    assert inst.attributes["0018,1000"] == "SN-LATER"
    context.set_attribute("0018,1000", "SN-EDITED")
    assert inst.attributes["0018,1000"] == "SN-EDITED"

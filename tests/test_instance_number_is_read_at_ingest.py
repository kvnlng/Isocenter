"""`Instance.instance_number` is the file's InstanceNumber after ingest (#810).

Until #810 ingest built every instance as `Instance(sop, sop_class, 0, ...)`,
so the field was 0 for every file while the attribute `0020,0013`, and the
exported DICOM, carried the file's value. Three readers take the field, not
the attribute: the WFDB record name (`<patient>_<series>_<instance>`), the
store's `instance_number` column and the scan worker's clone. All three
said 0.

The owner's ruling: read InstanceNumber at ingest, 0 when the file has
none. A value that is not one DICOM IS integer -- empty, multi-valued,
not a number, outside IS's range -- is read as none, and ingest goes on:
an ill-formed Instance Number is not a reason to refuse the file. The
attribute is the file's element either way and is not touched by this.
"""
import os
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter.io_handlers import ingest_worker
from isocenter.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture

# A value no other element of CT_small.dcm holds, so a byte edit of the
# written file can find it uniquely. Even length, so the element's length
# field is untouched by an edit of the same length.
_MARKER = "97531 "


def _ct_with_instance_number(path, value):
    """CT_small.dcm with InstanceNumber set to `value`, or removed for None."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    if value is None:
        del ds.InstanceNumber
    else:
        ds.InstanceNumber = value
    ds.save_as(path)
    return path


def _ct_with_raw_instance_number(path, raw: bytes):
    """CT_small.dcm whose InstanceNumber bytes are `raw`, written past pydicom.

    pydicom refuses to build an IS from text that is not a number, so the
    file is written with a marker and the marker's bytes replaced.
    """
    assert len(raw) == len(_MARKER)
    _ct_with_instance_number(path, _MARKER.strip())
    with open(path, "rb") as f:
        data = f.read()
    assert data.count(_MARKER.encode()) == 1, "the marker is not unique"
    with open(path, "wb") as f:
        f.write(data.replace(_MARKER.encode(), raw))
    return path


def _ingested(path):
    meta, inst, *_rest, error = ingest_worker(str(path))
    assert error is None, f"ingest refused the file: {error}"
    assert inst is not None
    return inst


def test_the_files_instance_number_is_the_instances(tmp_path):
    inst = _ingested(_ct_with_instance_number(tmp_path / "a.dcm", "7"))
    assert inst.instance_number == 7
    assert type(inst.instance_number) is int, (
        "an IS subclass would be stored and pickled as pydicom's type")
    # The attribute is the file's element, as it was before #810.
    assert inst.attributes["0020,0013"] == 7
    assert isinstance(inst.attributes["0020,0013"], pydicom.valuerep.IS)


def test_a_file_with_no_instance_number_reads_as_zero(tmp_path):
    inst = _ingested(_ct_with_instance_number(tmp_path / "a.dcm", None))
    assert inst.instance_number == 0


@pytest.mark.parametrize("value", ["", ["3", "4"]], ids=["empty", "multi-valued"])
def test_an_instance_number_that_is_not_one_value_reads_as_zero(tmp_path, value):
    path = _ct_with_instance_number(tmp_path / "a.dcm", value)
    inst = _ingested(path)
    assert inst.instance_number == 0


@pytest.mark.parametrize("raw", [b"ab12cd", b"999999", b"4.5   "],
                         ids=["not-a-number", "six-digit-control", "fraction"])
def test_an_ill_formed_instance_number_does_not_refuse_the_file(tmp_path, raw):
    path = _ct_with_raw_instance_number(tmp_path / "a.dcm", raw)
    inst = _ingested(path)
    if raw == b"999999":
        # The control: a well-formed value written by the same byte edit
        # is read, so the zeros beside it are the reader's, not the edit's.
        assert inst.instance_number == 999999
    else:
        assert inst.instance_number == 0


def test_an_instance_number_outside_the_is_range_reads_as_zero(tmp_path):
    """IS holds -2**31 .. 2**31-1. A value past it is not one, and one long
    enough would not fit the store's INTEGER column either."""
    marker = "000000097531"
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.InstanceNumber = marker
    path = tmp_path / "a.dcm"
    ds.save_as(path)
    with open(path, "rb") as f:
        data = f.read()
    assert data.count(marker.encode()) == 1
    with open(path, "wb") as f:
        f.write(data.replace(marker.encode(), b"2147483648  "))
    inst = _ingested(path)
    assert inst.instance_number == 0


def test_the_store_column_holds_the_files_instance_number(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _ct_with_instance_number(src / "a.dcm", "12")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.save(sync=True)
    with sqlite3.connect(tmp_path / "s.db") as conn:
        rows = conn.execute("SELECT instance_number FROM instances").fetchall()
    assert rows == [(12,)]
    with DicomSession(str(tmp_path / "s.db")) as reopened:
        (inst,) = [i for p in reopened.store.patients for st in p.studies
                   for se in st.series for i in se.instances]
        assert inst.instance_number == 12


def test_the_wfdb_record_name_carries_the_files_instance_number(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=200)
    assert pydicom.dcmread(src / "ecg.dcm").InstanceNumber == 1
    with DicomSession(str(tmp_path / "w.db")) as session:
        session.ingest(str(src))
        session.anonymize()
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    assert len(paths) == 1
    record = os.path.basename(paths[0]).removesuffix(".hea")
    assert record.endswith("_1"), (
        f"record {record!r} does not end in the file's InstanceNumber, 1")

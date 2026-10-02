"""`export_dataframe("....parquet", expand_metadata=True)` writes a multi-valued tag (#816).

Measured on `main` at 7579d4df (pandas 3.0.5, pyarrow 25.0.1) over
`CT_small` and `MR_small`: the call raised `pyarrow.lib.ArrowInvalid:
Could not convert ['ORIGINAL', 'PRIMARY', 'AXIAL'] with type MultiValue`
on column `0008,0008`. The CSV arm wrote the same frame.

The owner's ruling (Q6-B) is an Arrow list column where a column's values
are uniform, and text otherwise, with nulls kept. "Uniform" is defined in
`session._parquet_column_shape`'s docstring, and each arm is pinned here:

* the session tests over real files: the list arm (`0008,0008` as
  `list<string>`, `0020,0032` as `list<double>`, a null for a file
  without the tag), a mixed column as text equal to the CSV's cell, and
  a scalar column of one family passed through with its Arrow type;
* the schema of a non-expanded file, recorded from `main` before the
  fix, so a rule that touched every object column is red;
* a table of hand-built columns for each line of the definition,
  including the two mixes pyarrow accepts *silently and lossily* (a
  `datetime` beside a `date` is truncated to the day, a `str` beside
  `bytes` is encoded), which the rule writes as text instead.

Read back with `pyarrow.parquet`, never by comparing a frame to itself:
the assertion is about what the file holds.
"""
import datetime
import os
import shutil

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.multival import MultiValue
from pydicom.uid import generate_uid
from pydicom.valuerep import DSfloat, IS, PersonName

from isocenter import session as session_module
from isocenter.session import DicomSession

#: The non-expanded file's schema, measured on `main` at 7579d4df over
#: CT_small + MR_small (pandas 3.0.5, pyarrow 25.0.1). The rule must not
#: move it: every base column holds one family of scalar.
BASE_SCHEMA = [
    ("PatientID", "large_string"), ("PatientName", "large_string"),
    ("StudyInstanceUID", "large_string"), ("StudyDate", "date32[day]"),
    ("SeriesInstanceUID", "large_string"), ("Modality", "large_string"),
    ("SOPInstanceUID", "large_string"), ("Manufacturer", "large_string"),
    ("Model", "large_string"), ("DeviceSerial", "large_string"),
]

MIXED_TAG = "0010,4000"  # Patient Comments: a str on one row, an int on another


def _inputs(tmp_path):
    src = tmp_path / "input"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(src / "CT_small.dcm"))
    shutil.copy(get_testdata_file("MR_small.dcm"), str(src / "MR_small.dcm"))
    # A second CT with no Image Type, so the list column holds a null.
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    del ds.ImageType
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.save_as(str(src / "CT_noimagetype.dcm"))
    return str(src)


def _session(tmp_path):
    session = DicomSession(persistence_file=str(tmp_path / "pq.db"))
    session.ingest(_inputs(tmp_path))
    by_file = {os.path.basename(i.source_path): i
               for p in session.store.patients for st in p.studies
               for se in st.series for i in se.instances}
    by_file["CT_small.dcm"].set_attr(MIXED_TAG, "free text")
    by_file["CT_noimagetype.dcm"].set_attr(MIXED_TAG, 7)
    return session, {name: inst.sop_instance_uid
                     for name, inst in by_file.items()}


def _rows(table):
    data = table.to_pydict()
    return {uid: {name: data[name][i] for name in data}
            for i, uid in enumerate(data["SOPInstanceUID"])}


def test_an_expanded_cohort_writes_multi_valued_tags_as_lists(tmp_path):
    session, uids = _session(tmp_path)
    out = str(tmp_path / "cohort.parquet")
    try:
        session.export_dataframe(out, expand_metadata=True)
        source = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    finally:
        session.close()

    table = pq.read_table(out)
    schema = {f.name: str(f.type) for f in table.schema}
    assert schema["0008,0008"] in ("list<element: large_string>",
                                   "list<element: string>",
                                   "list<item: string>",
                                   "list<item: large_string>"), schema["0008,0008"]
    assert pa.types.is_list(table.schema.field("0020,0032").type)
    assert pa.types.is_floating(table.schema.field("0020,0032").type.value_type)
    # A scalar column of one family keeps its Arrow type.
    assert pa.types.is_integer(table.schema.field("0028,0010").type)
    assert schema["StudyDate"] == "date32[day]"

    rows = _rows(table)
    ct = rows[uids["CT_small.dcm"]]
    assert ct["0008,0008"] == list(source.ImageType)
    assert ct["0020,0032"] == [float(v) for v in source.ImagePositionPatient]
    assert rows[uids["CT_noimagetype.dcm"]]["0008,0008"] is None


def test_a_mixed_column_is_written_as_the_csvs_text_with_nulls_kept(tmp_path):
    session, uids = _session(tmp_path)
    pq_out = str(tmp_path / "cohort.parquet")
    csv_out = str(tmp_path / "cohort.csv")
    try:
        session.export_dataframe(pq_out, expand_metadata=True)
        session.export_dataframe(csv_out, expand_metadata=True)
    finally:
        session.close()

    table = pq.read_table(pq_out)
    assert pa.types.is_string(table.schema.field(MIXED_TAG).type) or \
        pa.types.is_large_string(table.schema.field(MIXED_TAG).type)
    rows = _rows(table)
    csv = pd.read_csv(csv_out, dtype=str).set_index("SOPInstanceUID")

    for name in ("CT_small.dcm", "CT_noimagetype.dcm"):
        uid = uids[name]
        assert rows[uid][MIXED_TAG] == csv.loc[uid, MIXED_TAG]
    assert rows[uids["CT_noimagetype.dcm"]][MIXED_TAG] == "7"
    assert rows[uids["MR_small.dcm"]][MIXED_TAG] is None, (
        "a missing value was written as text (the 'nan' a blanket str() "
        "writes), not as a null")
    assert pd.isna(csv.loc[uids["MR_small.dcm"], MIXED_TAG])


def test_a_non_expanded_file_keeps_its_schema(tmp_path):
    session, _ = _session(tmp_path)
    out = str(tmp_path / "base.parquet")
    try:
        session.export_dataframe(out)
    finally:
        session.close()
    assert [(f.name, str(f.type)) for f in pq.read_schema(out)] == BASE_SCHEMA


def _written(values):
    """Write one object column through the rule and read its Arrow type and values."""
    df = pd.DataFrame({"c": pd.Series(values, dtype=object)})
    table = pa.Table.from_pandas(session_module._parquet_safe(df),
                                 preserve_index=False)
    return table.schema.field("c").type, table.column("c").to_pylist()


D = datetime.date(2020, 1, 2)
DT = datetime.datetime(2020, 1, 2, 3, 4, 5)


@pytest.mark.parametrize("values, expected", [
    # The list arm: every non-null cell a sequence, every non-null
    # element one family. Null cells and null elements stay null.
    pytest.param([MultiValue(str, ["A", "B"]), None, ("C",)],
                 [["A", "B"], None, ["C"]], id="list-of-str"),
    pytest.param([MultiValue(DSfloat, ["1.5", "2"]), [IS("3")], np.nan],
                 [[1.5, 2.0], [3.0], None], id="list-of-numbers"),
    pytest.param([["A", None], []], [["A", None], []], id="null-element-and-empty"),
], )
def test_a_uniform_sequence_column_is_an_arrow_list(values, expected):
    arrow_type, written = _written(values)
    assert pa.types.is_list(arrow_type), arrow_type
    assert written == expected


@pytest.mark.parametrize("values, expected", [
    pytest.param(["a", 1, None], ["a", "1", None], id="str-and-int"),
    pytest.param([True, 1], ["True", "1"], id="bool-and-int"),
    pytest.param([D, DT, np.nan], [str(D), str(DT), None],
                 id="date-and-datetime"),
    pytest.param([b"x", "y"], [str(b"x"), "y"], id="bytes-and-str"),
    pytest.param([DSfloat("40"), MultiValue(DSfloat, ["40", "400"]), None],
                 ["40", str(MultiValue(DSfloat, ["40", "400"])), None],
                 id="scalar-and-sequence"),
    pytest.param([["a", 1], ["b"]], [str(["a", 1]), str(["b"])],
                 id="sequence-of-mixed-elements"),
    pytest.param([[["a"]], None], [str([["a"]]), None], id="nested-sequence"),
    pytest.param([PersonName("Doe^John"), pd.NA], ["Doe^John", None],
                 id="not-a-scalar"),
    pytest.param([2 ** 64 - 1, None], [str(2 ** 64 - 1), None],
                 id="int-wider-than-int64"),
    # pyarrow writes these two as one timestamp column and drops the
    # offset (review of #899), so they are two families.
    pytest.param([DT.replace(tzinfo=datetime.timezone.utc), DT],
                 [str(DT.replace(tzinfo=datetime.timezone.utc)), str(DT)],
                 id="aware-and-naive-datetime"),
    # A 0-d array is neither a sequence (it cannot be iterated) nor a
    # Python scalar.
    pytest.param([np.array(5), None], ["5", None], id="zero-d-array"),
])
def test_a_column_that_is_not_uniform_is_text(values, expected):
    arrow_type, written = _written(values)
    assert pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type), arrow_type
    assert written == expected


@pytest.mark.parametrize("values, check", [
    pytest.param([1, 2.5, None], pa.types.is_floating, id="int-and-float"),
    pytest.param([IS("1"), IS("2")], pa.types.is_integer, id="ints"),
    pytest.param([D, None], pa.types.is_date, id="dates"),
    pytest.param([b"x", np.nan], pa.types.is_binary, id="bytes"),
    pytest.param([None, np.nan], pa.types.is_null, id="all-null"),
])
def test_a_column_of_one_scalar_family_is_passed_through(values, check):
    df = pd.DataFrame({"c": pd.Series(values, dtype=object)})
    safe = session_module._parquet_safe(df)
    assert safe["c"].tolist() == df["c"].tolist() or \
        all(pd.isna(a) and pd.isna(b) for a, b in zip(safe["c"], df["c"]))
    assert check(_written(values)[0])

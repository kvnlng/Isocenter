import pytest
import pandas as pd
import os
import sqlite3
from isocenter.session import DicomSession
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.persistence import IsocenterJSONEncoder
import json

@pytest.fixture
def session_with_data(tmp_path):
    db_path = tmp_path / "isocenter_test.db"
    session = DicomSession(str(db_path))

    # Manually populate the database with some hierarchical data
    # We use SQL directly to simulate a populated state, or use the object model if possible.
    # Using SQL directly is more robust for testing the persistence Read path specifically.

    # Or better: Create objects and use save_all.
    p = Patient("P1", "Test Patient")
    st = Study("ST1", "20230101")
    se = Series("SE1", "CT", 101, equipment=None)
    inst = Instance("I1", "1.2.840.123", 1)
    inst.file_path = "/tmp/fake.dcm"
    inst.attributes = {"PatientName": "Test Patient", "Modality": "CT", "SliceThickness": 1.5}

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)

    session.store.patients = [p]
    session.save() # This triggers the full save pipeline
    session.persistence_manager.flush() # Wait for DB write

    yield session
    session.close()

def test_export_dataframe_basic(session_with_data):
    df = session_with_data.export_dataframe()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    assert df.iloc[0]['PatientID'] == "P1"
    assert df.iloc[0]['SOPInstanceUID'] == "I1"
    # Check default columns exist
    expected_cols = ['PatientID', 'StudyInstanceUID', 'SeriesInstanceUID', 'SOPInstanceUID']
    for col in expected_cols:
        assert col in df.columns

def test_export_dataframe_parquet(session_with_data, tmp_path):
    output_path = tmp_path / "export.parquet"
    df = session_with_data.export_dataframe(str(output_path))

    assert os.path.exists(output_path)

    # Verify we can read it back
    df_read = pd.read_parquet(output_path)
    assert len(df_read) == 1
    assert df_read.iloc[0]['PatientID'] == "P1"

def test_export_dataframe_expand_metadata(session_with_data):
    # This requires us to modify the implementation to actually parse the JSON if expand_metadata=True
    # For now, let's assume we implement it or at least call it.
    df = session_with_data.export_dataframe(expand_metadata=True)

    # If expansion works, we should see "SliceThickness" as a column or at least check logic
    # The current plan is to implement it, so let's assert it.

    # Note: sqlite persistence stores attributes_json.
    # Our mocked data had SliceThickness = 1.5

    assert 'SliceThickness' in df.columns
    assert df.iloc[0]['SliceThickness'] == 1.5

def test_get_flattened_instances_still_has_a_caller_holding_its_coverage(
        session_with_data):
    """`SqliteStore.get_flattened_instances` has no production caller (#142).

    It had exactly one, `DicomSession.export_to_parquet`, deleted in #55
    when Parquet export was collapsed onto `export_dataframe`. This is
    the second time the method has been left uncovered by a deletion
    elsewhere: `DicomExporter.generate_export_from_db` went first, as
    dead code, and took the only test exercising the method with it.

    So the coverage is anchored here, on the method itself, rather than
    on whatever happens to call it this month. Whether the method should
    survive with no caller is #142's question -- but it must not be
    possible to delete it *by accident*, which is what losing the last
    test would amount to.
    """
    rows = list(session_with_data.store_backend.get_flattened_instances(["P1"]))

    assert len(rows) == 1
    # The DB path speaks SQL column names, unlike `get_cohort_report`'s
    # DICOM keywords -- the divergence that made two Parquet writers
    # produce two different frames.
    assert rows[0]["patient_id"] == "P1"
    assert rows[0]["sop_instance_uid"] == "I1"
    assert rows[0]["modality"] == "CT"


def test_get_flattened_instances_filters_by_patient_id(session_with_data):
    assert list(session_with_data.store_backend.get_flattened_instances(["NOPE"])) == []


@pytest.fixture
def session_with_two_patients(tmp_path):
    """Two patients, so a filter has something to exclude."""
    session = DicomSession(str(tmp_path / "cohort.db"))

    for pid in ("P1", "P2"):
        p = Patient(pid, f"Patient {pid}")
        st = Study(f"ST-{pid}", "20230101")
        se = Series(f"SE-{pid}", "CT", 101, equipment=None)
        inst = Instance(f"I-{pid}", "1.2.840.123", 1)
        inst.attributes = {"Modality": "CT"}
        se.instances.append(inst)
        st.series.append(se)
        p.studies.append(st)
        session.store.patients.append(p)

    session.save()
    session.persistence_manager.flush()

    yield session
    session.close()


def test_get_cohort_report_filters_by_patient_ids(session_with_two_patients):
    df = session_with_two_patients.get_cohort_report(patient_ids=["P1"])

    assert list(df["PatientID"]) == ["P1"]


def test_get_cohort_report_without_a_filter_returns_everyone(session_with_two_patients):
    df = session_with_two_patients.get_cohort_report()

    assert sorted(df["PatientID"]) == ["P1", "P2"]


def test_an_empty_patient_id_list_selects_nobody(session_with_two_patients):
    """`[]` is a filter that matched nothing, not a missing filter.

    `None` means "no filter given". Collapsing `[]` into it would make
    a caller passing a computed-and-empty cohort export the whole
    dataset -- the one outcome a filter exists to prevent.
    """
    df = session_with_two_patients.get_cohort_report(patient_ids=[])

    assert df.empty


def test_export_dataframe_filters_by_patient_ids(session_with_two_patients, tmp_path):
    output_path = tmp_path / "one.csv"
    df = session_with_two_patients.export_dataframe(
        str(output_path), patient_ids=["P2"])

    assert list(df["PatientID"]) == ["P2"]
    assert list(pd.read_csv(output_path)["PatientID"]) == ["P2"]


def test_export_dataframe_creates_a_missing_output_directory(session_with_data, tmp_path):
    """Carried over from `export_to_parquet`, which did this and
    `export_dataframe` did not. Without it a caller migrating a nested
    path gets a bare `FileNotFoundError` out of pandas."""
    output_path = tmp_path / "reports" / "2026" / "cohort.parquet"

    session_with_data.export_dataframe(str(output_path))

    assert os.path.exists(output_path)


def test_an_empty_cohort_still_writes_a_file(session_with_two_patients, tmp_path):
    """`export_to_parquet` returned early and wrote nothing here (#55).

    A scheduled job reading this path would then pick up the *previous*
    run's file and treat stale rows as current. Writing the empty frame
    makes "nothing matched" visible downstream.
    """
    output_path = tmp_path / "empty.csv"
    df = session_with_two_patients.export_dataframe(
        str(output_path), patient_ids=["NO-SUCH-PATIENT"])

    assert df.empty
    assert os.path.exists(output_path)


def test_an_empty_cohort_keeps_its_columns(session_with_two_patients, tmp_path):
    """An empty result must still have a schema.

    Writing the empty frame is only useful if a reader can treat it as
    "no rows". A bare `pd.DataFrame([])` has no columns at all, so the
    obvious downstream `df[df.Modality == "CT"]` raises `AttributeError`
    on an empty export while working on every non-empty one -- a failure
    that shows up only when the filter matched nothing.
    """
    output_path = tmp_path / "empty.parquet"
    df = session_with_two_patients.export_dataframe(
        str(output_path), patient_ids=["NO-SUCH-PATIENT"])

    assert df.empty
    assert list(df["PatientID"]) == []
    assert list(pd.read_parquet(output_path).columns) == list(df.columns)


def test_expand_metadata_still_adds_columns_beyond_the_fixed_set(session_with_data):
    """The fixed column list must not become a whitelist that clips
    expanded attributes back out."""
    df = session_with_data.export_dataframe(expand_metadata=True)

    assert "SliceThickness" in df.columns
    assert "PatientID" in df.columns


def test_the_declared_columns_match_the_rows_actually_built(session_with_data):
    """`COHORT_REPORT_COLUMNS` is only used when there are no rows, so
    nothing else would notice it drifting from the row dict. A column
    added to one and not the other would appear on every populated
    export and vanish on every empty one."""
    from isocenter.session import COHORT_REPORT_COLUMNS

    df = session_with_data.get_cohort_report()

    assert not df.empty, "fixture must produce rows or this pins nothing"
    assert list(df.columns) == COHORT_REPORT_COLUMNS


import pytest
import pandas as pd
from isocenter.session import DicomSession
from isocenter.entities import Patient, Study, Series, Instance, Equipment
from isocenter.io_handlers import DicomStore
from unittest.mock import MagicMock

@pytest.fixture
def populated_session():
    """Returns a session with a populated store."""
    session = DicomSession(":memory:")
    store = session.store

    # Create valid hierarchy
    p1 = Patient("P1", "Patient One")
    s1 = Study("S1.1", "20230101")
    se1 = Series("SE1.1", "CT", 0)
    se1.equipment = Equipment("GE MEDICAL SYSTEMS", "LightSpeed VCT", "12345")
    i1 = Instance("I1.1", "1.2.840.10008.5.1.4.1.1.2", 1)

    p1.studies.append(s1)
    s1.series.append(se1)
    se1.instances.append(i1)

    # Add another series with different equipment to same study
    se2 = Series("SE1.2", "MR", 0)
    se2.equipment = Equipment("SIEMENS", "Magnetom", "67890")
    i2 = Instance("I1.2", "1.2.840.10008.5.1.4.1.1.4", 1)
    s1.series.append(se2)
    se2.instances.append(i2)

    # Add second patient
    p2 = Patient("P2", "Patient Two")
    s2 = Study("S2.1", "20230202")
    se3 = Series("SE2.1", "CT", 0)
    se3.equipment = Equipment("GE MEDICAL SYSTEMS", "LightSpeed VCT", "11111") # Same model, diff serial
    i3 = Instance("I2.1", "1.2.840.10008.5.1.4.1.1.2", 1)

    p2.studies.append(s2)
    s2.series.append(se3)
    se3.instances.append(i3)

    store.patients.extend([p1, p2])
    return session

def test_inventory_output(populated_session, capsys):
    """Verifies inventory prints summary and grouped equipment."""
    populated_session.examine()
    captured = capsys.readouterr()

    # Check Summary
    assert "Inventory Summary:" in captured.out
    assert "Patients:  2" in captured.out
    assert "Studies:   2" in captured.out # S1.1, S2.1
    assert "Series:    3" in captured.out # SE1.1, SE1.2, SE2.1
    assert "Instances: 3" in captured.out

    # Check Equipment Grouping
    assert "Equipment Inventory:" in captured.out
    # GE LightSpeed should appear once with count 2 (from P1 and P2)
    assert "GE MEDICAL SYSTEMS - LightSpeed VCT" in captured.out
    assert "(Count: 2)" in captured.out

    # Siemens should appear once with count 1
    assert "SIEMENS - Magnetom" in captured.out
    assert "(Count: 1)" in captured.out

def test_cohort_report_dataframe(populated_session):
    """Verifies get_cohort_report returns correct DataFrame."""
    df = populated_session.get_cohort_report()

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 3 # One row per series/instance aggregation or mostly per series?
    # Usually report is per Series or Study. Let's assume Per Series for granularity.

    expected_cols = ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "Modality", "Manufacturer", "Model"]
    for col in expected_cols:
        assert col in df.columns

    # Check content
    p1_rows = df[df["PatientID"] == "P1"]
    assert len(p1_rows) == 2

    ge_rows = df[df["Manufacturer"] == "GE MEDICAL SYSTEMS"]
    assert len(ge_rows) == 2

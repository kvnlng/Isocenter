
import pytest
import os
import shutil
import datetime
import numpy as np
from gantry.io_handlers import DicomExporter
from gantry.entities import Patient, Study, Series, Instance

@pytest.fixture
def mock_patient(tmp_path):
    # Create object graph
    p = Patient("PID_001", "Test Subject")

    s = Study("STUDY_UID_1", datetime.date(2025, 1, 1))
    p.studies.append(s)

    se = Series("SERIES_UID_1", "CT", 1)
    s.series.append(se)

    # Create fake instance with minimal attributes
    inst = Instance("SOP_UID_1", "1.2.840.10008.5.1.4.1.1.2", 0)
    inst.attributes = {
        "0008,1030": "Chest CT",       # Study Description
        "0008,103e": "Axial 3mm",      # Series Description (lowercase hex,
                                        # matching how ingested attribute
                                        # keys are actually cased -- see
                                        # io_handlers.populate_attrs's
                                        # f"{elem.tag.group:04x},{elem.tag.element:04x}")
        "0020,0013": "10",             # Instance Number
        "0028,0010": 512,              # Rows
        "0028,0011": 512,              # Cols
    }
    # Mock pixel data
    # Set pixel_array explicitly (supported by parallel export for in-memory objects)
    inst.pixel_array = np.zeros((512, 512), dtype=np.uint16)

    se.instances.append(inst)

    return p

@pytest.fixture
def mock_validator(monkeypatch):
    from gantry.validation import IODValidator
    # Monkeypatch validate to always return [] (no errors)
    monkeypatch.setattr(IODValidator, "validate", lambda ds: [])

def test_structured_export(mock_patient, mock_validator, tmp_path):
    out_dir = tmp_path / "export_test"

    # Run export
    # Run export
    # Mock run_parallel to run synchronously so the IODValidator patch applies!
    from unittest.mock import patch
    with patch('gantry.io_handlers.run_parallel', side_effect=lambda func, items, *a, **k: [func(i) for i in items]):
        DicomExporter.save_patient(mock_patient, str(out_dir))

    # Expected Structure (the shared `export_folder_names` scheme -- same
    # as `session.export()` -- date as-rendered plus a UID suffix on the
    # Study folder, modality plus a UID suffix on the Series folder):
    # out_dir / Subject_PID_001 / Study_2025-01-01_Chest_CT_UID_1 / Series_1_CT_Axial_3mm_UID_1 / 0010.dcm

    subject_dir = out_dir / "Subject_PID_001"
    assert subject_dir.exists(), "Subject directory missing"

    study_dirs = list(subject_dir.glob("Study_*"))
    assert len(study_dirs) == 1
    assert "2025-01-01_Chest_CT" in study_dirs[0].name

    series_dirs = list(study_dirs[0].glob("Series_*"))
    assert len(series_dirs) == 1
    assert "1_CT_Axial_3mm" in series_dirs[0].name

    files = list(series_dirs[0].glob("*.dcm"))
    assert len(files) == 1
    assert files[0].name == "0010.dcm"

def test_sanitization():
    unsafe = "Bad/Name: With * Characters?"
    safe = DicomExporter._sanitize(unsafe)
    assert "/" not in safe
    assert ":" not in safe
    assert "*" not in safe
    assert "?" not in safe
    assert safe == "BadName_With__Characters" # Depending on implementation details

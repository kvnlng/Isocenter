import pytest
import numpy as np
import warnings

# Suppress all pydicom warnings during tests
warnings.filterwarnings("ignore", module="pydicom.*")

import os
import json
from datetime import date
from isocenter.entities import Patient, Study, Series, Instance, Equipment
from isocenter.builders import DicomBuilder

@pytest.fixture(autouse=True)
def redirect_logging(tmp_path):
    """Redirects isocenter.log to a temp file for all tests."""
    log_file = tmp_path / "isocenter.log"
    os.environ["ISOCENTER_LOG_FILE"] = str(log_file)
    yield
    if "ISOCENTER_LOG_FILE" in os.environ:
        del os.environ["ISOCENTER_LOG_FILE"]

@pytest.fixture
def dummy_pixel_array_2d():
    return np.zeros((512, 512), dtype=np.uint16)


@pytest.fixture
def dummy_patient(dummy_pixel_array_2d):
    """Creates a full object graph using the Builder."""
    return (
        DicomBuilder.start_patient("P123", "Test^Patient")
        .add_study("1.2.840.111.1", date(2023, 1, 1))
        .add_series("1.2.840.111.1.1", "CT", 1)
        .set_equipment("TestManu", "TestModel", "SN-999")
        .add_instance("1.2.840.111.1.1.1", "1.2.840.10008.5.1.4.1.1.2", 1)
        .set_pixel_data(dummy_pixel_array_2d)

        # Type 1 (Pos/Orient/Spacing)
        .set_attribute("0020,0032", ["0", "0", "0"])
        .set_attribute("0020,0037", ["1", "0", "0", "0", "1", "0"])
        .set_attribute("0028,0030", ["0.5", "0.5"])

        # --- FIX: ADD TYPE 2 MANDATORY TAGS ---
        .set_attribute("0018,0050", "2.5")  # SliceThickness
        .set_attribute("0018,0060", "120")  # KVP

        .end_instance()
        .end_series()
        .end_study()
        .build()
    )

@pytest.fixture
def config_file(tmp_path):
    """Creates a temporary YAML config file."""
    data = {
        "version": "1.0",
        "machines": [
            {
                "serial_number": "SN-999",
                "model_name": "TestModel",
                "redaction_zones": [{"roi": [10, 50, 10, 50]}]
            }
        ]
    }
    p = tmp_path / "rules.yaml"
    import yaml
    with open(p, "w") as f:
        yaml.dump(data, f)
    return str(p)
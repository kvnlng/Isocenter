
import pytest
from unittest.mock import MagicMock, patch
from isocenter.services import RedactionService
from isocenter.io_handlers import DicomStore
from isocenter.entities import Patient, Study, Series, Instance, Equipment

@pytest.fixture
def mock_store():
    store = DicomStore()
    p = Patient("P1", "Test")
    st = Study("S1", "20230101")
    se = Series("SE1", "CT", 1)
    se.equipment = Equipment(manufacturer="Mock", model_name="Mock", device_serial_number="M1")
    inst = Instance("I1", "1.2.3", 1)
    # Mock pixel data using the sidecar loader mechanism
    import numpy as np
    inst._pixel_loader = lambda: np.zeros((10, 10), dtype=np.uint8)

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    store.patients.append(p)
    return store

def test_skip_empty_zones(mock_store):
    """Verify process_machine_rules returns early if zones are empty."""
    service = RedactionService(mock_store)
    service.logger = MagicMock()
    service.index.get_by_machine = MagicMock() # Should NOT be called

    rule = {"serial_number": "M1", "redaction_zones": []}

    service.process_machine_rules(rule, verbose=True)

    # Assert Warning/Info Log
    service.logger.info.assert_called_with("Machine M1 has no redaction zones configured. Skipping.")

    # Assert Index was NOT queried (Optimization check)
    service.index.get_by_machine.assert_not_called()

def test_process_valid_zones(mock_store):
    """Verify process_machine_rules proceeds if zones exist."""
    service = RedactionService(mock_store)
    service.logger = MagicMock()
    service.redact_machine_instances = MagicMock()

    rule = {"serial_number": "M1", "redaction_zones": [[0,10,0,10]]}

    service.process_machine_rules(rule)

    service.redact_machine_instances.assert_called_once()


@patch("isocenter.services.tqdm")
def test_redact_feedback_tqdm(mock_tqdm, mock_store):
    """Verify tqdm is initialized during redaction."""
    service = RedactionService(mock_store)

    # Actual logic calls tqdm(targets, ...)
    # targets will be [inst]

    service.redact_machine_instances("M1", [(0,10,0,10)])

    # Check if tqdm was called
    assert mock_tqdm.called
    args, kwargs = mock_tqdm.call_args
    assert "desc" in kwargs
    assert "Redacting M1" in kwargs["desc"]

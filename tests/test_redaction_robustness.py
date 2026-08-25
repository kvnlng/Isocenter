
import pytest
from unittest.mock import MagicMock, call
from isocenter.services import RedactionService
from isocenter.entities import Instance, Series, Study, Patient, Equipment
from isocenter.io_handlers import DicomStore

@pytest.fixture
def mock_store():
    store = DicomStore()
    p = Patient("P1", "Test Patient")
    st = Study("S1", "20230101")
    se = Series("SE1", "US", 1)
    se.equipment = Equipment("Man", "Mod", "SN-FAIL") # Default equipment

    st.series.append(se)
    p.studies.append(st)
    store.patients.append(p)
    return store, se

def test_redaction_crash_prevention(mock_store):
    """
    Verifies that redact_machine_region does NOT raise AttributeError
    when get_pixel_data returns None.
    """
    store, series = mock_store

    # 1. Setup Instance with NO pixel data (None)
    inst = Instance("I_FAIL", "SOP1", 1)
    # Mocking get_pixel_data to return None - Handled by patch.object below
    # inst.get_pixel_data = MagicMock(return_value=None)
    series.instances.append(inst)

    # 2. Run Redaction
    service = RedactionService(store)
    service.logger = MagicMock()

    # Patch the CLASS method because slots prevent instance monkeypatching
    from unittest.mock import patch
    with patch.object(Instance, 'get_pixel_data', return_value=None):
        try:
            service.redact_machine_instances("SN-FAIL", [(0, 100, 0, 100)], verbose=True)
        except AttributeError as e:
            pytest.fail(f"Crash detected: {e}")

    # 3. Verify Warning Logged
    service.logger.warning.assert_called_with(f"  Skipping {inst.sop_instance_uid}: No pixel data found (or file missing).")

def test_log_throttling(mock_store):
    """
    Verifies that scan_burned_in_annotations throttles error logs.
    """
    store, series = mock_store

    # 1. Setup 10 Untreated Instances
    for i in range(10):
        inst = Instance(f"I_{i}", f"SOP_{i}", i)
        inst.attributes["0028,0301"] = "YES" # Burned In
        # Missing "DERIVED" in Image Type -> Untreated
        series.instances.append(inst)

    # 2. Run Scan
    service = RedactionService(store)
    service.logger = MagicMock()

    service.scan_burned_in_annotations()

    # 3. Verify Logs
    # We expect 5 individual errors + 1 suppression message
    # Counts of 'error' calls:
    assert service.logger.error.call_count == 6

    # Verify the suppression message
    service.logger.error.assert_has_calls([
        call("... (Suppressing further individual errors for Burned In Annotations) ...")
    ], any_order=True)


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


# --- A zone that cannot be applied is a failure, not a no-op (#66) ----
#
# apply_redaction_to_array used to wrap the pixel-zeroing itself in
# `except Exception: pass` and the enclosing loop in a second handler
# that returned False. A zone that failed to apply was skipped, PHI
# stayed burned into the image, nothing was logged at any level, and the
# return value was indistinguishable from "there were no zones". The
# export worker (io_handlers._export_instance_worker) ignores the return
# value entirely and writes arr.tobytes() straight afterwards, so a
# silent failure meant exporting unredacted pixels as though clean.

import logging
import numpy as np

from isocenter.pixel_geometry import resolve_pixel_geometry


def test_a_zone_that_cannot_be_applied_raises_instead_of_reporting_nothing():
    """Silence here means PHI ships. The caller has to be told."""
    arr = np.ones((64, 64), dtype=np.uint16)
    arr.flags.writeable = False

    with pytest.raises(ValueError):
        RedactionService.apply_redaction_to_array(
            arr, [(0, 10, 0, 10)], geometry=resolve_pixel_geometry(arr.shape, {}))


def test_a_failed_zone_is_logged_with_enough_detail_to_find_it(caplog):
    """An exception alone does not say which zone, on what array."""
    arr = np.ones((64, 64), dtype=np.uint16)
    arr.flags.writeable = False

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError):
            RedactionService.apply_redaction_to_array(
                arr, [(0, 10, 0, 10)],
                geometry=resolve_pixel_geometry(arr.shape, {}))

    assert caplog.records, "a failed redaction produced no log record at all"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "(0, 10, 0, 10)" in logged or "0, 10, 0, 10" in logged
    assert "(64, 64)" in logged


def test_having_no_zones_to_apply_is_not_a_failure():
    """The legitimate no-op must stay distinguishable from a failure."""
    arr = np.ones((64, 64), dtype=np.uint16)

    assert RedactionService.apply_redaction_to_array(
        arr, [], geometry=resolve_pixel_geometry(arr.shape, {})) is False
    assert arr.sum() == 64 * 64, "an empty zone list must not modify the array"


def test_a_zone_that_applies_cleanly_still_reports_success():
    arr = np.ones((64, 64), dtype=np.uint16)

    assert RedactionService.apply_redaction_to_array(
        arr, [(0, 10, 0, 10)],
        geometry=resolve_pixel_geometry(arr.shape, {})) is True
    assert arr[0:10, 0:10].sum() == 0
    assert arr[10:, 10:].sum() == 54 * 54


def test_the_geometry_argument_has_no_default():
    """#217: the default arm was the heuristic that shipped the identifier.

    `geometry=None` meant `arr.shape[-1] in [3, 4]`, which cannot tell
    `(frames, rows, cols)` from `(rows, cols, samples)`. On a 4-frame 8x4
    volume with an identifier at rows 6-7 of every frame, zone
    `(6, 8, 0, 4)` addressed frames `6:8` of a 4-frame array -- an empty
    slice -- so all 32 identifier cells reached the exported file while
    redaction reported success. A default here is not a convenience; it is
    the leak, reachable by any caller who does not know to pass a keyword.

    This is the only test in the suite that fails if the default comes
    back. The four above assert the *passing* case and pass on both sides
    of #217, which is why this one exists.

    `match="geometry"` is load-bearing. A bare `pytest.raises(TypeError)`
    also passes on a malformed zone -- measured, `(0, None, 0, 8)` and
    `(0, [1], 0, 8)` both raise `TypeError` from inside the loop -- so it
    would keep passing against a restored default that then failed for an
    unrelated reason. The interpreter's message names the parameter.
    """
    arr = np.ones((64, 64), dtype=np.uint16)

    with pytest.raises(TypeError, match="geometry"):
        RedactionService.apply_redaction_to_array(arr, [(0, 10, 0, 10)])

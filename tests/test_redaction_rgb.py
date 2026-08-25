
import pytest
import numpy as np
from unittest.mock import MagicMock
from isocenter.services import RedactionService
from isocenter.entities import Instance, Series, Study, Patient, Equipment
from isocenter.io_handlers import DicomStore

def test_redaction_rgb_dimensions():
    """
    Verifies that redact_machine_region correctly interprets RGB images (Rows, Cols, 3).
    Problem: It might see (100, 100, 3) and mistake it for (Rows=100, Cols=3).
    """
    # 1. Setup RGB Instance
    # Shape: (Rows=100, Cols=100, Samples=3)
    rgb_data = np.zeros((100, 100, 3), dtype=np.uint8)

    inst = Instance("I_RGB", "SOP_RGB", 1)
    inst.set_pixel_data(rgb_data)
    inst.set_pixel_data(rgb_data)
    # inst.regenerate_uid = MagicMock() # Removed: slots prevent monkeypatching, and regex is fast enough

    store = DicomStore()
    p = Patient("P1", "Test")
    st = Study("S1", "20230101")
    se = Series("SE1", "US", 1)
    se.equipment = Equipment("Man", "Mod", "SN-RGB")
    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    store.patients.append(p)

    # 2. Setup Service
    service = RedactionService(store)
    service.logger = MagicMock()

    # 3. Apply Redaction
    # ROI: x=50, w=10 -> c1=50, c2=60
    # If logic thinks cols=3, this will trigger "completely outside" warning
    service.redact_machine_instances("SN-RGB", [(0, 10, 50, 60)])

    # 4. Verify Failure
    # If the bug exists, we expect a warning about "outside image dimensions"
    # We want to ASSERT the bug first to confirm reproduction
    # The user said: "WARNING: ROI ... is completely outside image dimensions"

    # Check if we logged the specific warning
    found_warning = False
    for call_args in service.logger.warning.call_args_list:
        msg = call_args[0][0]
        if "outside image dimensions" in msg:
            found_warning = True
            break

    # If test is to PROVE the bug, assert found_warning is True.
    # If test is to PROVE the fix, assert found_warning is False.
    assert found_warning is False, "RGB dimension bug persists: Warning 'outside image dimensions' still found."

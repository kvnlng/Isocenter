
import pytest
import numpy as np
from isocenter.entities import Instance
from isocenter.services import RedactionService

class MockStore:
    def __init__(self):
        self.patients = []

def test_apply_roi_to_instance_floats():
    """
    Regression Test for Float ROI Coordinates.
    Ensures that passing float coordinates (e.g. from JSON) does not cause
    silent failure in _apply_roi_to_instance due to TypeError in slicing.
    """
    inst = Instance("1.2.3", "1.2.3", 1)

    # 100x100 white image
    arr = np.zeros((100, 100), dtype=np.uint8) + 255
    inst.set_pixel_data(arr)

    service = RedactionService(MockStore(), None)

    # ROI with floats: (r1, r2, c1, c2)
    # 10.5 -> 20.5
    roi = (10.5, 20.5, 10.0, 20.0)

    # Call internal method directly
    success = service._apply_roi_to_instance(inst, arr, roi)

    assert success is True, "Operation returned False (Silent Failure)"

    # Check if pixels changed to 0 (Black)
    # Indices should be int(10.5)=10 to int(20.5)=20
    region = arr[10:20, 10:20]

    assert np.all(region == 0), "Pixels were not set to 0 (Redacted)"

    # Check outside region remains white
    assert arr[9, 9] == 255
    assert arr[21, 21] == 255

def test_apply_roi_dimensions():
    """
    Test ROI application on multi-dimensional array (RGB).
    """
    inst = Instance("1.2.4", "1.2.4", 1)
    # (H, W, 3) image
    arr = np.zeros((50, 50, 3), dtype=np.uint8) + 255
    inst.set_pixel_data(arr)
    service = RedactionService(MockStore(), None)

    roi = (10, 20, 10, 20)
    success = service._apply_roi_to_instance(inst, arr, roi)

    assert success is True

    # Check if all channels zeroed
    region = arr[10:20, 10:20, :]
    assert np.all(region == 0)

if __name__ == "__main__":
    test_apply_roi_to_instance_floats()
    test_apply_roi_dimensions()

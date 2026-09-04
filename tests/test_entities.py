import numpy as np
import pytest
from unittest.mock import patch
from isocenter.entities import Instance, Equipment


def test_equipment_equality():
    """Test that frozen dataclasses hash correctly."""
    e1 = Equipment("GE", "CT", "SN1")
    e2 = Equipment("GE", "CT", "SN1")
    e3 = Equipment("GE", "CT", "SN2")

    assert e1 == e2
    assert e1 != e3
    assert len({e1, e2, e3}) == 2  # Set should deduplicate e1 and e2


def test_pixel_unpacking_2d():
    inst = Instance()
    arr = np.zeros((100, 200))
    inst.set_pixel_data(arr)

    assert inst.attributes["0028,0010"] == 100  # Rows
    assert inst.attributes["0028,0011"] == 200  # Cols
    assert inst.attributes["0028,0002"] == 1  # Samples


def test_pixel_unpacking_rgb():
    inst = Instance()
    # 3D array where last dim is 3 (RGB)
    arr = np.zeros((100, 200, 3))
    inst.set_pixel_data(arr)

    assert inst.attributes["0028,0002"] == 3
    assert inst.attributes["0028,0004"] == "RGB"


def test_lazy_loading(tmp_path):
    """Verify get_pixel_data loads from disk if memory is empty."""
    inst = Instance(sop_instance_uid="1.2.3")

    dummy_file = tmp_path / "dummy.dcm"
    dummy_file.touch()  # <--- THIS WAS MISSING (Creates empty file)
    inst.file_path = str(dummy_file)

    # Mock pydicom.dcmread so we don't need a real file
    with patch("pydicom.dcmread") as mock_read:
        mock_ds = mock_read.return_value
        mock_ds.pixel_array = np.zeros((50, 50))

        # Act
        data = inst.get_pixel_data()

        # Assert
        assert data.shape == (50, 50)
        # `force=True` is part of the call, not decoration: it is what
        # makes this re-read accept the same files the eager ingest
        # accepted (#289, #281).
        mock_read.assert_called_once_with(inst.file_path, force=True)
        # Verify it cached the result
        assert inst.pixel_array is not None

def test_pixel_bits_allocated():
    """Verify that BitsAllocated is correctly inferred from dtype."""
    inst = Instance()

    # Test uint8 (8 bits)
    arr_uint8 = np.zeros((10, 10), dtype=np.uint8)
    inst.set_pixel_data(arr_uint8)
    assert inst.attributes["0028,0100"] == 8

    # Test uint16 (16 bits)
    arr_uint16 = np.zeros((10, 10), dtype=np.uint16)
    inst.set_pixel_data(arr_uint16)
    assert inst.attributes["0028,0100"] == 16

    arr_int32 = np.zeros((10, 10), dtype=np.int32)
    inst.set_pixel_data(arr_int32)
    assert inst.attributes["0028,0100"] == 32

def test_photometric_defaults():
    """
    Verify set_pixel_data handles PhotometricInterpretation correctly:
    1. Preserves existing MONOCHROME1
    2. Defaults to MONOCHROME2 if missing
    3. Forces RGB for 3-channel data
    """
    # Case 1: Defaulting to MONOCHROME2
    inst = Instance()
    inst.set_pixel_data(np.zeros((10, 10)))
    assert inst.attributes["0028,0004"] == "MONOCHROME2"

    # Case 2: Preserving MONOCHROME1
    inst2 = Instance()
    inst2.attributes["0028,0004"] = "MONOCHROME1"
    inst2.set_pixel_data(np.zeros((10, 10)))
    assert inst2.attributes["0028,0004"] == "MONOCHROME1"

    # Case 3: Forcing RGB
    inst3 = Instance()
    # Even if it said MONOCHROME2, if we pass RGB data it must switch
    inst3.attributes["0028,0004"] = "MONOCHROME2"
    inst3.set_pixel_data(np.zeros((10, 10, 3)))
    assert inst3.attributes["0028,0004"] == "RGB"
    # Also verify PlanarConfiguration is forced to 0 for RGB
    assert inst3.attributes["0028,0006"] == 0
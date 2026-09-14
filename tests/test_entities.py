import numpy as np
import pytest
from unittest.mock import patch
from isocenter.builders import DicomBuilder
from isocenter.entities import Instance, Equipment


def test_equipment_equality():
    """Test that frozen dataclasses hash correctly."""
    e1 = Equipment("GE", "CT", "SN1")
    e2 = Equipment("GE", "CT", "SN1")
    e3 = Equipment("GE", "CT", "SN2")

    assert e1 == e2
    assert e1 != e3
    assert len({e1, e2, e3}) == 2  # Set should deduplicate e1 and e2


# --- #290: one spelling of "does this series have equipment?" -----------


@pytest.mark.parametrize("man, model", [
    ("ACME", ""), ("", "Scanner"), ("ACME", "Scanner")])
def test_from_parts_treats_either_identifying_field_as_equipment(man, model):
    """A manufacturer *or* a model name is equipment (#290, #282).

    The partial rows are the ones that matter: a `from_parts` written
    with `and` passes the full row and the "neither" case below and
    fails only here. Each of the three fields is asserted against its
    input, because `is not None` alone would survive the helper
    constructing `cls(model_name, manufacturer, ...)` -- the swap that
    the suite could not see at either hydration site before this bunch.
    """
    eq = Equipment.from_parts(man, model, "SN-1")

    assert eq is not None
    assert eq.manufacturer == man
    assert eq.model_name == model
    assert eq.device_serial_number == "SN-1"


@pytest.mark.parametrize("serial", ["", "SN-ONLY"])
def test_from_parts_returns_none_when_neither_identifying_field_is_present(
        serial):
    """A serial alone is not equipment -- today's rule, pinned as it stands (#290).

    This is a characterization pin, not an endorsement. The serial is
    what every downstream rule matches on (`_match_machine_rule` gives an
    exact serial precedence over every other match), so a series whose
    file carries DeviceSerialNumber with an empty Manufacturer and no
    ManufacturerModelName ingests with no equipment and is skipped by
    redaction without a word. Widening the predicate to the serial is a
    semantic change filed separately from #290, which is a
    behaviour-preserving refactor; when that issue lands, this test
    flips with it. Until then it kills a `from_parts` that quietly
    widened on one route and not the others.
    """
    assert Equipment.from_parts("", "", serial) is None


def test_from_parts_does_not_normalise_its_inputs():
    """`from_parts` passes its arguments through untouched (#290).

    `Equipment("ACME", None, None)` round-trips through `save_all` and
    `load_patient` as `Equipment("ACME", None, None)`, so a well-meant
    `or ""` in the helper would change what a reload returns.
    """
    assert Equipment.from_parts("ACME", None, None) == Equipment("ACME", None, None)


def test_set_equipment_with_no_identifying_field_leaves_the_series_without_equipment():
    """`SeriesBuilder.set_equipment` applies the same rule as ingest and reload (#290).

    It was the fourth construction site and the only one with no
    predicate: `.set_equipment("", "", "SN")` set an `Equipment` that
    `save_all` wrote faithfully and neither `load_all` nor `load_patient`
    could return. The loss now shows at construction rather than at
    reload. No in-tree caller passes empty identifying fields.
    """
    without = (DicomBuilder.start_patient("P1", "Test^Patient")
               .add_study("1.2.3", "20230101")
               .add_series("1.2.3.4", "CT", 1)
               .set_equipment("", "", "SN"))
    assert without.series.equipment is None

    partial = (DicomBuilder.start_patient("P1", "Test^Patient")
               .add_study("1.2.3", "20230101")
               .add_series("1.2.3.4", "CT", 1)
               .set_equipment("ACME", "", "SN"))
    assert partial.series.equipment.manufacturer == "ACME"
    assert partial.series.equipment.device_serial_number == "SN"


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

    # Mock pydicom.dcmread so we don't need a real file. The door decodes
    # through `io_handlers._decode_pixels` (#453), which calls
    # `get_decoder(ts).as_array` -- what `Dataset.pixel_array` calls -- to
    # keep the decoder's colour-space answer (#482), so the decode is
    # mocked there. The door's own `get_decoder` only asks whether any
    # decoder implements the syntax; it gets the same mock.
    with patch("pydicom.dcmread") as mock_read, \
            patch("isocenter.io_handlers.get_decoder") as mock_decoder, \
            patch("isocenter.entities.get_decoder", mock_decoder):
        mock_ds = mock_read.return_value
        mock_ds.PhotometricInterpretation = "MONOCHROME2"
        mock_decoder.return_value.as_array.return_value = (
            np.zeros((50, 50)), {"photometric_interpretation": "MONOCHROME2"})

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
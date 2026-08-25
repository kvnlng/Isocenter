from pydicom.dataset import Dataset
from isocenter.validation import IODValidator


def test_validator_valid_ct():
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage

    # --- 1. Populate ALL Mandatory Fields (Common + CTImage) ---
    # Common Module (Type 1)
    ds.SOPInstanceUID = "1.2.3"
    ds.StudyDate = "20230101"
    ds.StudyTime = "120000"  # <--- Was missing
    ds.Modality = "CT"  # <--- Was missing
    ds.SeriesInstanceUID = "1.2.3.4"

    # CT Image Module (Type 1)
    ds.ImagePositionPatient = [0, 0, 0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [1, 1]

    # CT Image Module (Type 2 - Must exist, even if empty)
    ds.SliceThickness = ""
    ds.KVP = ""

    # --- 2. Verify Baseline (Should be 0 errors) ---
    errors = IODValidator.validate(ds)
    assert len(errors) == 0, f"Expected valid DS, but got: {errors}"

    # --- 3. Test Deletion (Type 1 Error) ---
    del ds.PixelSpacing

    errors = IODValidator.validate(ds)

    # Assert we caught the error
    assert len(errors) > 0
    # Search through ALL errors, not just the first one
    assert any("0028,0030" in e or "PixelSpacing" in e for e in errors)
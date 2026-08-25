
import pytest
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian
from isocenter.session import DicomSession
from isocenter.entities import Instance

def test_reproduce_no_pixel_data_crash(tmp_path):
    """
    Reproduces the crash when get_pixel_data is called on a file
    with no PixelData element.
    """
    # 1. Create a valid DICOM file WITHOUT PixelData
    dcm_path = tmp_path / "no_pixels.dcm"

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.200.1" # CT Defined Procedure Protocol Storage (Non-Image)
    file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5"
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(str(dcm_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientID = "123"
    ds.StudyInstanceUID = "1.2.3"
    ds.SeriesInstanceUID = "1.2.3.4"
    ds.SOPInstanceUID = "1.2.3.4.5"

    # Intentionally NOT setting ds.PixelData

    ds.save_as(str(dcm_path))

    # 2. Ingest
    session = DicomSession(":memory:")
    session.ingest(str(tmp_path))

    assert len(session.store.patients) == 1

    # 3. Export - Should NOT Crash
    export_dir = tmp_path / "export"

    try:
        session.export(str(export_dir))
    except RuntimeError as e:
        pytest.fail(f"Export should NOT have crashed for file w/o pixels: {e}")

    # Verify export occurred (e.g. file exists)
    # The 'Subject_123' folder should be there.
    assert (export_dir / "Subject_123").exists()

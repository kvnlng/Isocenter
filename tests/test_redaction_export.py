
import os
import shutil
import pytest
import pydicom
import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.session import DicomSession
from isocenter.configuration import IsocenterConfiguration

def create_synthetic_dicom(filepath, rows=100, cols=100):
    """Creates a simple synthetic DICOM for testing."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.2'
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(filepath, {}, file_meta=file_meta, preamble=b"\0" * 128)

    # Standard UIDs
    ds.PatientName = "Test^Patient"
    ds.PatientID = "123456"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.Modality = "CT"

    # Equipment for Rule Matching
    ds.Manufacturer = "TestMan"
    ds.ManufacturerModelName = "TestModel"
    ds.DeviceSerialNumber = "TEST-SN-001"

    # Date/Time
    ds.StudyDate = "20230101"
    ds.StudyTime = "120000"  # Type 1
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1

    # CT Image IOD Mandatory (Type 1/2)
    ds.SliceThickness = "2.5"  # Type 2
    ds.KVP = "120"  # Type 2
    ds.ImagePositionPatient = ["0", "0", "0"]  # Type 1
    ds.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]  # Type 1
    ds.PixelSpacing = ["0.5", "0.5"]  # Type 1

    # Pixel Data (White Square)
    arr = np.ones((rows, cols), dtype=np.uint16) * 1000
    # Add a feature to redact (e.g. invalid value 2000 in a specific region)
    arr[20:40, 20:40] = 2000

    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelData = arr.tobytes()

    ds.save_as(filepath, write_like_original=False)
    return ds.SOPInstanceUID

def test_export_compressed_redaction(tmp_path):
    """
    Regression Test for Export Compression Bug.
    Ensures that when 'use_compression=True' is used, redaction rules are still honored.
    """
    # 1. Setup Data
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dcm_path = src_dir / "test.dcm"
    sop_uid = create_synthetic_dicom(str(dcm_path))

    # 2. Configure Session & Rules
    # We use a persistent DB file in tmp to simulate real usage
    db_path = tmp_path / "test.db"

    # Initialize Configuration with a Rule
    config = IsocenterConfiguration()
    # Redact the 2000 value region [20:40, 20:40]
    # We use a slightly larger zone to be sure
    config.add_rule(
        "TEST-SN-001",
        zones=[[20, 40, 20, 40]] # [r1, r2, c1, c2]
    )

    # 3. Ingest
    # DicomSession loads configuration internally usually, or via set_configuration?
    # Checking source: __init__(self, db_path='session_store.db', clean=True, store_backend=None)
    session = DicomSession(str(db_path))

    # Inject our custom configuration with the rule
    # The session creates a default configuration on init. We can override it.
    session.configuration = config

    # Use DicomImporter directly or check session methods
    # Session generally exposes import functionality.
    # Based on outline: ingest(self, directory: str)
    session.ingest(str(src_dir))

    # 4. Export WITH Compression (and implicit redaction)
    # Important: We DO NOT call session.redact(). We expect export to handle it.
    out_dir = tmp_path / "export"
    session.export(str(out_dir), use_compression=True)

    # 5. Verify Export
    # Find the exported file
    exported_files = list(out_dir.glob("**/*.dcm"))
    assert len(exported_files) == 1

    ds_out = pydicom.dcmread(str(exported_files[0]))

    # Check Compression (J2K Transfer Syntax)
    # 1.2.840.10008.1.2.4.90 is JPEG 2000 Image Compression (Lossless Only)
    # 1.2.840.10008.1.2.4.91 is JPEG 2000 Image Compression
    assert ds_out.file_meta.TransferSyntaxUID in ['1.2.840.10008.1.2.4.90', '1.2.840.10008.1.2.4.91']

    # Check Redaction
    # The region [20:40, 20:40] should be 0
    arr = ds_out.pixel_array
    patch = arr[20:40, 20:40]

    # Sum should be 0 (fully blacked out)
    assert patch.sum() == 0, f"Region was not redacted! Sum: {patch.sum()}"

    # Check Integrity of non-redacted parts (should still be 1000)
    # e.g. [50:60, 50:60]
    background = arr[50:60, 50:60]
    assert background.mean() == 1000, "Background pixels were modified inappropriately"

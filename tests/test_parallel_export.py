
import pytest
import os
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, UID
from isocenter.session import DicomSession
from isocenter.entities import Instance

def create_dcm(path, patient_id, study_uid, series_uid, sop_uid, seri_num=0, inst_num=1):
    """Helper to create a valid minimal DICOM file"""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7" # Secondary Capture
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientName = f"Patient_{patient_id}"
    ds.PatientID = patient_id
    ds.StudyInstanceUID = study_uid
    ds.StudyDate = "20230101"
    ds.SeriesInstanceUID = series_uid
    ds.SeriesNumber = seri_num
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID # Required in body
    ds.Modality = "OT"
    ds.ConversionType = "WSD" # Required for SC

    # Set Instance Number
    ds.InstanceNumber = inst_num

    # Add dummy pixels (10x10)
    ds.Rows = 10
    ds.Columns = 10
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = (b'\0' * 100)

    ds.save_as(str(path))

def test_parallel_export(tmp_path):
    """
    Verifies that export runs without error and produces output files
    using the new parallel implementation.
    """
    # 1. Setup Input Repo
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    # Create 5 files across 2 studies
    # Ensure distinct Series/Instance numbers to verify file counts
    create_dcm(input_dir / "1.dcm", "PAT1", "STUDY1", "SERIES1", "1.1.1.1", seri_num=1, inst_num=1)
    create_dcm(input_dir / "2.dcm", "PAT1", "STUDY1", "SERIES1", "1.1.1.2", seri_num=1, inst_num=2)
    create_dcm(input_dir / "3.dcm", "PAT1", "STUDY1", "SERIES2", "1.1.2.1", seri_num=2, inst_num=1)
    create_dcm(input_dir / "4.dcm", "PAT1", "STUDY2", "SERIES3", "1.2.3.1", seri_num=3, inst_num=1)
    create_dcm(input_dir / "5.dcm", "PAT2", "STUDY3", "SERIES4", "2.1.1.1", seri_num=4, inst_num=1)

    # 2. Ingest
    session = DicomSession(":memory:")
    session.ingest(str(input_dir))

    assert len(session.store.patients) == 2

    # 3. Export
    export_dir = tmp_path / "export"
    session.export(str(export_dir))

    # 4. Verify Output
    # We expect recursive finding of 5 .dcm files
    exported_files = list(export_dir.rglob("*.dcm"))

    print("\n--- Exported Files ---")
    for f in exported_files:
        print(f)
    print("----------------------\n")

    assert len(exported_files) == 5, f"Should have exported 5 files, found {len(exported_files)}"

    # Verify content of one
    one_dcm = exported_files[0]
    ds = pydicom.dcmread(one_dcm)
    assert ds.PatientID in ["PAT1", "PAT2"]

    print(f"Verified parallel export of {len(exported_files)} files.")

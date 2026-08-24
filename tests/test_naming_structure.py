
import os
import shutil
import pytest
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import generate_uid, ImplicitVRLittleEndian
from gantry.session import DicomSession
from gantry.io_handlers import DicomExporter

TEST_DIR = "tests_data_naming"
EXPORT_DIR = "tests_export_naming"

def setup_module():
    if os.path.exists(TEST_DIR): shutil.rmtree(TEST_DIR)
    if os.path.exists(EXPORT_DIR): shutil.rmtree(EXPORT_DIR)
    os.makedirs(TEST_DIR)

def teardown_module():
    if os.path.exists(TEST_DIR): shutil.rmtree(TEST_DIR)
    if os.path.exists(EXPORT_DIR): shutil.rmtree(EXPORT_DIR)

def create_dicom(path, pid, study_desc, series_desc, modality):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7" # Secondary Capture
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(path, {}, file_meta=meta, preamble=b"\0"*128)
    ds.is_little_endian = True
    ds.is_implicit_VR = True

    ds.PatientID = pid
    ds.PatientName = f"Subject_{pid}"

    ds.StudyInstanceUID = generate_uid()
    ds.StudyDate = "20250101"
    ds.StudyDescription = study_desc

    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = 1
    ds.Modality = modality
    ds.SeriesDescription = series_desc

    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.ConversionType = "WSD"

    ds.save_as(path)
    return ds

def test_folder_naming_structure():
    # Create a DICOM with specific descriptions
    dcm_path = os.path.join(TEST_DIR, "test.dcm")
    ds = create_dicom(dcm_path, "PAT001", "Brain_Scan", "Axial_T1", "MR")

    # Ingest
    db_path = os.path.join(TEST_DIR, "gantry.db")
    session = DicomSession(db_path)
    session.ingest(TEST_DIR)

    # Export
    session.export(EXPORT_DIR)

    # Verify Structure
    # Should be: Subject_PAT001 / Study_2025-01-01_Brain_Scan_XXXXX / Series_1_MR_Axial_T1_XXXXX

    # 1. Patient Folder
    pat_folder = os.path.join(EXPORT_DIR, "Subject_PAT001")
    assert os.path.exists(pat_folder), "Patient folder missing"

    # 2. Study Folder
    studies = os.listdir(pat_folder)
    assert len(studies) == 1
    study_name = studies[0]
    print(f"Study Folder: {study_name}")
    assert study_name.startswith("Study_2025-01-01_Brain_Scan_"), f"Study name {study_name} failed format"

    # 3. Series Folder
    study_path = os.path.join(pat_folder, study_name)
    series = os.listdir(study_path)
    assert len(series) == 1
    series_name = series[0]
    print(f"Series Folder: {series_name}")
    assert series_name.startswith("Series_1_MR_Axial_T1_"), f"Series name {series_name} failed format"

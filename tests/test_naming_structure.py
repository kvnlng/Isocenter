
import os
import pytest
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import generate_uid, ImplicitVRLittleEndian
from isocenter.session import DicomSession
from isocenter.io_handlers import DicomExporter

# Relative to the test's own tmp_path, which is its cwd (#707). These were
# made by a `setup_module` that ran in the repository root, outside any
# test, while the test itself now runs elsewhere and could not find them.
TEST_DIR = "tests_data_naming"
EXPORT_DIR = "tests_export_naming"

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
    os.makedirs(TEST_DIR)
    dcm_path = os.path.join(TEST_DIR, "test.dcm")
    ds = create_dicom(dcm_path, "PAT001", "Brain_Scan", "Axial_T1", "MR")

    # Ingest
    db_path = os.path.join(TEST_DIR, "isocenter.db")
    # `with`, not a bare constructor: `close()` releases a
    # ProcessPoolExecutor and two threads holding sqlite handles on
    # `db_path`, which lives inside TEST_DIR. Left open, those threads
    # outlive the test and `teardown_module`'s `rmtree` races them --
    # SQLite creates and removes -wal/-shm files without asking, so
    # `exists()` said yes and `rmtree` then raised `FileNotFoundError`
    # on a path that had gone. That surfaced as `1542 passed, 1 error`
    # on 3.12, twice, on commits that had nothing to do with this file
    # (#371). The teardown noise was the symptom; the leak is the
    # defect, because a pool and two sqlite threads outliving their
    # test make every later test in the run less deterministic, and
    # this suite has been bitten by load-dependent races repeatedly
    # (#250, #343, #274, #297).
    with DicomSession(db_path) as session:
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

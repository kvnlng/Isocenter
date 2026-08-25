import pytest
import pandas as pd
import os
import shutil
from isocenter.session import DicomSession
from isocenter.entities import Patient, Study, Series, Instance
import time

@pytest.fixture
def session_for_query(tmp_path, monkeypatch):
    db_path = tmp_path / "isocenter_query.db"
    session = DicomSession(str(db_path))

    # Check dependencies
    try:
        import pydicom
        from pydicom.dataset import FileDataset, FileMetaDataset
        from pydicom.uid import ImplicitVRLittleEndian, generate_uid
    except ImportError:
        pytest.skip("pydicom not installed")

    # Keep track of UIDs
    uids = {}

    def create_dummy_dicom(path, modality):
        uid = generate_uid()
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7" # Secondary Capture
        file_meta.MediaStorageSOPInstanceUID = uid
        file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

        ds = FileDataset(path, {}, file_meta=file_meta, preamble=b"\0" * 128)
        ds.PatientName = "Test^Patient"
        ds.PatientID = "P1"
        ds.StudyInstanceUID = generate_uid()
        ds.SeriesInstanceUID = generate_uid()
        ds.SOPInstanceUID = uid
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7" # Secondary Capture
        ds.Modality = modality

        # Pixels
        import numpy as np
        ds.Rows = 10
        ds.Columns = 10
        ds.SamplesPerPixel = 1
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = np.zeros((10, 10), dtype=np.uint8).tobytes()
        ds.save_as(path)
        return uid

    ct_path = tmp_path / "ct.dcm"
    mr_path = tmp_path / "mr.dcm"

    uids['CT'] = create_dummy_dicom(str(ct_path), "CT")
    uids['MR'] = create_dummy_dicom(str(mr_path), "MR")

    # Ingest them
    session.ingest(str(tmp_path))

    session.save()
    session.persistence_manager.flush()

    # Attach UIDs to session for tests to access
    session.test_uids = uids

    yield session
    session.close()

def test_export_query_string(session_for_query, tmp_path):
    out_dir = tmp_path / "export_ct"
    ct_uid = session_for_query.test_uids['CT']
    mr_uid = session_for_query.test_uids['MR']

    # Filter for CT only
    # Note: Column is now 'Modality' (PascalCase) due to our fix
    session_for_query.export(str(out_dir), subset="Modality == 'CT'", show_progress=False)

    found_uids = []
    for root, _, files in os.walk(out_dir):
        for f in files:
            if f.endswith(".dcm"):
                found_uids.append(f.replace(".dcm", ""))

    assert ct_uid in found_uids
    assert mr_uid not in found_uids

def test_export_dataframe_subset(session_for_query, tmp_path):
    out_dir = tmp_path / "export_mr"
    ct_uid = session_for_query.test_uids['CT']
    mr_uid = session_for_query.test_uids['MR']

    # Get DF, filter for MR
    df = session_for_query.export_dataframe(expand_metadata=True)
    subset_df = df[df['Modality'] == 'MR']

    session_for_query.export(str(out_dir), subset=subset_df, show_progress=False)

    found_uids = []
    for root, _, files in os.walk(out_dir):
        for f in files:
            found_uids.append(f.replace(".dcm", ""))

    assert mr_uid in found_uids
    assert ct_uid not in found_uids

def test_export_list_subset(session_for_query, tmp_path):
    out_dir = tmp_path / "export_manual"
    ct_uid = session_for_query.test_uids['CT']
    mr_uid = session_for_query.test_uids['MR']

    # Manual list
    subset = [ct_uid, mr_uid]

    session_for_query.export(str(out_dir), subset=subset, show_progress=False)

    found_uids = []
    for root, _, files in os.walk(out_dir):
        for f in files:
            found_uids.append(f.replace(".dcm", ""))

    assert ct_uid in found_uids
    assert mr_uid in found_uids

import pytest
import os
from isocenter.io_handlers import DicomImporter, DicomStore, DicomExporter
from isocenter.entities import Patient
from isocenter.builders import DicomBuilder
from datetime import date

def test_recursive_import(tmp_path):
    # 1. Setup Data hierarchy
    root = tmp_path / "root"
    subdir = root / "subdir"
    os.makedirs(subdir)

    # 2. Create Dummy Patients - MANUAL WRITE


    # Needs valid pixels for export? The Importer only needs metadata.
    # But Exporter might fail if we don't handle missing pixels gracefully or mock them.
    # Let's use the valid dummy_patient fixture logic if possible, or just minimalist manual pydicom write.

    # Alternative: Write minimal valid DICOMs manually using pydicom to avoid complexity of Exporter+Builder deps here if desired.
    # But let's try to trust our tools.

    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ImplicitVRLittleEndian

    def write_dcm(path, pid):
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
        meta.MediaStorageSOPInstanceUID = f"1.2.3.{pid}"
        meta.TransferSyntaxUID = ImplicitVRLittleEndian
        ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
        ds.PatientID = pid
        ds.PatientName = f"Patient^{pid}"
        ds.SOPInstanceUID = f"1.2.3.{pid}"
        ds.is_little_endian = True
        ds.is_implicit_VR = True
        ds.save_as(str(path))

    write_dcm(root / "file1.dcm", "P1")
    write_dcm(subdir / "file2.dcm", "P2")

    # 3. Import
    store = DicomStore()
    DicomImporter.import_files([str(root)], store)

    # 4. Verify
    assert len(store.patients) == 2
    pids = [p.patient_id for p in store.patients]
    assert "P1" in pids
    assert "P2" in pids

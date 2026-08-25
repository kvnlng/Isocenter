import pytest
from isocenter.session import DicomSession
from isocenter.io_handlers import DicomImporter
import os
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian

# Mock tqdm to prevent console spam during tests and verify calls
class MockTqdm:
    def __init__(self, iterable=None, total=None, desc=None, unit=None, **kwargs):
        self.iterable = iterable
        self.total = total
        self.count = 0
    def __iter__(self):
        return iter(self.iterable) if self.iterable else iter([])

    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb): pass
    def update(self, n=1): self.count += n

def test_import_with_progress(tmp_path, monkeypatch):
    # 1. Setup Dummy Files
    dcm_dir = tmp_path / "dicoms"
    dcm_dir.mkdir()

    # Create 5 dummy files
    for i in range(5):
        fp = dcm_dir / f"test_{i}.dcm"
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = "1.2.3"
        meta.MediaStorageSOPInstanceUID = f"1.2.3.{i}"
        meta.TransferSyntaxUID = ImplicitVRLittleEndian
        ds = FileDataset(str(fp), {}, file_meta=meta, preamble=b"\0" * 128)
        ds.is_little_endian = True
        ds.is_implicit_VR = True
        ds.PatientID = "TEST_PATIENT"
        ds.save_as(str(fp))

    # 2. Mock Tqdm in parallel module
    import isocenter.parallel
    monkeypatch.setattr(isocenter.parallel, "tqdm", MockTqdm)

    # 3. Create Session (initializes logger)
    session = DicomSession(str(tmp_path / "session.db"))

    # 4. Import Folder
    session.ingest(str(dcm_dir))

    # 5. Verify Logger created file
    log_file = os.getenv("ISOCENTER_LOG_FILE", "isocenter.log")
    assert os.path.exists(log_file)
    with open(log_file, "r") as f:
        content = f.read()
        assert "Importing 5 files" in content

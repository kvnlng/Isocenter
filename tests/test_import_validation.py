
import pytest
from isocenter.session import DicomSession

def test_import_validation_headerless(tmp_path):
    """
    Regression Test: Ensures that files missing critical DICOM metadata
    (like SOPInstanceUID) are rejected during import, preventing downstream crashes.
    """
    # 1. Create a dummy text file masking as DICOM
    bad_file = tmp_path / "bad_log.txt"
    bad_file.write_text("This is not a DICOM file.\nIt is a log file.")

    session = DicomSession(":memory:")

    # 2. Import folder
    # This should ignore the bad file
    session.ingest(str(tmp_path))

    # 3. Verify Store is Empty
    assert len(session.store.patients) == 0, "Bad file was incorrectly ingested!"

    # 4. Export (Validation)
    # Should not crash
    export_dir = tmp_path / "export"
    try:
        session.export(str(export_dir))
    except Exception as e:
        pytest.fail(f"Export crashed: {e}")

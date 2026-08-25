import pytest
import pydicom
import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian
from isocenter.session import DicomSession

def test_ingest_planar_normalization(tmp_path):
    """
    Regression Test:
    Verify that ingesting a DICOM with PlanarConfiguration=1 (RRR...GGG...)
    results in an Instance/Series with PlanarConfiguration=0 (RGB RGB...)
    in its metadata, matching the converted numpy array.
    """
    # 1. Create Input DICOM with PlanarConfg = 1
    rows, cols = 10, 10
    arr_rgb = np.zeros((rows, cols, 3), dtype=np.uint8)
    arr_rgb[:, :, 0] = 255 # Red

    input_dir = tmp_path / "input_planar"
    input_dir.mkdir()
    dcm_path = input_dir / "test_planar.dcm"

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    file_meta.MediaStorageSOPInstanceUID = "1.2.999"
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(str(dcm_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPInstanceUID = "1.2.999"
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.PatientID = "PxNorm"
    ds.Modality = "OT"
    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.PlanarConfiguration = 1 # RRR...
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0

    # Construct Planar Config 1 Bytes
    r = arr_rgb[:,:,0].tobytes()
    g = arr_rgb[:,:,1].tobytes()
    b = arr_rgb[:,:,2].tobytes()
    ds.PixelData = r + g + b

    ds.save_as(str(dcm_path))

    # 2. Ingest
    session = DicomSession(":memory:")
    session.ingest(str(input_dir))

    # 3. Verify Internal State
    # Get the instance from the session
    # We navigate: Session -> Patient -> Study -> Series -> Instance
    assert len(session.store.patients) == 1
    pat = session.store.patients[0]
    metrics = pat.studies[0].series[0].instances[0]

    # The CRITICAL ASSERTION:
    # Isocenter should have updated the metadata to match the numpy array (Interleaved)
    # So PlanarConfiguration should be 0, not 1.
    assert metrics.attributes["0028,0006"] == 0, "Ingestion failed to normalize PlanarConfiguration to 0"

    # 4. Verify Export retains consistency
    export_dir = tmp_path / "export_norm"
    session.export(str(export_dir), use_compression=False)

    exported_files = list(export_dir.rglob("*.dcm"))
    ds_out = pydicom.dcmread(exported_files[0])

    # Exported file should also specify PlanarConfig = 0
    assert ds_out.PlanarConfiguration == 0
    assert np.array_equal(ds_out.pixel_array, arr_rgb)

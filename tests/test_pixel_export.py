
import pytest
import os
import pydicom
import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian
from isocenter.session import DicomSession

def test_pixel_integrity(tmp_path):
    # CASE 3: RGB Image with Planar Configuration = 1 (R..G..B..)
    rows, cols = 10, 10
    # RGB Pattern
    arr_rgb = np.zeros((rows, cols, 3), dtype=np.uint8)
    # Set R channel to 255, G to 128, B to 0
    arr_rgb[:, :, 0] = 255
    arr_rgb[:, :, 1] = 128

    # 2. Save as input DICOM
    input_dir = tmp_path / "input"
    if not input_dir.exists(): input_dir.mkdir()
    dcm_path = input_dir / "test_rgb.dcm"

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7" # SC Image
    file_meta.MediaStorageSOPInstanceUID = "1.2.3.99"
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(str(dcm_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientName = "TestRGB"
    ds.PatientID = "TestRGB"
    ds.SOPInstanceUID = "1.2.3.99"
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SeriesInstanceUID = "1.2.99"
    ds.StudyInstanceUID = "1.99"
    ds.Modality = "OT"
    ds.ConversionType = "WSD"

    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.PlanarConfiguration = 1 # RRR...GGG...BBB...
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0

    # Manually construction Planar Config 1 bytes
    # R plane + G plane + B plane
    r_plane = arr_rgb[:, :, 0].tobytes()
    g_plane = arr_rgb[:, :, 1].tobytes()
    b_plane = arr_rgb[:, :, 2].tobytes()
    ds.PixelData = r_plane + g_plane + b_plane

    ds.save_as(str(dcm_path))

    # Verify input first
    ds_in = pydicom.dcmread(dcm_path)
    # pydicom should convert Planar Config 1 to standard interleaved numpy array (rows, cols, 3)
    assert ds_in.pixel_array.shape == (rows, cols, 3)
    assert np.array_equal(ds_in.pixel_array, arr_rgb)
    assert ds_in.PlanarConfiguration == 1

    # 3. Ingest and Export
    session = DicomSession(":memory:")
    session.ingest(str(input_dir))

    export_dir = tmp_path / "export_rgb"
    session.export(str(export_dir), use_compression=False)

    # 4. Verify Output
    exported_files = list(export_dir.rglob("*.dcm"))
    ds_out = pydicom.dcmread(exported_files[0])

    # WE EXPECT THIS TO PASS now.

    # Check if BitsAllocated matches data size
    expected_bytes = rows * cols * 3 # 8 bit RGB = 3 bytes/pixel
    assert len(ds_out.PixelData) == expected_bytes, \
        f"PixelData size mismatch! Got {len(ds_out.PixelData)}, expected {expected_bytes}"

    # Check Pixel Values
    assert np.array_equal(ds_out.pixel_array, arr_rgb), "RGB Pixel data mismatch!"

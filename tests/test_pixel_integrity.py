
import pytest
import os
import pydicom
import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian
from isocenter.session import DicomSession

def create_dicom(path, rows=10, cols=10, samples=1, photometric="MONOCHROME2", bits=16, pixel_data=None, instance_num=1):
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7" # Secondary Capture
    file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
    file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientName = "TestIntegrity"
    ds.PatientID = "PID_INTEGRITY"
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID # Ensure in dataset
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    ds.StudyInstanceUID = pydicom.uid.generate_uid()
    ds.Modality = "OT"
    ds.ConversionType = "WSD" # Mandatory for SC
    ds.StudyDate = "20230101"
    ds.SeriesNumber = instance_num
    ds.InstanceNumber = instance_num

    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = samples
    ds.PhotometricInterpretation = photometric
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = 0

    if samples > 1:
        ds.PlanarConfiguration = 0 # Default to interleaved for input

    # Ensure transfer syntax attributes are set for write
    ds.is_little_endian = True
    ds.is_implicit_VR = True

    if pixel_data is not None:
        ds.PixelData = pixel_data
    else:
        # Generate dummy data
        if samples == 1:
            arr = np.zeros((rows, cols), dtype=np.uint16 if bits > 8 else np.uint8)
        else:
            arr = np.zeros((rows, cols, samples), dtype=np.uint8)
        ds.PixelData = arr.tobytes()

    ds.save_as(str(path), write_like_original=False)
    return ds

def test_monochrome_preservation(tmp_path):
    # Test 1: MONOCHROME2 (Standard)
    dcm_path_m2 = tmp_path / "mono2.dcm"
    create_dicom(dcm_path_m2, photometric="MONOCHROME2", samples=1, instance_num=1)

    # Test 2: MONOCHROME1 (Inverted)
    dcm_path_m1 = tmp_path / "mono1.dcm"
    create_dicom(dcm_path_m1, photometric="MONOCHROME1", samples=1, instance_num=2)


    # Ingest
    session = DicomSession(":memory:")
    session.ingest(str(tmp_path))

    # Export
    out_dir = tmp_path / "export_mono"
    session.export(str(out_dir))

    # Verify
    exported_files = list(out_dir.rglob("*.dcm"))
    assert len(exported_files) == 2

    for f in exported_files:
        ds = pydicom.dcmread(f)
        ds_orig = pydicom.dcmread(dcm_path_m2) if ds.SOPInstanceUID == pydicom.dcmread(dcm_path_m2).SOPInstanceUID else pydicom.dcmread(dcm_path_m1)

        assert ds.PhotometricInterpretation == ds_orig.PhotometricInterpretation, \
            f"PhotometricInterpretation mismatch! Expected {ds_orig.PhotometricInterpretation}, got {ds.PhotometricInterpretation}"
        assert ds.SamplesPerPixel == 1

def test_rgb_preservation(tmp_path):
    # Test 3: RGB
    dcm_path_rgb = tmp_path / "rgb.dcm"
    # Create RGB Data
    rows, cols = 10, 10
    arr = np.zeros((rows, cols, 3), dtype=np.uint8)
    arr[:,:,0] = 255 # R

    create_dicom(dcm_path_rgb, rows=rows, cols=cols, samples=3, photometric="RGB", bits=8, pixel_data=arr.tobytes())

    # Ingest
    session = DicomSession(":memory:")
    session.ingest(str(tmp_path))

    # Export
    out_dir = tmp_path / "export_rgb"
    session.export(str(out_dir))

    # Verify
    exported_files = list(out_dir.rglob("*.dcm"))
    assert len(exported_files) == 1
    ds = pydicom.dcmread(exported_files[0])

    assert ds.PhotometricInterpretation == "RGB"
    assert ds.SamplesPerPixel == 3
    assert ds.Rows == rows
    assert ds.Columns == cols
    assert ds.PlanarConfiguration == 0 # Enforced by our fix

    # Verify Data
    assert np.array_equal(ds.pixel_array, arr)

def test_samples_per_pixel_integrity(tmp_path):
    # Verify that we don't accidentally promote grayscale to 3 channels or vice versa
    dcm_path = tmp_path / "gray.dcm"
    create_dicom(dcm_path, samples=1, photometric="MONOCHROME2")

    # Ingest
    session = DicomSession(":memory:")
    session.ingest(str(tmp_path))

    out_dir = tmp_path / "export_spp"
    session.export(str(out_dir))

    ds = pydicom.dcmread(list(out_dir.rglob("*.dcm"))[0])
    assert ds.SamplesPerPixel == 1
    assert "PixelData" in ds

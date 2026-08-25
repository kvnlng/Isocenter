
"""
High-Performance DICOM Dataset Generator for Stress Testing.
Generates a structured directory of valid DICOM files.
"""

import os
import sys
import argparse
import time
import uuid
import random
import pydicom
import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import generate_uid, ExplicitVRLittleEndian, RLELossless
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm

def create_template_ds(pixel_size=512, frames=1):
    """Creates a base DICOM dataset with dummy pixel data."""
    suffix = '.dcm'
    filename = 'template' + suffix

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.2' # CT Image Storage
    file_meta.MediaStorageSOPInstanceUID = '1.2.3'
    file_meta.ImplementationClassUID = '1.2.3.4'
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(filename, {}, file_meta=file_meta, preamble=b"\0" * 128)

    # Standard Tags
    ds.PatientName = "Test^Patient"
    ds.PatientID = "123456"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.2'

    ds.Modality = "CT"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1

    ds.StudyDate = "20230101"
    ds.StudyTime = "120000"
    ds.SeriesDate = "20230101"
    ds.ContentDate = "20230101"

    ds.Rows = pixel_size
    ds.Columns = pixel_size
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.PhotometricInterpretation = "MONOCHROME2"

    # Mandatory CT Tags
    ds.SliceThickness = "1.0"
    ds.KVP = "120"
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [0.5, 0.5]

    # Use multi-frame if requested
    if frames > 1:
        ds.NumberOfFrames = frames
        # technically this is Enhanced CT but Standard CT supports it too

    # Create Dummy Pixel Data (Compressible Gradient)
    # Rationale: Random noise does not compress. We need patterns.
    # Create a simple X-Y gradient
    x = np.linspace(0, 4095, pixel_size, dtype=np.uint16)
    y = np.linspace(0, 4095, pixel_size, dtype=np.uint16)
    xv, yv = np.meshgrid(x, y)
    arr = (xv + yv) // 2
    arr = arr.astype(np.uint16)

    # Expand frames
    if frames > 1:
        arr = np.repeat(arr[np.newaxis, :, :], frames, axis=0)

    ds.PixelData = arr.tobytes()

    return ds

TEMPLATE_DS = None

def worker_generate_series(args):
    """
    Generates a single series directory with N instances.
    """
    study_dir, study_uid, series_uid, num_instances, patient_id, template_path, manufacturer, num_frames, compress = args

    series_dir = os.path.join(study_dir, f"Series_{series_uid[-6:]}")
    os.makedirs(series_dir, exist_ok=True)

    # Re-hydrate template locally
    ds = pydicom.dcmread(template_path)

    ds.PatientID = patient_id
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Manufacturer = manufacturer
    ds.ManufacturerModelName = manufacturer + "_Scanner"
    ds.DeviceSerialNumber = "GEN_" + manufacturer[:3].upper() + "_001"

    # Expand Frames if needed
    if num_frames > 1:
        ds.NumberOfFrames = num_frames
        one_frame_bytes = ds.PixelData
        ds.PixelData = one_frame_bytes * num_frames

    # Apply Compression if requested (RLE Lossless)
    # We use RLE as it's fast and standard.
    if compress:
        try:
            ds.compress(RLELossless)
        except Exception as e:
            # Fallback if specific encoder missing, warning mostly
            print(f"Warning: Compression failed: {e}")

    generated_files = []

    for i in range(num_instances):
        inst_uid = generate_uid()
        ds.SOPInstanceUID = inst_uid
        ds.InstanceNumber = i + 1

        # Add some random metadata to simulate variety
        ds.ImagePositionPatient = [float(i), 0.0, 0.0]

        fname = os.path.join(series_dir, f"Instance_{i+1}.dcm")
        ds.save_as(fname)
        generated_files.append(fname)

    return len(generated_files)

def generate_dataset(output_dir, total_instances, patients=100, series_per_study=5, frames=1, prefix="PATIENT", compress=False):
    """
    Orchestrates parallel generation.
    """
    print(f"Generating {total_instances} instances across {patients} patients (Frames={frames}, Prefix={prefix})...")
    start_time = time.time()

    # 1. Prepare Template on Disk (for workers to read fast)
    global TEMPLATE_DS_PATH
    TEMPLATE_DS_PATH = os.path.join(output_dir, ".template.dcm")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Always create 1-frame template, workers will expand it
    ds = create_template_ds(frames=1)
    ds.save_as(TEMPLATE_DS_PATH)

    # 2. Plan Hierarchy
    tasks = []
    instances_per_patient = total_instances // patients

    # Parse frames argument
    min_frames, max_frames = 1, 1
    if isinstance(frames, str) and '-' in frames:
        parts = frames.split('-')
        min_frames = int(parts[0])
        max_frames = int(parts[1])
    else:
        min_frames = int(frames)
        max_frames = int(frames)

    # Generate Studies structure
    for p_idx in range(patients):
        p_id = f"{prefix}_{p_idx:04d}"
        p_dir = os.path.join(output_dir, p_id)

        # 1 Study per patient for simplicity
        study_uid = generate_uid()

        instances_per_series = instances_per_patient // series_per_study
        if instances_per_series == 0: instances_per_series = 1

        for s_idx in range(series_per_study):
            series_uid = generate_uid()
            # Randomize Manufacturer
            # Expanded to 8 types as requested
            mfr_options = [
                "IsocenterGen", "Siemens", "GE", "Philips",
                "Canon", "Toshiba", "Hitachi", "Fujifilm"
            ]
            mfr = random.choice(mfr_options)

            # Determine frames for this series
            s_frames = random.randint(min_frames, max_frames)

            tasks.append((p_dir, study_uid, series_uid, instances_per_series, p_id, TEMPLATE_DS_PATH, mfr, s_frames, compress))

    # 3. Execute
    cpu_count = os.cpu_count() or 1
    total_files = 0

    with ProcessPoolExecutor(max_workers=cpu_count) as executor:
        futures = {executor.submit(worker_generate_series, t): t for t in tasks}

        for future in tqdm(as_completed(futures), total=len(futures), desc="Generating Series"):
            try:
                count = future.result()
                total_files += count
            except Exception as e:
                print(f"Worker failed: {e}")

    # Cleanup
    if os.path.exists(TEMPLATE_DS_PATH):
        os.remove(TEMPLATE_DS_PATH)

    duration = time.time() - start_time
    print(f"Done. Generated {total_files} files in {duration:.2f}s ({total_files/duration:.1f} files/s)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate DICOM Dataset")
    parser.add_argument("--output", "-o", type=str, required=True, help="Output Directory")
    parser.add_argument("--count", "-n", type=int, default=10000, help="Total Instances")
    parser.add_argument("--patients", "-p", type=int, default=50, help="Number of Patients")
    parser.add_argument("--frames", "-f", type=str, default="1", help="Number of Frames per Instance (int or range '100-1000')")
    parser.add_argument("--prefix", type=str, default="PATIENT", help="Patient ID Prefix")
    parser.add_argument("--compress", action="store_true", help="Enable RLE Compression")

    args = parser.parse_args()

    generate_dataset(args.output, args.count, args.patients, frames=args.frames, prefix=args.prefix, compress=args.compress)

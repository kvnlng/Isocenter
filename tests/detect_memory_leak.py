
import os
import sys
import psutil
import time
import numpy as np
import gc
import threading
import concurrent.futures
from typing import List
from isocenter.io_handlers import ExportContext, _export_instance_worker, DicomExporter

# --- Configuration ---
ITERATIONS = 50
IMAGE_SIZE_MB = 10
COMPRESSION = 'j2k' # 'j2k' or None
Mode = 'Threads' # 'Threads' or 'Processes'
FRAMES = 50 # Multiframe Test


# --- Mock Data ---
class MockInstance:
    def __init__(self, uid, size_mb):
        self.sop_instance_uid = uid
        self.attributes = {
            "0028,0002": 1,
            "0028,0004": "MONOCHROME2",
            "0028,0100": 16,
            "0028,0008": FRAMES  # Number of Frames
        }
        self.sequences = {}

        # Calculate dimensions for Multiframe
        # Total Pixels = (MB * 1024^2) / 2
        # Dimensions = Frames * H * W
        num_pixels = int((size_mb * 1024 * 1024) / 2)
        pixels_per_frame = num_pixels // FRAMES
        side = int(np.sqrt(pixels_per_frame))

        self.rows = side
        self.columns = side

        # 3D Array for Multiframe: (Frames, Rows, Cols)
        # Random data ensures compression actually works somewhat
        self.pixel_data = np.random.randint(0, 65535, (FRAMES, side, side), dtype=np.uint16)
        # Ensure we have NumberOfFrames frame attribute set on instance for logic checks?
        # Actually isocenter/io_handlers.py checks len(shape) mostly or NumberOfFrames attribute.
        # We set 0028,0008 above, but helper might check property.

    def get_pixel_data(self):
        return self.pixel_data

def get_process_memory():
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / 1024 / 1024

# --- Real Sidecar Setup ---
def create_sidecar(path, iterations, size_mb):
    # Create zlib compressed chunks
    data = os.urandom(int(size_mb * 1024 * 1024))
    import zlib
    compressed = zlib.compress(data)
    offsets = []

    with open(path, 'wb') as f:
        for _ in range(iterations):
            pos = f.tell()
            f.write(compressed)
            offsets.append((pos, len(compressed)))
    return offsets

def worker_task(i, sidecar_path, offsets):
    offset, length = offsets[i]

    # Simulate SidecarPixelLoader logic manually or import it
    from isocenter.io_handlers import SidecarPixelLoader

    # Mock instance just needs attributes for reconstruction
    inst = MockInstance(f"1.2.3.{i}", IMAGE_SIZE_MB)

    # The loader needs: sidecar_path, offset, length, alg, instance
    loader = SidecarPixelLoader(sidecar_path, offset, length, 'zlib', inst)

    # LOAD PIXELS (The suspected leak)
    pixel_array = loader()

    # Create context with loaded pixels
    ctx = ExportContext(
        instance=inst,
        output_path=f"temp_leak_test_{i}.dcm",
        patient_attributes={"PatientName": "TEST", "PatientID": "123"},
        study_attributes={"StudyInstanceUID": "1.2.3", "StudyDate": "20230101"},
        series_attributes={"SeriesInstanceUID": "1.2.3.4", "Modality": "CT"},
        pixel_array=pixel_array, # Pass the loaded array
        compression=COMPRESSION
    )

    # Run Worker
    _export_instance_worker(ctx)

    # Cleanup file
    if os.path.exists(ctx.output_path):
        os.remove(ctx.output_path)

    return i

def run_experiment(mode):
    print(f"\n=== Memory Leak Experiment: {mode} (Comp: {COMPRESSION}, Sidecar: YES) ===")

    # Setup Sidecar
    sidecar_path = "temp_leak_sidecar.bin"
    offsets = create_sidecar(sidecar_path, ITERATIONS, IMAGE_SIZE_MB)
    print(f"Created sidecar with {ITERATIONS} chunks of size {IMAGE_SIZE_MB}MB")

    gc.collect()
    start_mem = get_process_memory()
    print(f"Start Memory: {start_mem:.2f} MB")

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    print(f"Running {ITERATIONS} iterations...")

    for i in range(ITERATIONS):
        # Pass sidecar info
        future = executor.submit(worker_task, i, sidecar_path, offsets)
        future.result()

        if i % 10 == 0:
            gc.collect()
            curr = get_process_memory()
            print(f"Iter {i}: {curr:.2f} MB (Growth: {curr - start_mem:.2f} MB)")

    end_mem = get_process_memory()
    growth = end_mem - start_mem
    print(f"End Memory: {end_mem:.2f} MB")
    print(f"Total Growth: {growth:.2f} MB")

    if os.path.exists(sidecar_path):
        os.remove(sidecar_path)

    return growth

if __name__ == "__main__":
    growth = run_experiment('Threads')


import os
import sys
import psutil
import time
import numpy as np
import threading
from isocenter.session import DicomSession
from isocenter.io_handlers import DicomExporter, ExportContext, _export_instance_worker

# Mock objects
class MockInstance:
    def __init__(self, uid, size_mb):
        self.sop_instance_uid = uid
        self.attributes = {
            "0028,0002": 1, # SamplesPerPixel
            "0028,0004": "MONOCHROME2",
            "0028,0100": 16, # BitsAllocated
        }
        self.sequences = {}
        # Create random image
        # 16-bit = 2 bytes per pixel.
        # Size MB = (Pixels * 2) / 1024 / 1024
        # Pixels = (MB * 1024 * 1024) / 2
        num_pixels = int((size_mb * 1024 * 1024) / 2)
        side = int(np.sqrt(num_pixels))
        self.rows = side
        self.columns = side
        self.pixel_data = np.random.randint(0, 65535, (side, side), dtype=np.uint16)

    def get_pixel_data(self):
        return self.pixel_data

def get_memory_usage():
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / 1024 / 1024  # MB

def run_experiment(size_mb, compression='j2k'):
    print(f"\n--- Experiment: Export {size_mb}MB Image (Compression: {compression}) ---")

    # 1. Baseline Memory
    import gc
    gc.collect()
    mem_start = get_memory_usage()
    print(f"Baseline Memory: {mem_start:.2f} MB")

    # 2. Prepare Context
    inst = MockInstance("1.2.3.4", size_mb)
    ctx = ExportContext(
        instance=inst,
        output_path=f"temp_export_{size_mb}mb.dcm",
        patient_attributes={"PatientName": "TEST^MEM", "PatientID": "123"},
        study_attributes={"StudyInstanceUID": "1.2.3", "StudyDate": "20230101"},
        series_attributes={"SeriesInstanceUID": "1.2.3.4", "Modality": "CT"},
        pixel_array=inst.pixel_data,
        compression=compression
    )

    mem_loaded = get_memory_usage()
    print(f"Memory with Image Loaded: {mem_loaded:.2f} MB (+{mem_loaded - mem_start:.2f} MB)")

    # 3. Peak Tracking
    peak_mem = [mem_loaded]
    stop_tracker = False

    def tracker():
        while not stop_tracker:
            m = get_memory_usage()
            peak_mem.append(m)
            time.sleep(0.01)

    t = threading.Thread(target=tracker)
    t.start()

    # 4. Run Export
    try:
        # We need to mock DicomExporter methods if they rely on DB or files,
        # but _export_instance_worker is mostly standalone except for Sidecar logic which we bypassed by passing pixel_array.
        # However, it calls _finalize_dataset -> _compress_j2k

        _export_instance_worker(ctx)

    except Exception as e:
        print(f"Export Failed: {e}")
    finally:
        stop_tracker = True
        t.join()

    # 5. Report
    mem_final = get_memory_usage()
    peak = max(peak_mem)

    print(f"Peak Memory: {peak:.2f} MB (+{peak - mem_start:.2f} MB overhead)")
    print(f"Final Memory: {mem_final:.2f} MB")

    cost_per_worker = peak - mem_start
    print(f">> Estimated Memory Cost Per Worker: {cost_per_worker:.2f} MB")

    # Cleanup
    if os.path.exists(ctx.output_path):
        os.remove(ctx.output_path)

    return cost_per_worker

if __name__ == "__main__":
    costs = []
    costs.append(run_experiment(10, compression=None))
    costs.append(run_experiment(50, compression=None))

    # J2K is the heavy one
    try:
        costs.append(run_experiment(10, compression='j2k'))
        costs.append(run_experiment(50, compression='j2k'))
    except RuntimeError as e:
        print(f"Skipping J2K: {e}")

    avg_cost = sum(costs) / len(costs)

    total_ram_gb = psutil.virtual_memory().total / (1024**3)
    safe_ram_gb = total_ram_gb * 0.7 # Leave 30% headroom

    # Conservatively use the max cost observed
    max_cost = max(costs)

    recommended_workers = int((safe_ram_gb * 1024) / max_cost)

    print(f"\n=== CONCLUSION ===")
    print(f"System RAM: {total_ram_gb:.1f} GB")
    print(f"Max Task Memory Cost: {max_cost:.2f} MB")
    print(f"Recommended Max Workers: {recommended_workers}")

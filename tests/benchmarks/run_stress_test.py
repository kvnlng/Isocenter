
"""
Headless Stress Test Runner for Gantry.
Executes Import -> Redact -> Export pipeline and reports metrics.
"""

import os
import time
import argparse
import resource
import shutil
import logging
from gantry.session import DicomSession
from gantry.logger import get_logger

def report_resource_usage(stage_name):
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # Max RSS is in KB on Linux, bytes on Mac (usually, but let's assume KB/MB relevant)
    # Actually Python resource.getrusage behavior varies.
    # On Linux: KB. On Mac: Bytes.
    # We will just report raw and let user interpret or normalize if we detect OS.
    # Let's simple report MB (assuming KB input for Linux, dividing by 1024, or Mac dividing by 1024*1024?)
    # Safer to just print raw.
    print(f"[{stage_name}] Max RSS: {usage.ru_maxrss} (units OS dependent)")

def run_benchmark(input_dir, output_dir, db_path, return_stats=False, compress_export=True):
    print(f"--- Starting Safety Pipeline Stress Test ---")
    print(f"Input: {input_dir}")
    print(f"Output: {output_dir}")
    print(f"DB: {db_path}")

    # ensure clean start
    if os.path.exists(db_path):
        os.remove(db_path)
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    start_global = time.time()

    # [1] Initialize & Ingest
    print("\n[Step 1 & 2] Ingest")
    t0 = time.time()
    sess = DicomSession(db_path)
    sess.ingest(input_dir)
    sess.save()
    duration_ingest = time.time() - t0
    print(f"Ingest Duration: {duration_ingest:.2f}s")

    # [3] Examine
    print("\n[Step 3] Examine")
    t0 = time.time()
    sess.examine()
    duration_examine = time.time() - t0
    print(f"Examine Duration: {duration_examine:.2f}s")

    # [4] Configure (Create & Load)
    # [4] Configure (Create & Load)
    print("\n[Step 4] Configure")
    # Write a dynamic config compatible with generated data
    # Machines: GantryGen, Siemens, GE, Philips, Canon, Toshiba, Hitachi, Fujifilm
    # Serial Format: GEN_{MFR}_001

    machines_list = [
        "GantryGen", "Siemens", "GE", "Philips",
        "Canon", "Toshiba", "Hitachi", "Fujifilm"
    ]

    machine_yaml_blocks = []
    for mfr in machines_list:
        # Match logic in generate_dataset.py
        serial = f"GEN_{mfr[:3].upper()}_001"
        block = f"""  - manufacturer: "{mfr}"
    serial_number: "{serial}"
    redaction_zones:
      - [0, 10, 0, 10]"""
        machine_yaml_blocks.append(block)

    machines_yaml = "\n".join(machine_yaml_blocks)

    config_path = "stress_config.yaml"
    with open(config_path, "w") as f:
        f.write(f"""
privacy_profile: "basic"
date_jitter:
  min_days: -10
  max_days: -1
remove_private_tags: true
machines:
{machines_yaml}

phi_tags:
  "0008,0020": {{ "action": "EMPTY" }}
  "0008,0030": {{ "action": "EMPTY" }}
  "0008,0021": {{ "action": "EMPTY" }}
  "0008,0031": {{ "action": "EMPTY" }}
""")
    sess.load_config(config_path)

    # [5] Audit (Measure Twice)
    print("\n[Step 5] Audit")
    t0 = time.time()
    report = sess.audit()
    duration_audit = time.time() - t0
    print(f"Audit Found {len(report)} issues.")
    print(f"Audit Duration: {duration_audit:.2f}s")

    # [6] Backup (Reversibility)
    print("\n[Step 6] Backup Identity")
    t0 = time.time()
    sess.enable_reversible_anonymization()
    sess.lock_identities(report, auto_persist_chunk_size=200)
    sess.save()
    duration_backup = time.time() - t0
    print(f"Backup Duration: {duration_backup:.2f}s")

    # [7] Anonymize (Metadata)
    print("\n[Step 7] Anonymize")
    t0 = time.time()
    sess.anonymize(report)
    duration_anonymize = time.time() - t0
    print(f"Anonymize Duration: {duration_anonymize:.2f}s")

    # [8] Redact (Pixel Data)
    print("\n[Step 8] Redact")
    t0 = time.time()
    sess.redact(show_progress=False)
    duration_redact = time.time() - t0
    print(f"Redact Duration: {duration_redact:.2f}s")
    report_resource_usage("Post-Redact")

    # [9] Verify & Export (Cut Once)
    print("\n[Step 9] Export (Verify & Write)")
    t0 = time.time()
    sess.export(output_dir, use_compression=compress_export, show_progress=False)
    duration_export = time.time() - t0
    print(f"Export Duration: {duration_export:.2f}s")
    report_resource_usage("Post-Export")

    # Calculate Totals
    total_time = time.time() - start_global

    # Get counts for throughput calculation
    # We can infer from session.store
    # (Checking private store objects is messy but acceptable for a benchmark script)
    total_instances = 0
    # A bit inefficient to recount, but safe.
    # Alternatively, capture from sess.examine output if possible, but sess.examine prints to stdout.
    # Let's count via SQL for speed if possible, or just iterate.
    # Actually, we can just use the store backend directly.
    total_instances = sess.store_backend.get_total_instances()

    # Metrics
    fps_ingest = total_instances / duration_ingest if duration_ingest > 0 else 0
    fps_export = total_instances / duration_export if duration_export > 0 else 0
    fps_overall = total_instances / total_time if total_time > 0 else 0

    print("\n" + "="*60)
    print(f"BENCHMARK REPORT")
    print("="*60)
    print(f"Total Instances: {total_instances}")
    print(f"Total Time:      {total_time:.2f}s")
    print(f"Overall Rate:    {fps_overall:.0f} inst/sec")
    print("-" * 60)
    print(f"{'STEP':<20} | {'DURATION':<10} | {'RATE (inst/s)':<15}")
    print("-" * 60)
    print(f"{'Ingest':<20} | {duration_ingest:<10.2f} | {fps_ingest:<15.0f}")
    print(f"{'Examine':<20} | {duration_examine:<10.2f} | {'-':<15}")
    print(f"{'Audit':<20} | {duration_audit:<10.2f} | {'-':<15}")
    print(f"{'Backup':<20} | {duration_backup:<10.2f} | {'-':<15}")
    print(f"{'Anonymize':<20} | {duration_anonymize:<10.2f} | {'-':<15}")
    print(f"{'Redact':<20} | {duration_redact:<10.2f} | {'-':<15}")
    print(f"{'Export':<20} | {duration_export:<10.2f} | {fps_export:<15.0f}")
    print("-" * 60)
    print("Resource Usage:")
    report_resource_usage("Final")
    print("="*60)

    # Ensure Shutdown
    try:
        sess.close()
    except Exception as e:
        print(f"Warning: Failed to close session: {e}")

    if return_stats:
        return {
            "Ingest Duration": duration_ingest,
            "Examine Duration": duration_examine,
            "Audit Duration": duration_audit,
            "Backup Duration": duration_backup,
            "Anonymize Duration": duration_anonymize,
            "Redact Duration": duration_redact,
            "Export Duration": duration_export,
            "Total Time": total_time,
            "Total Instances": total_instances,
            "Overall Rate": fps_overall,
            "Ingest Rate": fps_ingest,
            "Export Rate": fps_export
        }

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--db", default="benchmark.db")
    parser.add_argument("--compress", action="store_true", help="Enable Export Compression (J2K)")
    args = parser.parse_args()

    run_benchmark(args.input, args.output, args.db, compress_export=args.compress)

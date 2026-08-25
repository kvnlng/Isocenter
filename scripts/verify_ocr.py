import sys
import os
import shutil

# Ensure we can import isocenter
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from isocenter.session import DicomSession

def verify_ocr():
    print("Initializing Verification Session...")
    
    db_path = "ocr_verify_session.db"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    session = DicomSession(db_path)
    
    data_dir = os.path.abspath("test_data/ocr_test_set")
    if not os.path.exists(data_dir):
        print("Regenerating test data...")
        import subprocess
        subprocess.check_call([sys.executable, "scripts/generate_ocr_test_data.py"])
        
    print(f"Ingesting data from {data_dir}...")
    session.ingest(data_dir)
    
    print("\n--- Baseline Scan (No Rules) ---")
    report_baseline = session.scan_pixel_content()
    print(f"Baseline Found {len(report_baseline)} regions.")
    
    # Now Apply a Rule that COVERS the top-left region
    # Most text in test data is at (10, 10).
    # Let's add a rule for ALL equipment (since test data has random serials)
    # Actually wait, test data has no serials? Or random ones?
    # generate_ocr_test_data makes new instances.
    # We need to target them.
    
    # Let's just create a global rule by manually patching the config?
    # Or updated generate script to use fixed serials?
    
    # Hack: Inject a rule for the first instance found
    # Instance doesn't have equipment, Series does.
    first_series = session.store.patients[0].studies[0].series[0]
    serial = first_series.equipment.device_serial_number if first_series.equipment else "UNKNOWN"
    
    print(f"\nAdding Redaction Rule for Serial: {serial}")
    print("Zone: [0, 0, 500, 200] (Should cover top text)")
    
    new_rule = {
        "serial_number": serial,
        "redaction_zones": [[0, 0, 500, 200]] 
    }
    
    session.configuration.rules.append(new_rule)
    
    print("\n--- Verified Scan (With Rule) ---")
    report_filtered = session.scan_pixel_content()
    print(f"Filtered Found {len(report_filtered)} regions.")
    
    if len(report_filtered) < len(report_baseline):
        print("\nSUCCESS: Some text was filtered out by the rule!")
    elif len(report_baseline) == 0:
         print("\nWARNING: Baseline found nothing (OCR missing?), so filtering test is inconclusive.")
    else:
        print("\nFAILURE: Filtering didn't reduce findings. Check rule matching.")

    session.close()
    if os.path.exists(db_path):
        os.remove(db_path)

if __name__ == "__main__":
    verify_ocr()

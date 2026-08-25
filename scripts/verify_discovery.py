import sys
import os
import shutil

# Ensure we use local isocenter package
sys.path.insert(0, os.path.abspath('.'))

from isocenter.session import DicomSession

def verify_discovery():
    print("Initializing Discovery Test...")
    
    db_path = "discovery_test.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    session = DicomSession(db_path)
    
    # 1. Ingest Data
    # Assuming test data exists from previous runs
    data_dir = "test_data/ocr_test_set"
    if not os.path.exists(data_dir):
        print("Regenerating test data...")
        import subprocess
        subprocess.check_call([sys.executable, "scripts/generate_ocr_test_data.py"])
        
    session.ingest(data_dir)
    
    # 2. Setup Config: Empty Zones (Scaffolded)
    serial = "SN-9999" # Known from generator
    session.configuration.rules = [{
        "serial_number": serial,
        "redaction_zones": [] # EMPTY
    }]
    
    # 3. Test Verification Skip
    print("\n--- Testing Verification Skip (Empty Zones) ---")
    report = session.scan_pixel_content(serial_number=serial)
    if len(report) == 0:
        print("SUCCESS: Skipped analysis due to empty zones.")
    else:
        print(f"FAILURE: Analyzed images despite empty zones! (Found {len(report)} findings)")
        
    # 4. Test Discovery
    print("\n--- Testing Discovery ---")
    result = session.discover_redaction_zones(serial, sample_size=10)
    zones = result.to_zones() # Use defaults
    print(f"Discovered Zones: {zones}")
    
    if len(zones) > 0:
        print("SUCCESS: Discovered hotspot zones.")
        # Optional: Validate zone coverage?
        # Check if approx [0,0,500,200] is covered?
        # Test data usually has text at top left.
        
        # Apply to config
        print("Applying discovered zones to config...")
        session.configuration.rules[0]["redaction_zones"] = [z['zone'] for z in zones]
        
        # 5. Verify again (Should now runs and filter)
        print("\n--- Re-Verifying with Discovered Zones ---")
        report_v2 = session.scan_pixel_content(serial_number=serial)
        # Should be filtered (low number of findings)
        print(f"Findings after discovery: {len(report_v2)}")
        
    else:
        print("FAILURE: Discovery found nothing.")

    session.close()
    if os.path.exists(db_path):
        os.remove(db_path)

if __name__ == "__main__":
    verify_discovery()

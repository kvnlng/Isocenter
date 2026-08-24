
import pytest
from gantry.entities import Patient, Study, Series, Instance
from gantry.session import DicomSession
from gantry.io_handlers import DicomExporter
import os

def test_safe_export_skips_phi(tmp_path):
    # 1. Setup Session & Store
    sess = DicomSession(":memory:")

    # --- Dirty Patient (Has Name) ---
    p_dirty = Patient("P_DIRTY", "Real Name")
    st1 = Study("S1", "20230101")
    se1 = Series("SE1", "OT", 1)
    # Valid attributes for export
    inst1 = Instance("I1", "1.2.840", 1)
    inst1.attributes.update({
        "0008,0020": "20230101",
        "0008,0030": "120000",
        "0018,0050": "1.0",
        "0018,0060": "120",
        "0020,0032": ["0","0","0"],
        "0020,0037": ["1","0","0","0","1","0"],
        "0028,0030": ["0.5","0.5"],
        "0028,0002": 1,
        "0028,0004": "MONOCHROME2",
        "0028,0010": 10,
        "0028,0011": 10,
        "0028,0100": 8,
        "0028,0101": 8,
        "0028,0102": 7,
        "0028,0103": 0
    })
    # Add dummy pixels
    import numpy as np
    inst1.set_pixel_data(np.zeros((10,10), dtype=np.uint8))

    se1.instances.append(inst1)
    st1.series.append(se1)
    p_dirty.studies.append(st1)
    sess.store.patients.append(p_dirty)

    # --- Clean Patient (Anonymized Name) ---
    p_clean = Patient("ANON_CLEAN", "ANONYMIZED")
    st2 = Study("S2", None) # No date allowed in safe mode currently
    se2 = Series("SE2", "OT", 1)

    inst2 = Instance("I2", "1.2.840.2", 1)
    # We must reset attributes that might be picked up?
    # Inspector currently scans OBJECT attributes (Patient.patient_name), not the instance dict.
    # But let's be safe.
    inst2.attributes.update({
        "0008,0020": "", # Empty Study Date
        "0008,0030": "120000",
        "0018,0050": "1.0",
        "0018,0060": "120",
        "0020,0032": ["0","0","0"],
        "0020,0037": ["1","0","0","0","1","0"],
        "0028,0030": ["0.5","0.5"],
        "0028,0002": 1,
        "0028,0004": "MONOCHROME2",
        "0028,0010": 10,
        "0028,0011": 10,
        "0028,0100": 8,
        "0028,0101": 8,
        "0028,0102": 7,
        "0028,0103": 0
    })
    inst2.set_pixel_data(np.zeros((10,10), dtype=np.uint8))

    se2.instances.append(inst2)
    st2.series.append(se2)
    p_clean.studies.append(st2)
    sess.store.patients.append(p_clean)

    # 2. Config for PHI Scan (Minimal)
    # We need a PHI config to define what is "dirty"
    # Create a simple config file
    config_file = tmp_path / "phi_check.json"
    import json
    config_file.write_text(json.dumps({
        "phi_tags": {
             "0010,0010": "PatientName"
        }
    }))

    # 3. Safe Export
    out_dir = tmp_path / "safe_export_out"

    # By default, I1 (P_DIRTY) should have a finding on PatientName="Real Name"
    # I2 (P_CLEAN) should have NO finding on PatientName="ANONYMIZED" (hardcoded safe in privacy.py logic)
    # Wait, privacy.py hardcodes "ANONYMIZED" check?
    # Yes: if patient.patient_name != "ANONYMIZED" -> Finding.

    # No config_path is loaded on `sess`, so this relies on the hardcoded
    # baseline PHI check in privacy.py. check_burned_in=True makes export()
    # run scan_for_phi() first and skip any patient with a finding.

    sess.export(str(out_dir), check_burned_in=True)

    # 4. Assertions
    # Dirty Patient file I1 should NOT exist
    # Scan recursively to be sure
    all_files = list(out_dir.rglob("*.dcm"))

    # We expect I2 (Clean) to exist as 0001.dcm in Subject_ANON_CLEAN folder
    # We expect I1 (Dirty) to NOT exist (it would be 0001.dcm in Subject_P_DIRTY)

    dirty_files = [f for f in all_files if "Subject_P_DIRTY" in str(f)]
    assert len(dirty_files) == 0

    clean_files = [f for f in all_files if "Subject_ANON_CLEAN" in str(f) and f.name == "I2.dcm"]
    assert len(clean_files) == 1
    assert clean_files[0].exists()

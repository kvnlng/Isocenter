
import pytest
import os
from gantry.session import DicomSession
from gantry.entities import Patient, Study, Series, Instance
from datetime import date
import numpy as np

def test_safe_export_allows_jittered_dates(tmp_path):
    """
    Regression Test: Ensures that 'Safe Export' does NOT skip data
    that has been successfully remediated (e.g. date shifted).
    """
    # 1. Setup Session
    session = DicomSession(":memory:")

    # 2. Add Data with PHI Date
    p = Patient("P_TEST", "ANONYMIZED") # Name is safe
    s = Study("S1", date(2023, 1, 1))   # Date is PHI (unsafe)
    se = Series("SE1", "CT", 1)
    inst = Instance("I1", "1.2.3", 1)
    inst.set_pixel_data(np.zeros((10,10), dtype=np.uint16))

    # Essential attributes for export validation
    inst.attributes.update({
        "0008,0020": "20230101", # Study Date
        "0018,0050": "1.0",
        "0020,0032": ["0","0","0"],
        "0020,0037": ["1","0","0","0","1","0"],
        "0028,0030": ["0.5","0.5"]
    })

    se.instances.append(inst)
    s.series.append(se)
    p.studies.append(s)
    session.store.patients.append(p)

    # 3. Apply Jitter Remediation
    # Using 'audit' effectively, but manually calling scan_for_phi/remediate
    # to simulate the exact workflow user described.
    findings = session.scan_for_phi()
    assert len(findings) > 0, "Setup failed: PHI finding should be detected."

    session.anonymize(findings)

    # Verify date changed and flag set
    assert s.study_date != date(2023, 1, 1)
    assert getattr(s, "date_shifted", False) is True

    # 4. Perform Safe Export
    export_dir = tmp_path / "export_success"
    session.export(str(export_dir), check_burned_in=True)

    # 5. Verify Export Succeeded
    # The folder should be Subject_{PatientID} (which is anonymized/remediated)
    # We can check session patient ID
    exported_patient_folder = export_dir / f"Subject_{p.patient_id}"
    assert exported_patient_folder.exists(), f"Safe export failed. Expected folder {exported_patient_folder} not found."

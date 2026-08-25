import pytest
import os
from isocenter.session import DicomSession
from isocenter import Builder
from datetime import date
from isocenter.privacy import PhiReport
from isocenter.io_handlers import DicomExporter

def test_parallel_import_and_scan(tmp_path):
    # 1. Generate Data
    raw_dir = tmp_path / "raw"
    db_path = str(tmp_path / "parallel.db")

    # Create 20 files
    # We create one patient per loop or just one patient with many instances?
    # Loop creates P_0, P_1...
    for i in range(20):
        # We need actual .dcm files for import
        p = (
            Builder.start_patient(f"P_{i}", f"Patient {i}")
            .add_study(f"S_{i}", date(2023,1,1))
            .add_series(f"SE_{i}", "CT", 1)
            .add_instance(f"I_{i}", "1.2.840.10008.5.1.4.1.1.2", 1)
                .set_attribute("0020,0032", ["0", "0", "0"]) # Image Position
                .set_attribute("0020,0037", ["1", "0", "0", "0", "1", "0"]) # Orientation
                .set_attribute("0028,0030", ["1", "1"]) # Pixel Spacing
                .set_attribute("0018,0050", "1.0") # Slice Thickness (Type 2)
                .set_attribute("0018,0060", "120") # KVP (Type 2)
                .end_instance()
            .end_series()
            .end_study()
            .build()
        )

        # Inject dummy pixels to pass export validation
        import numpy as np
        for st in p.studies:
            for se in st.series:
                for inst in se.instances:
                    inst.set_pixel_data(np.zeros((10,10), dtype=np.uint8))

        DicomExporter.save_patient(p, str(raw_dir))

    # 2. Parallel Import
    session = DicomSession(db_path)
    session.ingest(str(raw_dir))

    assert len(session.store.patients) == 20

    # 3. Parallel Scan
    report = session.scan_for_phi()
    assert isinstance(report, PhiReport)
    assert len(report) >= 20 # At least Names should be flagged

    # 4. Verify Rehydration
    # Check if finding.entity refers to the LIVE object in session.store
    finding = report[0]
    live_patient = next(p for p in session.store.patients if p.patient_id == finding.entity_uid)

    # Identity check might fail if rehydration missed, logic check is safer
    assert finding.entity is live_patient, "Finding entity should be the live object, not a clone"

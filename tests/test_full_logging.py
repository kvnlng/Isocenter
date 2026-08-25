import pytest
import os
import json
from isocenter.session import DicomSession
from isocenter.builders import DicomBuilder
from datetime import date
import numpy as np

def test_full_logging_coverage(tmp_path):
    # 1. Setup
    log_file = os.getenv("ISOCENTER_LOG_FILE", "isocenter.log")
    if os.path.exists(log_file):
        os.remove(log_file)

    session_file = tmp_path / "logging_session.pkl"
    session = DicomSession(str(session_file))

    # 2. Add Data manually (skipping import logging which is tested elsewhere)
    p = DicomBuilder.start_patient("P_LOG", "Log Tester") \
        .add_study("1.1", date(2023,1,1)) \
            .add_series("1.1.1", "CT", 1) \
                .set_equipment("Mfg", "Model", "SN-LOG") \
                .add_instance("1.1.1.1", "1.2", 1) \
                    .set_pixel_data(np.zeros((10,10), dtype=np.uint16)) \
                .end_instance() \
            .end_series() \
        .end_study() \
        .build()
    session.store.patients.append(p)

    # 3. Excercise Session Methods that should log

    # Inventory
    session.examine()

    # PHI Scan (Empty config warning)
    session.scan_for_phi()

    # Config Loading
    config_path = tmp_path / "logging_config.yaml"
    with open(config_path, "w") as f:
        f.write('version: "1.0"\nmachines: []\n')

    session.load_config(str(config_path))

    # Config Execution (Empty)
    session.redact()

    # Scaffold
    scaffold_path = tmp_path / "scaffold_log.json"
    session.create_config(str(scaffold_path))

    # Export
    export_dir = tmp_path / "export_log"
    session.export(str(export_dir))

    # 4. Verify Log Content
    assert os.path.exists(log_file)
    with open(log_file, "r") as f:
        log_content = f.read()

    print("\n--- LOG CONTENT ---")
    print(log_content)
    print("-------------------")

    # Check for expected strings from our recent changes
    assert "Session started" in log_content
    assert "Generating inventory report" in log_content
    assert "PHI Scan Complete" in log_content
    assert "Loading configuration from" in log_content
    assert "Exporting session to" in log_content
    # Parallel export logs summaries, not individual files
    # assert "Starting parallel export" in log_content  # Removed: suppressed in session.export()
    assert "Export Complete" in log_content
    # Check specifically for the scaffold message
    assert "Scaffolded Unified Config" in log_content

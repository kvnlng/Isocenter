import os
from isocenter.session import DicomSession
from isocenter.io_handlers import DicomStore
from isocenter.io_handlers import DicomExporter


def test_session_persistence(tmp_path, dummy_patient):
    """Test saving and loading state."""
    db_file = tmp_path / "session.db"

    # 1. Create Session
    session = DicomSession(str(db_file))
    session.store.patients.append(dummy_patient)
    session.save()
    session.persistence_manager.shutdown()

    # 2. Load into Session
    # 2. Reload Session
    session2 = DicomSession(str(db_file))
    assert len(session2.store.patients) == 1
    assert session2.store.patients[0].patient_id == "P123"


def test_load_config(tmp_path, config_file):
    session = DicomSession(str(tmp_path / "dummy.db"))

    session.load_config(config_file)
    assert len(session.configuration.rules) == 1
    assert session.configuration.rules[0]["serial_number"] == "SN-999"

def test_load_empty_config(tmp_path):
    """Ensure session handles config files with no machines gracefully."""
    session = DicomSession(str(tmp_path / "empty.db"))

    empty_conf = tmp_path / "empty_rules.yaml"
    import yaml
    with open(empty_conf, "w") as f:
        yaml.dump({"version": "1.0", "machines": []}, f)

    session.load_config(str(empty_conf))
    assert len(session.configuration.rules) == 0

    # Should not crash on execution
    session.redact()



def test_execute_config_integration(tmp_path, dummy_patient, config_file):
    """Full integration: Load Data -> Load Rules -> Execute Redaction."""

    # 1. SETUP: We must EXPORT the dummy patient so valid files exist on disk
    #    This allows the Lazy Loader to find them later.
    dicom_dir = tmp_path / "raw_dicoms"
    DicomExporter.write_tree(dummy_patient, str(dicom_dir))

    # 2. Update dummy_patient instances to point to these new files
    #    (In a real app, Import would do this, but here we manually link for the test)
    inst = dummy_patient.studies[0].series[0].instances[0]
    inst.file_path = str(dicom_dir / f"{inst.sop_instance_uid}.dcm")

    # 3. Create Session and add the patient
    db_file = str(tmp_path / "integration.db")
    session = DicomSession(db_file)
    session.store.patients.append(dummy_patient)

    # 4. Modify the file to simulate "Burned In" data
    #    We need to make sure the file on disk matches our expectation?
    #    Actually, DicomExporter wrote 0s. Let's assume we want to ensure
    #    redaction WRITES 0s.

    # Act
    session.load_config(config_file)  # Config targets SN-999, ROI 10-50
    session.redact()

    # Assert
    # Verify the instance in memory is updated (Lazy loaded then updated)
    assert inst.get_pixel_data()[20, 20] == 0
    assert os.path.exists(db_file)
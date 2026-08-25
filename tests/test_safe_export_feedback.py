
import pytest
import os
from isocenter.session import DicomSession
from isocenter.builders import DicomBuilder
import datetime

def test_safe_export_feedback(tmp_path, capsys):
    # 1. Create a session with PHI (Patient Name, Study Date)
    session_dir = tmp_path / "session"
    session = DicomSession(str(session_dir))

    p = DicomBuilder.start_patient("P123", "John Doe") \
        .add_study("S1", datetime.date(2023, 1, 1)) \
        .add_series("SE1", "CT", 1) \
        .add_instance("I1", "1.2.3", 1) \
        .end_instance() \
        .end_series() \
        .end_study() \
        .build()

    session.store.patients.append(p)

    # 2. Attempt Safe Export (Should find PHI)
    export_dir = tmp_path / "export"
    session.export(str(export_dir), check_burned_in=True)

    # 3. Capture Output
    captured = capsys.readouterr()
    stdout = captured.out

    print("--- STDOUT ---")
    print(stdout)
    print("--------------")

    # 4. Assert Detailed Feedback
    assert "Safety Scan Found Issues" in stdout
    assert "The following tags were flagged as dirty:" in stdout

    # Check table headers
    assert "Tag" in stdout
    assert "Description" in stdout
    assert "Count" in stdout
    assert "Examples" in stdout

    # Check content
    assert "0010,0010" in stdout # PatientName
    assert "John Doe" in stdout
    assert "0008,0020" in stdout # StudyDate

    # Check Config Suggestion
    assert "Suggested Config Update:" in stdout
    assert '"action": "REMOVE"' in stdout
    assert '"name": "patient_name"' in stdout

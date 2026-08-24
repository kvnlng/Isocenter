
import pytest
import os
from gantry.session import DicomSession
from gantry.builders import DicomBuilder
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
    session.export(str(export_dir), safe=True)

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

    # 5. The suggested config block must be valid, parseable JSON,
    # printed exactly once (no duplicated closing braces).
    import json as _json

    block = stdout.split("Suggested Config Update:")[-1]
    start = block.index("{")
    end = block.rindex("}")
    parsed = _json.loads(block[start:end + 1])
    assert parsed["phi_tags"]["0010,0010"]["action"] == "REMOVE"
    # counts live in the findings table, not in the JSON block
    assert "//" not in block[start:end + 1]
    # exactly one closing pair, not the previous duplicate
    assert stdout.count('    }') == 1

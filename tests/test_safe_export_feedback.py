
import pytest
import os
from isocenter.session import DicomSession
from isocenter.builders import DicomBuilder
import datetime

def test_safe_export_feedback(tmp_path, capsys):
    # 1. Create a session with PHI (Patient Name, Study Date)
    session_dir = tmp_path / "session"
    with DicomSession(str(session_dir)) as session:
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
        # "dirty" means "has unsaved changes" everywhere else in the
        # codebase; the PHI report no longer borrows the word.
        assert "The following tags still carry identifiers:" in stdout
        assert "dirty" not in stdout.lower()

        # Check table headers
        assert "Tag" in stdout
        assert "Description" in stdout
        assert "Count" in stdout
        # No Examples column (#578): it printed the first flagged value per
        # tag, which is a patient's name on the console and in any CI log
        # capturing it. The tag, description and count say what to fix.
        assert "Examples" not in stdout

        # Check content
        assert "0010,0010" in stdout # PatientName
        assert "John Doe" not in stdout + captured.err
        assert "0008,0020" in stdout # StudyDate

        # Check Config Suggestion
        assert "Suggested Config Update:" in stdout
        # The fragment is YAML since #20, and whether it *parses* is pinned
        # by tests/test_suggested_config.py. Here we only check the report
        # reaches the point of offering one.
        assert "phi_tags:" in stdout
        assert "action: REMOVE" in stdout


def test_the_safety_table_prints_no_instance_level_value(tmp_path, capsys):
    """An identifier only an instance holds never reaches the console (#578).

    Referring Physician's Name, on the instance and nowhere else, so its
    row's example could only ever have been this value: a patient-level
    tag would have shown the patient's name first and left the sentinel
    unprinted whether or not the column was there.
    """
    with DicomSession(str(tmp_path / "session.db")) as session:
        p = DicomBuilder.start_patient("P578", "Row^Holder") \
            .add_study("S1", datetime.date(2023, 1, 1)) \
            .add_series("SE1", "CT", 1) \
            .add_instance("I1", "1.2.3", 1) \
            .end_instance() \
            .end_series() \
            .end_study() \
            .build()
        instance = p.studies[0].series[0].instances[0]
        instance.set_attr("0008,0090", "SENTINEL^578")
        session.store.patients.append(p)

        session.export(str(tmp_path / "export"), check_burned_in=True)
        captured = capsys.readouterr()

    assert "0008,0090" in captured.out, captured.out
    assert "SENTINEL^578" not in captured.out + captured.err
    assert "Row^Holder" not in captured.out + captured.err

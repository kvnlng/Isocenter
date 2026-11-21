"""`format="wfdb"` reads `subset` and `check_burned_in`, and sets the counters (#783).

Owner rulings of 2026-11-20: Q10, a WFDB export takes `subset` as `dicom`
does; Q11, it takes `check_burned_in`, which re-audits the tags only (a
waveform has no pixels to read); Q12, it sets the report's "Instances
Written" row. Before #783 the first two raised `TypeError` and the row was
always removed after a WFDB export.

Each test pairs the option with a call that differs only in it, so a
pipeline that ignores the option fails on the count, not on a premise.
"""
import os
import sqlite3

import pytest

from isocenter.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")


def _two_patients(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "a.dcm"), num_samples=64,
                  patient_id="WFPAT-A", patient_name="Alpha^Ann")
    write_fixture(str(src / "b.dcm"), num_samples=64,
                  patient_id="WFPAT-B", patient_name="Beta^Bob")
    session = DicomSession(persistence_file=str(tmp_path / "w.db"))
    session.ingest(str(src))
    return session


def _records(paths):
    return sorted(os.path.basename(p) for p in paths if p.endswith(".hea"))


def _report(session, path):
    session.generate_report(str(path))
    return path.read_text(encoding="utf-8")


def _rows(tmp_path, action, needle):
    """Read after the session is closed, so the audit writer has drained."""
    with sqlite3.connect(str(tmp_path / "w.db")) as conn:
        rows = conn.execute(
            "SELECT action_type, details FROM audit_log ORDER BY rowid"
        ).fetchall()
    return [d for a, d in rows if a == action and needle in d]


def _sop_of(session, patient_id):
    [patient] = [p for p in session.store.patients
                 if p.patient_id == patient_id]
    return [inst.sop_instance_uid for st in patient.studies
            for se in st.series for inst in se.instances]


def test_subset_selects_the_records_written(tmp_path):
    with _two_patients(tmp_path) as session:
        [uid] = _sop_of(session, "WFPAT-A")
        every = session.export(str(tmp_path / "all"), format="wfdb")
        some = session.export(str(tmp_path / "one"), format="wfdb",
                              subset=[uid])
    assert len(_records(every)) == 2, every
    assert len(_records(some)) == 1, some


def test_an_unmatched_subset_value_writes_its_row(tmp_path):
    with _two_patients(tmp_path) as session:
        written = session.export(str(tmp_path / "out"), format="wfdb",
                                 subset=["1.2.3.4.5.6.7.8.9"])
    rows = _rows(tmp_path, "WARNING", f"export to {tmp_path / 'out'}: ")
    assert written == []
    assert len(rows) == 1 and rows[0].startswith("WFDB export to "), rows


def test_check_burned_in_withholds_a_waveform_still_carrying_phi(tmp_path):
    """Nothing is anonymized, so the tag re-audit finds the name on both;
    the same export without the option writes both records."""
    with _two_patients(tmp_path) as session:
        session.audit()
        plain = session.export(str(tmp_path / "plain"), format="wfdb")
        checked = session.export(str(tmp_path / "checked"), format="wfdb",
                                 check_burned_in=True)
    withheld = _rows(tmp_path, "WARNING", "withheld instance")
    exports = _rows(tmp_path, "EXPORT", "checked")
    assert len(_records(plain)) == 2, plain
    assert _records(checked) == [], checked
    assert len([r for r in withheld if r.startswith("WFDB export to ")]) == 2
    assert exports and "2 more withheld by the pre-export scan" in exports[-1]


def test_check_burned_in_passes_an_anonymized_waveform(tmp_path):
    with _two_patients(tmp_path) as session:
        session.audit()
        session.anonymize()
        checked = session.export(str(tmp_path / "checked"), format="wfdb",
                                 check_burned_in=True)
    withheld = _rows(tmp_path, "WARNING", "withheld instance")
    assert len(_records(checked)) == 2, checked
    assert withheld == []


def test_a_wfdb_export_sets_the_instances_written_row(tmp_path):
    with _two_patients(tmp_path) as session:
        session.audit()
        session.anonymize()
        session.export(str(tmp_path / "out"), format="wfdb")
        text = _report(session, tmp_path / "r.md")
    assert "| Instances Written | 2 of 2 requested |" in text, text


def test_a_withheld_waveform_counts_as_asked_for(tmp_path):
    with _two_patients(tmp_path) as session:
        session.audit()
        session.export(str(tmp_path / "out"), format="wfdb",
                       check_burned_in=True)
        text = _report(session, tmp_path / "r.md")
    assert "| Instances Written | 0 of 2 requested |" in text, text

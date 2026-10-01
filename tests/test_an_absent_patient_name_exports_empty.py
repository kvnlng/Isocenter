"""An absent Patient's Name is exported empty, never as `Unknown` (#746).

Measured at 7579d4df: ingest gave a source with no Patient's Name the name
`Unknown` (`io_handlers.ingest_worker`'s meta default), the export stamped
it from the `Patient`, and the scan's REPLACE arm exempted the literal by
name (`privacy.PhiInspector.scan_patient`). A file with no name was
therefore exported carrying `0010,0010 PN 'Unknown'` beside
`(0012,0062) YES`, under `KEEP` and under the default floor alike, and the
run graded PASS. Eleven `fingerprint/output.json` members carried it.

A placeholder cannot be told from recorded data downstream (#60's
class), so absent now becomes empty: what Type 2 "unknown" is, and what
#584 does for a Patient ID. With the placeholder gone, a source whose name
is literally `Unknown` is that patient's recorded value, and REPLACE
replaces it, as #584 ruled for a Patient ID reading `UNKNOWN`.
Owner ruling Q4 (2026-10-01): CONFIG_VERSION stays 2.0.
"""
import os
import sqlite3

import pydicom
import pytest

from isocenter import Session

from support.ct_small_files import write_ct


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(tmp_path, name):
    """CT_small with Patient ID `PA`; `name` None deletes Patient's Name."""
    path = write_ct(tmp_path / "in" / "a.dcm", "PA", 746)
    ds = pydicom.dcmread(path)
    if name is None:
        del ds.PatientName
    else:
        ds.PatientName = name
    ds.save_as(path)
    return path


def _exported(folder):
    files = [os.path.join(root, f) for root, _, names in os.walk(folder)
             for f in names if f.endswith(".dcm")]
    assert len(files) == 1, files
    return pydicom.dcmread(files[0])


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    line = next(line for line in path.read_text().splitlines()
                if "Validation Status" in line)
    for grade in ("REVIEW_REQUIRED", "PASS", "FAIL"):
        if f"**{grade}**" in line:
            return grade
    return line


def _run(tmp_path, name, rule=None):
    _write(tmp_path, name)
    session = Session(str(tmp_path / "s.db"))
    if rule is not None:
        session.configuration.set_phi_tag("0010,0010", rule)
    session.ingest(str(tmp_path / "in"))
    report = session.audit()
    session.anonymize(report)
    session.export(str(tmp_path / "out"), use_compression=False,
                   show_progress=False)
    return session, report


def test_an_absent_name_under_keep_is_held_and_exported_empty(tmp_path):
    session, _ = _run(tmp_path, None, rule="KEEP")
    with session:
        assert [p.patient_name for p in session.store.patients] == [""]
        ds = _exported(tmp_path / "out")
        element = ds[0x00100010]
        assert element.VR == "PN"
        assert str(element.value) == ""


def test_an_absent_name_under_the_floor_is_exported_empty(tmp_path):
    session, report = _run(tmp_path, None)
    with session:
        assert [f for f in report.findings if f.tag == "0010,0010"] == []
        ds = _exported(tmp_path / "out")
        assert str(ds[0x00100010].value) == ""
        assert ds.PatientIdentityRemoved == "YES"
        assert _grade(session, tmp_path) == "PASS"


def test_an_absent_name_reads_back_empty_after_a_reopen(tmp_path):
    session, _ = _run(tmp_path, None, rule="KEEP")
    with session:
        session.save(sync=True)
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        assert conn.execute("SELECT patient_name FROM patients").fetchall() == [("",)]
    with Session(str(tmp_path / "s.db")) as reopened:
        assert [p.patient_name for p in reopened.store.patients] == [""]


def test_a_source_name_reading_unknown_is_replaced(tmp_path):
    """The literal is a recorded name now, and the floor's REPLACE
    replaces it like any other."""
    session, report = _run(tmp_path, "Unknown")
    with session:
        assert [(f.entity_type, f.value) for f in report.findings
                if f.tag == "0010,0010" and f.entity_type == "Patient"] == [
                    ("Patient", "Unknown")]
        assert str(_exported(tmp_path / "out")[0x00100010].value) == "ANONYMIZED"


def test_an_absent_name_under_empty_refuses_the_lock(tmp_path):
    """The blank-name refusal now covers an absent name, as it already
    covered a present empty one: the token used to stash `Unknown`."""
    _write(tmp_path, None)
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.set_phi_tag("0010,0010", "EMPTY")
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization()
        with pytest.raises(RuntimeError,
                           match="holds no value in 0010,0010 under a rule of EMPTY"):
            session.lock_identities("PA")


def test_a_wfdb_export_of_an_absent_name_writes_no_placeholder(tmp_path):
    from scripts.generate_waveform_test_data import write_fixture

    source = tmp_path / "in"
    source.mkdir()
    path = write_fixture(str(source / "ecg.dcm"), num_samples=64,
                         patient_id="PA", patient_name="Alpha^Ann")
    ds = pydicom.dcmread(path)
    del ds.PatientName
    ds.save_as(path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        assert [p.patient_name for p in session.store.patients] == [""]
        session.anonymize(session.audit())
        written = session.export(str(tmp_path / "wfdb"), format="wfdb")
        assert written
    contents = b"".join(open(os.path.join(root, f), "rb").read()
                        for root, _, names in os.walk(tmp_path / "wfdb")
                        for f in names)
    assert b"Unknown" not in contents

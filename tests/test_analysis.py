import pytest
import pandas as pd
from isocenter.session import DicomSession
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.privacy import PhiReport
from datetime import date
import os

def test_phi_report_analysis(tmp_path):
    db_path = str(tmp_path / "analysis.db")
    session = DicomSession(db_path)

    # Setup data with PHI
    p = Patient("P_PHI", "John Doe")
    session.store.patients.append(p)
    session.save()

    # Scan
    report = session.audit()
    assert isinstance(report, PhiReport)
    assert len(report) > 0 # Should find Name and ID

    # Test DataFrame
    df = report.to_dataframe()
    assert isinstance(df, pd.DataFrame)
    assert "patient_id" in df.columns
    assert "reason" in df.columns
    assert "John Doe" in df["value"].values

    # Verify iteration behavior (list-like)
    count = 0
    for finding in report:
        count += 1
    assert count == len(report)

def test_batch_preserve_from_report(tmp_path):
    db_path = str(tmp_path / "batch_report.db")
    key_path = str(tmp_path / "batch.key")
    session = DicomSession(db_path)
    session.enable_reversible_anonymization(key_path)

    # Create patient
    pid = "BATCH_P1"
    p = Patient(pid, "Batch Patient")
    st = Study("S1", date(2023,1,1))
    se = Series("SE1", "CT", 1)
    inst = Instance("I1", "1.2.3", 1)
    inst.file_path = None
    inst.set_attr("0010,0010", "Batch Patient")
    inst.set_attr("0010,0020", pid)
    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    session.store.patients.append(p)
    session.save()

    # Scan
    report = session.audit()

    # Preserve using Report directly
    session.lock_identities_batch(report)
    session.save()
    session.persistence_manager.shutdown()

    # Verify persistence
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT attributes_json FROM instances")
    row = cur.fetchone()
    # Now that we use sequences and serialize them, we expect 0400,0500
    # in the persisted attributes_json (inside __sequences__ block)
    assert "0400,0500" in row[0]
    conn.close()

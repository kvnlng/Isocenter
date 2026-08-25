import pytest
import os
import sqlite3
import json
from isocenter.session import DicomSession
from isocenter.entities import Patient, Study, Series, Instance
from datetime import date

def test_optimization_preservation(tmp_path):
    db_path = str(tmp_path / "opt.db")
    key_path = str(tmp_path / "opt.key")

    session = DicomSession(db_path)
    session.enable_reversible_anonymization(key_path)

    # 1. Create Patient with Instances
    pid = "OPT_001"
    p = Patient(pid, "Optimization Test")
    st = Study("ST_1", date(2023,1,1))
    se = Series("SE_1", "CT", 1)

    instances = []
    for i in range(5):
        inst = Instance(f"SOP_{i}", "1.2.3", i)
        inst.file_path = None
        inst.set_attr("0010,0010", "Optimization Test") # Name
        inst.set_attr("0010,0020", pid) # ID
        instances.append(inst)
        se.instances.append(inst)

    st.series.append(se)
    p.studies.append(st)
    session.store.patients.append(p)
    session.save()
    session.persistence_manager.flush()

    # 2. Monitor SQL to verify update_attributes is used
    # We will hook into sqlite3 to check calls or just verify data is updated without full re-insert
    # But since we mocked/implmented update_attributes, let's verify functional correctness first.

    # Preserve Identity
    session.lock_identities(pid)
    session.save()
    session.persistence_manager.flush()

    # Verify data persistence
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Check if attributes_json has the private tag
    # Check if attributes_json has the persisted sequence
    cur.execute("SELECT attributes_json FROM instances WHERE sop_instance_uid = 'SOP_0'")
    row = cur.fetchone()
    attrs = json.loads(row[0])

    assert "__sequences__" in attrs
    assert "0400,0500" in attrs["__sequences__"]

    conn.close()

def test_batch_reversibility(tmp_path):
    db_path = str(tmp_path / "batch.db")
    key_path = str(tmp_path / "batch.key")
    session = DicomSession(db_path)
    session.enable_reversible_anonymization(key_path)

    # Create multiple patients
    ids = ["P1", "P2", "P3"]
    for pid in ids:
        p = Patient(pid, f"Name {pid}")
        st = Study(f"ST_{pid}", date(2023,1,1))
        se = Series(f"SE_{pid}", "CT", 1)
        inst = Instance(f"SOP_{pid}", "1.2.3", 1)
        inst.file_path = None
        inst.set_attr("0010,0010", f"Name {pid}")
        inst.set_attr("0010,0020", pid)
        se.instances.append(inst)
        st.series.append(se)
        p.studies.append(st)
        session.store.patients.append(p)

    session.save()
    session.persistence_manager.flush()

    # Batch Preserve
    session.lock_identities_batch(ids)
    session.save()
    session.persistence_manager.flush()

    # Check persistence
    # Checking for the tag 0400,0500 in the JSON blob
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM instances WHERE attributes_json LIKE '%0400,0500%'")
    count = cur.fetchone()[0]
    assert count == 3
    conn.close()

def test_batch_chunking(tmp_path):
    """Verify auto_persist_chunk_size logic."""
    db_path = str(tmp_path / "chunk.db")
    key_path = str(tmp_path / "chunk.key")
    session = DicomSession(db_path)
    session.enable_reversible_anonymization(key_path)

    # Create multiple patients
    ids = ["C1", "C2", "C3"]
    for pid in ids:
        p = Patient(pid, f"Name {pid}")
        st = Study(f"ST_{pid}", date(2023,1,1))
        se = Series(f"SE_{pid}", "CT", 1)
        # 10 instances each
        for i in range(10):
            inst = Instance(f"SOP_{pid}_{i}", "1.2.3", i)
            inst.file_path = None
            inst.set_attr("0010,0010", f"Name {pid}")
            inst.set_attr("0010,0020", pid)
            se.instances.append(inst)
        st.series.append(se)
        p.studies.append(st)
        session.store.patients.append(p)

    session.save()
    session.persistence_manager.flush()

    # Batch Preserve with Chunking
    # 3 patients * 10 instances = 30 instances. Chunk 5 => 6 flushes.
    res = session.lock_identities_batch(ids, auto_persist_chunk_size=5)

    # Must return empty list
    assert res == []

    # Verify persistence
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Check if we have 30 modified instances
    cur.execute("SELECT count(*) FROM instances WHERE attributes_json LIKE '%0400,0500%'")
    count = cur.fetchone()[0]
    assert count == 30
    conn.close()

def test_lock_identities_wrapper_chunking(tmp_path):
    """
    Verify that calling the wrapper `lock_identities` (singular) with a list
    AND `auto_persist_chunk_size` correctly forwards the argument to `lock_identities_batch`
    and results in memory clearing (empty return).
    """
    db_path = str(tmp_path / "wrapper.db")
    key_path = str(tmp_path / "wrapper.key")
    session = DicomSession(db_path)
    session.enable_reversible_anonymization(key_path)

    # Create 2 patients
    ids = ["W1", "W2"]
    for pid in ids:
        p = Patient(pid, f"Name {pid}")
        st = Study(f"ST_{pid}", date(2023,1,1))
        se = Series(f"SE_{pid}", "CT", 1)
        inst = Instance(f"SOP_{pid}", "1.2.3", 1)
        inst.file_path = None
        inst.set_attr("0010,0010", f"Name {pid}")
        inst.set_attr("0010,0020", pid)
        se.instances.append(inst)
        st.series.append(se)
        p.studies.append(st)
        session.store.patients.append(p)

    session.save()
    session.persistence_manager.flush()

    # Act: Call wrapper with chunk size = 1
    # Should trigger chunking: persist 1, clear 1.
    res = session.lock_identities(ids, auto_persist_chunk_size=1)

    # Assert: Should return empty list because chunking happened
    assert res == []

    # Verify persistence in DB
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM instances WHERE attributes_json LIKE '%0400,0500%'")
    count = cur.fetchone()[0]
    assert count == 2
    conn.close()


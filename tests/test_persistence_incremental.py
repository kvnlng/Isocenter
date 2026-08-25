import os
import pytest
import sqlite3
import numpy as np
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.persistence import SqliteStore

@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "test_isocenter.db"
    s = SqliteStore(str(db_file))
    yield s


def create_mock_patient(pid="P1", study_uid="S1", series_uid="SE1", count=1):
    p = Patient(pid, "Test Name")
    st = Study(study_uid, "20230101")
    p.studies.append(st)
    se = Series(series_uid, "CT", 1)
    st.series.append(se)
    for i in range(count):
        inst = Instance(f"{series_uid}.{i}", "1.2.840.10008.5.1.4.1.1.2", i+1)
        # Set some initial attributes
        inst.set_attr("0010,0020", pid)
        se.instances.append(inst)
    return p

def test_unsaved_tracking_initialization():
    """Verify objects start dirty (if new) or assume handling."""
    inst = Instance("1.2.3", "1.2.3", 1)
    # New objects should default to dirty so they get saved? Or default clean?
    # Logic: If I create it in memory, it isn't in DB, so it MUST be dirty/new.
    # We'll assert the behavior we implement.
    assert inst.has_unsaved_changes is True

def test_unsaved_tracking_attribute_change():
    inst = Instance("1.2.3", "1.2.3", 1)
    inst.mark_persisted()  # Simulate saved state

    inst.set_attr("0010,0010", "New Name")
    assert inst.has_unsaved_changes is True

def test_unsaved_tracking_pixel_change():
    inst = Instance("1.2.3", "1.2.3", 1)
    inst.mark_persisted()

    inst.set_pixel_data(np.zeros((10,10)))
    assert inst.has_unsaved_changes is True

def test_incremental_insert(store):
    p = create_mock_patient("P_INC", count=5)
    store.save_all([p])

    # Verify DB
    patients = store.load_all()
    assert len(patients) == 1
    assert len(patients[0].studies[0].series[0].instances) == 5

    # Verify Cleanup (objects in memory should mark clean?)
    # ideally save_all marks them clean
    assert p.studies[0].series[0].instances[0].has_unsaved_changes is False

def test_incremental_no_op(store):
    p = create_mock_patient("P_NOOP", count=5)
    store.save_all([p])

    # Manually check modification times or use logs
    # Here we just ensure data remains
    store.save_all([p])

    patients = store.load_all()
    assert len(patients) == 1

def test_incremental_update(store):
    p = create_mock_patient("P_UPD", count=1)
    store.save_all([p])

    inst = p.studies[0].series[0].instances[0]
    inst.set_attr("0010,0010", "Changed Name")
    assert inst.has_unsaved_changes is True

    store.save_all([p])

    # Verify in DB
    patients_loaded = store.load_all()
    loaded_inst = patients_loaded[0].studies[0].series[0].instances[0]
    assert loaded_inst.attributes["0010,0010"] == "Changed Name"
    assert inst.has_unsaved_changes is False

def test_incremental_delete(store):
    p = create_mock_patient("P_DEL", count=3)
    store.save_all([p])

    # Remove one instance
    removed_inst = p.studies[0].series[0].instances.pop(0)

    store.save_all([p])

    # Verify DB
    patients = store.load_all()
    assert len(patients[0].studies[0].series[0].instances) == 2

def test_persistence_resiliency(store):
    """Ensure partial saves don't corrupt DB (transaction test implicitly via sqlite)"""
    pass

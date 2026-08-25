
import os
import shutil
import glob
import pytest
import numpy as np
from isocenter.session import DicomSession
from isocenter.entities import Instance, Patient, Study, Series
from isocenter.sidecar import SidecarManager

TEST_DB = "test_sidecar_suite.db"
TEST_PIXELS = "test_sidecar_suite_pixels.bin"

@pytest.fixture
def clean_env():
    # Setup
    if os.path.exists(TEST_DB): os.remove(TEST_DB)
    # Remove SHM/WAL
    for f in glob.glob(f"{TEST_DB}*"):
        try: os.remove(f)
        except: pass

    if os.path.exists(TEST_PIXELS): os.remove(TEST_PIXELS)

    yield

    # Teardown
    if os.path.exists(TEST_DB): os.remove(TEST_DB)
    for f in glob.glob(f"{TEST_DB}*"):
        try: os.remove(f)
        except: pass
    if os.path.exists(TEST_PIXELS): os.remove(TEST_PIXELS)

def test_sidecar_manager_basics(clean_env):
    """Test raw SidecarManager read/write."""
    mgr = SidecarManager(TEST_PIXELS)

    data1 = b"Hello World"
    off1, len1 = mgr.write_frame(data1, compression='raw')

    data2 = np.zeros((10, 10), dtype=np.uint8).tobytes()
    off2, len2 = mgr.write_frame(data2, compression='zlib')

    assert off1 == 0
    assert len1 == len(data1)

    assert off2 == len1
    # Check read back
    out1 = mgr.read_frame(off1, len1, compression='raw')
    assert out1 == data1

    out2 = mgr.read_frame(off2, len2, compression='zlib')
    assert out2 == data2

def test_session_sidecar_persistence(clean_env):
    """Test full integration with DicomSession."""
    session = DicomSession(TEST_DB)

    # Create Instance with Pixels
    arr = np.zeros((50, 50, 3), dtype=np.uint8)
    arr[25, 25] = [1, 2, 3]

    p = Patient("P_TEST", "Test Patient")
    st = Study("S_TEST", "20230101")
    se = Series("SE_TEST", "OT", 1)
    inst = Instance("I_TEST", "1.2.3", 1)
    inst.set_pixel_data(arr)

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    session.store.patients.append(p)

    # Save (Bypassing async to ensure immediate write for test)
    # session.save() is async.
    session.store_backend.save_all(session.store.patients)

    assert os.path.exists(TEST_PIXELS)
    assert os.path.getsize(TEST_PIXELS) > 0

    # Reload
    session2 = DicomSession(TEST_DB)
    patients = session2.store.patients
    assert len(patients) == 1

    loaded_inst = patients[0].studies[0].series[0].instances[0]

    # Check Lazy Loading
    assert loaded_inst.pixel_array is None

    # Check Access
    rec_arr = loaded_inst.get_pixel_data()
    assert rec_arr.shape == (50, 50, 3)
    assert np.array_equal(rec_arr[25, 25], [1, 2, 3])

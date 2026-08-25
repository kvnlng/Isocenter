
import os
import shutil
import pytest
import numpy as np
from isocenter.session import DicomSession
from isocenter.entities import Instance, Patient, Study, Series, Equipment

TEST_DB = "test_memory_redaction.db"

@pytest.fixture
def clean_env():
    if os.path.exists(TEST_DB): os.remove(TEST_DB)
    sidecar = TEST_DB.replace(".db", "_pixels.bin")
    if os.path.exists(sidecar): os.remove(sidecar)

    yield

    if os.path.exists(TEST_DB): os.remove(TEST_DB)
    if os.path.exists(sidecar): os.remove(sidecar)

def test_redaction_memory_swap(clean_env):
    """
    Verifies that redaction immediately offloads modified pixels to the sidecar,
    allowing pixel_array to be cleared from memory (None).
    """
    sess = DicomSession(TEST_DB)

    # 1. Create a simulated patient with 10 instances
    # Each instance has 100x100 pixels
    p = Patient("P_MEM", "Memory Test")
    st = Study("S_MEM", "20230101")
    se = Series("SE_MEM", "OT", 1, Equipment("Isocenter", "MemTest", "SERIAL_123"))

    instances = []
    for i in range(10):
        inst = Instance(f"I_{i}", "1.2.840.10008.5.1.4.1.1.2", i+1)
        # Create pure white image
        arr = np.ones((100, 100), dtype=np.uint8) * 255
        inst.set_pixel_data(arr)
        instances.append(inst)
        se.instances.append(inst)

    st.series.append(se)
    p.studies.append(st)
    sess.store.patients.append(p)

    # Save first to establish baseline (though not strictly needed if we are just testing redactor flow)
    # But RedactionService expects index to be built.
    sess.save()

    # 2. Configure Redaction for "SERIAL_123"
    # Redact top left corner [0, 10, 0, 10]
    rois = [[0, 10, 0, 10]]

    # 3. Check Pre-Condition: Instance loaded -> pixel_array present
    # Force load one
    instances[0].get_pixel_data()
    assert instances[0].pixel_array is not None

    # 4. Run Redaction
    # This should:
    # - Load pixels
    # - Modify them
    # - Persist to sidecar
    # - Unload pixels (Set to None)

    from isocenter.services import RedactionService
    svc = RedactionService(sess.store, sess.store_backend)
    svc.redact_machine_instances("SERIAL_123", rois, verbose=True)

    # 5. Verify Memory State
    for i, inst in enumerate(instances):
        # The key expectation: pixel_array should be None because it was unloaded
        assert inst.pixel_array is None, f"Instance {i} held onto memory after redaction!"

        # Verify Linkage
        assert inst._pixel_loader is not None, f"Instance {i} lost its pixel loader!"

        # Verify Content (Lazy Load)
        data = inst.get_pixel_data()
        assert data is not None
        assert data.shape == (100, 100)
        # Check Redaction (0,0 should be 0)
        assert data[0, 0] == 0, f"Instance {i} was not redacted!"
        # Check Non-Redacted (50, 50 should be 255)
        assert data[50, 50] == 255

    sess.close()

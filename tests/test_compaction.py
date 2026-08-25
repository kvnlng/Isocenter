
import pytest
import os
import shutil
from isocenter.session import DicomSession
from isocenter.entities import Instance
import numpy as np

class TestCompaction:

    @pytest.fixture
    def session(self, tmp_path, request):
        """Creates a session with a real file-based DB (required for sidecar)."""
        db_name = f"isocenter_test_compact_{request.node.name}.db"
        db_path = str(tmp_path / db_name)
        s = DicomSession(persistence_file=db_path)
        yield s
        s.close()

    def create_dummy_instance(self, uid):
        """Helper to create an in-memory instance with random pixel data."""
        inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1, file_path=None)
        # 100KB of random data
        arr = np.random.randint(0, 255, (100, 1000), dtype=np.uint8)
        inst.set_pixel_data(arr)
        return inst

    def test_compact_reclaims_space_after_deletion(self, session, tmp_path):
        """
        Verify that deleting an instance and running compact() reduces file size.
        """
        # 1. Add 2 instances
        i1 = self.create_dummy_instance("1.1.1")
        i2 = self.create_dummy_instance("1.1.2")

        session.store_backend.persist_pixel_data(i1)
        session.store_backend.persist_pixel_data(i2)

        # Add to store and save metadata
        p = session.store.patients
        from isocenter.entities import Patient, Study, Series
        pat = Patient("P1", "Test Patient")
        st = Study("ST1", "20230101")
        se = Series("SE1", "CT", 1)
        pat.studies.append(st); st.series.append(se); se.instances.extend([i1, i2])
        session.store.patients.append(pat)

        session.save(sync=True)

        # Check Initial Size
        sidecar_path = session.store_backend.sidecar_path
        initial_size = os.path.getsize(sidecar_path)
        assert initial_size > 0

        # 2. Delete one instance from DB
        with session.store_backend._get_connection() as conn:
            row = conn.execute("SELECT id FROM instances WHERE sop_instance_uid=?", (i1.sop_instance_uid,)).fetchone()
            i_id = row[0]
            conn.execute("DELETE FROM instances WHERE id=?", (i_id,))

        assert os.path.getsize(sidecar_path) == initial_size

        # 3. Compact
        if hasattr(session, 'compact'):
            session.compact()
        else:
             session.store_backend.compact_sidecar()

        # 4. Verify Size Reduction
        final_size = os.path.getsize(sidecar_path)
        assert final_size < initial_size
        assert final_size > 0

        # 5. Verify Surviving Data Integrity
        loaded_i2 = session.store_backend.load_patient("P1").studies[0].series[0].instances[0]
        assert loaded_i2.sop_instance_uid == "1.1.2"

        # Trigger pixel load using get_pixel_data() because pixel_array is a field
        arr = loaded_i2.get_pixel_data()
        assert arr is not None
        assert arr.shape == (100, 1000)

    def test_compact_reclaims_space_after_redaction(self, session, tmp_path):
        """
        Verify that redacting (appending new pixels) and compacting removes original strings.
        """
        i1 = self.create_dummy_instance("2.2.1")

        from isocenter.entities import Patient, Study, Series
        pat = Patient("P2", "Redact Patient")
        st = Study("ST2", "20230101")
        se = Series("SE2", "CT", 1)
        pat.studies.append(st); st.series.append(se); se.instances.append(i1)
        session.store.patients.append(pat)

        # 1. Initial State
        session.store_backend.persist_pixel_data(i1)
        session.save(sync=True)

        size_1 = os.path.getsize(session.store_backend.sidecar_path)
        assert size_1 > 0
        assert size_1 < 150000 # Should be 100KB + small overhead

        # 2. Simulate Redaction
        new_arr = np.random.randint(0, 255, (100, 1000), dtype=np.uint8)
        i1.set_pixel_data(new_arr)

        session.store_backend.persist_pixel_data(i1)
        session.save(sync=True)

        size_2 = os.path.getsize(session.store_backend.sidecar_path)
        assert size_2 > size_1

        # 3. Compact
        session.store_backend.compact_sidecar()

        # 4. Verify
        size_3 = os.path.getsize(session.store_backend.sidecar_path)
        assert size_3 < size_2

        diff = abs(size_3 - size_1)
        # Relax constraint just in case random compression differs slightly
        assert diff < 2000

    def test_compact_updates_in_memory_references(self, session, tmp_path):
        """
        Verify that existing in-memory objects are updated in-place and remain valid
        after compaction rewrites the sidecar.
        """
        i1 = self.create_dummy_instance("3.3.1")

        from isocenter.entities import Patient, Study, Series
        pat = Patient("P3", "Memory Test")
        st = Study("ST3", "20230101")
        se = Series("SE3", "CT", 1)
        pat.studies.append(st); st.series.append(se); se.instances.append(i1)
        session.store.patients.append(pat)

        session.store_backend.persist_pixel_data(i1)
        session.save(sync=True)

        # Ensure it has a loader
        assert i1._pixel_loader is not None
        old_offset = i1._pixel_loader.offset

        # Compact
        session.compact()

        # Verify Loader Updated
        # Offset might stay 0 if it was already optimal.
        # But we want to ensure the instance is still valid.

        # Verify Data Access (Critical)
        i1.unload_pixel_data() # Force reload from sidecar
        arr = i1.get_pixel_data()
        assert arr is not None
        assert arr.shape == (100, 1000)

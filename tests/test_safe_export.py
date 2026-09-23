
import pytest
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.session import DicomSession
from isocenter.io_handlers import DicomExporter
from isocenter.privacy import _replacement_uid_for
import os

from support.project_secret import FIXED_A, load_fixed_secret


def _replaced(uid):
    """`uid` as this project replaces it (#544): what a de-identified
    instance holds, so the floor has nothing left to raise on it."""
    return _replacement_uid_for(uid, FIXED_A)

def test_safe_export_skips_phi(tmp_path):
    # 1. Setup Session & Store
    with DicomSession(":memory:") as sess:
        load_fixed_secret(sess)
        # --- Patient still carrying an identifier (a real name) ---
        p_identifying = Patient("P_DIRTY", "Real Name")
        st1 = Study("S1", "20230101")
        se1 = Series("SE1", "OT", 1)
        # Valid attributes for export
        inst1 = Instance("I1", "1.2.840", 1)
        inst1.attributes.update({
            "0008,0020": "20230101",
            "0008,0030": "120000",
            "0018,0050": "1.0",
            "0018,0060": "120",
            "0020,0032": ["0","0","0"],
            "0020,0037": ["1","0","0","0","1","0"],
            "0028,0030": ["0.5","0.5"],
            "0028,0002": 1,
            "0028,0004": "MONOCHROME2",
            "0028,0010": 10,
            "0028,0011": 10,
            "0028,0100": 8,
            "0028,0101": 8,
            "0028,0102": 7,
            "0028,0103": 0
        })
        # Add dummy pixels
        import numpy as np
        inst1.set_pixel_data(np.zeros((10,10), dtype=np.uint8))

        se1.instances.append(inst1)
        st1.series.append(se1)
        p_identifying.studies.append(st1)
        sess.store.patients.append(p_identifying)

        # --- Clean Patient (Anonymized Name) ---
        p_clean = Patient("ANON_CLEAN", "ANONYMIZED")
        # Its UIDs are already replacements (#544): the floor replaces a
        # source UID, so an instance holding one is not clean.
        st2 = Study(_replaced("S2"), None) # No date allowed in safe mode currently
        se2 = Series(_replaced("SE2"), "OT", 1)

        inst2 = Instance(_replaced("I2"), "1.2.840.2", 1)
        # Carries no value any floor-policy rule would act on (#495): no
        # Study Date at all (JITTER flags even an empty one, since it has
        # not been shifted), and Study Time already empty (EMPTY is
        # satisfied by ""). Before #495 the bare session scanned no
        # instance tags, so this instance carried Study Time "120000" and
        # an empty Study Date and still counted as clean.
        inst2.attributes.update({
            "0008,0030": "",
            "0018,0050": "1.0",
            "0018,0060": "120",
            "0020,0032": ["0","0","0"],
            "0020,0037": ["1","0","0","0","1","0"],
            "0028,0030": ["0.5","0.5"],
            "0028,0002": 1,
            "0028,0004": "MONOCHROME2",
            "0028,0010": 10,
            "0028,0011": 10,
            "0028,0100": 8,
            "0028,0101": 8,
            "0028,0102": 7,
            "0028,0103": 0
        })
        inst2.set_pixel_data(np.zeros((10,10), dtype=np.uint8))

        se2.instances.append(inst2)
        st2.series.append(se2)
        p_clean.studies.append(st2)
        sess.store.patients.append(p_clean)

        # --- A patient whose name is already ANONYMIZED, but whose
        # instance still carries a real Study Time. The pin on #495's
        # Breaking change: before it, a bare session's scan saw only the
        # patient name, ID and study date, so this instance was written;
        # under the floor policy Study Time is an identifier and the
        # instance is skipped until `anonymize()` has run.
        p_floor = Patient("ANON_FLOOR", "ANONYMIZED")
        st3 = Study("S3", None)
        se3 = Series("SE3", "OT", 1)
        inst3 = Instance("I3", "1.2.840.3", 1)
        inst3.attributes.update(dict(inst2.attributes))
        inst3.attributes["0008,0030"] = "120000"
        inst3.set_pixel_data(np.zeros((10,10), dtype=np.uint8))
        se3.instances.append(inst3)
        st3.series.append(se3)
        p_floor.studies.append(st3)
        sess.store.patients.append(p_floor)

        # 2. Config for PHI Scan (Minimal)
        # We need a PHI config to define what is "dirty"
        # Create a simple config file
        config_file = tmp_path / "phi_check.json"
        import json
        config_file.write_text(json.dumps({
            "phi_tags": {
                 "0010,0010": "PatientName"
            }
        }))

        # 3. Safe Export
        out_dir = tmp_path / "safe_export_out"

        # By default, I1 (P_DIRTY) should have a finding on PatientName="Real Name"
        # I2 (P_CLEAN) should have NO finding on PatientName="ANONYMIZED" (hardcoded safe in privacy.py logic)
        # Wait, privacy.py hardcodes "ANONYMIZED" check?
        # Yes: if patient.patient_name != "ANONYMIZED" -> Finding.

        # No config is loaded on `sess`, so the scan is the floor policy
        # (#495) plus the hardcoded patient/study checks in privacy.py.
        # check_burned_in=True makes export() run audit() first and skip
        # any instance with a finding on itself or a parent.

        sess.export(str(out_dir), check_burned_in=True)

        # 4. Assertions
        # Dirty Patient file I1 should NOT exist
        # Scan recursively to be sure
        all_files = list(out_dir.rglob("*.dcm"))

        # We expect I2 (Clean) to exist as 0001.dcm in Subject_ANON_CLEAN folder
        # We expect I1 (Dirty) to NOT exist (it would be 0001.dcm in Subject_P_DIRTY)

        dirty_files = [f for f in all_files if "Subject_P_DIRTY" in str(f)]
        assert len(dirty_files) == 0

        clean_files = [f for f in all_files if "Subject_ANON_CLEAN" in str(f) and f.name == f"{_replaced('I2')}.dcm"]
        assert len(clean_files) == 1
        assert clean_files[0].exists()

        floor_files = [f for f in all_files if "Subject_ANON_FLOOR" in str(f)]
        assert floor_files == [], (
            "an instance carrying a real Study Time was exported by a "
            "safe export on a bare session; the floor policy flags it")

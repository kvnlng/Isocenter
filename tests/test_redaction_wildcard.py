
import unittest
import os
import shutil
import pydicom
import numpy as np
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import generate_uid, ImplicitVRLittleEndian

from isocenter.session import DicomSession
from isocenter.io_handlers import DicomExporter
import isocenter
print(f"DEBUG: isocenter loaded from: {isocenter.__file__}")

class TestWildcardRedaction(unittest.TestCase):
    def setUp(self):
        self.test_dir = "test_data_wildcard"
        self.output_dir = "test_output_wildcard"
        self.db_path = "test_wildcard.db"

        # Clean up
        if os.path.exists(self.test_dir): shutil.rmtree(self.test_dir)
        if os.path.exists(self.output_dir): shutil.rmtree(self.output_dir)
        if os.path.exists(self.db_path): os.remove(self.db_path)

        os.makedirs(self.test_dir)

        # Generate Dummy Data with Different Serial Numbers
        self.create_dummy_dicom(self.test_dir, "S1", "MachineA")
        self.create_dummy_dicom(self.test_dir, "S2", "MachineB")
        self.create_dummy_dicom(self.test_dir, "S3", "MachineC")

        # Initialize Session
        self.sess = DicomSession(self.db_path)
        self.sess.ingest(self.test_dir)

    def tearDown(self):
        self.sess.close()
        if os.path.exists(self.test_dir): shutil.rmtree(self.test_dir)
        if os.path.exists(self.output_dir): shutil.rmtree(self.output_dir)
        if os.path.exists(self.db_path): os.remove(self.db_path)
        # Cleanup potential temp files
        if os.path.exists("test_wildcard.yaml"): os.remove("test_wildcard.yaml")

    def create_dummy_dicom(self, folder, filename, serial_number):
        ds = Dataset()
        ds.is_little_endian = True
        ds.is_implicit_VR = True
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7" # Secondary Capture
        ds.SOPInstanceUID = generate_uid()
        ds.SeriesInstanceUID = generate_uid()
        ds.StudyInstanceUID = generate_uid()
        ds.PatientID = "TestPatient"
        ds.PatientName = "Test^Patient"
        ds.Manufacturer = "TestMfg"
        ds.ManufacturerModelName = "TestModel"
        ds.DeviceSerialNumber = serial_number

        # Create Pixel Data (10x10 white square)
        arr = np.ones((10, 10), dtype=np.uint8) * 255
        ds.Rows = 10
        ds.Columns = 10
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = arr.tobytes()

        path = os.path.join(folder, f"{filename}.dcm")
        ds.save_as(path, write_like_original=False)

    def test_wildcard_redaction(self):
        """
        Verify that a rule with serial_number='*' is applied to ALL machines.
        """
        # 1. Create Config with Wildcard (*)
        # We redact a 5x5 box at top-left. Since original is all 255 (white),
        # checking for 0 (black) confirms redaction.
        config_content = """
machines:
  - manufacturer: "TestMfg"
    serial_number: "*"
    redaction_zones:
      - [0, 5, 0, 5]
"""
        with open("test_wildcard.yaml", "w") as f:
            f.write(config_content)

        # Verify Store Content
        print(f"DEBUG: Patients: {len(self.sess.store.patients)}")
        for p in self.sess.store.patients:
            print(f"  Patient: {p.patient_id}")
            for st in p.studies:
                print(f"    Study: {st.study_instance_uid}")
                for se in st.series:
                    sn = se.equipment.device_serial_number if se.equipment else "None"
                    print(f"      Series: {se.series_instance_uid} (SN: {sn}) - Instances: {len(se.instances)}")

        # 2. Load and Apply
        self.sess.load_config("test_wildcard.yaml")
        self.sess.redact()
        self.sess.save(sync=True)

        # 3. Verify ALL instances were modified
        # We can inspect the store directly
        count = 0
        for p in self.sess.store.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        count += 1
                        arr = inst.get_pixel_data()
                        # Check Top-Left pixel (should be 0)
                        self.assertEqual(arr[0,0], 0, f"Instance {inst.sop_instance_uid} (SN: {se.equipment.device_serial_number}) was NOT redacted.")
                        # Check Bottom-Right pixel (should be 255)
                        self.assertEqual(arr[9,9], 255, "Redaction area too large/broad.")

                        # Verify Redaction Hash attribute is set
                        self.assertIsNotNone(inst.attributes.get("_ISOCENTER_REDACTION_HASH"))

        self.assertEqual(count, 3, "Should represent 3 mocked instances.")

if __name__ == '__main__':
    unittest.main()

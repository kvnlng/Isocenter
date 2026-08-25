import os
import unittest
import shutil
import tempfile
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian
import numpy as np
import hashlib

from isocenter.session import DicomSession

class TestMetadataRefactorFull(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "isocenter.db")
        self.session = DicomSession(self.db_path)

    def tearDown(self):
        self.session.store_backend.stop()
        del self.session
        shutil.rmtree(self.test_dir)

    def create_dummy_dicom(self, filename):
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.2'
        file_meta.MediaStorageSOPInstanceUID = '1.2.3.4.5'
        file_meta.ImplementationClassUID = '1.2.3.4'
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = FileDataset(filename, {}, file_meta=file_meta, preamble=b"\0" * 128)
        ds.PatientName = "Test^Patient"
        ds.PatientID = "123456"
        ds.StudyInstanceUID = "1.2.3.4.5.6"
        ds.SeriesInstanceUID = "1.2.3.4.5.6.7"
        ds.SOPInstanceUID = "1.2.3.4.5"
        ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.2'
        ds.Modality = "CT"

        # Private Tag (Odd Group) - Should go to Vertical
        ds.add_new((0x0099, 0x1001), 'LO', "PrivateValue")

        # Binary Tag (OB) - Should be Dropped
        # Using a standard-ish tag or private? Private OB.
        ds.add_new((0x0099, 0x1002), 'OB', b'\x01\x02\x03')

        # Pixel Data
        arr = np.arange(100, dtype=np.uint16).reshape((10, 10))
        ds.Rows = 10
        ds.Columns = 10
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = arr.tobytes()

        ds.save_as(filename, write_like_original=False)
        return arr.tobytes(), hashlib.sha256(arr.tobytes()).hexdigest()

    def test_full_pipeline(self):
        dcm_path = os.path.join(self.test_dir, "test.dcm")
        orig_bytes, orig_hash = self.create_dummy_dicom(dcm_path)

        # 1. Ingest
        print(f"Ingesting {dcm_path}...")
        self.session.ingest(dcm_path)

        # 2. Check Database (Instances)
        with self.session.store_backend._get_connection() as conn:
            row = conn.execute("SELECT pixel_hash, attributes_json FROM instances WHERE sop_instance_uid='1.2.3.4.5'").fetchone()
            if row is None:
                all_rows = conn.execute("SELECT sop_instance_uid FROM instances").fetchall()
                print(f"DEBUG: Found instances: {all_rows}")

            self.assertIsNotNone(row)
            db_hash = row['pixel_hash']
            attrs_json = row['attributes_json']

            # Verify Hash Persisted
            self.assertEqual(db_hash, orig_hash)

            # Verify Core Attributes JSON does NOT contain vertical tags
            # Private tag 0099,1001
            import json
            core_attrs = json.loads(attrs_json)
            self.assertNotIn("0099,1001", core_attrs)

            # 3. Check Vertical Table
            v_row = conn.execute("SELECT value_text FROM instance_attributes WHERE instance_uid='1.2.3.4.5' AND group_id='0099' AND element_id='1001'").fetchone()
            self.assertIsNotNone(v_row)
            self.assertEqual(v_row['value_text'], "PrivateValue")

            # 4. Check Binary Dropped
            # 0099,1002 (OB) should NOT be in Vertical OR Core
            self.assertNotIn("0099,1002", core_attrs)
            v_bin = conn.execute("SELECT * FROM instance_attributes WHERE instance_uid='1.2.3.4.5' AND group_id='0099' AND element_id='1002'").fetchone()
            self.assertIsNone(v_bin, "Binary tag should be dropped")

        # 5. Check Sidecar & Integrity
        inst = self.session.store.patients[0].studies[0].series[0].instances[0]

        # Ensure loader is SidecarPixelLoader
        from isocenter.io_handlers import SidecarPixelLoader
        self.assertIsInstance(inst._pixel_loader, SidecarPixelLoader)
        self.assertEqual(inst._pixel_hash, orig_hash)

        # Load pixels (Should verify hash implicitly)
        arr = inst.get_pixel_data()
        self.assertIsNotNone(arr)
        self.assertEqual(arr.tobytes(), orig_bytes)

        # 6. Test Integrity Failure
        # Modify Sidecar Bit
        sc_path = self.session.store_backend.sidecar.filepath
        with open(sc_path, "r+b") as f:
            f.seek(inst._pixel_loader.offset) # Seek to start of frame
            byte = f.read(1)
            f.seek(inst._pixel_loader.offset)
            # Flip a bit/byte
            f.write(b'\xFF' if byte != b'\xFF' else b'\x00')

        # Clear cache to force reload
        inst.pixel_array = None

        print("Testing Integrity Failure...")
        with self.assertRaises(RuntimeError) as cm:
            inst.get_pixel_data()

        self.assertIn("Integrity Error", str(cm.exception))

if __name__ == "__main__":
    unittest.main()

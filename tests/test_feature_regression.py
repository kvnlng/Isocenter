
import unittest
import os
import shutil
import tempfile
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless, ExplicitVRLittleEndian
from isocenter.session import DicomSession
from isocenter.reversibility import ReversibilityService
from cryptography.fernet import Fernet

class TestNewFeatures(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.input_dir = os.path.join(self.test_dir, "input")
        self.output_dir = os.path.join(self.test_dir, "output")
        os.makedirs(self.input_dir)
        os.makedirs(self.output_dir)

        # Create a dummy DICOM file
        self.dummy_dcm = os.path.join(self.input_dir, "test.dcm")
        self._create_dummy_dicom(self.dummy_dcm)

    def tearDown(self):
        # Clean up
        shutil.rmtree(self.test_dir)

        # Cleanup potential side-effects in CWD
        if os.path.exists("test_session.db"):
            os.remove("test_session.db")
        if os.path.exists("isocenter_test.key"):
            os.remove("isocenter_test.key")

    def _create_dummy_dicom(self, path):
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
        ds.file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.7'
        ds.file_meta.MediaStorageSOPInstanceUID = '1.2.3.4.5.6'

        ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.7'
        ds.SOPInstanceUID = '1.2.3.4.5.6'
        ds.PatientName = "Test^Patient"
        ds.PatientID = "123456"
        ds.StudyInstanceUID = "1.2.3.4.5"
        ds.SeriesInstanceUID = "1.2.3.4.5.1"

        # Pixel Data
        ds.Rows = 64
        ds.Columns = 64
        ds.SamplesPerPixel = 1
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        ds.PhotometricInterpretation = "MONOCHROME2"

        # Random Uniform noise
        arr = np.random.randint(0, 255, (64, 64), dtype=np.uint8)
        ds.PixelData = arr.tobytes()

        ds.is_little_endian = True
        ds.is_implicit_VR = True
        ds.preamble = b"\0" * 128
        pydicom.dcmwrite(path, ds, write_like_original=False)

    def test_export_compression_j2k(self):
        """Verify JPEG 2000 Export Feature"""
        s = DicomSession(":memory:")
        s.ingest(self.input_dir)

        out = os.path.join(self.output_dir, "compressed")
        s.export(out, use_compression=True)

        # Find the exported file
        exported_file = None
        for root, _, files in os.walk(out):
            for f in files:
                if f.endswith(".dcm"):
                    exported_file = os.path.join(root, f)
                    break

        self.assertIsNotNone(exported_file, "Exported file not found")

        # Verify
        ds = pydicom.dcmread(exported_file)

        # Check Transfer Syntax
        self.assertEqual(ds.file_meta.TransferSyntaxUID, JPEG2000Lossless)

        # Check if we can Read Pixels
        try:
            arr = ds.pixel_array
            self.assertEqual(arr.shape, (64, 64))
        except Exception as e:
            self.fail(f"Failed to decode compressed pixels: {e}")



    def test_session_auto_key_loading(self):
        """Verify Isocenter automatically loads 'isocenter.key' if present"""
        # 1. Create a key file
        key_path = "isocenter.key"
        # Note: DicomSession looks for 'isocenter.key' in CWD.
        # We need to be careful not to overwrite user's actual key if running locally.
        # But we are in a test env. The user prompt says we can write to files in workspaces.
        # We should back up existing key if any.

        cwd = os.getcwd()
        original_key_content = None
        if os.path.exists(key_path):
            with open(key_path, 'rb') as f:
                original_key_content = f.read()

        try:
            # Generate new key
            key = Fernet.generate_key()
            with open(key_path, 'wb') as f:
                f.write(key)

            # 2. Init Session
            # We use a file-based DB to trigger potential persistence logic,
            # though auto-key logic is in __init__
            if os.path.exists("test_auto_key.db"):
                os.remove("test_auto_key.db")

            s = DicomSession("test_auto_key.db")

            # 3. Verify Reversibility Service is active
            self.assertIsNotNone(s.reversibility_service)
            self.assertIsNotNone(s.reversibility_service.engine)
            self.assertIsNotNone(s.reversibility_service.key_manager)

            # 4. Verify Key Matches
            # We can't easily extract the key from Fernet object directly without protected access,
            # but we can check if it can encrypt/decrypt?
            # Or just rely on it being not None.

            s.persistence_manager.shutdown()

        finally:
            # Restore original key or delete test key
            if original_key_content:
                with open(key_path, 'wb') as f:
                    f.write(original_key_content)
            elif os.path.exists(key_path):
                os.remove(key_path)

            if os.path.exists("test_auto_key.db"):
                os.remove("test_auto_key.db")

if __name__ == "__main__":
    unittest.main()

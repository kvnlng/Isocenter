
import pytest
import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless
from isocenter.io_handlers import _compress_j2k

class TestCompressHandlers:
    """
    Tests for internal compression helpers in io_handlers.
    """

    def _create_base_ds(self, rows=10, cols=10, frames=1, samples=1):
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
        ds.is_implicit_VR = True
        ds.is_little_endian = True

        ds.Rows = rows
        ds.Columns = cols
        ds.NumberOfFrames = frames
        ds.SamplesPerPixel = samples
        ds.BitsAllocated = 8
        ds.PixelRepresentation = 0
        ds.PhotometricInterpretation = "MONOCHROME2" if samples == 1 else "RGB"
        return ds

    def test_compress_flat_array_single_frame(self):
        """
        Verify that a 1D (flattened) array is correctly reshaped for single-frame compression.
        """
        # 10x10 = 100 pixels
        ds = self._create_base_ds(10, 10, 1, 1)
        flat_arr = np.zeros((100,), dtype=np.uint8)

        # Should not raise IndexError
        _compress_j2k(ds, pixel_array=flat_arr)

        assert ds.file_meta.TransferSyntaxUID == JPEG2000Lossless
        assert hasattr(ds, 'PixelData')
        assert len(ds.PixelData) > 0

    def test_compress_flat_array_multi_frame(self):
        """
        Verify that a 1D (flattened) array is correctly reshaped for multi-frame compression.
        """
        # 2 frames, 10x10 = 200 pixels
        ds = self._create_base_ds(10, 10, 2, 1)
        flat_arr = np.zeros((200,), dtype=np.uint8)

        # Should not raise
        _compress_j2k(ds, pixel_array=flat_arr)

        assert ds.file_meta.TransferSyntaxUID == JPEG2000Lossless

    def test_compress_flat_array_rgb(self):
        """
        Verify that a 1D (flattened) RGB array is reshaped correctly.
        """
        # 1 frame, 5x5, 3 samples = 75 values
        ds = self._create_base_ds(5, 5, 1, 3)
        flat_arr = np.zeros((75,), dtype=np.uint8)

        _compress_j2k(ds, pixel_array=flat_arr)
        assert ds.file_meta.TransferSyntaxUID == JPEG2000Lossless

    def test_reshape_failure_handled(self):
        """
        Verify that if reshape fails (dimension mismatch), we catch usage error or propagate appropriately.
        """
        # Frames=2 ensures that if reshape fails (remains 1D), iterating it yields scalars -> Crash
        ds = self._create_base_ds(10, 10, 2, 1)
        # 201 pixels (mismatch for 2x10x10=200)
        flat_arr = np.zeros((201,), dtype=np.uint8)

        with pytest.raises(RuntimeError) as excinfo:
            _compress_j2k(ds, pixel_array=flat_arr)

        # Pillow raises IndexError/TypeError on scalar, caught and re-raised as RuntimeError
        assert "Compression failed" in str(excinfo.value)

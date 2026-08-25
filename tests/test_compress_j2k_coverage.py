
import pytest
from unittest.mock import MagicMock, patch
import numpy as np
import sys
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import JPEG2000Lossless
from isocenter.io_handlers import _compress_j2k

@pytest.fixture
def mock_dataset_compress():
    ds = MagicMock(spec=Dataset)
    ds.file_meta = MagicMock()
    ds.Rows = 10
    ds.Columns = 10
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = 8
    ds.NumberOfFrames = 1
    # Mock pixel data as bytes
    ds.PixelData = b'\x00' * 100
    return ds

def test_compress_j2k_with_array(mock_dataset_compress):
    # Provide explicit pixel array
    arr = np.zeros((10, 10), dtype=np.uint8)

    # Patch the global modules that _compress_j2k imports
    with patch('isocenter.io_handlers.Image') as mock_img_cls: # Mock PIL.Image class
        # Patch encapsulation in isocenter.io_handlers directly
        with patch('isocenter.io_handlers.encapsulate', return_value=b"compressed_data") as mock_enc:

            mock_img_cls.fromarray.return_value.save.side_effect = lambda fp, **kwargs: fp.write(b"compressed_data")

            _compress_j2k(mock_dataset_compress, pixel_array=arr)

            assert mock_dataset_compress.PixelData == b"compressed_data"

def test_compress_j2k_success(mock_dataset_compress):
    arr = np.zeros((10, 10), dtype=np.uint8)

    with patch('isocenter.io_handlers.encapsulate', return_value=b"encapsulated_frames"):
        with patch('isocenter.io_handlers.Image.fromarray') as mock_fromarray:
             mock_fromarray.return_value.save.side_effect = lambda fp, **kwargs: fp.write(b"j2k_bytes")

             _compress_j2k(mock_dataset_compress, pixel_array=arr)

             assert mock_dataset_compress.PixelData == b"encapsulated_frames"
             assert mock_dataset_compress.file_meta.TransferSyntaxUID == JPEG2000Lossless

def test_compress_j2k_fallback_reconstruct(mock_dataset_compress):
    # No array provided, should reconstruct from ds.PixelData
    with patch('isocenter.io_handlers.encapsulate', return_value=b"encapsulated"):
        with patch('isocenter.io_handlers.Image.fromarray') as mock_fromarray:
            _compress_j2k(mock_dataset_compress, pixel_array=None)

            mock_fromarray.assert_called_once()
            args, _ = mock_fromarray.call_args
            assert args[0].shape == (10, 10) # Reconstructed shape

def test_compress_j2k_frames(mock_dataset_compress):
    mock_dataset_compress.NumberOfFrames = 2
    mock_dataset_compress.PixelData = b'\x00' * 200 # 2 frames

    with patch('isocenter.io_handlers.encapsulate', return_value=b"encapsulated"):
         with patch('isocenter.io_handlers.Image.fromarray') as mock_fromarray:
             _compress_j2k(mock_dataset_compress, pixel_array=None)
             assert mock_fromarray.call_count == 2 # Called for each frame

def test_compress_j2k_import_error(mock_dataset_compress):
    # Simulate ImportError
    with patch('isocenter.io_handlers.Image.fromarray', side_effect=ImportError("No Pillow")):
        with pytest.raises(RuntimeError, match="Pillow or pydicom not installed"):
             _compress_j2k(mock_dataset_compress, pixel_array=np.zeros((10,10)))

def test_compress_j2k_generic_exception(mock_dataset_compress):
     with patch('isocenter.io_handlers.Image.fromarray', side_effect=ValueError("Bad Data")):
        with pytest.raises(RuntimeError, match="Compression failed"):
             _compress_j2k(mock_dataset_compress, pixel_array=np.zeros((10,10)))


import pytest
from unittest.mock import MagicMock, patch
import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import UID
from isocenter import imagecodecs_handler

# Define UIDs for convenience (matching those in imagecodecs_handler)
JPEGLossless = UID("1.2.840.10008.1.2.4.57")
RLELossless = UID("1.2.840.10008.1.2.5")
UnsupportedUID = UID("1.2.840.10008.1.2.4.100")  # hypothetical unsupported

@pytest.fixture
def mock_dataset():
    ds = MagicMock(spec=Dataset)
    ds.file_meta = MagicMock()
    ds.Rows = 10
    ds.Columns = 10
    ds.PixelData = b"fake_pixel_data"
    ds.NumberOfFrames = 1
    return ds

def test_imagecodecs_not_available(mock_dataset):
    """Test behavior when imagecodecs is reported as not available."""
    with patch('isocenter.imagecodecs_handler.is_available', return_value=False):
        with pytest.raises(RuntimeError, match="imagecodecs is not available"):
            imagecodecs_handler.get_pixel_data(mock_dataset)

def test_unsupported_transfer_syntax(mock_dataset):
    """Test behavior when an unsupported transfer syntax is encountered."""
    mock_dataset.file_meta.TransferSyntaxUID = UnsupportedUID
    with patch('isocenter.imagecodecs_handler.is_available', return_value=True):
        with pytest.raises(RuntimeError, match="imagecodecs failed to decode"):
            imagecodecs_handler.get_pixel_data(mock_dataset)


def test_decode_error_handling(mock_dataset):
    """Test that decode exceptions are caught and raised as RuntimeErrors."""
    # Use a mock UID to control properties
    mock_uid = MagicMock(spec=UID)
    mock_uid.__eq__.side_effect = lambda x: x == JPEGLossless # Allow comparison
    mock_dataset.file_meta.TransferSyntaxUID = JPEGLossless

    # Mock generate_fragments (used by single frame path)
    with patch('isocenter.imagecodecs_handler.generate_fragments', return_value=[b"chunk"]):
        # Unconditionally patch the local reference to imagecodecs in the handler
        with patch('isocenter.imagecodecs_handler.imagecodecs') as mock_ic:
            mock_ic.ljpeg_decode.side_effect = ValueError("Bad data")
            with pytest.raises(RuntimeError, match="imagecodecs failed to decode"):
                imagecodecs_handler.get_pixel_data(mock_dataset)

def test_rle_lossless_handling(mock_dataset):
    """Test RLE Lossless specific path."""
    # Mock UID to allow setting is_encapsulated
    mock_uid = MagicMock()
    mock_uid.is_encapsulated = True
    # We need equality check to pass for the handler's if-check
    mock_uid.__eq__.side_effect = lambda x: x == RLELossless

    mock_dataset.file_meta.TransferSyntaxUID = mock_uid
    expected_output = np.zeros((10, 10), dtype=np.uint8)

    with patch('isocenter.imagecodecs_handler.generate_fragments', return_value=[b"rle_chunk"]):
        with patch('isocenter.imagecodecs_handler.imagecodecs') as mock_ic:
            mock_ic.rle_decode.return_value = expected_output
            result = imagecodecs_handler.get_pixel_data(mock_dataset)
            mock_ic.rle_decode.assert_called_once()
            assert result is expected_output

def test_multi_frame_handling(mock_dataset):
    """Test multi-frame image decoding logic."""
    mock_dataset.NumberOfFrames = 2

    mock_uid = MagicMock()
    mock_uid.is_encapsulated = True
    mock_uid.__eq__.side_effect = lambda x: x == JPEGLossless
    mock_dataset.file_meta.TransferSyntaxUID = mock_uid

    frame1 = np.zeros((10, 10), dtype=np.uint8)
    frame2 = np.ones((10, 10), dtype=np.uint8)

    # Mock generate_frames to return two frames
    with patch('isocenter.imagecodecs_handler.generate_frames', return_value=[b"f1", b"f2"]):
        with patch('isocenter.imagecodecs_handler.imagecodecs') as mock_ic:
            mock_ic.ljpeg_decode.side_effect = [frame1, frame2]
            result = imagecodecs_handler.get_pixel_data(mock_dataset)
            assert result.shape == (2, 10, 10)
            np.testing.assert_array_equal(result[0], frame1)
            np.testing.assert_array_equal(result[1], frame2)

def test_is_available_import_error():
    """Test is_available returns False when import fails."""
    # We can't easily unload the module if it's already loaded, but we can simulate the state
    # where imagecodecs is None.
    with patch('isocenter.imagecodecs_handler.imagecodecs', None):
        assert imagecodecs_handler.is_available() is False

def test_is_available_success():
    """Test is_available returns True when module is present."""
    with patch('isocenter.imagecodecs_handler.imagecodecs', MagicMock()):
        assert imagecodecs_handler.is_available() is True

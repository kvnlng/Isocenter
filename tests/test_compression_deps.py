
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from isocenter.entities import Instance

def test_missing_compression_deps_error(tmp_path):
    """
    Verifies that a friendly error message is raised when pixel data
    decompression fails due to missing plugins.
    """
    dcm_path = tmp_path / "compressed.dcm"
    dcm_path.touch()

    inst = Instance("1.2.3", "1.2.3.4", 1, file_path=str(dcm_path))

    # Mock pydicom.dcmread to return a dataset that fails on .pixel_array access
    with patch("isocenter.entities.pydicom.dcmread") as mock_read:
        mock_ds = MagicMock()
        # Define a property that raises the specific RuntimeError
        type(mock_ds).pixel_array = PropertyMock(side_effect=RuntimeError(
            "Unable to decompress 'JPEG Baseline' pixel data because all plugins are missing dependencies"
        ))
        mock_read.return_value = mock_ds

        p = PropertyMock(side_effect=RuntimeError(
            "Unable to decompress 'JPEG Baseline' pixel data because all plugins are missing dependencies"
        ))
        type(mock_ds).pixel_array = p

        with pytest.raises(RuntimeError) as excinfo:
            inst.get_pixel_data()

        assert "Missing image codecs" in str(excinfo.value)
        assert "Missing image codecs" in str(excinfo.value)
        assert "pillow" in str(excinfo.value) and "gdcm" in str(excinfo.value)

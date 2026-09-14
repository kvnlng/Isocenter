
import pytest
from unittest.mock import MagicMock, patch
from isocenter.entities import Instance

def test_missing_compression_deps_error(tmp_path):
    """
    Verifies that a friendly error message is raised when pixel data
    decompression fails due to missing plugins.
    """
    dcm_path = tmp_path / "compressed.dcm"
    dcm_path.touch()

    inst = Instance("1.2.3", "1.2.3.4", 1, file_path=str(dcm_path))

    # Mock pydicom.dcmread to return a dataset whose decode fails. The
    # Instance door decodes through `io_handlers._decode_pixels` (#453),
    # which calls `get_decoder(ts).as_array` -- what `Dataset.pixel_array`
    # calls -- so that it can keep the decoder's colour-space answer
    # (#482); the decoder is where the failure goes. The door's own
    # `get_decoder` only asks whether a decoder exists; it gets the same
    # mock.
    with patch("isocenter.entities.pydicom.dcmread") as mock_read, \
            patch("isocenter.io_handlers.get_decoder") as mock_decoder, \
            patch("isocenter.entities.get_decoder", mock_decoder):
        mock_ds = MagicMock()
        mock_read.return_value = mock_ds
        mock_decoder.return_value.as_array.side_effect = RuntimeError(
            "Unable to decompress 'JPEG Baseline' pixel data because all plugins are missing dependencies"
        )

        with pytest.raises(RuntimeError) as excinfo:
            inst.get_pixel_data()

        assert "Missing image codecs" in str(excinfo.value)
        assert "Missing image codecs" in str(excinfo.value)
        assert "pillow" in str(excinfo.value) and "gdcm" in str(excinfo.value)

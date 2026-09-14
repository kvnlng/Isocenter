
"""`imagecodecs_handler`'s codec dispatch, on datasets built to reach it.

These asked `imagecodecs_handler.get_pixel_data` until #453 deleted it
(Q10). The dispatch they pin -- `_decode_frame`, one codec per syntax --
is reached now through `decode_declared_frames`, which is what
`io_handlers._decode_with_imagecodecs` calls. A `MagicMock` dataset
cannot pass `_decode_pixels`' own checks (pydicom's validation, the
photometric allow-list), so these call the decode directly; the
checks have their own tests in `test_one_decode_answer_per_file.py`.
`decode_declared_frames` does not wrap a codec's exception: the fallback
does, as "imagecodecs could not decode it either: <type>: <words>".
"""
import pytest
from unittest.mock import MagicMock, patch
import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
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
            imagecodecs_handler.decode_declared_frames(mock_dataset, 1)

def test_unsupported_transfer_syntax(mock_dataset):
    """Test behavior when an unsupported transfer syntax is encountered."""
    mock_dataset.file_meta.TransferSyntaxUID = UnsupportedUID
    mock_dataset.PixelData = encapsulate([CHUNK])
    with patch('isocenter.imagecodecs_handler.is_available', return_value=True):
        with pytest.raises(RuntimeError,
                           match=f"Unsupported syntax: {UnsupportedUID}"):
            imagecodecs_handler.decode_declared_frames(mock_dataset, 1)


# Even-length on purpose: `encapsulate` pads an odd-length fragment with a
# trailing null (items must be even, PS3.5 7.5), and a padded payload would
# make the "handed exactly this" assertions below read `b"chunk\x00"`.
CHUNK = b"ljpeg_chunk!"


def test_decode_error_handling(mock_dataset):
    """A codec exception reaches the caller, on real bytes.

    The encapsulated `PixelData` is built with `encapsulate()` rather than
    mocked, because the version of this test that patched
    `generate_fragments` proved nothing about the arm it names: the real
    call also raised on `b"fake_pixel_data"`, so it passed whether or not
    the codec was ever reached (#407).
    """
    mock_dataset.file_meta.TransferSyntaxUID = JPEGLossless
    mock_dataset.PixelData = encapsulate([CHUNK])

    # Unconditionally patch the local reference to imagecodecs in the handler
    with patch('isocenter.imagecodecs_handler.imagecodecs') as mock_ic:
        mock_ic.ljpeg_decode.side_effect = ValueError("Bad data")
        with pytest.raises(ValueError, match="Bad data"):
            imagecodecs_handler.decode_declared_frames(mock_dataset, 1)
        # The codec saw the fragment alone: had the Basic Offset Table been
        # joined in front of it, this would be four zero bytes longer.
        assert mock_ic.ljpeg_decode.call_args[0][0] == CHUNK

def test_the_handler_does_not_claim_rle():
    """R2: RLE Lossless is pydicom's to decode, and the handler says so (#447).

    Replaces `test_rle_lossless_handling`, which mocked
    `imagecodecs.rle_decode` and asserted it was called -- a function no
    imagecodecs this package supports has ever had. So the handler listed
    RLE, `Instance.get_pixel_data()` handed RLE files to it, and every one
    raised `module 'imagecodecs' has no attribute 'rle_decode'`, while the
    mock kept this test green. pydicom's own RLE decoder needs no
    dependency and is what always read RLE here.

    A real RLE dataset, not a mock: with the arm kept, the message is the
    `AttributeError`'s, not the refusal below.
    """
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = UID("1.2.840.10008.1.2.1")
    ds.Rows, ds.Columns = 2, 3
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = bytes(range(6))
    ds.compress(RLELossless, encoding_plugin="pydicom")
    assert ds.file_meta.TransferSyntaxUID == RLELossless

    assert imagecodecs_handler.supports_transfer_syntax(RLELossless) is False
    with pytest.raises(RuntimeError) as exc:
        imagecodecs_handler.decode_declared_frames(ds, 1)
    assert f"Unsupported syntax: {RLELossless}" in str(exc.value), \
        str(exc.value)


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
            result = imagecodecs_handler.decode_declared_frames(
                mock_dataset, 2)
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


# Every supported syntax, and the one codec it must reach. Before #414 the
# suite pinned only the ljpeg arm and an rle arm that never decoded
# (#447), so returning None from the JPEG Baseline/Extended arm or the
# JPEG-LS arm -- or routing one syntax family to another's codec -- left
# it green.
_CODEC_FOR = {
    "1.2.840.10008.1.2.4.57": "ljpeg_decode",   # JPEG Lossless
    "1.2.840.10008.1.2.4.70": "ljpeg_decode",   # JPEG Lossless SV1
    "1.2.840.10008.1.2.4.50": "jpeg_decode",    # JPEG Baseline
    "1.2.840.10008.1.2.4.51": "jpeg_decode",    # JPEG Extended
    "1.2.840.10008.1.2.4.90": "jpeg2k_decode",  # JPEG 2000 Lossless
    "1.2.840.10008.1.2.4.91": "jpeg2k_decode",  # JPEG 2000
    "1.2.840.10008.1.2.4.80": "jpegls_decode",  # JPEG-LS Lossless
    "1.2.840.10008.1.2.4.81": "jpegls_decode",  # JPEG-LS Near-Lossless
}
_CODECS = sorted(set(_CODEC_FOR.values()))
DISPATCH_CHUNK = b"codestream!!"  # even length: no pad byte from encapsulate


@pytest.mark.parametrize("syntax", sorted(_CODEC_FOR))
def test_each_syntax_reaches_its_own_codec_and_returns_its_result(syntax):
    """One syntax, one codec, and that codec's array is what comes back.

    Each codec returns its own sentinel, so a mutant returning None, a
    swapped arm, or a fall-through to the wrong family is red here: the
    identity check catches the first, and `assert_not_called` on every
    other codec catches the other two.
    """
    ds = MagicMock(spec=Dataset)
    ds.file_meta = MagicMock()
    ds.file_meta.TransferSyntaxUID = UID(syntax)
    ds.Rows = 2
    ds.Columns = 3
    ds.NumberOfFrames = 1
    ds.PixelData = encapsulate([DISPATCH_CHUNK])
    sentinels = {name: np.full((2, 3), i, dtype=np.uint8)
                 for i, name in enumerate(_CODECS)}

    with patch('isocenter.imagecodecs_handler.imagecodecs') as mock_ic:
        for name in _CODECS:
            getattr(mock_ic, name).return_value = sentinels[name]
        result = imagecodecs_handler.decode_declared_frames(ds, 1)

        expected = _CODEC_FOR[syntax]
        assert result is sentinels[expected]
        getattr(mock_ic, expected).assert_called_once()
        assert getattr(mock_ic, expected).call_args[0][0] == DISPATCH_CHUNK
        for name in _CODECS:
            if name != expected:
                getattr(mock_ic, name).assert_not_called()


def test_supports_exactly_the_eight_syntaxes():
    """Eight since #447: RLE Lossless is not the handler's to claim."""
    assert sorted(imagecodecs_handler.SUPPORTED_TRANSFER_SYNTAXES) == \
        sorted(_CODEC_FOR)
    for syntax in _CODEC_FOR:
        assert imagecodecs_handler.supports_transfer_syntax(UID(syntax)) is True
    assert imagecodecs_handler.supports_transfer_syntax(RLELossless) is False
    assert imagecodecs_handler.supports_transfer_syntax(
        UID("1.2.840.10008.1.2.1")) is False  # Explicit VR Little Endian


# ---------------------------------------------------------------------------
# #444 -- an unavailable imagecodecs says why
# ---------------------------------------------------------------------------

#: The shape a broken wheel produces: the module is installed and a shared
#: library it links is not. Its words are the only clue to the fix.
_BROKEN_IMPORT = ImportError(
    "libjpeg.so.8: cannot open shared object file: No such file or directory")


def test_unavailable_imagecodecs_names_why(monkeypatch, mock_dataset):
    """I1: the raise site carries the import failure, and chains it (#444).

    Each raised a bare "imagecodecs is not available", and the cause
    reached only a stderr print in `is_available()`, which a worker, a
    notebook or a log-only deployment may never show.
    """
    monkeypatch.setattr(imagecodecs_handler, "imagecodecs", None)
    monkeypatch.setattr(imagecodecs_handler, "IMPORT_ERROR", _BROKEN_IMPORT)
    with pytest.raises(RuntimeError) as exc:
        imagecodecs_handler.decode_declared_frames(mock_dataset, 1)
    msg = str(exc.value)
    assert "imagecodecs is not available" in msg, msg
    assert "ImportError" in msg, msg
    assert "libjpeg.so.8" in msg, msg
    assert exc.value.__cause__ is _BROKEN_IMPORT


def test_the_unavailable_print_names_a_message_less_import_error(
        monkeypatch, capsys):
    """`is_available()`'s stderr line names the type too (#500).

    The one site of #500's shape the AST scan in
    `tests/test_log_lines_name_their_exception.py` structurally cannot
    see: the `except ImportError as e` at the top of the module assigns
    the exception to `IMPORT_ERROR` and returns, and the line that
    formats it is in another function, so the name it formats is a module
    global rather than a bound handler name. A broken install whose
    `__init__` ends in a bare `raise ImportError` renders as `str()` of
    nothing, so the line read "NOT AVAILABLE. Import Error: " and named
    neither the type nor a reason -- the same silence, in the one place a
    reader looks first when the codec is missing.

    Killing mutation: the f-string back to `{IMPORT_ERROR}`.
    """
    monkeypatch.setattr(imagecodecs_handler, "imagecodecs", None)
    monkeypatch.setattr(imagecodecs_handler, "IMPORT_ERROR", ImportError())

    assert imagecodecs_handler.is_available() is False
    line = capsys.readouterr().err
    assert "Import Error: ImportError" in line, (
        f"a message-less ImportError left the stderr line saying a codec "
        f"is unavailable without saying why: {line!r}")


def test_the_unavailable_print_survives_an_unrecorded_import_error(
        monkeypatch, capsys):
    """No import error recorded is said, not crashed on (#500).

    `imagecodecs` set to None with `IMPORT_ERROR` left at its
    import-time None is a real state: `test_is_available_import_error`
    above produces it, and so does any caller that stubs the module out.
    `describe_exception(None)` has no type to name, so the guard is the
    point -- the line says the failure was not recorded rather than
    raising `AttributeError` inside a diagnostic.
    """
    monkeypatch.setattr(imagecodecs_handler, "imagecodecs", None)
    monkeypatch.setattr(imagecodecs_handler, "IMPORT_ERROR", None)

    assert imagecodecs_handler.is_available() is False
    assert "Import Error: none recorded" in capsys.readouterr().err

"""`_compress_j2k`'s arms, exercised against its collaborator directly.

Rewritten in #404, when the JPEG 2000 encoder changed from Pillow's
`Image.fromarray(...).save(..., format="JPEG2000")` to
`imagecodecs.jpeg2k_encode(frame, level=0, codecformat="J2K")`. Every test
here patched `isocenter.io_handlers.Image`, a name the module no longer
has, so each one was about a collaborator that does not exist rather than
about the function.

Two of them asserted the **reconstruct-from-bytes** branch --
`_compress_j2k(ds, pixel_array=None)` rebuilding the array from
`ds.PixelData` -- which #404 deleted as unreachable rather than corrected.
The replacement is `test_the_no_array_call_encodes_nothing` below, which
pins the deletion; the end-to-end characterization lives in
`tests/test_signed_pixels_survive_a_compressed_export.py::
test_compress_j2k_without_an_array_writes_nothing_and_raises_nothing`.

The `ImportError` test went with them. `imagecodecs` is imported unguarded
at module scope now, so an `ImportError` inside this function is not a
reachable state and the `except ImportError` arm that turned it into
"Pillow or pydicom not installed" is gone.
"""
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless

from isocenter.io_handlers import _compress_j2k


@pytest.fixture
def mock_dataset_compress():
    ds = MagicMock(spec=Dataset)
    ds.file_meta = MagicMock()
    ds.Rows = 10
    ds.Columns = 10
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = 8
    ds.PixelRepresentation = 0
    ds.NumberOfFrames = 1
    ds.PixelData = b'\x00' * 100
    return ds


def test_compress_j2k_with_array(mock_dataset_compress):
    """The array handed in is the array encoded, and the result is stored."""
    arr = np.zeros((10, 10), dtype=np.uint8)

    with patch('isocenter.io_handlers.jpeg2k_encode',
               return_value=b"codestream") as encode:
        with patch('isocenter.io_handlers.encapsulate',
                   return_value=b"compressed_data"):
            _compress_j2k(mock_dataset_compress, pixel_array=arr)

    encode.assert_called_once()
    assert mock_dataset_compress.PixelData == b"compressed_data"


def test_compress_j2k_asks_for_a_lossless_bare_codestream(mock_dataset_compress):
    """`level=0` and `codecformat="J2K"` are both load-bearing.

    `level=0` is lossless; anything else silently degrades the pixels.
    `codecformat="J2K"` emits a bare codestream, which is what transfer
    syntax 1.2.840.10008.1.2.4.90 names -- the default wraps it in a JP2
    box, which is what every release before #404 wrote.
    """
    arr = np.zeros((10, 10), dtype=np.uint8)

    with patch('isocenter.io_handlers.jpeg2k_encode',
               return_value=b"codestream") as encode:
        with patch('isocenter.io_handlers.encapsulate',
                   return_value=b"encapsulated_frames"):
            _compress_j2k(mock_dataset_compress, pixel_array=arr)

    _args, kwargs = encode.call_args
    assert kwargs["level"] == 0
    assert kwargs["codecformat"] == "J2K"
    assert mock_dataset_compress.PixelData == b"encapsulated_frames"
    assert mock_dataset_compress.file_meta.TransferSyntaxUID == JPEG2000Lossless


def test_compress_j2k_encodes_each_frame_separately(mock_dataset_compress):
    """Encapsulated multi-frame pixel data is one fragment per frame."""
    mock_dataset_compress.NumberOfFrames = 2
    arr = np.zeros((2, 10, 10), dtype=np.uint8)

    with patch('isocenter.io_handlers.jpeg2k_encode',
               return_value=b"codestream") as encode:
        with patch('isocenter.io_handlers.encapsulate',
                   return_value=b"encapsulated"):
            _compress_j2k(mock_dataset_compress, pixel_array=arr)

    assert encode.call_count == 2
    for call in encode.call_args_list:
        assert call.args[0].shape == (10, 10)


def test_the_no_array_call_encodes_nothing():
    """The deleted branch, pinned at the unit level.

    `pixel_array is None` means "nothing to compress" and nothing else. It
    used to mean "rebuild the array from `ds.PixelData`", reading those
    bytes as `uint16` regardless of `PixelRepresentation` -- a second
    decoder that could disagree with `SidecarPixelLoader`, and one no
    caller could reach. *Red when:* the reconstruct-from-bytes arm is
    restored.
    """
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    ds.Rows = ds.Columns = 10
    ds.SamplesPerPixel = 1
    ds.NumberOfFrames = 1
    ds.BitsAllocated = 16
    ds.PixelRepresentation = 1
    ds.PixelData = b'\x00' * 200

    with patch('isocenter.io_handlers.jpeg2k_encode') as encode:
        _compress_j2k(ds, pixel_array=None)

    encode.assert_not_called()
    assert ds.file_meta.TransferSyntaxUID == ImplicitVRLittleEndian
    assert ds.PixelData == b'\x00' * 200


def test_compress_j2k_generic_exception(mock_dataset_compress):
    """A codec failure still reaches the caller as `Compression failed`."""
    with patch('isocenter.io_handlers.jpeg2k_encode',
               side_effect=ValueError("Bad Data")):
        with pytest.raises(RuntimeError, match="Compression failed"):
            _compress_j2k(mock_dataset_compress,
                          pixel_array=np.zeros((10, 10), dtype=np.uint8))


def test_the_frame_refusal_is_not_rewrapped_by_the_generic_handler(
        mock_dataset_compress):
    """`Compression failed: Compression failed: ...` is the shape to avoid.

    The refusal is raised inside the same `try` the codec runs in, so
    without its own `except ... raise` clause the outer handler would
    stringify it into its own message. The sentence the user reads has to
    be ours, unwrapped.
    """
    mock_dataset_compress.BitsAllocated = 32
    with patch('isocenter.io_handlers.jpeg2k_encode') as encode:
        with pytest.raises(RuntimeError) as excinfo:
            _compress_j2k(mock_dataset_compress,
                          pixel_array=np.zeros((10, 10), dtype=np.int32))

    encode.assert_not_called()
    message = str(excinfo.value)
    assert message.count("Compression failed") == 1, message
    assert "int32" in message
    assert "use_compression=False" in message


# ---------------------------------------------------------------------------
# #473: a compressed length pydicom would mistake for an uncompressed one
# ---------------------------------------------------------------------------

import itertools
import warnings

import pydicom
from imagecodecs import jpeg2k_encode
from pydicom.encaps import encapsulate

from isocenter import io_handlers
from isocenter.entities import Instance
from isocenter.io_handlers import ExportContext, _export_instance_worker

#: pydicom 3.0.2 `pixels/decoders/base.py`, `_validate_buffer`: the warning
#: this is about, matched on its own words.
HEURISTIC = "matches the expected number for uncompressed data"

_serial = itertools.count(1)


def _codestream(frame):
    """What `_compress_j2k` encodes a 1-sample frame to."""
    return jpeg2k_encode(frame, level=0, codecformat="J2K", mct=False)


def _search(make, expected_window, limit=2000):
    """Seeds whose BOT encapsulation lands in pydicom's window.

    Searched at test time rather than hard-coded: the encoder's output
    is a property of the installed `imagecodecs`, and a seed list measured
    on one release is not a collision on another. The search failing is a
    failure, not a skip -- a silent skip would read as a pass.
    """
    hits = []
    for seed in range(limit):
        frames = make(seed)
        n = len(encapsulate([_codestream(f) for f in frames]))
        if n in expected_window:
            hits.append(seed)
            if len(hits) == 3:
                break
    assert hits, f"no colliding seed in {limit}; re-measure #473"
    return hits


def _bool_mask(seed, shape=(16, 16)):
    return np.random.default_rng(seed).integers(0, 2, shape, np.uint8) \
        .astype(bool)


def _sparse_odd(seed):
    """A 9x19 8-bit frame: an odd expected length, 171 bytes."""
    return (np.random.default_rng(seed).random((9, 19)) < 0.02) \
        .astype(np.uint8)


def _instance(arr, frames=None):
    inst = Instance(f"1.2.826.0.1.473.{next(_serial)}",
                    "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "OT"), ("0028,0002", 1),
                       ("0028,0004", "MONOCHROME2")):
        inst.set_attr(tag, value)
    if frames is not None:
        inst.set_attr("0028,0008", frames)
    inst.set_pixel_data(arr)
    return inst


def _export_recording(tmp_path, inst):
    """The worker, with every warning recorded (3.12 has no context-aware
    warnings, so this runs on the test's own thread)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        outcome = _export_instance_worker(ExportContext(
            instance=inst,
            output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
            patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
            study_attributes={"0020,000d": "1.2.826.0.2.1"},
            series_attributes={"0020,000e": "1.2.826.0.3.1"},
            compression="j2k", verify_readback=True))
    return outcome, [str(w.message) for w in caught]


def _bot_length(pixel_data):
    """The Basic Offset Table item's value length (PS3.5 A.4)."""
    assert pixel_data[:4] == b"\xfe\xff\x00\xe0", "no BOT item"
    return int.from_bytes(pixel_data[4:8], "little")


def test_the_window_is_pydicoms_own():
    """`expected`, and `expected + 1` only when `expected` is odd (#473).

    pydicom 3.0.2 warns for `actual in (expected, expected + expected %
    2)`. Killing mutation (M19, unit half): the window computed as
    `expected` alone.
    """
    ds = Dataset()
    ds.Rows, ds.Columns, ds.BitsAllocated = 9, 19, 8
    assert set(io_handlers._heuristic_lengths(ds, 1, 1)) == {171, 172}
    ds.Rows, ds.Columns = 16, 16
    assert set(io_handlers._heuristic_lengths(ds, 2, 1)) == {512}


def test_the_window_asks_nothing_the_encoder_did_not():
    """A dataset with no Rows, Columns or BitsAllocated has no window (#473).

    `_compress_j2k` reads its geometry with `getattr(ds, "Rows", 0)`, so a
    direct caller without it was never an AttributeError before the #473
    check; the window must not make it one. An empty window of `0` matches
    no encapsulated stream, which is never shorter than its item tags.
    Killing mutation: `int(ds.Rows)` for the `getattr` reads.
    """
    assert io_handlers._heuristic_lengths(Dataset(), 1, 1) == (0, 0)


@pytest.mark.parametrize("make, shape, window", [
    (lambda seed: [_bool_mask(seed).view(np.uint8)], "bool-16x16", (256,)),
    (lambda seed: [_sparse_odd(seed)], "uint8-9x19-odd", (172,)),
], ids=["even-expected", "odd-expected"])
def test_a_colliding_length_is_written_without_an_offset_table(
        tmp_path, make, shape, window):
    """No false "check the transfer syntax" warning on our own file (#473).

    About 1 in 5 random 16x16 bool masks encode to exactly 256 bytes of
    encapsulated Pixel Data -- the uncompressed length -- and pydicom's
    decoder then warns on the caller's stream that the transfer syntax may
    be wrong, during `verify_readback=True` and at every later read. An
    empty Basic Offset Table is PS3.5 A.4-legal and moves the length by 4
    bytes per frame, out of the window, so the file is written with one
    in exactly that case. The odd-expected case is the window's second
    value, `expected + 1`.

    Killing mutations: the `has_bot=False` branch deleted (M18, the
    warning returns); the window computed as `expected` only (M19, the
    odd case warns).
    """
    for seed in _search(make, window):
        frame = make(seed)[0]
        arr = frame.astype(bool) if shape.startswith("bool") else frame
        outcome, caught = _export_recording(tmp_path, _instance(arr))

        assert outcome.ok, outcome.error
        assert [m for m in caught if HEURISTIC in m] == [], caught
        written = pydicom.dcmread(outcome.output_path)
        assert len(written.PixelData) not in window
        assert _bot_length(written.PixelData) == 0
        assert np.array_equal(written.pixel_array, frame.astype(np.uint8))


def test_a_non_colliding_length_keeps_its_offset_table(tmp_path):
    """The table stays wherever the length is not in the window (#473).

    Killing mutation (M20): `has_bot=False` unconditional.
    """
    for seed in range(40):
        mask = _bool_mask(seed)
        if len(encapsulate([_codestream(mask.view(np.uint8))])) != 256:
            break
    outcome, caught = _export_recording(tmp_path, _instance(mask))

    assert outcome.ok, outcome.error
    assert _bot_length(pydicom.dcmread(outcome.output_path).PixelData) == 4


def test_a_two_frame_colliding_export_reingests(tmp_path):
    """Multi-frame, with no table: pydicom, the fallback and ingest agree (#473).

    An empty table still leaves one fragment per frame, and the readers
    here split frames by fragment. What the file loses is the table-based
    frame check (`offset_table_frame_count` answers None for it), which
    the readback does not need: it compares every frame's samples.
    """
    from isocenter.io_handlers import DicomImporter
    from isocenter.store import DicomStore

    lengths = {}
    for seed in range(400):
        lengths.setdefault(
            len(_codestream(_bool_mask(seed).view(np.uint8))), seed)
    pair = next(((a, lengths[512 - 32 - n]) for n, a in lengths.items()
                 if 512 - 32 - n in lengths), None)
    assert pair is not None, "no colliding 2-frame pair; re-measure #473"
    arr = np.stack([_bool_mask(pair[0]), _bool_mask(pair[1])])
    assert len(encapsulate([_codestream(f.view(np.uint8)) for f in arr])) \
        == 512

    outcome, caught = _export_recording(tmp_path, _instance(arr, frames=2))

    assert outcome.ok, outcome.error
    assert [m for m in caught if HEURISTIC in m] == [], caught
    written = pydicom.dcmread(outcome.output_path)
    assert _bot_length(written.PixelData) == 0
    expected = arr.astype(np.uint8)
    assert np.array_equal(written.pixel_array, expected)
    fallback, _ = io_handlers._decode_with_imagecodecs(
        written, None, RuntimeError("forced to the fallback"))
    assert np.array_equal(fallback, expected)
    store = DicomStore()
    summary = DicomImporter.import_files([outcome.output_path], store)
    assert summary.ingested == 1, summary.failures
    reread = store.patients[0].studies[0].series[0].instances[0]
    assert np.array_equal(reread.get_pixel_data().astype(np.uint8), expected)

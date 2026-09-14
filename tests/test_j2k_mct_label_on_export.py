"""An MCT codestream is declared `YBR_RCT`, and only an RGB source gets MCT (#490).

`imagecodecs.jpeg2k_encode`'s default turns the multiple-component
transform on for every 3-component frame. `_compress_j2k` took that
default and wrote the label the instance already had, so every colour
export -- `session.export(folder)` compresses by default -- wrote an MCT
codestream under `PhotometricInterpretation = RGB`. PS3.5 8.2.4 gives such
a codestream exactly two values, `YBR_RCT` for the reversible transform
and `YBR_ICT` for the irreversible one, so the file was non-conformant: it
told a reader no colour transform had been applied while the codestream
said one had.

Nothing here reads the pixels wrong, which is why it went unnoticed for
every release: the transform is recorded in the codestream's own `COD`
segment, so `imagecodecs.jpeg2k_decode`, `opj_decompress` and pydicom with
Pillow all recover the samples exactly under either label (measured,
maxdiff 0). A reader that trusts the DICOM label over the codestream is
the one this file is about, and it is the one the standard describes.

The fix is at the encoder, and it is one decision: MCT on for an RGB
source, declared `YBR_RCT`; `mct=False` for every other source, label
untouched. The second half matters on its own -- a `YBR_FULL` source is
already decorrelated, and MCT over it measured 66482 bytes against
`mct=False`'s 48819, a 36% loss for a file that would also have been
mislabelled.

`YBR_RCT` and not `YBR_ICT`: `level=0` is the reversible transform and it
is the only encode `_compress_j2k` makes.

The transform flag is read from the codestream's `COD` segment in every
test here, so no case is satisfied by the label alone.
"""
import os

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate, generate_frames
from pydicom.uid import (ExplicitVRLittleEndian, JPEG2000Lossless,
                         generate_uid)

from isocenter import io_handlers
from isocenter.io_handlers import _compress_j2k
from isocenter.session import DicomSession

#: Three channels far enough apart that a colour read through the wrong
#: transform is unmistakable.
RGB = (220, 40, 90)
SIZE = 8


def _cod_transform(codestream):
    """The `COD` segment's multiple-component-transform byte (15444-1 A.6.1).

    `SGcod` follows the 2-byte marker, the 2-byte length and `Scod`: the
    progression order, the layer count (2 bytes) and then this byte.

    The `COD` marker is **walked to, never searched for.** `SOC` is two
    bytes and `SIZ` follows it carrying its own length, so `COD` -- the
    first segment after `SIZ` in every stream this encoder writes -- sits
    at a position this reads rather than guesses, and the assert below
    says so. A `find(b"\\xff\\x52")` would be the #478 defect in a test
    helper: `FF 52` is an ordinary byte pair inside packet data, and a
    search that happened to land on the real marker for these streams
    would keep passing while reading a payload byte for another.
    """
    data = bytes(codestream)
    assert data[:2] == b"\xff\x4f", "no SOC"
    assert data[2:4] == b"\xff\x51", "no SIZ"
    position = 4 + int.from_bytes(data[4:6], "big")
    assert data[position:position + 2] == b"\xff\x52", "COD is not after SIZ"
    return data[position + 8]


def _frame(ds):
    return next(generate_frames(ds.PixelData, number_of_frames=1))


def _dataset(photometric, samples, dtype=np.uint8):
    """A minimal uncompressed dataset, and the array to compress."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.PatientID, ds.PatientName = "PAT490", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows, ds.Columns = SIZE, SIZE
    ds.SamplesPerPixel = samples
    if samples > 1:
        ds.PlanarConfiguration = 0
    ds.PhotometricInterpretation = photometric
    bits = np.dtype(dtype).itemsize * 8
    ds.BitsAllocated = ds.BitsStored = bits
    ds.HighBit, ds.PixelRepresentation = bits - 1, 0
    ds.NumberOfFrames = 1
    if samples > 1:
        arr = np.empty((SIZE, SIZE, samples), dtype=dtype)
        for channel in range(samples):
            arr[..., channel] = RGB[channel % 3]
    else:
        arr = np.full((SIZE, SIZE), RGB[0], dtype=dtype)
    return ds, arr


# ---------------------------------------------------------------------------
# The encoder's own decision
# ---------------------------------------------------------------------------

def test_an_rgb_source_gets_the_transform_and_the_ybr_rct_label():
    """Before: transform 1 under `RGB`, which PS3.5 8.2.4 does not allow.

    Mutant: `mct=mct` back to the codec's default. The flag stays 1 and
    the label assertion is what is left to fail -- so both halves are
    asserted here, not just the label.
    """
    ds, arr = _dataset("RGB", 3)
    _compress_j2k(ds, pixel_array=arr)
    assert _cod_transform(_frame(ds)) == 1
    assert str(ds.PhotometricInterpretation) == "YBR_RCT"
    assert ds.file_meta.TransferSyntaxUID == JPEG2000Lossless


@pytest.mark.parametrize("photometric", ["YBR_FULL", "YBR_FULL_422",
                                         "YBR_PARTIAL_420"])
def test_a_three_sample_source_in_a_luma_chroma_space_keeps_mct_off(
        photometric):
    """`mct=False`, and the label the source had (#490 case 3).

    A luma/chroma source is already decorrelated: MCT over it is both
    unnameable (the label would describe one transform over samples that
    went through two) and larger -- 66482 bytes against `mct=False`'s
    48819 on a `YBR_FULL` frame. The label describes what the codestream
    did, and this codestream does nothing to these components.

    `YBR_RCT` and `YBR_ICT` are deliberately **not** here; they are
    `test_a_source_already_labelled_for_the_transform_is_transformed`
    below. That is N2's ruling, and it is the half #490 first got wrong.

    Mutant: the label test dropped from `mct`, so any 3-sample source
    transforms. Every case goes red at the flag.
    """
    ds, arr = _dataset(photometric, 3)
    _compress_j2k(ds, pixel_array=arr)
    assert _cod_transform(_frame(ds)) == 0
    assert str(ds.PhotometricInterpretation) == photometric


@pytest.mark.parametrize("photometric", ["YBR_RCT", "YBR_ICT"])
def test_a_source_already_labelled_for_the_transform_is_transformed(
        photometric):
    """`mct=True`, and **the label it came with** (#490 case 2, N2).

    PS3.5 8.2.4 binds transform and label in both directions, and this is
    the direction #490 first got wrong: encoding `mct=False` under a
    label the source keeps declares RCT over a `COD` transform byte of 0
    -- the same non-conformance as an `RGB` label over MCT, mirrored.

    A 3-sample instance carrying such a label can only have come from a
    J2K source whose samples this library already inverse-transformed
    into memory (`jpeg2k_decode` undoes the transform, #448), so applying
    the transform on re-encode is what makes the label true of *this*
    codestream. The label is not rewritten: `YBR_ICT` stays `YBR_ICT`
    rather than becoming `YBR_RCT`, because relabelling would name a
    transform this function did not choose, and relabelling to `RGB`
    would discard the source's stated colour space against #482.

    Mutant: `_J2K_MCT_SOURCES` narrowed back to `{"RGB"}`, or the
    relabel's `photometric == "RGB"` guard dropped. The first goes red at
    the flag, the second at the label.
    """
    ds, arr = _dataset(photometric, 3)
    _compress_j2k(ds, pixel_array=arr)
    assert _cod_transform(_frame(ds)) == 1
    assert str(ds.PhotometricInterpretation) == photometric
    assert ds.file_meta.TransferSyntaxUID == JPEG2000Lossless


@pytest.mark.parametrize("photometric", ["MONOCHROME1", "MONOCHROME2",
                                         "PALETTE COLOR"])
def test_a_single_sample_source_is_encoded_without_the_transform(photometric):
    """One component has nothing to transform, and keeps its label."""
    ds, arr = _dataset(photometric, 1)
    _compress_j2k(ds, pixel_array=arr)
    assert _cod_transform(_frame(ds)) == 0
    assert str(ds.PhotometricInterpretation) == photometric


def test_a_single_sample_source_mislabelled_rgb_is_not_relabelled_on_top():
    """A header that says `RGB` over one sample gets no second wrong label.

    `SamplesPerPixel 1` with `PhotometricInterpretation RGB` is
    nonconformant (PS3.3 C.7.6.3.1.2 gives `RGB` three samples), and such
    files exist. It matters here because **openjpeg ignores `mct` unless
    the frame has exactly three components**: measured, a 1-component and
    a 4-component frame both encode to `COD` transform 0 under `mct=True`,
    byte for byte as under `mct=False`. So the sample count in the `mct`
    decision is load-bearing for the *label*, not for the encode -- drop
    it and a mislabelled monochrome file comes back declaring `YBR_RCT`
    over a codestream that carries no transform, one wrong label replaced
    by another.

    Mutant: `samples == 3` dropped from `mct`, leaving the label test
    alone. Red here, and green in every other case in this module, which
    is why this case exists.
    """
    ds, arr = _dataset("RGB", 1)
    _compress_j2k(ds, pixel_array=arr)
    assert _cod_transform(_frame(ds)) == 0
    assert str(ds.PhotometricInterpretation) == "RGB"


def test_a_refused_frame_leaves_the_label_alone():
    """`_compress_j2k` mutates nothing when it refuses, the label included.

    A 32-bit frame is refused by name before any encode, so this holds the
    frame guard's own arm. The encode-time failure below is the case that
    pins *where* the relabel is written.
    """
    ds, arr = _dataset("RGB", 3, dtype=np.uint32)
    with pytest.raises(RuntimeError):
        _compress_j2k(ds, pixel_array=arr)
    assert str(ds.PhotometricInterpretation) == "RGB"
    assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian


def test_a_second_frame_that_fails_to_encode_leaves_the_label_alone(
        monkeypatch):
    """The relabel is written after the last frame encodes, not before it.

    The frame guard runs ahead of the `mct` decision, so a refusal it
    raises says nothing about where the relabel sits: both orders leave
    the label alone there. This is the case that separates them -- a
    two-frame instance whose first frame encodes and whose second raises.
    `_compress_j2k` turns that into `Compression failed: …` and the
    dataset must come back describing the pixels it still has, under the
    transfer syntax it still has.

    Mutant: the relabel moved above the encode loop. Red here alone.
    """
    ds, arr = _dataset("RGB", 3)
    ds.NumberOfFrames = 2
    frames = np.stack([arr, arr[::-1]])
    real = io_handlers.jpeg2k_encode
    seen = []

    def _encode(frame, **options):
        seen.append(len(seen))
        if len(seen) == 2:
            raise RuntimeError("the second frame's encode fails")
        return real(frame, **options)

    monkeypatch.setattr(io_handlers, "jpeg2k_encode", _encode)
    with pytest.raises(RuntimeError, match="Compression failed"):
        _compress_j2k(ds, pixel_array=frames)
    assert seen == [0, 1], "the first frame must encode before the second"
    assert str(ds.PhotometricInterpretation) == "RGB"
    assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian


def test_the_transform_is_reversible_at_both_depths():
    """The samples survive it, which is what `level=0` promises.

    `YBR_RCT` names the *reversible* transform. If the encode were lossy
    -- or the label named `YBR_ICT` over a reversible stream -- this is
    the assertion that shows it.
    """
    for dtype in (np.uint8, np.uint16):
        ds, arr = _dataset("RGB", 3, dtype=dtype)
        arr = (arr.astype(np.int64) * (7 if dtype is np.uint8 else 257)
               % np.iinfo(dtype).max).astype(dtype)
        _compress_j2k(ds, pixel_array=arr)
        got = imagecodecs.jpeg2k_decode(_frame(ds))
        assert got.dtype == arr.dtype, dtype
        assert got.tolist() == arr.tolist(), dtype


# ---------------------------------------------------------------------------
# End to end, through the pipeline that compresses by default
# ---------------------------------------------------------------------------

def _native_rgb(folder):
    ds, arr = _dataset("RGB", 3)
    ds.PixelData = arr.tobytes()
    path = os.path.join(str(folder), "rgb.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path, arr


def _rng_rgb(rows=8, cols=8):
    """A deterministic 8-bit RGB frame with every channel distinct."""
    return np.random.default_rng(490).integers(
        0, 256, size=(rows, cols, 3), dtype=np.uint8)


def _write_j2k(path, arr, photometric, mct):
    """`arr` as a real J2K file labelled `photometric`.

    `mct=True` writes a codestream that genuinely carries the transform,
    so a `YBR_RCT` fixture is a conformant file rather than a mislabelled
    one -- otherwise this test would be measuring the defect #490 fixes
    instead of the round trip.
    """
    codestream = imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K", mct=mct)
    assert _cod_transform(codestream) == (1 if mct else 0), (
        "fixture does not carry the transform it claims")
    ds, _unused = _dataset(photometric, 3)
    ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
    ds.PixelData = encapsulate([codestream])
    ds["PixelData"].is_undefined_length = True
    ds.save_as(str(path), enforce_file_format=True)
    return str(path)


def _exported(out):
    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, written
    return written[0]


def test_an_ingested_ybr_rct_file_is_stored_rgb_so_the_export_relabels_it(
        tmp_path):
    """Which of #490's three cases an *ingested* J2K colour file reaches.

    This is the boundary between case 1 and case 2, and it is measured
    rather than assumed because #482 and this change touch the same
    label. `jpeg2k_decode` undoes the codestream's transform and returns
    RGB, and `_FALLBACK_J2K` stores that answer (#448, #482) -- so a
    `YBR_RCT` **file** is stored as an `RGB` instance, and the export
    that follows takes **case 1**: transform on, relabelled `YBR_RCT`.

    So case 2 -- a `YBR_RCT`/`YBR_ICT` label surviving as far as the
    encoder -- is reachable only through a graph built by hand or a
    caller who sets `PhotometricInterpretation` itself, never through
    `ingest()`. That is why N2 is a conformance fix at the encoder and
    not a change to any ingested file's round trip, and why the owner
    declined to refuse the combination: no ingested file reaches it.

    The samples survive both passes, which is the thing a relabel must
    not cost.
    """
    src = tmp_path / "src"
    src.mkdir()
    arr = _rng_rgb()
    _write_j2k(src / "ybr.dcm", arr, "YBR_RCT", mct=True)
    out = tmp_path / "out"

    with DicomSession(str(tmp_path / "one.db")) as session:
        summary = session.ingest(str(src))
        assert summary.ingested == 1, summary.failures
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert instance.attributes.get("0028,0004") == "RGB", (
            "the decoder's answer is what is stored (#448, #482)")
        assert instance.get_pixel_data().tolist() == arr.tolist()
        session.export(str(out), show_progress=False)

    exported = pydicom.dcmread(_exported(out))
    assert _cod_transform(_frame(exported)) == 1
    assert str(exported.PhotometricInterpretation) == "YBR_RCT", (
        "case 1: stored RGB, so transformed and relabelled")
    assert exported.pixel_array.tolist() == arr.tolist()


def test_an_exported_colour_file_declares_the_transform_it_carries(tmp_path):
    """The whole path: ingest an RGB file, export it, read the export.

    Before: `PhotometricInterpretation = RGB` beside a codestream whose
    transform flag was 1. Now `YBR_RCT` beside the same flag, and pydicom
    still reads the source colour -- it takes the transform from the
    codestream, as every reader measured here does.

    And the file re-ingests as `RGB` with the same samples: `jpeg2k_decode`
    undoes the transform and the label follows the decoder's answer
    (#448, #482), so the round trip is stable rather than accumulating a
    relabel each pass.
    """
    src = tmp_path / "src"
    src.mkdir()
    _path, arr = _native_rgb(src)
    out = tmp_path / "out"

    with DicomSession(str(tmp_path / "one.db")) as session:
        session.ingest(str(src))
        assert session.store.patients[0].studies[0].series[0].instances[
            0].attributes.get("0028,0004") == "RGB"
        session.export(str(out), show_progress=False)

    exported_path = _exported(out)
    exported = pydicom.dcmread(exported_path)
    assert exported.file_meta.TransferSyntaxUID == JPEG2000Lossless
    assert _cod_transform(_frame(exported)) == 1
    assert str(exported.PhotometricInterpretation) == "YBR_RCT", (
        "the exported file declares %r over an MCT codestream; PS3.5 8.2.4 "
        "gives that codestream YBR_RCT or YBR_ICT and nothing else (#490)"
        % exported.PhotometricInterpretation)
    assert exported.pixel_array.tolist() == arr.tolist()

    with DicomSession(str(tmp_path / "two.db")) as session:
        summary = session.ingest(os.path.dirname(exported_path))
        assert summary.ingested == 1, summary.failures
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert instance.attributes.get("0028,0004") == "RGB"
        assert instance.get_pixel_data().tolist() == arr.tolist()


# ---------------------------------------------------------------------------
# #532: a lower-case RGB is RGB to the encoder too
# ---------------------------------------------------------------------------

def test_lower_case_rgb_is_transformed_under_j2k(tmp_path):
    """`'rgb'` gets case 1: the transform, and `YBR_RCT` (#532).

    `_compress_j2k` compares the label case-sensitively, so a declared
    `rgb` used to be encoded `mct=False` and written `rgb` -- neither the
    transform nor a label a reader accepts. The worker now writes the
    label as the Code String it spells before the encoder reads it, so
    the encoder is unchanged and sees `RGB`.

    Killing mutation (M4, the J2K half): the worker's normalization
    removed (transform byte 0, label `rgb`).
    """
    from isocenter.entities import Instance
    from isocenter.io_handlers import ExportContext, _export_instance_worker

    inst = Instance("1.2.826.0.1.532.1", "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "OT"), ("0028,0002", 3)):
        inst.set_attr(tag, value)
    arr = _rng_rgb()
    inst.set_pixel_data(arr)
    inst.set_attr("0028,0004", "rgb")

    outcome = _export_instance_worker(ExportContext(
        instance=inst, output_path=str(tmp_path / "out" / "rgb.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        compression="j2k", verify_readback=True))

    assert outcome.ok, outcome.error
    exported = pydicom.dcmread(outcome.output_path)
    assert _cod_transform(_frame(exported)) == 1
    assert str(exported.PhotometricInterpretation) == "YBR_RCT"
    assert exported.pixel_array.tolist() == arr.tolist()

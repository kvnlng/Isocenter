"""High-Throughput JPEG 2000 decodes, through openjpeg, at every door (#459).

HTJ2K (`1.2.840.10008.1.2.4.201`, `.202`, `.203`) is JPEG 2000 Part 15: a
codestream with the same SOC and SIZ markers and a faster block coder.
pydicom decodes it only with pylibjpeg-openjpeg, which this package does
not require, so every door refused it: "all plugins are missing
dependencies". Owner ruling Q6 (a): decode it through the imagecodecs
fallback, the way JPEG 2000 is decoded when Pillow cannot.

**`jpeg2k_decode`, never `htj2k_decode`.** openjpeg reads an HTJ2K
codestream exactly, for every shape here, at imagecodecs 2024.6.1 (the
floor) and 2026.8.16 (measured, dev-F1/htj2k_floor_2024.6.1.raw).
`htj2k_decode` (openjph) returns an RGB stream written without the colour
transform *planar*, `(3, rows, cols)`, which is a different image in the
same number of samples. So an HTJ2K syntax is a member of
`imagecodecs_handler.J2K_SYNTAXES`, and inherits all of JPEG 2000's rules:
the colour transform relabel, the container, the signed-codestream gate
(#524) and PixelRepresentation's reinterpretation (#460).

Every fixture first asserts that pydicom cannot decode it, so no case
here passes through pydicom's door and says nothing about the fallback.
Every expected array is the encoder's input, never a decode of it.
"""
import os

import imagecodecs
import numpy as np
import pydicom
import pytest

from support.decode_doors import (HTJ2K, HTJ2K_LOSSLESS, HTJ2K_RPCL,
                                  at_decode_pixels, at_ingest, at_instance,
                                  dataset, pydicom_answer, same, write)

SYNTAXES = [HTJ2K_LOSSLESS, HTJ2K_RPCL, HTJ2K]
SYNTAX_IDS = [".201", ".202", ".203"]

_RNG = np.random.default_rng(459)
#: Every value distinct from its neighbours, and every RGB channel from the
#: others, so a plane swap or a planar read cannot compare equal.
SOURCES = {
    "mono8": _RNG.integers(0, 256, (8, 8), dtype=np.uint8),
    "mono16": _RNG.integers(0, 65536, (8, 8), dtype=np.uint16),
    "int16": _RNG.integers(-32768, 32768, (8, 8), dtype=np.int16),
    "rgb8": _RNG.integers(0, 256, (8, 8, 3), dtype=np.uint8),
    "rgb16": _RNG.integers(0, 65536, (8, 8, 3), dtype=np.uint16),
}


def _htj2k(arr, colour_transform=False):
    """A reversible HTJ2K codestream; RGB without the transform unless asked."""
    options = {}
    if arr.ndim == 3 and not colour_transform:
        options["rgb"] = False
    return imagecodecs.htj2k_encode(arr, reversible=True, **options)


def _file(tmp_path, ts, arr, photometric, colour_transform=False,
          pixel_representation=None, name="one"):
    samples = arr.shape[2] if arr.ndim == 3 else 1
    signed = arr.dtype.kind == "i"
    ds = dataset(ts, [_htj2k(arr, colour_transform)], rows=arr.shape[0],
                 cols=arr.shape[1], samples=samples,
                 bits_allocated=arr.dtype.itemsize * 8,
                 pixel_representation=(int(signed)
                                       if pixel_representation is None
                                       else pixel_representation),
                 photometric=photometric)
    path = write(tmp_path, ds, name=name)
    assert isinstance(pydicom_answer(path), RuntimeError), \
        "pydicom decoded it, so this case would measure pydicom"
    return path


@pytest.mark.parametrize("ts", SYNTAXES, ids=SYNTAX_IDS)
@pytest.mark.parametrize("shape", list(SOURCES))
def test_an_htj2k_file_reads_exactly_at_every_door(tmp_path, monkeypatch,
                                                   ts, shape):
    """M18, M19: the encoder's input, in its dtype and shape, at all three.

    Before: refused at every door in pydicom's "missing dependencies"
    words. A syntax missing from `_IMAGECODECS_FALLBACK_SYNTAXES` is
    refused again; `htj2k_decode` in place of `jpeg2k_decode` returns the
    RGB cells planar, which the fallback's shape check refuses.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    want = SOURCES[shape]
    photometric = "RGB" if want.ndim == 3 else "MONOCHROME2"
    path = _file(tmp_path, ts, want, photometric)

    decoded = at_decode_pixels(path)
    assert isinstance(decoded, tuple), decoded
    assert same(decoded[0], want) and decoded[1] == photometric, decoded
    read = at_instance(path)
    assert isinstance(read, tuple), read
    assert same(read[0], want), read
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got["failure"]
    assert same(got["array"], want), got["array"]
    assert got["label"] == photometric
    assert got["rows"] == [], got["rows"]


@pytest.mark.parametrize("ts", SYNTAXES, ids=SYNTAX_IDS)
@pytest.mark.parametrize("bits", [8, 16])
def test_an_htj2k_ybr_rct_file_is_stored_as_rgb(tmp_path, monkeypatch, ts,
                                                 bits):
    """M20: the colour transform openjpeg undoes is a relabel, as under .90.

    A reversible colour transform decodes to the RGB the encoder was
    given, exactly, so a file declaring `YBR_RCT` holds RGB once decoded
    and is labelled `RGB`, at every door. Without the syntax in
    `_FALLBACK_DECODER_CONVERTS` the fallback believes it must convert,
    and refuses the 16-bit cell as a 16-bit YBR conversion.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    want = SOURCES[f"rgb{bits}"]
    path = _file(tmp_path, ts, want, "YBR_RCT", colour_transform=True)

    decoded = at_decode_pixels(path)
    assert isinstance(decoded, tuple), decoded
    assert same(decoded[0], want) and decoded[1] == "RGB", decoded
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got["failure"]
    assert same(got["array"], want), got["array"]
    assert got["label"] == "RGB"


@pytest.mark.parametrize("ts", SYNTAXES, ids=SYNTAX_IDS)
def test_a_signed_htj2k_codestream_under_pixel_representation_0_is_refused_in_the_gate_words(  # noqa: E501  pylint: disable=line-too-long
        tmp_path, monkeypatch, ts):
    """M22: #524's gate reads an HTJ2K SIZ as it reads a JPEG 2000 one.

    Asserted on the unwrapped words. A gate that skipped HTJ2K would leave
    the refusal to the fallback's own `_against_pixel_representation`,
    whose words arrive after "imagecodecs could not decode it either", so
    a substring check would pass either way.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    path = _file(tmp_path, ts, SOURCES["int16"], "MONOCHROME2",
                 pixel_representation=0)
    words = ("the JPEG 2000 codestream is signed at precision 16, where "
             "PixelRepresentation 0 declares unsigned samples")
    decoded = at_decode_pixels(path)
    assert isinstance(decoded, RuntimeError), decoded
    assert str(decoded).startswith(words), str(decoded)
    got = at_ingest(tmp_path, path)
    assert got["failure"].startswith(
        f"Decompression Failed: RuntimeError: {words}"), got["failure"]


@pytest.mark.parametrize("compression", [True, False],
                         ids=["compressed", "native"])
def test_an_htj2k_source_exports_and_reads_back(tmp_path, monkeypatch,
                                                 compression):
    """Attack A20: nothing writes HTJ2K, so an export writes J2K or native.

    A 16-bit `YBR_RCT` source is stored as RGB at ingest. The native
    export writes it under `RGB`; the compressed one under `YBR_RCT`,
    because `_compress_j2k` writes an RGB source with the reversible
    colour transform (#516). The export's own readback, which decodes
    through `_decode_pixels` too, accepts both. Read back here through
    the same door, each is the source's RGB samples, labelled RGB.
    """
    from isocenter.session import DicomSession  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    want = SOURCES["rgb16"]
    path = _file(tmp_path, HTJ2K_LOSSLESS, want, "YBR_RCT",
                 colour_transform=True)
    out = tmp_path / "out"
    with DicomSession(persistence_file=str(tmp_path / "x.db")) as session:
        assert session.ingest(os.path.dirname(path)).ingested == 1
        summary = session.export(str(out), use_compression=compression)
        assert (summary.written, summary.failures) == (1, []), summary
    written = [os.path.join(root, name) for root, _dirs, names in os.walk(out)
               for name in names if name.endswith(".dcm")]
    assert len(written) == 1, written
    exported = pydicom.dcmread(written[0])
    assert str(exported.file_meta.TransferSyntaxUID) not in SYNTAXES
    assert exported.PhotometricInterpretation == (
        "YBR_RCT" if compression else "RGB")
    decoded = at_decode_pixels(written[0])
    assert isinstance(decoded, tuple), decoded
    assert same(decoded[0], want) and decoded[1] == "RGB", decoded

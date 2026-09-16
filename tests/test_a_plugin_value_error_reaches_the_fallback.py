"""A `ValueError` out of pydicom's decode reaches the imagecodecs fallback (#663).

pydicom's runner reads a plugin's output buffer with the **header's**
dtype rather than widening the stream's own container, as
`imagecodecs_handler._in_declared_container` does. pylibjpeg-libjpeg
returns a precision-8 JPEG Lossless stream's samples one byte each, so
under BitsAllocated 16 `DecodeRunner` raises numpy's `ValueError: could
not broadcast input array from shape (32,) into shape (64,)` -- out of
pydicom, not out of the plugin. `_decode_pixels` caught only
`RuntimeError`, so the file was lost: 0 instances, an `ERROR` row and
`REVIEW_REQUIRED` where the fallback decodes it correctly, and *precision
8, BitsStored 8, BitsAllocated 16* is a fully conformant DICOM file.

**CI installs no pylibjpeg, so the plugin's exception is injected here.**
The real plugin was measured on a8b6d3f in a throwaway environment
(pylibjpeg 2.1.0, pylibjpeg-libjpeg 2.4.0, pydicom 3.0.2) on 3.12 and on
3.14t: of 128 `.57`/`.70` cells, **72 raised that `ValueError` and the
fallback decodes 60 of them**; the other 12 -- a stream *wider* than its
container -- the fallback refuses on its own dtype terms. The logs are
`.agent/scratch-098/probes-J9/p663-ljp312.jsonl`, `p663-ljp314t.jsonl`
and `psess-ljp312.txt`. Every test in this file runs on the gate, because
`broadcast_failure` injects exactly the measured exception rather than
needing the plugin that raises it.

The safety argument is the other half, and it is testable here without
any plugin: a **validation** `ValueError` cannot be masked, because
`_validate_like_pydicom` (#453) runs before the fallback and outside it
and re-raises pydicom's own header refusal verbatim. Measured over eight
malformed headers under both a plugin route and Pillow's, `as_array` and
`_validate_like_pydicom` raise the same class with the same message in 16
of 16 cases (`p663b-ljp312.jsonl`).
"""
import concurrent.futures
import os
import sqlite3
from contextlib import contextmanager

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.pixels.decoders.base import Decoder

from isocenter.io_handlers import _decode_pixels
from isocenter.session import DicomSession
from support.decode_doors import (J2K_LOSSLESS, LJPEG, LJPEG_SV1, dataset,
                                  write)

#: The exact message pydicom's runner raised with pylibjpeg-libjpeg
#: installed, for a precision-8 stream under BitsAllocated 16 (measured).
NARROW_WORDS = "could not broadcast input array from shape (32,) into shape (64,)"
#: And for a precision-12 stream under BitsAllocated 8 -- the population
#: the fallback refuses, in its own dtype words.
WIDE_WORDS = "could not broadcast input array from shape (128,) into shape (64,)"

_YY, _XX = np.mgrid[0:8, 0:8]
#: Precision-8 samples, 1..252: conformant under BitsStored 8.
IMG8 = ((_XX * 32 + _YY * 4) + 1).astype(np.uint16)
#: Precision-12 samples, up to 3920: wider than an 8-bit container.
IMG12 = (_XX * 500 + _YY * 60).astype(np.uint16)
#: The same precision-8 samples read under PixelRepresentation 1: the
#: fallback sign-extends every decode from BitsStored, so 129 reads -127
#: (`imagecodecs_handler._sign_extend`). The point of the signed arm is
#: that the widened catch changed nothing about how a sign is read.
IMG8_SIGNED = np.where(IMG8 >= 128, IMG8.astype(np.int32) - 256,
                       IMG8).astype(np.int16)


def _ljpeg(samples, bits):
    pattern = (samples.astype(np.int64) & ((1 << bits) - 1)).astype(np.uint16)
    return imagecodecs.ljpeg_encode(pattern, bitspersample=bits)


@pytest.fixture
def broadcast_failure(monkeypatch):
    """pydicom's runner raises the measured `ValueError` for `.57`/`.70`.

    Yields `{"n": raises, "ingests": in-process ingests, "words": ...}`.
    `words` is set per test before the ingest. Every other syntax decodes
    as it always does.

    An `ingest()` in this test runs on a one-thread pool **in this
    process**, because the session's own pool spawns processes whatever
    `ISOCENTER_FORCE_THREADS` says and a monkeypatch does not reach a
    spawned worker: `DicomSession._ingest_executor` is swapped, exactly as
    `support.decode_doors.pydicom_cannot` swaps it, and each test asserts
    the swap was used and the patch fired. A green test whose counter
    never moved would be measuring the real decoder.
    """
    real = Decoder.as_array
    calls = {"n": 0, "ingests": 0, "words": NARROW_WORDS}

    @contextmanager
    def in_process(_session):
        calls["ingests"] += 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            yield pool

    def broadcasts(self, src, **kwargs):
        if str(self.UID) not in (LJPEG, LJPEG_SV1):
            return real(self, src, **kwargs)
        calls["n"] += 1
        raise ValueError(calls["words"])

    monkeypatch.setattr(Decoder, "as_array", broadcasts)
    monkeypatch.setattr(DicomSession, "_ingest_executor", in_process)
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    yield calls


def _file(ts, stream, *, bits_allocated, bits_stored, pr=0, drop=()):
    return dataset(ts, [stream], rows=8, cols=8,
                   bits_allocated=bits_allocated, bits_stored=bits_stored,
                   pixel_representation=pr, drop=drop)


def _ingest(tmp_path, ds, name="s"):
    """Ingest the one file, then read it back from the sidecar and export."""
    path = write(tmp_path, ds, name)
    db = str(tmp_path / f"{name}.db")
    out = tmp_path / f"{name}-out"
    report = tmp_path / f"{name}.md"
    got = {"failures": [], "array": None, "exported": None}
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        got["failures"] = list(summary.failures)
        instances = [i for p in session.store.patients for st in p.studies
                     for se in st.series for i in se.instances]
        if instances:
            (inst,) = instances
            assert inst.unload_pixel_data() is True
            got["array"] = inst.get_pixel_data()
            session.export(str(out), format="dicom")
            (written,) = [os.path.join(r, f)
                          for r, _d, fs in os.walk(str(out))
                          for f in fs if f.endswith(".dcm")]
            got["exported"] = pydicom.dcmread(written)
        session.generate_report(str(report))
    got["grade"] = "\n".join(
        line for line in report.read_text(encoding="utf-8").splitlines()
        if "Validation Status" in line)
    with sqlite3.connect(db) as conn:
        got["rows"] = conn.execute(
            "SELECT action_type, details FROM audit_log WHERE action_type "
            "IN ('WARNING', 'DATA_LOSS', 'ERROR')").fetchall()
    return got


# ---------------------------------------------------------------------------
# T1: the fallback answers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1], ids=[".57", ".70"])
@pytest.mark.parametrize("pr", [0, 1], ids=["unsigned", "signed"])
def test_a_plugin_value_error_falls_through_to_the_fallback(
        broadcast_failure, ts, pr):
    """`_decode_pixels` returns the fallback's array where it raised."""
    stream = _ljpeg(IMG8, 8)
    ds = _file(ts, stream, bits_allocated=16, bits_stored=8, pr=pr)

    arr, photometric = _decode_pixels(ds)

    assert broadcast_failure["n"] == 1, "pydicom was never asked"
    want = IMG8_SIGNED if pr else IMG8
    assert arr.tolist() == want.tolist()
    assert arr.dtype == np.dtype("int16" if pr else "uint16")
    assert photometric == "MONOCHROME2"


# ---------------------------------------------------------------------------
# T2: the conformant file ingests, with no row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1], ids=[".57", ".70"])
@pytest.mark.parametrize("bits_stored", [8, 12], ids=["bs8", "bs12"])
def test_a_conformant_eight_bit_stream_under_bits_allocated_sixteen_ingests(
        tmp_path, broadcast_failure, ts, bits_stored):
    """Red on a8b6d3f: 0 instances, an `ERROR` row, `REVIEW_REQUIRED`.

    `psess-ljp312.txt` rows 1-3 are the same three shapes through a real
    session with the real plugin.
    """
    ds = _file(ts, _ljpeg(IMG8, 8), bits_allocated=16,
               bits_stored=bits_stored)
    got = _ingest(tmp_path, ds)

    assert broadcast_failure["ingests"] == 1, "the ingest ran out of process"
    assert broadcast_failure["n"] >= 1, "pydicom was never asked"
    assert not got["failures"], got["failures"]
    assert got["array"].tolist() == IMG8.tolist()
    assert not got["rows"], got["rows"]
    assert "PASS" in got["grade"] and "REVIEW" not in got["grade"], \
        got["grade"]
    assert got["exported"].BitsStored == bits_stored
    assert got["exported"].pixel_array.tolist() == IMG8.tolist()


# ---------------------------------------------------------------------------
# T3: a validation ValueError is still the refusal, in pydicom's words
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patched", [True, False],
                         ids=["plugin-raises", "no-plugin"])
def test_a_validation_value_error_is_still_the_refusal(
        tmp_path, request, patched):
    """BitsStored 17 under `.70`: pydicom's own words, not an imagecodecs one.

    `_validate_like_pydicom` runs before the fallback and outside it, so
    the header refusal is re-raised verbatim whether pydicom raised a
    `ValueError` of its own or never got to validate at all.
    """
    if patched:
        request.getfixturevalue("broadcast_failure")
    ds = _file(LJPEG_SV1, _ljpeg(IMG8, 8), bits_allocated=16, bits_stored=8)
    ds.BitsStored = 17
    ds.HighBit = 16

    with pytest.raises(ValueError) as caught:
        _decode_pixels(ds)

    message = str(caught.value)
    assert message.startswith(
        "A (0028,0101) 'Bits Stored' value of '17' is invalid"), message
    assert "imagecodecs could not decode it either" not in message


def test_a_missing_type_1_element_is_still_pydicoms_attribute_error(tmp_path):
    """`AttributeError` is deliberately not caught (#281), and need not be.

    A file missing PlanarConfiguration under SamplesPerPixel 3 is refused
    by `_validate_like_pydicom` before imagecodecs is asked -- which is
    why the docstring clause that a `ValueError` must stay a refusal
    "because imagecodecs ignores PlanarConfiguration" has been false since
    #453.
    """
    ds = dataset(LJPEG_SV1, [_ljpeg(IMG8, 8)], rows=8, cols=8, samples=3,
                 bits_allocated=8, bits_stored=8, photometric="RGB",
                 drop=("PlanarConfiguration",))

    with pytest.raises(AttributeError) as caught:
        _decode_pixels(ds)

    assert "Planar Configuration" in str(caught.value), str(caught.value)


# ---------------------------------------------------------------------------
# T4: the cells the fallback still refuses keep both reasons
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1], ids=[".57", ".70"])
def test_a_plugin_value_error_the_fallback_cannot_decode_keeps_both_reasons(
        broadcast_failure, ts):
    """A stream *wider* than its container: refused, with pydicom's reason first.

    The refusal's class is `RuntimeError`, not `ValueError`:
    `_decode_with_imagecodecs.refused` interpolates pydicom's *message*
    into a `RuntimeError` of its own, so the wrapped class is dropped.
    Measured here rather than assumed -- the row a user reads carries no
    `ValueError:` token at all. That the class is lost is #683.
    """
    broadcast_failure["words"] = WIDE_WORDS
    ds = _file(ts, _ljpeg(IMG12, 12), bits_allocated=8, bits_stored=8)

    with pytest.raises(RuntimeError) as caught:
        _decode_pixels(ds)

    assert str(caught.value) == (
        f"{WIDE_WORDS}; imagecodecs could not decode it either: it decoded "
        f"to uint16, where BitsAllocated 8 and PixelRepresentation 0 "
        f"declare uint8")
    assert not isinstance(caught.value, ValueError)


def test_the_wide_shape_is_refused_at_ingest_with_both_reasons(
        tmp_path, broadcast_failure):
    """And the ingest row a user reads, end to end."""
    broadcast_failure["words"] = WIDE_WORDS
    got = _ingest(tmp_path, _file(LJPEG_SV1, _ljpeg(IMG12, 12),
                                  bits_allocated=8, bits_stored=8))

    assert len(got["failures"]) == 1, got["failures"]
    assert got["failures"][0][1] == (
        f"Decompression Failed: RuntimeError: {WIDE_WORDS}; imagecodecs "
        f"could not decode it either: it decoded to uint16, where "
        f"BitsAllocated 8 and PixelRepresentation 0 declare uint8 "
        f"(caused by ValueError: {WIDE_WORDS})")


# ---------------------------------------------------------------------------
# The gate on the widened catch
# ---------------------------------------------------------------------------

def test_a_value_error_outside_the_fallback_syntaxes_is_re_raised(tmp_path):
    """The `_IMAGECODECS_FALLBACK_SYNTAXES` gate still decides who falls through.

    A widened `except` that forgot the gate would hand every syntax's
    `ValueError` to a fallback whose checks were designed around two named
    shapes. Patched for JPEG 2000, which the gate does admit, and for
    Explicit VR Little Endian, which it does not.
    """
    from unittest import mock  # pylint: disable=import-outside-toplevel
    from pydicom.uid import ExplicitVRLittleEndian  # pylint: disable=import-outside-toplevel
    real = Decoder.as_array
    native = dataset(ExplicitVRLittleEndian, rows=8, cols=8,
                     bits_allocated=8, bits_stored=8,
                     native=IMG8.astype(np.uint8).tobytes())

    def always(self, src, **kwargs):  # pylint: disable=unused-argument
        raise ValueError("a reason of pydicom's own")

    with mock.patch.object(Decoder, "as_array", always):
        with pytest.raises(ValueError) as caught:
            _decode_pixels(native)
    assert str(caught.value) == "a reason of pydicom's own"
    assert Decoder.as_array is real


def test_a_j2k_value_error_reaches_the_fallback_too(tmp_path):
    """JPEG 2000 is in the fallback set, so its `ValueError` falls through."""
    from unittest import mock  # pylint: disable=import-outside-toplevel
    codestream = imagecodecs.jpeg2k_encode(IMG8.astype(np.uint16), level=0,
                                           codecformat="J2K")
    ds = _file(J2K_LOSSLESS, codestream, bits_allocated=16, bits_stored=16)
    calls = []

    def broadcasts(self, src, **kwargs):  # pylint: disable=unused-argument
        calls.append(str(self.UID))
        raise ValueError(NARROW_WORDS)

    with mock.patch.object(Decoder, "as_array", broadcasts):
        arr, _photometric = _decode_pixels(ds)

    assert calls == [J2K_LOSSLESS]
    assert arr.tolist() == IMG8.tolist()

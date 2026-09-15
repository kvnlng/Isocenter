"""A stream whose own precision exceeds BitsStored is read by it, and said (#622).

JPEG, JPEG-LS and JPEG 2000 streams state their own sample precision.
PS3.5 8.2.1: "should the characteristics explicitly specified in the
compressed data stream ... be inconsistent with those specified in the
DICOM Data Elements, those explicitly specified in the compressed data
stream should be used to control the decompression". The v0.9.8 ruling
(Q2, and OQ1 option A) is to read by the stream's precision at every door,
frame by frame, and write one `WARNING` row at ingest -- only where a
decoded sample does not fit BitsStored (the owner's ruling on review F2 of
#659: DCMTK's true-lossless encoder writes precision 16 under BitsStored 12
for ordinary files, whose samples all fit).

Measured on abcb3aa, a 12-bit stream under BitsAllocated 16 / BitsStored 8:

* every family read the 12-bit samples with no row, and graded PASS;
* a **signed** JPEG Lossless stream was sign-extended from BitsStored 8:
  `[-2048, -1548, -1048, ...]` was stored and exported as
  `[0, -12, -24, ...]`, under a BitsStored 8 those wrong values fit;
* an 8-bit JPEG Baseline stream under BitsStored 6 was masked to 6 bits by
  pydicom (252 read 60), while the same samples as JPEG 2000 were not.

For a conformant stream (precision at most BitsStored) nothing here
changes: the guards at the bottom pin that.

**The pylibjpeg half is measured, not in CI.** Only Pillow's route
(`.50`, 8-bit) is available to this suite on pydicom's side; JPEG Lossless
has no pydicom plugin here and decodes through the imagecodecs fallback.
pydicom with pylibjpeg-libjpeg was measured in a throwaway environment for
#622 (pylibjpeg 2.1.0, pylibjpeg-libjpeg 2.4.0, pydicom 3.0.2; `.57`/`.70`
12-bit under BitsStored 8): masked by default, unmasked with
`correct_unused_bits=False`, and equal to the source once sign-extended at
the stream's precision, which is what `_decode_pixels` now does. This
file's wider-stream tests pass in that environment, and fail there with
either half of that removed.
"""
import logging
import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.pixels.decoders.base import Decoder
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import _decode_pixels, _sample_beyond
from isocenter.session import DicomSession
from support.decode_doors import (J2K_LOSSLESS, JPEGLS, LJPEG, LJPEG_SV1,
                                  at_instance, dataset, write)

JPEG_BASELINE = "1.2.840.10008.1.2.4.50"
JPEG_EXTENDED = "1.2.840.10008.1.2.4.51"

#: DCMTK's true-lossless shape from pydicom's own test data: a JPEG-LS
#: stream of precision 16 under BitsAllocated 16, BitsStored 12, whose
#: samples all fit 12 bits (review F2 of #659). Resolved at import, so a
#: missing one fails loudly.
EMRI_JPEG_LS = get_testdata_file("emri_small_jpeg_ls_lossless.dcm")
assert EMRI_JPEG_LS

_YY, _XX = np.mgrid[0:8, 0:8]
#: 12-bit samples, up to 3920: above 255, so BitsStored 8 cannot hold them.
IMG12 = (_XX * 500 + _YY * 60).astype(np.uint16)
#: The same, signed: -2048 .. 1872.
S12 = (IMG12.astype(np.int16) - 2048)
#: 16-bit samples, up to 59500, for a 16-bit stream under BitsStored 12.
IMG16 = (_XX * 8000 + _YY * 500).astype(np.uint16)
#: 8-bit samples, up to 252, for a stream wider than BitsStored 6.
IMG8 = (np.arange(64, dtype=np.uint16).reshape(8, 8) * 4).astype(np.uint8)

PRECISION_LEAD = "the JPEG Lossless stream's precision is"


def _ljpeg(samples, bits):
    pattern = (samples.astype(np.int64) & ((1 << bits) - 1)).astype(np.uint16)
    return imagecodecs.ljpeg_encode(pattern, bitspersample=bits)


def _j2k12(samples):
    return imagecodecs.jpeg2k_encode(samples, level=0, codecformat="J2K",
                                     bitspersample=12)


def _jpeg12(samples):
    """A 12-bit JPEG Extended (SOF1) stream; lossy, so compare with its decode."""
    return imagecodecs.jpeg8_encode(samples, level=95, bitspersample=12)


def _file(ts, stream, *, bits_stored, pr=0, high_bit=None, bits=16):
    return dataset(ts, [stream], rows=8, cols=8, bits_allocated=bits,
                   bits_stored=bits_stored, high_bit=high_bit,
                   pixel_representation=pr)


def _frames(ts, streams, *, bits_stored, pr):
    return dataset(ts, streams, rows=8, cols=8, bits_allocated=16,
                   bits_stored=bits_stored, pixel_representation=pr,
                   frames=len(streams))


def _run(tmp_path, ds, name="s"):
    path = write(tmp_path, ds, name)
    db = str(tmp_path / f"{name}.db")
    out = tmp_path / f"{name}-out"
    report = tmp_path / f"{name}.md"
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        assert not summary.failures, summary.failures
        (inst,) = [i for p in session.store.patients for st in p.studies
                   for se in st.series for i in se.instances]
        assert inst.unload_pixel_data() is True
        stored = inst.get_pixel_data()
        uid = inst.sop_instance_uid
        session.export(str(out), format="dicom")
        session.generate_report(str(report))
    (written,) = [os.path.join(r, f) for r, _d, fs in os.walk(str(out))
                  for f in fs if f.endswith(".dcm")]
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type = 'WARNING'").fetchall()
    (grade,) = [line for line in report.read_text(encoding="utf-8")
                .splitlines() if "Validation Status" in line]
    return {"path": path, "stored": stored, "uid": uid, "rows": rows,
            "grade": grade, "exported": pydicom.dcmread(written)}


def _precision_rows(rows):
    return [r for r in rows if "precision is" in r[1]]


# ---------------------------------------------------------------------------
# 1-2: read by the stream, and said
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts, stream, bits_stored, precision, samples, name", [
    (LJPEG_SV1, _ljpeg(IMG12, 12), 8, 12, IMG12, "JPEG Lossless stream"),
    (LJPEG, _ljpeg(IMG12, 12), 8, 12, IMG12, "JPEG Lossless stream"),
    (J2K_LOSSLESS, _j2k12(IMG12), 8, 12, IMG12, "JPEG 2000 codestream"),
    # A stream at BitsAllocated itself is still wider than BitsStored.
    (LJPEG_SV1, _ljpeg(IMG16, 16), 12, 16, IMG16, "JPEG Lossless stream"),
    # `jpegls_encode` writes precision 16 for any `uint16` input.
    (JPEGLS, imagecodecs.jpegls_encode(IMG16), 12, 16, IMG16,
     "JPEG-LS stream"),
    # Lossy, so `samples` is the stream's own decode (below).
    (JPEG_EXTENDED, _jpeg12(IMG12), 8, 12, None, "JPEG stream"),
], ids=[".70-12-under-8", ".57-12-under-8", ".90-12-under-8",
        ".70-16-under-12", ".80-16-under-12", ".51-12-under-8"])
def test_a_lossless_stream_wider_than_bits_stored_writes_a_warning(
        tmp_path, ts, stream, bits_stored, precision, samples, name):
    if samples is None:
        samples = imagecodecs.jpeg8_decode(stream)
        assert int(samples.max()) > 255, int(samples.max())
    got = _run(tmp_path, _file(ts, stream, bits_stored=bits_stored))

    rows = _precision_rows(got["rows"])
    assert len(rows) == 1, got["rows"]
    entity, details = rows[0]
    assert entity == got["uid"]
    assert details == (
        f"BitsStored {bits_stored} with BitsAllocated 16, and the {name}'s "
        f"precision is {precision}: read as right-aligned {precision}-bit "
        f"samples, as PS3.5 8.2.1 directs for a stream that contradicts the "
        f"Data Elements, and a sample reads {int(samples.max())}, which "
        f"BitsStored {bits_stored} cannot hold; an export writes BitsStored "
        f"from the samples."), details
    assert got["stored"].tolist() == samples.tolist()
    if ts != JPEG_EXTENDED:
        # `.51` also writes #601's lossy row, which grades it on its own.
        assert "REVIEW_REQUIRED" in got["grade"], got["grade"]
    # The graph keeps the declared BitsStored (OQ4 (a)); the export writes
    # the container's width, the narrowest #468 writes that holds them.
    assert got["exported"].BitsStored == 16
    assert got["exported"].pixel_array.tolist() == samples.tolist()


@pytest.mark.parametrize("ts", [LJPEG_SV1, LJPEG], ids=[".70", ".57"])
def test_a_signed_lossless_stream_keeps_its_values(tmp_path, ts):
    """Sign-extended from the stream's 12 bits, not BitsStored's 8."""
    got = _run(tmp_path, _file(ts, _ljpeg(S12, 12), bits_stored=8, pr=1))

    assert got["stored"].dtype == np.int16
    assert int(got["stored"][0, 0]) == -2048
    assert got["stored"].tolist() == S12.tolist()
    assert got["exported"].pixel_array.tolist() == S12.tolist()
    # Asserted after the values: under the old reading the wrong values fit
    # BitsStored 8, so the export kept 8 -- BitsStored alone would pass for
    # a reason unrelated to the samples.
    assert got["exported"].BitsStored == 16
    arr, _label = at_instance(got["path"])
    assert arr.tolist() == S12.tolist()
    (row,) = _precision_rows(got["rows"])
    # -2048 is 1920 below BitsStored 8's -128; 1872 is 1745 above its 127.
    assert "a sample reads -2048, which BitsStored 8 cannot hold" in row[1], \
        row[1]


# ---------------------------------------------------------------------------
# 3: pydicom's own route (Pillow) reads by the stream too
# ---------------------------------------------------------------------------

def test_the_pillow_route_reads_by_precision_too(tmp_path):
    stream = imagecodecs.jpeg8_encode(IMG8, level=100)
    unsigned = _file(JPEG_BASELINE, stream, bits_stored=6, bits=8)
    arr, _label = _decode_pixels(unsigned)
    # pydicom masked to BitsStored 6 before: 252 read 60.
    assert int(arr.max()) == 252

    got = _run(tmp_path, unsigned)
    rows = _precision_rows(got["rows"])
    assert len(rows) == 1, got["rows"]
    assert rows[0][1].startswith(
        "BitsStored 6 with BitsAllocated 8, and the JPEG stream's precision "
        "is 8: "), rows[0][1]
    assert int(got["stored"].max()) == 252

    # Signed: the unmasked decode, reinterpreted at the stream's 8 bits.
    signed = _file(JPEG_BASELINE, stream, bits_stored=6, bits=8, pr=1)
    signed_arr, _label = _decode_pixels(signed)
    assert signed_arr.dtype == np.int8
    assert signed_arr.tolist() == arr.view(np.int8).tolist()
    assert int(signed_arr.min()) == -128


# ---------------------------------------------------------------------------
# 4: an icon
# ---------------------------------------------------------------------------

def test_an_icon_stream_wider_than_its_bits_stored_writes_a_nested_warning(
        tmp_path):
    top = _file(J2K_LOSSLESS, _j2k12(IMG12), bits_stored=12)
    icon = Dataset()
    icon.Rows = icon.Columns = 8
    icon.BitsAllocated, icon.BitsStored, icon.HighBit = 16, 8, 7
    icon.SamplesPerPixel = 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.PixelRepresentation = 0
    icon.add_new(0x7FE00010, "OW", encapsulate([_j2k12(IMG12)]))
    icon["PixelData"].is_undefined_length = True
    top.IconImageSequence = Sequence([icon])
    got = _run(tmp_path, top)

    rows = _precision_rows(got["rows"])
    assert len(rows) == 1, got["rows"]
    assert rows[0][1].startswith(
        "Standard tag 7fe0,0010 (OW) at 0088,0200[0]: BitsStored 8 with "
        "BitsAllocated 16, and the JPEG 2000 codestream's precision is 12: "
        ), rows[0][1]
    exported = got["exported"].IconImageSequence[0]
    exported.file_meta = FileMetaDataset()
    exported.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    assert exported.pixel_array.tolist() == IMG12.tolist()
    # The row's tail is true of an icon: BitsStored from its samples.
    assert exported.BitsStored == 16


# ---------------------------------------------------------------------------
# 5: guards -- a conformant stream reads as it did, with no row
# ---------------------------------------------------------------------------

def test_a_stream_at_bits_stored_writes_nothing(tmp_path):
    got = _run(tmp_path, _file(LJPEG_SV1, _ljpeg(IMG12, 12), bits_stored=12))
    assert not got["rows"], got["rows"]
    assert got["stored"].tolist() == IMG12.tolist()
    assert "PASS" in got["grade"] and "REVIEW" not in got["grade"]


@pytest.mark.parametrize("pr", [0, 1], ids=["unsigned", "signed"])
def test_a_stream_narrower_than_bits_stored_writes_nothing(tmp_path, pr):
    """An 8-bit stream under BitsStored 12: #446's reading, unchanged.

    Signed, it is extended from BitsStored 12 (`max`), so bit 7 set in an
    8-bit sample is a positive 12-bit value: 200 stays 200, where an
    extension from the stream's 8 bits would read -56. Whether that is
    right is its own question (F5), not this issue's.
    """
    samples = (np.arange(64, dtype=np.uint16).reshape(8, 8) * 4)
    stream = _ljpeg(samples, 8)
    got = _run(tmp_path, _file(LJPEG_SV1, stream, bits_stored=12, pr=pr),
               name=f"n{pr}")
    assert not _precision_rows(got["rows"]), got["rows"]
    assert got["stored"].tolist() == samples.tolist()
    assert int(got["stored"].max()) == 252


def test_the_highbit_row_names_the_jpeg_precision(tmp_path):
    """HighBit 11 over BitsStored 8 and a 12-bit stream: read at 12 bits."""
    got = _run(tmp_path, _file(LJPEG_SV1, _ljpeg(IMG12, 12), bits_stored=8,
                               high_bit=11))
    (high_bit,) = [d for _u, d in got["rows"] if "HighBit 11" in d]
    assert ("Read as right-aligned 12-bit samples (the JPEG Lossless "
            "stream's precision 12)") in high_bit, high_bit
    assert got["stored"].tolist() == IMG12.tolist()


# ---------------------------------------------------------------------------
# 5b: a wider stream whose samples all fit writes nothing (owner ruling on
# review F2 of #659: a row only where a value does not fit BitsStored)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts, stream, pr, samples", [
    (LJPEG_SV1, imagecodecs.ljpeg_encode(IMG12, bitspersample=16), 0, IMG12),
    # A signed cell sign-extended above HighBit, the ordinary case: the
    # stream's 16 bits hold -2048 as 0xF800.
    (LJPEG_SV1, imagecodecs.ljpeg_encode(S12.view(np.uint16),
                                         bitspersample=16), 1, S12),
    (JPEGLS, imagecodecs.jpegls_encode(IMG12), 0, IMG12),
    (J2K_LOSSLESS, imagecodecs.jpeg2k_encode(
        IMG12, level=0, codecformat="J2K", bitspersample=16), 0, IMG12),
], ids=[".70-unsigned", ".70-signed-extended", ".80-unsigned",
        ".90-unsigned"])
def test_a_wider_stream_whose_samples_fit_writes_nothing(
        tmp_path, ts, stream, pr, samples):
    """Precision 16 under BitsStored 12, every sample inside 12 bits."""
    got = _run(tmp_path, _file(ts, stream, bits_stored=12, pr=pr))

    assert not got["rows"], got["rows"]
    assert "PASS" in got["grade"] and "REVIEW" not in got["grade"], \
        got["grade"]
    assert got["stored"].tolist() == samples.tolist()
    # Read by the stream and written as declared: nothing to widen.
    assert got["exported"].BitsStored == 12
    assert got["exported"].pixel_array.tolist() == samples.tolist()


def test_a_sample_beyond_a_stream_no_wider_than_bits_stored_writes_no_precision_row(
        tmp_path):
    """Precision 12 under BitsStored 12, and lj92 reads samples above 4095.

    A JPEG Lossless stream carries its differences modulo 2^16, so a
    precision-12 stream can reconstruct samples above 2^12, and the
    fallback returns them (review of #659, round 2, F1). A sample that does
    not fit then comes from a stream no wider than BitsStored, and the
    stream is not why: the #622 row's words would be false. Only the row
    is asserted -- with pylibjpeg installed pydicom masks the same file to
    4095, a split of its own (#671).
    """
    stream = imagecodecs.ljpeg_encode((_XX * 700 + _YY * 10).astype(np.uint16),
                                      bitspersample=12)
    got = _run(tmp_path, _file(LJPEG_SV1, stream, bits_stored=12))

    assert not _precision_rows(got["rows"]), got["rows"]


def test_a_dcmtk_true_lossless_corpus_file_keeps_pass(tmp_path):
    """pydicom's `emri_small_jpeg_ls_lossless.dcm`: precision 16, BitsStored 12."""
    ds = pydicom.dcmread(EMRI_JPEG_LS)
    assert (ds.BitsAllocated, ds.BitsStored) == (16, 12)
    got = _run(tmp_path, ds)

    assert not _precision_rows(got["rows"]), got["rows"]
    assert "PASS" in got["grade"] and "REVIEW" not in got["grade"], \
        got["grade"]
    assert int(got["stored"].max()) < 4096


@pytest.mark.parametrize("values, bits_stored, signed, beyond", [
    ([0, 4095], 12, False, None),
    ([0, 4096], 12, False, 4096),
    ([-2048, 2047], 12, True, None),
    ([-2049, 2047], 12, True, -2049),
    ([-2048, 2048], 12, True, 2048),
    # Both outside: the farther names it.
    ([-2050, 2048], 12, True, -2050),
    ([-2049, 2050], 12, True, 2050),
], ids=["u-fits", "u-2^BS", "s-fits", "s-below", "s-above", "s-both-low",
        "s-both-high"])
def test_the_row_asks_whether_a_sample_fits_bits_stored(
        values, bits_stored, signed, beyond):
    arr = np.array(values, dtype=np.int16 if signed else np.uint16)
    assert _sample_beyond(arr, bits_stored, signed) == beyond


# ---------------------------------------------------------------------------
# 5c: every frame's precision, not frame 0's (review M1 of #659)
# ---------------------------------------------------------------------------

#: A signed 12-bit pattern, unextended, as a 16-bit container holds it.
S12_PATTERN = (S12.astype(np.int64) & 0xFFF).astype(np.int16)

#: Each case: frames as `(stream, what pylibjpeg-libjpeg returns with
#: correct_unused_bits=False, the value read)`, under BitsStored 12,
#: PixelRepresentation 1.
FRAME_CASES = {
    # frame 0 wider; frame 1 conformant, which must still read -2048;
    # frame 2 narrower, extended from BitsStored (F5): 252 stays 252.
    "wider-first": [
        (imagecodecs.ljpeg_encode(S12.view(np.uint16), bitspersample=16),
         S12, S12),
        (_ljpeg(S12, 12), S12_PATTERN, S12),
        (_ljpeg(IMG8.astype(np.uint16), 8), IMG8.astype(np.int16),
         IMG8.astype(np.int16)),
    ],
    # frame 0 conformant; frame 1 a masked pattern at precision 16, read
    # by its 16 bits (S1g's shape), so the mask must be off for the file.
    "wider-later": [
        (_ljpeg(S12, 12), S12_PATTERN, S12),
        (imagecodecs.ljpeg_encode(S12_PATTERN.view(np.uint16),
                                  bitspersample=16),
         S12_PATTERN, S12_PATTERN),
    ],
}


@pytest.mark.parametrize("case", sorted(FRAME_CASES))
def test_pydicoms_route_extends_each_frame_at_its_own_precision(
        tmp_path, monkeypatch, case):
    """pydicom asked unmasked, each frame extended at max(P_i, BitsStored).

    pydicom has no JPEG Lossless plugin here, so `as_array` is patched to
    return what pylibjpeg-libjpeg returns with `correct_unused_bits=False`
    (measured for #622: the unextended pattern). The fallback, which
    reads each frame's own header, is the reference: one answer per frame
    on both routes.
    """
    frames = FRAME_CASES[case]
    ds = _frames(LJPEG_SV1, [stream for stream, _raw, _want in frames],
                 bits_stored=12, pr=1)
    path = write(tmp_path, ds)
    asked = []

    def unmasked(self, src, **kwargs):
        asked.append(kwargs)
        return (np.stack([raw for _stream, raw, _want in frames]),
                {"photometric_interpretation": "MONOCHROME2"})

    with monkeypatch.context() as patch:
        patch.setattr(Decoder, "as_array", unmasked)
        arr, _label = _decode_pixels(pydicom.dcmread(path))
    assert asked and asked[0].get("correct_unused_bits") is False, asked
    want = [w.tolist() for _stream, _raw, w in frames]
    assert arr.tolist() == want

    reference, _label = at_instance(path)
    assert reference.tolist() == want


def test_a_single_colour_frame_is_extended_as_one_frame(monkeypatch):
    """SamplesPerPixel 3, one frame: pydicom returns (rows, columns, 3).

    lj92 cannot encode colour, so the stream is monochrome and stands in
    for its header only; pylibjpeg-libjpeg decodes colour JPEG Lossless.
    Taken as frames, the rows past the first would be extended from
    BitsStored 12 and a 16-bit 2048 would read -2048.
    """
    stream = imagecodecs.ljpeg_encode(S12_PATTERN.view(np.uint16),
                                      bitspersample=16)
    ds = dataset(LJPEG_SV1, [stream], rows=8, cols=8, samples=3,
                 bits_allocated=16, bits_stored=12, pixel_representation=1)
    raw = np.stack([S12_PATTERN] * 3, axis=-1)

    def unmasked(self, src, **kwargs):
        return raw, {"photometric_interpretation": "RGB"}

    monkeypatch.setattr(Decoder, "as_array", unmasked)
    arr, _label = _decode_pixels(ds)
    assert int(raw.max()) >= 2048
    assert arr.tolist() == raw.tolist()


def test_a_wider_frame_behind_a_conformant_frame_0_writes_the_row(tmp_path):
    """The row is decided over every frame: frame 1 is precision 16."""
    frames = FRAME_CASES["wider-later"]
    got = _run(tmp_path, _frames(LJPEG_SV1, [s for s, _r, _w in frames],
                                 bits_stored=12, pr=1))

    (row,) = _precision_rows(got["rows"])
    assert row[1].startswith(
        "BitsStored 12 with BitsAllocated 16, and the JPEG Lossless "
        "stream's precision is 16: "), row[1]
    assert (f"a sample reads {int(S12_PATTERN.max())}, which BitsStored 12 "
            "cannot hold") in row[1], row[1]
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]
    assert got["stored"].tolist() == [w.tolist() for _s, _r, w in frames]


def test_a_wider_frame_0_whose_samples_fit_writes_nothing(tmp_path):
    frames = FRAME_CASES["wider-first"]
    got = _run(tmp_path, _frames(LJPEG_SV1, [s for s, _r, _w in frames],
                                 bits_stored=12, pr=1))

    assert not got["rows"], got["rows"]
    assert "PASS" in got["grade"] and "REVIEW" not in got["grade"]
    assert got["stored"].tolist() == [w.tolist() for _s, _r, w in frames]


# ---------------------------------------------------------------------------
# 6: its own log cap
# ---------------------------------------------------------------------------

def test_one_log_cap_per_rule(tmp_path, monkeypatch, caplog):
    """Seven precision rows print five lines and one suppression line, and
    use none of the HighBit cap: an eighth file's HighBit line still prints.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    src = tmp_path / "src"
    src.mkdir()
    stream = _ljpeg(IMG12, 12)
    for index in range(7):
        ds = _file(LJPEG_SV1, stream, bits_stored=8)
        ds.save_as(str(src / f"a{index}.dcm"), enforce_file_format=False)
    # BitsStored 12 with HighBit 15: the #455 row, and no precision row.
    high = _file(LJPEG_SV1, stream, bits_stored=12, high_bit=15)
    high.save_as(str(src / "z-high-bit.dcm"), enforce_file_format=False)
    db = str(tmp_path / "cap.db")
    with DicomSession(persistence_file=db) as session:
        # After construction: every Session() replaces the log handler
        # (#611).
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            summary = session.ingest(str(src))
    assert summary.ingested == 8, summary
    messages = [record.getMessage() for record in caplog.records]
    assert len([m for m in messages if PRECISION_LEAD in m]) == 5, messages
    assert len([m for m in messages if "suppressing further per-instance "
                "messages for a stream wider than BitsStored" in m]) == 1, \
        messages
    assert len([m for m in messages
                if "PS3.5 8.1.1 requires HighBit" in m]) == 1, messages
    with sqlite3.connect(db) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type = 'WARNING' "
            "AND details LIKE ?", (f"%{PRECISION_LEAD}%",)).fetchone()
    assert count == 7

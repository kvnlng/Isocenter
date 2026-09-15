"""A stream whose own precision exceeds BitsStored is read by it, and said (#622).

JPEG, JPEG-LS and JPEG 2000 streams state their own sample precision.
PS3.5 8.2.1: "should the characteristics explicitly specified in the
compressed data stream ... be inconsistent with those specified in the
DICOM Data Elements, those explicitly specified in the compressed data
stream should be used to control the decompression". The v0.9.8 ruling
(Q2, and OQ1 option A) is to read by the stream's precision at every door
and write one `WARNING` row at ingest.

Measured on abcb3aa, a 12-bit stream under BitsAllocated 16 / BitsStored 8:

* every family read the 12-bit samples with no row, and graded PASS;
* a **signed** JPEG Lossless stream was sign-extended from BitsStored 8:
  `[-2048, -1548, -1048, ...]` was stored and exported as
  `[0, -12, -24, ...]`, under a BitsStored 8 those wrong values fit;
* an 8-bit JPEG Baseline stream under BitsStored 6 was masked to 6 bits by
  pydicom (252 read 60), while the same samples as JPEG 2000 were not.

For a conformant stream (precision at most BitsStored) nothing here
changes: the guards at the bottom pin that.

**The pylibjpeg half is argued here and measured elsewhere.** Only
Pillow's route (`.50`, 8-bit) is available to this suite on pydicom's
side; JPEG Lossless has no pydicom plugin here and decodes through the
imagecodecs fallback. pydicom with pylibjpeg-libjpeg was measured in a
throwaway environment for #622 (`.57`/`.70` 12-bit under BitsStored 8):
masked by default, unmasked with `correct_unused_bits=False`, and equal to
the source once sign-extended at the stream's precision, which is what
`_decode_pixels` now does.
"""
import logging
import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import _decode_pixels
from isocenter.session import DicomSession
from support.decode_doors import (J2K_LOSSLESS, LJPEG, LJPEG_SV1, at_instance,
                                  dataset, write)

JPEG_BASELINE = "1.2.840.10008.1.2.4.50"

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


def _file(ts, stream, *, bits_stored, pr=0, high_bit=None, bits=16):
    return dataset(ts, [stream], rows=8, cols=8, bits_allocated=bits,
                   bits_stored=bits_stored, high_bit=high_bit,
                   pixel_representation=pr)


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
], ids=[".70-12-under-8", ".57-12-under-8", ".90-12-under-8",
        ".70-16-under-12"])
def test_a_lossless_stream_wider_than_bits_stored_writes_a_warning(
        tmp_path, ts, stream, bits_stored, precision, samples, name):
    got = _run(tmp_path, _file(ts, stream, bits_stored=bits_stored))

    rows = _precision_rows(got["rows"])
    assert len(rows) == 1, got["rows"]
    entity, details = rows[0]
    assert entity == got["uid"]
    assert details == (
        f"BitsStored {bits_stored} with BitsAllocated 16, and the {name}'s "
        f"precision is {precision}: read as right-aligned {precision}-bit "
        f"samples, as PS3.5 8.2.1 directs for a stream that contradicts the "
        f"Data Elements; an export writes BitsStored from the samples."), \
        details
    assert got["stored"].tolist() == samples.tolist()
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
    assert len(_precision_rows(got["rows"])) == 1


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

"""A stream whose decoded samples exceed its own declared precision is said (#671).

A lossless JPEG's frame header states the sample precision every decoder
reconstructs against. A stream whose samples exceed it is one the decoders
disagree about, and they disagree *silently*. Measured on a8b6d3f, a `.70`
frame written by `imagecodecs.ljpeg_encode(..., bitspersample=12)` holding
samples up to 4970 (16 of 64 cells above 4095):

* the imagecodecs fallback -- CI's route, and a default `pip install
  isocenter`'s -- stores `0..4970`, what the stream encodes, and exports
  BitsStored 16;
* pydicom with pylibjpeg-libjpeg **saturates** them to `0..4095` and
  exports BitsStored 12.

Neither wrote a row and both graded `PASS`: one file, two sets of pixel
values, and nothing said so. Saturation, not masking -- a mask to 12 bits
would read 104 for 4200, and pylibjpeg reads 4095 -- which is what removes
the easy fix: making the fallback agree would mean writing 4095 over the
4200 the stream encodes, altering pixels to match another library's silent
alteration.

**This is a different question from #622's row and both can fire.** #622
asks whether a decoded sample fits *BitsStored*; this asks whether it fits
*the stream's own precision*. The file under BitsStored 8 fails both and
writes both rows (`test_both_rows_are_written_when_a_sample_fits_neither`).
The owner's 2026-09-15 ruling on #622 is untouched, and this row
deliberately contains no `precision is`, the substring
`tests/test_a_stream_wider_than_bits_stored_is_read_and_said.py` filters
#622's rows on.

**Two limits ship with it, and both are pinned here.**

*Signed streams are not reported* (`test_a_signed_stream_beyond_its_
precision_writes_no_row`). A signed decode is masked back inside its
precision when it is sign-extended, on both routes, so no check after the
decode can see the divergence -- and it is there: the same file under
PixelRepresentation 1 reads `104` through the fallback and `-1` with
pylibjpeg, differing in 16 of 64 cells, with no row and `PASS` on both
(`.agent/scratch-098/probes-J9/p671b-ljp312.log`, `p671b-ljp314t.log`).
That is #682.

*It is not visible on pydicom's plugin route*, which has already clamped
the samples, so a clamped array always fits. **That half needs pylibjpeg
and is not tested here**; `p671-ljp312.jsonl` and `psess-ljp312.txt` are
the evidence, and `docs/installation.md` records it.

Which is why every ingest below runs under **`pydicom_cannot`**: the row
is a statement about what the imagecodecs fallback read, so the route has
to be pinned rather than left to whatever plugins happen to be installed.
CI has none and would take the fallback anyway; a developer with
pylibjpeg-libjpeg installed would otherwise see six of these go red for
the very reason the row exists. Each test asserts the fallback actually
answered (`got["pydicom_calls"]`), so a patch that failed to reach the
ingest cannot pass for a measurement.

**Only T.81 lossless can be built with this shape** -- see
`test_the_shape_is_only_constructible_for_t81`.
"""
import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.encaps import encapsulate
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import (_beyond_precision_words,
                                   _decode_pixels,
                                   _samples_beyond_stream_precision)
from isocenter.session import DicomSession
from support.decode_doors import (LJPEG, LJPEG_SV1, dataset,  # noqa: F401
                                  pydicom_cannot, write)

_YY, _XX = np.mgrid[0:8, 0:8]

#: The filed file's samples: 0..4970, with 16 of 64 cells above 4095.
FILED = (_XX * 700 + _YY * 10).astype(np.uint16)
#: Every sample inside precision 12 -- the control.
FITS = (FILED % 3571).astype(np.uint16)
#: A maximum of **exactly 2^12**, and its neighbour at 2^12 - 1. A bound
#: written `1 << precision` instead of `(1 << precision) - 1` reads these
#: two the same way, which is how J3's round 1 shipped a live mutant.
AT_LIMIT = np.where(FILED > 4095, 0, FILED).astype(np.uint16)
AT_LIMIT[7, 7] = 4096
UNDER_LIMIT = AT_LIMIT.copy()
UNDER_LIMIT[7, 7] = 4095
#: Narrow enough for a precision-8 frame, for the two-frame case.
NARROW = (FILED % 200).astype(np.uint16)

FILED_ROW = (
    "The JPEG Lossless stream declares precision 12 in its frame header, "
    "and a decoded sample reads 4970, which 12 bits cannot hold. Read as "
    "decoded, and exported as read; a decoder that clamps a sample to the "
    "declared precision would read at most 4095 here, so another reader may "
    "see different values.")


def _ljpeg(samples, bits):
    """`samples` as a lossless JPEG **declaring** `bits` of precision.

    Deliberately unmasked: `ljpeg_encode` writes its differences modulo
    2^16, so a sample above 2^bits survives the round trip and the stream
    declares a precision its own samples exceed. That is the shape.
    """
    return imagecodecs.ljpeg_encode(samples.astype(np.uint16),
                                    bitspersample=bits)


def _file(ts, streams, *, bits_stored, pr=0, frames=None):
    return dataset(ts, list(streams), rows=8, cols=8, bits_allocated=16,
                   bits_stored=bits_stored, pixel_representation=pr,
                   frames=frames)


def _run(tmp_path, ds, cannot, name="s"):
    """Ingest, read back from the sidecar, export and grade, on the fallback.

    `cannot` is `pydicom_cannot`'s counter; the ingest is asserted to have
    run in this process and to have reached the fallback, so what the
    assertions below describe is the route the row is about.
    """
    path = write(tmp_path, ds, name)
    db = str(tmp_path / f"{name}.db")
    out = tmp_path / f"{name}-out"
    report = tmp_path / f"{name}.md"
    before = dict(cannot)
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        assert cannot["ingests"] > before["ingests"], \
            "the ingest ran on the session's process pool, where the patch " \
            "does not reach"
        assert cannot["n"] > before["n"], "the fallback was never asked"
        assert not summary.failures, summary.failures
        (inst,) = [i for p in session.store.patients for st in p.studies
                   for se in st.series for i in se.instances]
        assert inst.unload_pixel_data() is True
        stored = inst.get_pixel_data()
        session.export(str(out), format="dicom")
        session.generate_report(str(report))
    (written,) = [os.path.join(r, f) for r, _d, fs in os.walk(str(out))
                  for f in fs if f.endswith(".dcm")]
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT action_type, details FROM audit_log "
            "WHERE action_type IN ('WARNING', 'DATA_LOSS', 'ERROR')"
        ).fetchall()
    grade = "\n".join(line for line in report.read_text(encoding="utf-8")
                      .splitlines() if "Validation Status" in line)
    return {"stored": stored, "rows": rows, "grade": grade,
            "exported": pydicom.dcmread(written)}


def _beyond_rows(rows):
    return [r for r in rows if "declares precision" in r[1]]


def _bits_stored_rows(rows):
    """#622's rows, filtered exactly as its own test file filters them."""
    return [r for r in rows if "precision is" in r[1]]


# ---------------------------------------------------------------------------
# T14 / T19: the row, under both T.81 lossless syntaxes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1], ids=[".57", ".70"])
def test_a_sample_above_the_streams_own_precision_writes_a_warning(
        tmp_path, pydicom_cannot, ts):
    """One `WARNING`, `REVIEW_REQUIRED`, and no value altered."""
    got = _run(tmp_path, _file(ts, [_ljpeg(FILED, 12)], bits_stored=12),
               pydicom_cannot)

    rows = _beyond_rows(got["rows"])
    assert len(rows) == 1, got["rows"]
    assert rows[0][0] == "WARNING"
    assert rows[0][1] == FILED_ROW
    # Not #622's question, and not its words.
    assert not _bits_stored_rows(got["rows"]), got["rows"]
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]
    # Read as decoded, and exported as read.
    assert got["stored"].tolist() == FILED.tolist()
    assert int(got["stored"].max()) == 4970
    assert got["exported"].BitsStored == 16
    # Read back through `_decode_pixels`, not `pixel_array`: the export
    # keeps the source's transfer syntax, and `pydicom_cannot` is still in
    # effect -- so this is the same fallback route the row describes.
    decoded, _label = _decode_pixels(got["exported"])
    assert decoded.tolist() == FILED.tolist()


def test_a_stream_whose_samples_all_fit_its_precision_writes_nothing(
        tmp_path, pydicom_cannot):
    got = _run(tmp_path, _file(LJPEG_SV1, [_ljpeg(FITS, 12)], bits_stored=12),
               pydicom_cannot)

    assert int(got["stored"].max()) <= 4095
    assert not got["rows"], got["rows"]
    assert "PASS" in got["grade"] and "REVIEW" not in got["grade"], \
        got["grade"]


# ---------------------------------------------------------------------------
# The boundary: 2^P is beyond, 2^P - 1 is not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("samples, sample, rows_wanted", [
    (AT_LIMIT, 4096, 1),
    (UNDER_LIMIT, None, 0),
], ids=["exactly-2^12", "exactly-2^12-minus-1"])
def test_the_bound_is_two_to_the_precision_minus_one(
        tmp_path, pydicom_cannot, samples, sample, rows_wanted):
    """Precision 12 holds 0..4095, so 4096 is beyond it and 4095 is not."""
    got = _run(tmp_path, _file(LJPEG_SV1, [_ljpeg(samples, 12)],
                               bits_stored=12), pydicom_cannot)

    rows = _beyond_rows(got["rows"])
    assert len(rows) == rows_wanted, got["rows"]
    if rows_wanted:
        assert f"a decoded sample reads {sample}," in rows[0][1], rows[0][1]
        assert "would read at most 4095 here" in rows[0][1], rows[0][1]


# ---------------------------------------------------------------------------
# The widest declared frame decides, not frame 0
# ---------------------------------------------------------------------------

def test_the_widest_declared_frames_precision_is_the_one_reported(
        tmp_path, pydicom_cannot):
    """Frame 0 declares precision 8 and frame 1 declares 12.

    Read from frame 0 the row would say "precision 8 ... at most 255",
    which is false of the frame the sample came from.
    """
    ds = _file(LJPEG_SV1, [_ljpeg(NARROW, 8), _ljpeg(FILED, 12)],
               bits_stored=12, frames=2)
    got = _run(tmp_path, ds, pydicom_cannot)

    (row,) = _beyond_rows(got["rows"])
    assert row[1] == FILED_ROW
    assert got["stored"].shape == (2, 8, 8)
    assert got["stored"][1].tolist() == FILED.tolist()


# ---------------------------------------------------------------------------
# T16 / T17: both rows, and #622's filter still separates them
# ---------------------------------------------------------------------------

def test_both_rows_are_written_when_a_sample_fits_neither(
        tmp_path, pydicom_cannot):
    """The BitsStored 8 neighbour fails both tests, and says both."""
    got = _run(tmp_path, _file(LJPEG_SV1, [_ljpeg(FILED, 12)], bits_stored=8),
               pydicom_cannot)

    (beyond,) = _beyond_rows(got["rows"])
    (wider,) = _bits_stored_rows(got["rows"])
    assert beyond[1] == FILED_ROW
    assert wider[1].startswith("BitsStored 8 with BitsAllocated 16, and the ")
    # Distinguishable at a glance, and by a substring filter.
    assert "precision is" not in beyond[1]
    assert "declares precision" not in wider[1]
    assert len(got["rows"]) == 2, got["rows"]


def test_the_622_filter_does_not_pick_up_the_new_row(
        tmp_path, pydicom_cannot):
    """`_precision_rows`' substring, applied to the file #622's test pins.

    `test_a_sample_beyond_a_stream_no_wider_than_bits_stored_writes_no_
    precision_row` builds exactly this file and asserts no `precision is`
    row. It must stay green.
    """
    got = _run(tmp_path, _file(LJPEG_SV1, [_ljpeg(FILED, 12)],
                               bits_stored=12), pydicom_cannot)

    assert not _bits_stored_rows(got["rows"]), got["rows"]
    assert len(_beyond_rows(got["rows"])) == 1, got["rows"]


# ---------------------------------------------------------------------------
# T18: the signed limit, pinned
# ---------------------------------------------------------------------------

def test_a_signed_stream_beyond_its_precision_writes_no_row(
        tmp_path, pydicom_cannot):
    """The stated limit (#682), so an "improvement" that fires is red.

    `_sign_extend` keeps the low `precision` bits and extends the sign, so
    after extension every signed sample is inside
    `[-2^(P-1), 2^(P-1) - 1]` **by construction** and no post-decode check
    can see the divergence -- while the two routes differ in 16 of 64
    cells (`-1` against `104`). Firing here would mean reporting every
    signed file, or reporting nothing that is true.
    """
    got = _run(tmp_path, _file(LJPEG_SV1, [_ljpeg(FILED, 12)],
                               bits_stored=12, pr=1), pydicom_cannot)

    assert got["stored"].dtype == np.dtype("int16")
    assert int(got["stored"].min()) < 0
    assert -2048 <= int(got["stored"].min())
    assert int(got["stored"].max()) <= 2047
    assert not _beyond_rows(got["rows"]), got["rows"]


# ---------------------------------------------------------------------------
# The icon depth
# ---------------------------------------------------------------------------

def test_an_icon_beyond_its_streams_precision_writes_the_row_too(
        tmp_path, pydicom_cannot):
    """The same rule at the #433 depth, as #622's row has at that depth."""
    ds = _file(LJPEG_SV1, [_ljpeg(FITS, 12)], bits_stored=12)
    item = Dataset()
    item.Rows, item.Columns = 8, 8
    item.BitsAllocated, item.BitsStored, item.HighBit = 16, 12, 11
    item.SamplesPerPixel = 1
    item.PhotometricInterpretation = "MONOCHROME2"
    item.PixelRepresentation = 0
    item.add_new(0x7FE00010, "OB", encapsulate([_ljpeg(FILED, 12)]))
    item["PixelData"].is_undefined_length = True
    ds.IconImageSequence = Sequence([item])
    got = _run(tmp_path, ds, pydicom_cannot)

    (row,) = _beyond_rows(got["rows"])
    assert row[0] == "WARNING"
    assert row[1] == (f"Standard tag 7fe0,0010 (OB) at 0088,0200[0]: "
                      f"{FILED_ROW}")


# ---------------------------------------------------------------------------
# The facts, and what they are made of
# ---------------------------------------------------------------------------

def test_the_facts_are_plain_types_that_ride_out_of_a_worker():
    """They cross a process boundary on `meta`, so no numpy scalar."""
    ds = _file(LJPEG_SV1, [_ljpeg(FILED, 12)], bits_stored=12)
    facts = _samples_beyond_stream_precision(ds, FILED)

    assert facts == {"precision": 12, "stream": "JPEG Lossless stream",
                     "sample": 4970, "limit": 4095}
    for value in facts.values():
        assert type(value) in (int, str), (value, type(value))
    assert _beyond_precision_words(facts) == FILED_ROW


def test_a_native_file_has_no_stream_precision_to_exceed():
    ds = dataset(ExplicitVRLittleEndian, rows=8, cols=8, bits_allocated=16,
                 bits_stored=12, native=FILED.tobytes())
    assert _samples_beyond_stream_precision(ds, FILED) is None


def test_the_shape_is_only_constructible_for_t81():
    """Why `.80` and `.90` are gated but untested (#684).

    `imagecodecs.jpegls_encode` takes no `bitspersample` at all, so a
    JPEG-LS stream's precision is always derived from the array and can
    never be under-declared; and `jpeg2k_encode(..., bitspersample=12)`
    **clamps at encode time**, so the codestream never contains an
    over-precision sample. The check is written codec-agnostically because
    the precision it compares against is read the same way for all of
    them; only the evidence is T.81.
    """
    with pytest.raises(TypeError):
        imagecodecs.jpegls_encode(FILED, bitspersample=12)
    codestream = imagecodecs.jpeg2k_encode(FILED, level=0, codecformat="J2K",
                                           bitspersample=12)
    assert int(imagecodecs.jpeg2k_decode(codestream).max()) == 4095

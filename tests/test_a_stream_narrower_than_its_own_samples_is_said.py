"""A stream whose decoded samples exceed its own declared precision is said (#671).

A lossless JPEG, and a JPEG 2000 codestream, state the sample precision
every decoder reconstructs against. A stream whose samples exceed it is one the decoders
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

*Signed streams are reported for T.81 above the precision, and for
nothing else.* rev-098j9 took three rounds to get this right, and the
reason is that one gate covers three families whose decoders treat
signedness three different ways -- the answers come from
`_sign_extend`'s three call sites, not from the check:

* **T.81 (`.57`/`.70`)** -- the precision is passed only when *wider*
  than BitsStored (#622), so the width is `max(precision, BitsStored)`
  and masking into the precision happens only where BitsStored <=
  precision. One precision-12 stream reads `[-1996, 1470]` at BitsStored
  12 (masked, no row), and `[-3992, 3570]` at 13 and `[0, 4970]` at 16
  against `[0, 4095]` on the plugin route, 16 of 64 cells apart -- the
  reported half (`test_a_signed_t81_stream_above_its_precision_writes_
  the_row`, per-arm, since 13 exercises the low half alone and 16 the
  high).
* **JPEG-LS (`.80`/`.81`)** -- the frame's precision is passed
  *unconditionally* (#478), so a signed sample is always masked inside
  it and "by construction" is true here without qualification
  (`test_a_signed_jpegls_stream_is_masked_at_its_own_precision`).
* **JPEG 2000** -- the SIZ segment carries the signedness, so negatives
  are ordinary data and say nothing about precision
  (`test_the_unscoped_signed_bound_would_fire_on_a_signed_codestream`).

Two candidate bounds were refused on measurement and both are pinned by
a test rather than by prose: `[-2^(P-1), 2^(P-1) - 1]` fires on a
conformant signed T.81 stream both routes read identically, and the
unsigned bound applied across the whole gate fires on `693_J2KR.dcm`
from pydicom's own test data. #682 keeps the masked T.81 half, which
needs a hook inside the decoder rather than a bound outside it.

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
import logging
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
from support.decode_doors import (J2K_LOSSLESS, JPEGLS, LJPEG,  # noqa: F401
                                  LJPEG_SV1, dataset, pydicom_cannot, write)

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
    "The JPEG Lossless stream declares a sample precision of 12, "
    "and a decoded sample reads 4970, which 12 bits cannot hold. Read as "
    "decoded, and exported as read; a decoder that clamps a sample to the "
    "declared precision reads a value inside the range 12 bits can hold, "
    "so another reader may see different values.")


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
    return [r for r in rows if "declares a sample precision of" in r[1]]


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
        assert "the range 12 bits can hold" in rows[0][1], rows[0][1]


# ---------------------------------------------------------------------------
# The widest declared frame decides, not frame 0
# ---------------------------------------------------------------------------

def test_the_widest_declared_frames_precision_is_the_one_reported(
        tmp_path, pydicom_cannot):
    """Frame 0 declares precision 8 and frame 1 declares 12.

    Read from frame 0 the row would say "precision 8 ... sample 300",
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
    assert "declares a sample precision of" not in wider[1]
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
# Its own log cap (rev-098j9 P3)
# ---------------------------------------------------------------------------

def test_one_log_cap_per_rule(tmp_path, pydicom_cannot, caplog):
    """Seven over-precision rows print five lines and one suppression line.

    The cap shipped untested, so both of its numbers were live mutants:
    `<= 5` could become `<= 0` and print nothing, and `== 6` could become
    any other number and never print the suppression line. Seven
    instances in one ingest is the shape that reads both.

    It also asserts the cap is this rule's own, `_record_lossy`'s reason:
    an eighth file tripping #622's BitsStored rule still prints its line,
    so a cohort that trips this rule cannot suppress another rule's
    output. And the audit rows are **not** capped -- the cap is on the
    log, not the record -- so all seven are in `audit_log`.
    """
    src = tmp_path / "src"
    src.mkdir()
    stream = _ljpeg(FILED, 12)
    for index in range(7):
        ds = _file(LJPEG_SV1, [stream], bits_stored=12)
        ds.save_as(str(src / f"a{index}.dcm"), enforce_file_format=False)
    # BitsStored 8: #622's row as well, and the only file here with one.
    ds = _file(LJPEG_SV1, [stream], bits_stored=8)
    ds.save_as(str(src / "z-wider.dcm"), enforce_file_format=False)
    db = str(tmp_path / "cap.db")
    before = dict(pydicom_cannot)
    with DicomSession(persistence_file=db) as session:
        # After construction: every Session() replaces the log handler.
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            summary = session.ingest(str(src))
    assert summary.ingested == 8, summary
    assert pydicom_cannot["n"] > before["n"], "the fallback was never asked"

    messages = [record.getMessage() for record in caplog.records]
    assert len([m for m in messages
                if "declares a sample precision of" in m]) == 5, messages
    assert len([m for m in messages if "suppressing further per-instance "
                "messages for a sample beyond the stream's own precision"
                in m]) == 1, messages
    # #622's cap is untouched by this rule's eight files.
    assert len([m for m in messages
                if "stream's precision is" in m]) == 1, messages

    with sqlite3.connect(db) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type = 'WARNING' "
            "AND details LIKE ?", ("%declares a sample precision of%",)
        ).fetchone()
    assert count == 8, "the cap is on the log, not the audit row"


# ---------------------------------------------------------------------------
# T18: the signed arm -- T.81 above its precision, and nothing else
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bits_stored, bounds, sample", [
    (13, (-3992, 3570), -3992),
    (16, (0, 4970), 4970),
], ids=["bs13-low-half", "bs16-high-half"])
def test_a_signed_t81_stream_above_its_precision_writes_the_row(
        tmp_path, pydicom_cannot, bits_stored, bounds, sample):
    """The reported signed half: T.81, BitsStored above the precision.

    `_decode_frame` passes a T.81 stream's precision to `_sign_extend`
    only when it is *wider* than BitsStored (#622), so the extension
    width is `max(precision, BitsStored)` and above the precision nothing
    masks the samples back inside it. A negative there can only have come
    from a pattern above `2^P - 1`, which is why the unsigned bound's low
    half is a true proxy for over-precision on this family.

    Measured, one precision-12 stream, identical on 3.12 and 3.14t
    (`.agent/scratch-098/dev-J9/p671signed-*.json`):

    * **BitsStored 13** -- `[-3992, 3570]`. The high side *fits* 4095;
      only the negative is outside, so this arm exercises the low half
      alone and the row names `-3992`.
    * **BitsStored 16** -- `[0, 4970]`. Nothing is negative, so this arm
      exercises the high half and the row names `4970`.

    Both read `[0, 4095]` on pydicom's plugin route, 16 of 64 cells
    apart, which is the divergence the row is about.

    Per-arm expectations, deliberately: one shared assertion set
    (`min >= -2048 and max <= 2047`) is what let the earlier false "by
    construction" claim stand, because it passes for the masked arm and
    was only ever run there.
    """
    got = _run(tmp_path, _file(LJPEG_SV1, [_ljpeg(FILED, 12)],
                               bits_stored=bits_stored, pr=1), pydicom_cannot)
    low, high = int(got["stored"].min()), int(got["stored"].max())

    assert got["stored"].dtype == np.dtype("int16")
    assert (low, high) == bounds
    # Not masked into the precision -- the premise of reporting it.
    assert not (-2048 <= low and high <= 2047), (low, high)
    (row,) = _beyond_rows(got["rows"])
    assert row[0] == "WARNING"
    assert f"reads {sample}," in row[1], row[1]
    assert "the range 12 bits can hold" in row[1], row[1]
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]
    # Nothing rewritten. Read back through `_decode_pixels`, not
    # `pixel_array`: the export keeps the source's transfer syntax and
    # `pydicom_cannot` is still in effect, so this is the fallback route
    # the row is about.
    decoded, _label = _decode_pixels(got["exported"])
    assert (int(decoded.min()), int(decoded.max())) == bounds


def test_a_signed_t81_stream_at_its_precision_writes_no_row(tmp_path,
                                                            pydicom_cannot):
    """The masked half, which stays #682's and stays silent.

    At BitsStored 12 the extension width is 12, so the samples really are
    pushed back inside `[-2048, 2047]` and read `[-1996, 1470]` on both
    routes. Nothing after the decode can see the divergence, so there is
    nothing honest to say about it -- this is the half of #682 that needs
    a hook inside the decoder rather than a bound outside it.
    """
    got = _run(tmp_path, _file(LJPEG_SV1, [_ljpeg(FILED, 12)],
                               bits_stored=12, pr=1), pydicom_cannot)
    low, high = int(got["stored"].min()), int(got["stored"].max())

    assert got["stored"].dtype == np.dtype("int16")
    assert (low, high) == (-1996, 1470)
    assert -2048 <= low and high <= 2047, "masked into the precision"
    assert not _beyond_rows(got["rows"]), got["rows"]
    assert "PASS" in got["grade"], got["grade"]


def test_the_signed_bound_would_fire_on_a_conformant_stream(tmp_path,
                                                            pydicom_cannot):
    """The first refused bound, pinned (rev-098j9 F1).

    The bound first ruled for the signed arm was
    `[-2^(P-1), 2^(P-1) - 1]`, on the premise that it "fires exactly
    where the extension does not mask". It does fire there -- and also
    here, on a stream every one of whose samples is a legal 12-bit
    pattern, which both routes read identically.

    `_sign_extend`'s width is `max(precision, BitsStored)` = 16, so the
    12-bit patterns are not sign-extended at all and come back as the
    unsigned values `[0, 3570]`. Those exceed `2^11 - 1`, so that bound
    fires; and the plugin route reads the same `[0, 3570]` in all 64
    cells, so the row's "another reader may see different values" would
    be false of the file.

    **The bound that shipped does not fire here**, and that is what the
    no-row assertion below now measures rather than merely recording:
    `3570` fits `2^12 - 1`, and nothing is negative. The premise cannot
    be re-adopted without this going red.
    """
    conformant = (FILED & 0xFFF).astype(np.uint16)
    got = _run(tmp_path, _file(LJPEG_SV1, [_ljpeg(conformant, 12)],
                               bits_stored=16, pr=1), pydicom_cannot)
    low, high = int(got["stored"].min()), int(got["stored"].max())

    # Every sample a legal 12-bit pattern, so nothing exceeds precision 12.
    assert int(conformant.max()) <= 4095
    assert (low, high) == (0, 3570)
    # ... yet it is outside the signed range the refused bound would use.
    assert high > (1 << 11) - 1
    # ... and inside the one that shipped, which is why there is no row.
    assert low >= 0 and high <= (1 << 12) - 1
    assert not _beyond_rows(got["rows"]), got["rows"]
    assert "PASS" in got["grade"], got["grade"]


def test_the_unscoped_signed_bound_would_fire_on_a_signed_codestream(
        tmp_path, pydicom_cannot):
    """The second refused bound, pinned (rev-098j9 round 3).

    The bound ruled next was the unsigned one, `[0, 2^P - 1]` with its
    low half, applied to PixelRepresentation 1 wherever BitsStored
    exceeded the precision -- across the whole gate. The gate admits
    JPEG 2000, and **a codestream carries its own signedness** in the SIZ
    segment, so its decoders return negatives with no sign extension
    involved at all and `lowest < 0` says nothing about precision.

    The corpus sweep found it on a real file: `693_J2KR.dcm` from
    pydicom's test data, precision 14, BitsStored 16,
    PixelRepresentation 1, read `int16 [-2000, 2492]` by Pillow *and* by
    the imagecodecs fallback, identical in all 262144 cells, every sample
    legal at 14 bits. The unscoped bound fires on `-2000` and flips the
    grade to `REVIEW_REQUIRED`, claiming 14 bits cannot hold a legal
    14-bit value (`.agent/scratch-098/dev-J9/p693-ci312.json`,
    `p693-ci314t.json`, `sweep3/`).

    This is that shape, built small: a signed codestream under
    PixelRepresentation 1 with BitsStored above its precision, every
    sample inside the precision, negatives present. It must write no row.
    """
    samples = (_XX * 500 + _YY * 10 - 1800).astype(np.int16)
    stream = imagecodecs.jpeg2k_encode(samples, level=0, reversible=True,
                                       bitspersample=12, codecformat="J2K")
    got = _run(tmp_path, _file(J2K_LOSSLESS, [stream], bits_stored=16, pr=1),
               pydicom_cannot)
    low, high = int(got["stored"].min()), int(got["stored"].max())

    # The shape: signed, negatives present, BitsStored above the precision.
    assert got["stored"].dtype == np.dtype("int16")
    assert (low, high) == (-1800, 1770)
    assert low < 0, "the half the unscoped bound fired on"
    # Every sample legal at precision 12, so there is nothing to report.
    assert -(1 << 11) <= low and high <= (1 << 11) - 1
    assert not _beyond_rows(got["rows"]), got["rows"]
    assert "PASS" in got["grade"], got["grade"]


def test_a_signed_jpegls_stream_is_masked_at_its_own_precision(tmp_path,
                                                               pydicom_cannot):
    """The third family, and why its masking claim needs no BitsStored test.

    `_decode_frame` passes a JPEG-LS frame's own precision to
    `_sign_extend` **unconditionally** (#478) -- not "when wider than
    BitsStored", as the T.81 arm does -- so the extension width is the
    precision at every BitsStored and a signed sample always lands inside
    `[-2^(P-1), 2^(P-1) - 1]`. The "masked by construction" claim that
    was false for T.81 is unconditionally true here.

    Measured: an 8-bit sample of 150 under BitsStored 16 reads `-106`.
    That is the masking, not a divergence, which is why the signed arm is
    scoped to T.81 and this family stays silent however far BitsStored
    sits above the precision.
    """
    samples = (_XX * 30 + _YY).astype(np.uint8)
    assert int(samples.max()) > 127, "a sample the masking will make negative"
    got = _run(tmp_path, _file(JPEGLS, [imagecodecs.jpegls_encode(samples)],
                               bits_stored=16, pr=1), pydicom_cannot)
    low, high = int(got["stored"].min()), int(got["stored"].max())

    # Masked into precision 8 despite BitsStored 16.
    assert (low, high) == (-106, 127)
    assert -(1 << 7) <= low and high <= (1 << 7) - 1
    assert not _beyond_rows(got["rows"]), got["rows"]
    assert "PASS" in got["grade"], got["grade"]


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


def test_a_negative_sample_under_pixel_representation_0_is_the_one_reported():
    """The low half of the bound, directly (rev-098j9 R10/R11).

    `lowest < 0` and `max(beyond)[1]` shipped unexercised: every ingest
    fixture is unsigned and non-negative, so only the high half ran, and
    a bound written without the `lowest < 0` clause -- or one that
    reported the *nearest* sample instead of the farthest -- passed.

    An array that is negative under PixelRepresentation 0 is what makes
    the clause reachable at all: it is the shape the guard above lets
    through, and it is why that guard is testable.

    The two distances are measured from different places, which is worth
    stating because it is not obvious and it decides the row: the high
    side is `highest - limit` (how far past the precision), the low side
    is `-lowest` (how far below zero). So both orderings are pinned here.
    """
    ds = _file(LJPEG_SV1, [_ljpeg(FILED, 12)], bits_stored=12)
    facts = _samples_beyond_stream_precision(
        ds, np.array([[-5]], dtype=np.int16))

    assert facts is not None
    assert facts["sample"] == -5
    assert facts["limit"] == 4095
    assert facts["precision"] == 12

    # The farthest wins, not the first found, and each side can win.
    high_wins = _samples_beyond_stream_precision(
        ds, np.array([[-5, 4970]], dtype=np.int16))
    assert high_wins["sample"] == 4970, high_wins
    low_wins = _samples_beyond_stream_precision(
        ds, np.array([[-5000, 4970]], dtype=np.int16))
    assert low_wins["sample"] == -5000, low_wins


def test_a_narrow_frames_excess_is_not_reported_behind_a_wider_one():
    """The one-precision-per-instance limit, pinned (rev-098j9 P4).

    The widest declared frame decides, so a narrow frame's own
    over-precision samples are invisible behind a wider sibling. Frame 0
    at precision 8 holding 300 exceeds its own 8 bits; behind frame 1 at
    precision 12 the comparison is against 12 and 300 fits, so no row.
    Alone, frame 0 reports it.

    Stated as a limit rather than fixed: a per-frame comparison needs the
    frame axis this function is not given, and the row would then have to
    name a frame.
    """
    narrow = np.full((8, 8), 300, dtype=np.uint16)
    wide = np.full((8, 8), 3000, dtype=np.uint16)
    both = _file(LJPEG_SV1, [_ljpeg(narrow, 8), _ljpeg(wide, 12)],
                 bits_stored=12, frames=2)
    assert _samples_beyond_stream_precision(both, np.stack([narrow, wide])) \
        is None

    alone = _file(LJPEG_SV1, [_ljpeg(narrow, 8)], bits_stored=12)
    facts = _samples_beyond_stream_precision(alone, narrow)
    assert facts is not None
    assert (facts["precision"], facts["sample"], facts["limit"]) \
        == (8, 300, 255)


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

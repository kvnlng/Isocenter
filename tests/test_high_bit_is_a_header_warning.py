"""HighBit other than BitsStored - 1 is read, with one WARNING row (#455, #523).

PS3.5 8.1.1 requires HighBit to be BitsStored - 1. A file that says
otherwise was handled three ways depending on its route:

- a **signed** JPEG Lossless or JPEG-LS frame was refused (#446's Q1),
  at ingest and at the read doors, in words claiming a sign extension
  "from BitsStored" that the JPEG-LS route never made (#478);
- an **unsigned JPEG 2000** codestream under PixelRepresentation 1 was
  refused where its decode reached the imagecodecs handler, and read
  where it reached Pillow -- the refusal depended on the codestream's
  sign bit, a property unrelated to the header defect (#523);
- **everything else** was read, at every door, with no row and no log,
  and the export rewrote HighBit to BitsStored - 1 in silence (#455).

No decoder here reads HighBit. Each returns right-aligned samples:
JPEG Lossless by BitsStored, JPEG-LS and JPEG 2000 by the stream's own
precision, and a native frame is masked to its low BitsStored bits by
pydicom. So the owner's ruling (Q2, Q3) is one rule for every route:
read the file as those decoders do, and say so in one `WARNING` row per
instance -- the frozen action type (#411), which bars a `PASS` grade
(#479). The row names BitsAllocated, BitsStored, HighBit, the stream's
precision where there is one, and the width the read used. It names the
SOP Instance UID, never the source file, whose folder may be named for
the patient.

Native data is read as pydicom reads it (Q3): samples genuinely stored in
bits 4..15 under BitsStored 12 / HighBit 15 come back wrapped to their
low 12 bits, and the row is the only signal. That layout cannot be told
apart from a right-aligned frame carrying overlay bits above BitsStored.
"""
import os

import imagecodecs
import numpy as np
import pytest

from support.decode_doors import (EXPLICIT_LE, J2K_LOSSLESS, JPEGLS,
                                  LJPEG_SV1, at_decode_pixels, at_ingest,
                                  at_instance, dataset, same, write)

#: A 12-bit signed frame, and its masked unsigned pattern.
SIGNED12 = np.array([-2048, -800, -1, 0, 2047, 5, -5, 100] * 2,
                    np.int16).reshape(4, 4)
PATTERN12 = (SIGNED12.astype(np.int64) & 0xFFF).astype(np.uint16)
#: An unsigned 12-bit frame.
UNSIGNED12 = (np.arange(16, dtype=np.int64) * 250).astype(np.uint16).reshape(
    4, 4)


def _warning_rows(got):
    return [row for row in got["rows"] if row[0] == "WARNING"
            and "HighBit" in row[2]]


def _one_row(got, high_bit, bits_stored, bits_allocated, read_words):
    rows = _warning_rows(got)
    assert len(rows) == 1, got["rows"]
    _action, entity, details = rows[0]
    assert entity == got["uid"], rows
    assert details.startswith(
        f"HighBit {high_bit} with BitsStored {bits_stored} and "
        f"BitsAllocated {bits_allocated}: PS3.5 8.1.1 requires HighBit to "
        f"be BitsStored - 1. "), details
    assert read_words in details, details
    assert details.endswith(
        "; HighBit is not an input to this decode, and an export writes "
        "HighBit as BitsStored - 1."), details
    return details


def _j2k(arr, **kwargs):
    return imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K", **kwargs)


# ---------------------------------------------------------------------------
# The refusals become reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, ts, codestream, samples, want, read_words", [
    ("ljpeg-signed", LJPEG_SV1,
     imagecodecs.ljpeg_encode(PATTERN12, bitspersample=12), 1, SIGNED12,
     "Read as right-aligned 12-bit samples (BitsStored 12)"),
    ("jpeg-ls-signed", JPEGLS, imagecodecs.jpegls_encode(PATTERN12), 1,
     SIGNED12,
     "Read as right-aligned 16-bit samples (the JPEG-LS stream's "
     "precision 16)"),
    ("j2k-rgb-unsigned-codestream-under-pr1", J2K_LOSSLESS,
     _j2k(np.stack([PATTERN12] * 3, -1), bitspersample=12, mct=False), 3,
     np.stack([SIGNED12] * 3, -1),
     "Read as right-aligned 12-bit samples (the JPEG 2000 codestream's "
     "precision 12)"),
], ids=lambda value: value if isinstance(value, str) and "-" in value
   and " " not in value else "")
def test_a_signed_frame_with_high_bit_15_reads_and_writes_one_warning_row(
        tmp_path, monkeypatch, name, ts, codestream, samples, want,
        read_words):
    """M8: the #446 refusal, and #523's J2K refusal, read now.

    `imagecodecs.jpegls_encode` writes a 12-bit pattern as a precision-16
    stream, and JPEG-LS is read by its precision, so -800's pattern is
    3296 and reads back as 3296 -- the row says 16 bits because that is
    the width the read used. The J2K colour file is refused by Pillow on
    both routes, so it is the imagecodecs route that reads it.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    if ts == JPEGLS:
        want = PATTERN12.astype(np.int16)
    path = write(tmp_path, dataset(
        ts, [codestream], rows=4, cols=4, samples=samples, bits_allocated=16,
        bits_stored=12, high_bit=15, pixel_representation=1))
    got = at_ingest(tmp_path, path)
    assert got["failure"] is None, got
    assert same(got["array"], want), got["array"]
    assert got["attributes"]["0028,0102"] == 15
    details = _one_row(got, 15, 12, 16, read_words)
    assert os.path.dirname(path) not in details
    assert same(at_decode_pixels(path)[0], want)
    assert same(at_instance(path)[0], want)


# ---------------------------------------------------------------------------
# The silent reads get their row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_high_bit_mismatch_writes_one_warning_row_naming_bs_hb_ba_and_the_width_read(
        tmp_path, monkeypatch, mode):
    """M9: unsigned JPEG Lossless BitsStored 12 / HighBit 15 (#455's file).

    Read at every door as the right-aligned samples, with no row, before.
    Under both worker strategies, because the facts cross the process
    boundary on `meta` (attack A9).
    """
    if mode == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    path = write(tmp_path, dataset(
        LJPEG_SV1, [imagecodecs.ljpeg_encode(UNSIGNED12, bitspersample=12)],
        rows=4, cols=4, bits_allocated=16, bits_stored=12, high_bit=15))
    got = at_ingest(tmp_path, path)
    assert same(got["array"], UNSIGNED12), got
    _one_row(got, 15, 12, 16,
             "Read as right-aligned 12-bit samples (BitsStored 12)")


def test_a_high_bit_below_bits_stored_minus_one_writes_a_row(tmp_path,
                                                             monkeypatch):
    """M10: the rule is `!=`, so HighBit 10 under BitsStored 12 counts too."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    path = write(tmp_path, dataset(
        LJPEG_SV1, [imagecodecs.ljpeg_encode(UNSIGNED12, bitspersample=12)],
        rows=4, cols=4, bits_allocated=16, bits_stored=12, high_bit=10))
    got = at_ingest(tmp_path, path)
    assert same(got["array"], UNSIGNED12), got
    _one_row(got, 10, 12, 16,
             "Read as right-aligned 12-bit samples (BitsStored 12)")


@pytest.mark.parametrize("ts", [LJPEG_SV1, EXPLICIT_LE])
def test_an_agreeing_high_bit_writes_no_row(tmp_path, monkeypatch, ts):
    """M11: HighBit 11 under BitsStored 12 is conformant and says nothing."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    kwargs = dict(rows=4, cols=4, bits_allocated=16, bits_stored=12,
                  high_bit=11)
    if ts == EXPLICIT_LE:
        ds = dataset(ts, native=UNSIGNED12.tobytes(), **kwargs)
    else:
        ds = dataset(ts, [imagecodecs.ljpeg_encode(UNSIGNED12,
                                                   bitspersample=12)],
                     **kwargs)
    got = at_ingest(tmp_path, write(tmp_path, ds))
    assert same(got["array"], UNSIGNED12), got
    assert got["rows"] == [], got["rows"]


def test_the_row_names_the_codestream_precision_for_j2k(tmp_path, monkeypatch):
    """M12: a precision-12 codestream under BitsStored 16 is read by its 12 bits.

    The width the read used is the codestream's, not BitsStored, and the
    row must say which: a row reading "16-bit samples (BitsStored 16)"
    would describe a reading nothing made.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    path = write(tmp_path, dataset(
        J2K_LOSSLESS, [_j2k(UNSIGNED12, bitspersample=12)], rows=4, cols=4,
        bits_allocated=16, bits_stored=16, high_bit=11))
    got = at_ingest(tmp_path, path)
    assert same(got["array"], UNSIGNED12), got
    _one_row(got, 11, 16, 16,
             "Read as right-aligned 12-bit samples (the JPEG 2000 "
             "codestream's precision 12)")


def test_a_declined_duplicate_writes_no_high_bit_row(tmp_path, monkeypatch):
    """M13: the row is for an instance the store holds, not a file it declined.

    Two copies of one SOP Instance UID in one folder: the first is linked
    and gets its row, the second is declined with the #431 WARNING and
    gets no HighBit row. Ingesting the folder again links neither -- one
    is skipped as already ingested, one declined (attack A8) -- and adds
    nothing.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    ds = dataset(
        LJPEG_SV1, [imagecodecs.ljpeg_encode(UNSIGNED12, bitspersample=12)],
        rows=4, cols=4, bits_allocated=16, bits_stored=12, high_bit=15)
    path = write(tmp_path, ds)
    ds.save_as(os.path.join(os.path.dirname(path), "two.dcm"),
               enforce_file_format=False)
    from isocenter.session import DicomSession  # pylint: disable=import-outside-toplevel
    import sqlite3  # pylint: disable=import-outside-toplevel
    db = str(tmp_path / "dup.db")
    with DicomSession(persistence_file=db) as session:
        first = session.ingest(os.path.dirname(path))
        again = session.ingest(os.path.dirname(path))
    assert (first.ingested, first.declined) == (1, 1), first
    assert again.ingested == 0 and again.declined + again.skipped == 2, again
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action_type='WARNING' "
            "AND details LIKE 'HighBit %'").fetchall()
    assert len(rows) == 1, rows


# ---------------------------------------------------------------------------
# Native: read as pydicom reads it (Q3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, stored, representation, want, read_words", [
    ("right-aligned", UNSIGNED12, 0, UNSIGNED12,
     "Read as pydicom reads it: the low 12 bits of each sample"),
    # Bits 4..15: pydicom keeps the low 12 bits, so 4000 << 4 reads 4000
    # & 0xFFF -- 4000 -- and 8000 << 4 reads 3904. Wrapped, and the row is
    # the only signal.
    ("left-aligned", (UNSIGNED12 << 4).astype(np.uint16), 0,
     ((UNSIGNED12 << 4).astype(np.int64) & 0xFFF).astype(np.uint16),
     "Read as pydicom reads it: the low 12 bits of each sample"),
    ("signed", PATTERN12, 1, SIGNED12,
     "Read as pydicom reads it: the low 12 bits of each sample, "
     "sign-extended"),
], ids=lambda value: value if isinstance(value, str) and " " not in value
   else "")
def test_a_native_frame_is_read_as_pydicom_reads_it_with_one_row(
        tmp_path, monkeypatch, name, stored, representation, want,
        read_words):
    """Q3: pydicom's mask stays; the row says what it did."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    path = write(tmp_path, dataset(
        EXPLICIT_LE, native=stored.tobytes(), rows=4, cols=4,
        bits_allocated=16, bits_stored=12, high_bit=15,
        pixel_representation=representation))
    got = at_ingest(tmp_path, path)
    assert same(got["array"], want), got["array"]
    _one_row(got, 15, 12, 16, read_words)


def test_the_row_grades_the_run_review_required_and_the_export_writes_high_bit_11(
        tmp_path, monkeypatch):
    """The row bars PASS (Q2), and the export writes what the row says."""
    import pydicom  # pylint: disable=import-outside-toplevel
    from isocenter.session import DicomSession  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    path = write(tmp_path, dataset(
        LJPEG_SV1, [imagecodecs.ljpeg_encode(UNSIGNED12, bitspersample=12)],
        rows=4, cols=4, bits_allocated=16, bits_stored=12, high_bit=15))
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    with DicomSession(persistence_file=str(tmp_path / "g.db")) as session:
        session.ingest(os.path.dirname(path))
        session.export(str(out), use_compression=False, show_progress=False)
        session.generate_report(str(report))
    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, written
    exported = pydicom.dcmread(written[0])
    assert (exported.BitsStored, exported.HighBit) == (12, 11)
    grade = [line for line in report.read_text(encoding="utf-8").splitlines()
             if "Validation Status" in line]
    assert len(grade) == 1 and "REVIEW_REQUIRED" in grade[0], grade


def test_a_cohort_writes_a_row_per_instance_and_five_log_lines(
        tmp_path, monkeypatch, caplog):
    """Row volume (attack A10): the trail is per instance, the console is not.

    Six instances with HighBit 15: six rows, because the audit log is
    the compliance trail and DATA_LOSS and declined rows are per instance
    too; five log lines and one suppression line, because a 2,000-instance
    legacy cohort would otherwise print 2,000. No line names the source
    folder.
    """
    import logging  # pylint: disable=import-outside-toplevel
    import sqlite3  # pylint: disable=import-outside-toplevel
    from isocenter.session import DicomSession  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    folder = tmp_path / "cohort"
    codestream = imagecodecs.ljpeg_encode(UNSIGNED12, bitspersample=12)
    for index in range(6):
        write(folder, dataset(LJPEG_SV1, [codestream], rows=4, cols=4,
                              bits_allocated=16, bits_stored=12, high_bit=15),
              name=f"i{index}")
    db = str(tmp_path / "cohort.db")
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with DicomSession(persistence_file=db) as session:
            summary = session.ingest(str(folder))
    assert summary.ingested == 6, summary
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type='WARNING' "
            "AND details LIKE 'HighBit 15 %'").fetchone()
    assert rows == (6,)
    messages = [record.getMessage() for record in caplog.records]
    lines = [m for m in messages if "HighBit 15 with BitsStored 12" in m]
    assert len(lines) == 5, messages
    assert len([m for m in messages if "suppressing further per-instance "
                "messages for HighBit" in m]) == 1, messages
    assert not [m for m in lines if str(folder) in m], lines

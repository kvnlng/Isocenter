"""Frames beyond NumberOfFrames that no offset table names are counted (#620).

#418 counted an excess only from a populated Basic or Extended Offset
Table. With an empty table -- or an Extended Offset Table whose Lengths
disagree, which pydicom drops -- pydicom walks the fragments by their end
markers and returns every frame it finds; natively it returns every whole
frame the element's length holds. Measured on abcb3aa, NumberOfFrames 2
over three JPEG 2000 fragments and no table:

* `Instance.get_pixel_data()` returned `(3, 4, 8)`;
* ingest stored 96 samples under geometry (2, 4, 8) with no row, the
  reload raised `Integrity Error`, and the export wrote an `ERROR` row and
  0 of 1;
* the imagecodecs fallback (every JPEG-LS file here, and any JPEG 2000 file
  pydicom's plugins cannot read) silently kept two frames and graded PASS;
* a native element holding three frames of bytes under NumberOfFrames 2,
  and an icon of either kind, behaved like the pydicom route; the icon was
  dropped at export with a row blaming the sidecar.

`offset_table_frame_count` now also walks the fragments with pydicom's own
generator, and reads a native element's whole frames by pydicom's own
rule, so every #418 consumer gets the excess: ingest keeps the declared
frames with the row, and the read door refuses.

The three J2K codestreams are distinct (`U16`, `U16[::-1] // 2`,
`U16 // 3`), so "kept frames 0 and 1" cannot pass for "kept 0 and 2": an
assertion on shape alone would. No fixture here carries a populated Basic
Offset Table -- that is #418's case and already passes.
"""
import os
import sqlite3
import struct
from io import BytesIO

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import (encapsulate, generate_fragmented_frames,
                            parse_basic_offsets, parse_fragments)
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from isocenter.io_handlers import (LOSS_SCOPE_SIGNAL, LOSS_SCOPE_STANDARD,
                                   _decode_pixels, ingest_worker)
from isocenter.session import DicomSession
from support.decode_doors import (EXPLICIT_LE, J2K_LOSSLESS, JPEGLS,
                                  SOP_CLASS, dataset, pydicom_cannot,
                                  through_the_fallback, write)

U16 = (np.arange(32) * 1000).astype(np.uint16).reshape(4, 8)
U16B = (U16[::-1] // 2).astype(np.uint16)
U16C = (U16 // 3).astype(np.uint16)
S16 = np.tile(np.array([-32768, -800, -1, 0, 32767, 5, -5, 100], np.int16),
              (4, 1))


def _j2k(arr):
    return imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K")


U, U2, U3, S = _j2k(U16), _j2k(U16B), _j2k(U16C), _j2k(S16)
G8 = (np.arange(64, dtype=np.uint8).reshape(8, 8) * 3)
ICON_FRAMES = [G8, G8 // 2, G8 // 3]

FRAGMENTS_WORDS = ("Pixel Data's fragments hold 3 frames by their end "
                   "markers; NumberOfFrames declares 2")
KEPT_WORDS = "Kept the first 2 and discarded 1."


def _no_table(ts, fragments, *, frames, rows=4, cols=8, bits=16):
    """Encapsulated `fragments` behind an **empty** Basic Offset Table."""
    ds = dataset(ts, [b"\0\0"], rows=rows, cols=cols, bits_allocated=bits,
                 frames=frames)
    ds.PixelData = encapsulate(list(fragments), has_bot=False)
    return ds


def _dropped_eot(fragments):
    """S10: an Extended Offset Table of 2 offsets and 1 length, NOF 2.

    pydicom drops a table whose two halves differ in length and walks the
    fragments; its offset count equals NumberOfFrames, so #418's table
    count saw nothing to report.
    """
    ds = _no_table(J2K_LOSSLESS, fragments, frames=2)
    first = len(fragments[0]) + len(fragments[0]) % 2 + 8
    ds.ExtendedOffsetTable = struct.pack("<2Q", 0, first)
    ds.ExtendedOffsetTableLengths = struct.pack("<1Q", len(fragments[0]))
    return ds


def _fragments(ds):
    buf = BytesIO(ds.PixelData)
    parse_basic_offsets(buf)
    return parse_fragments(buf)[0]


def _grade(session, tmp_path, name):
    report = tmp_path / f"{name}.md"
    session.generate_report(str(report))
    (line,) = [l for l in report.read_text(encoding="utf-8").splitlines()
               if "Validation Status" in l]
    return line


def _rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT action_type, details, loss_scope FROM audit_log "
            "WHERE action_type IN ('DATA_LOSS', 'ERROR')").fetchall()


def _pipeline(tmp_path, path, name="s", calls=None):
    """Ingest, reload from the sidecar, export, grade, reopen."""
    db = str(tmp_path / f"{name}.db")
    out = tmp_path / f"{name}-out"
    got = {}
    before = None if calls is None else calls["ingests"]
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        if calls is not None:
            assert calls["ingests"] > before, "ingest ran out of process"
        assert not summary.failures, summary.failures
        (inst,) = [i for p in session.store.patients for st in p.studies
                   for se in st.series for i in se.instances]
        assert inst.unload_pixel_data() is True
        got["array"] = inst.get_pixel_data()
        got["instance"] = inst
        session.export(str(out), format="dicom")
        got["written"] = [os.path.join(r, f) for r, _d, fs in os.walk(str(out))
                          for f in fs if f.endswith(".dcm")]
        got["grade"] = _grade(session, tmp_path, name)
    with DicomSession(persistence_file=db) as session:
        (inst,) = [i for p in session.store.patients for st in p.studies
                   for se in st.series for i in se.instances]
        got["reopened"] = inst.get_pixel_data()
    got["rows"] = _rows(db)
    return got


def _assert_signal_row(rows, words):
    losses = [r for r in rows if r[0] == "DATA_LOSS"]
    assert len(losses) == 1, rows
    _action, details, scope = losses[0]
    assert scope == LOSS_SCOPE_SIGNAL, rows
    assert f"{words}. {KEPT_WORDS}" in details, details
    assert not [r for r in rows if r[0] == "ERROR"], rows


# ---------------------------------------------------------------------------
# The fixtures carry the shapes they are named for
# ---------------------------------------------------------------------------

def test_the_fixtures_have_no_table_and_more_fragments_than_frames():
    """A fixture with a populated table would be #418's case, and pass today."""
    s11 = _no_table(J2K_LOSSLESS, [U, U2, U3], frames=2)
    assert parse_basic_offsets(s11.PixelData) == []
    assert "ExtendedOffsetTable" not in s11
    assert _fragments(s11) == 3
    s10 = _dropped_eot([U, U2, S])
    assert imagecodecs_handler.extended_offsets(s10) is None
    assert len(s10.ExtendedOffsetTable) // 8 == 2
    # The three frames the walk finds are three distinct images.
    walked = list(generate_fragmented_frames(s11.PixelData,
                                             number_of_frames=2))
    # (Each item is padded to even length, so compared by prefix.)
    assert [f[0][:len(c)] for f, c in zip(walked, (U, U2, U3))] == [U, U2, U3]
    assert len(walked) == 3
    assert len({U16.tobytes(), U16B.tobytes(), U16C.tobytes()}) == 3


# ---------------------------------------------------------------------------
# 1-2: the pydicom route
# ---------------------------------------------------------------------------

def test_an_empty_table_excess_is_kept_to_the_declared_frames_with_a_signal_row(
        tmp_path):
    path = write(tmp_path, _no_table(J2K_LOSSLESS, [U, U2, U3], frames=2))
    got = _pipeline(tmp_path, path)

    _assert_signal_row(got["rows"], FRAGMENTS_WORDS)
    assert got["array"].shape == (2, 4, 8)
    # Values, not shape: frames 0 and 2 have the right shape too.
    assert got["array"][0].tolist() == U16.tolist()
    assert got["array"][1].tolist() == U16B.tolist()
    assert got["reopened"].tolist() == got["array"].tolist()
    assert len(got["written"]) == 1
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]


def test_a_dropped_extended_table_excess_is_counted_by_the_walk(tmp_path):
    ds = _dropped_eot([U, U2, S])
    assert imagecodecs_handler.offset_table_frame_count(ds) == (
        3, 2, 2, "Pixel Data's fragments")
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    _assert_signal_row(got["rows"], FRAGMENTS_WORDS)
    assert got["array"][0].tolist() == U16.tolist()
    assert got["array"][1].tolist() == U16B.tolist()
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]

    inst = Instance(generate_uid(), SOP_CLASS, 1, file_path=path)
    with pytest.raises(RuntimeError) as exc:
        inst.get_pixel_data()
    assert "hold 3 frames" in str(exc.value), str(exc.value)


# ---------------------------------------------------------------------------
# 3: the imagecodecs fallback no longer truncates in silence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts,fragments", [
    (JPEGLS, [imagecodecs.jpegls_encode(a) for a in (U16, U16B, U16C)]),
    (J2K_LOSSLESS, [U, U2, U3]),
], ids=["jpeg-ls", "jpeg-2000"])
def test_the_fallback_route_no_longer_truncates_in_silence(
        tmp_path, pydicom_cannot, ts, fragments):
    ds = _no_table(ts, fragments, frames=2)
    path = write(tmp_path, ds)

    meta = ingest_worker(path)[0]
    assert meta.get("offset_table_excess") == (
        3, 2, 2, "Pixel Data's fragments")

    got = _pipeline(tmp_path, path, calls=pydicom_cannot)
    assert pydicom_cannot["n"] > 0
    _assert_signal_row(got["rows"], FRAGMENTS_WORDS)
    assert got["array"][1].tolist() == U16B.tolist()
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]

    # A fallback decode that was not asked to drop the excess refuses it,
    # rather than truncate on the caller's behalf.
    with pytest.raises(RuntimeError) as exc:
        through_the_fallback(pydicom.dcmread(path))
    assert FRAGMENTS_WORDS in str(exc.value), str(exc.value)
    assert "not asked to drop the excess" in str(exc.value)


# ---------------------------------------------------------------------------
# 4: the read door refuses (#418's ruling: it has no row to write)
# ---------------------------------------------------------------------------

def test_the_instance_door_refuses_a_walked_excess(tmp_path):
    path = write(tmp_path, _no_table(J2K_LOSSLESS, [U, U2, U3], frames=2))
    inst = Instance(generate_uid(), SOP_CLASS, 1, file_path=path)
    with pytest.raises(RuntimeError) as exc:
        inst.get_pixel_data()
    # The type and the words: the door's outer handler turns a message
    # holding "no pixel data" into None, and "decompress" into the
    # codecs-missing message.
    assert FRAGMENTS_WORDS in str(exc.value), str(exc.value)


# ---------------------------------------------------------------------------
# 5: native Pixel Data
# ---------------------------------------------------------------------------

def test_a_native_excess_is_kept_to_the_declared_frames(tmp_path):
    ds = dataset(EXPLICIT_LE, rows=2, cols=2, bits_allocated=8, frames=2,
                 native=bytes(range(12)))
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    _assert_signal_row(got["rows"], "Pixel Data's length holds 3 whole "
                                    "frames; NumberOfFrames declares 2")
    assert got["array"].shape == (2, 2, 2)
    assert got["array"].reshape(-1).tolist() == list(range(8))
    assert len(got["written"]) == 1
    assert "REVIEW_REQUIRED" in got["grade"], got["grade"]


def test_a_padded_native_frame_is_not_an_excess(tmp_path):
    """The trailing pad byte (PS3.5 7.1.1) is not a second one-byte frame.

    One sample, so the frame is one byte and the pad is a whole frame's
    length: the only shape on which a count that forgot the pad would
    find an excess.
    """
    ds = dataset(EXPLICIT_LE, rows=1, cols=1, bits_allocated=8, frames=1,
                 native=b"\x07")
    path = write(tmp_path, ds)
    assert len(pydicom.dcmread(path).PixelData) == 2
    assert imagecodecs_handler.offset_table_frame_count(
        pydicom.dcmread(path)) is None
    got = _pipeline(tmp_path, path)
    assert not got["rows"], got["rows"]
    assert got["array"].tolist() == [[7]]


def _native(rows, cols, bits, payload, **kwargs):
    return dataset(EXPLICIT_LE, rows=rows, cols=cols, bits_allocated=bits,
                   native=payload, **kwargs)


@pytest.mark.parametrize("ds", [
    # 15 bits a frame: pydicom's excess arithmetic divides by 1.875 and
    # raises TypeError rather than return frames.
    _native(3, 5, 1, bytes(4), frames=1),
    # Twice the 4:2:2 frame length: pydicom reads a longer 4:2:2 buffer as
    # a wrong label ("a third larger than expected"), not as frames.
    _native(2, 2, 8, bytes(16), frames=1, samples=3,
            photometric="YBR_FULL_422"),
    # A count pydicom refuses ("must be greater than or equal to 1").
    _native(2, 2, 8, bytes(12), frames=-1),
], ids=["one-bit-part-byte", "ybr-full-422", "negative-frames"])
def test_a_native_buffer_pydicom_does_not_read_as_frames_is_not_counted(ds):
    """The native count is pydicom's rule, including where pydicom declines.

    Each of these holds more bytes than its declared frames, and pydicom
    returns no extra frame for any: counted, the read door would refuse
    in this check's words where pydicom refuses in its own.
    """
    assert imagecodecs_handler.offset_table_frame_count(ds) is None


def test_a_zero_frame_count_is_read_as_one_and_counted():
    """pydicom reads NumberOfFrames 0 as 1 and returns every whole frame."""
    ds = _native(2, 2, 8, bytes(range(12)), frames=0)
    assert imagecodecs_handler.offset_table_frame_count(ds) == (
        3, 1, 0, "Pixel Data's length")
    assert imagecodecs_handler.frame_count_mismatch(ds) == (
        "Pixel Data's length holds 3 whole frames; "
        "NumberOfFrames is 0 (read as 1)")


# ---------------------------------------------------------------------------
# 6: an icon, at the #433 depth
# ---------------------------------------------------------------------------

def _icon(payload, *, frames, encapsulated):
    item = Dataset()
    item.Rows, item.Columns = ICON_FRAMES[0].shape if encapsulated else (2, 2)
    item.BitsAllocated = item.BitsStored = 8
    item.HighBit = 7
    item.SamplesPerPixel = 1
    item.PhotometricInterpretation = "MONOCHROME2"
    item.PixelRepresentation = 0
    if frames is not None:
        item.NumberOfFrames = frames
    item.add_new(0x7FE00010, "OB", payload)
    if encapsulated:
        item["PixelData"].is_undefined_length = True
    return item


def _icon_cases():
    j2k_top = dataset(J2K_LOSSLESS, [U], rows=4, cols=8, bits_allocated=16)
    j2k_top.IconImageSequence = Sequence([_icon(
        encapsulate([_j2k(f) for f in ICON_FRAMES], has_bot=False),
        frames=2, encapsulated=True)])
    native_top = dataset(EXPLICIT_LE, rows=2, cols=2, bits_allocated=8,
                         native=bytes(range(4)))
    native_top.IconImageSequence = Sequence([_icon(
        bytes(range(10, 18)), frames=None, encapsulated=False)])
    return [
        (j2k_top, f"{FRAGMENTS_WORDS}. {KEPT_WORDS}",
         np.stack(ICON_FRAMES[:2]).tolist()),
        (native_top, "Pixel Data's length holds 2 whole frames; "
                     "NumberOfFrames is absent (read as 1). Kept the first "
                     "1 and discarded 1.",
         [[10, 11], [12, 13]]),
    ]


@pytest.mark.parametrize("case", [0, 1], ids=["j2k-no-table", "native"])
def test_an_icon_walk_excess_is_kept_with_a_standard_row(tmp_path, case):
    ds, words, want = _icon_cases()[case]
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)

    losses = [r for r in got["rows"] if r[0] == "DATA_LOSS"]
    assert len(losses) == 1, got["rows"]
    _action, details, scope = losses[0]
    assert scope == LOSS_SCOPE_STANDARD
    assert details.startswith("Standard tag 7fe0,0010 (OB) at 0088,0200[0]: "
                              ), details
    assert words in details, details
    # Today's export-time row blamed the sidecar for a loss ingest made.
    assert "could not be restored from the sidecar" not in details

    (written,) = got["written"]
    icon = pydicom.dcmread(written).IconImageSequence[0]
    icon.file_meta = FileMetaDataset()
    icon.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    assert icon.pixel_array.tolist() == want


# ---------------------------------------------------------------------------
# 7: a signed excess frame no longer reaches a reader
# ---------------------------------------------------------------------------

def test_a_signed_excess_frame_no_longer_reaches_a_reader(tmp_path):
    """S11s: the third codestream is signed, and Pillow shifted it.

    Stored whole today, it made the instance unreadable; the frames kept
    are the two unsigned ones.
    """
    ds = _no_table(J2K_LOSSLESS, [U, U2, S], frames=2)
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)
    _assert_signal_row(got["rows"], FRAGMENTS_WORDS)
    assert got["array"].tolist() == np.stack([U16, U16B]).tolist()

    arr, _label = _decode_pixels(pydicom.dcmread(path),
                                 allow_excess_frames=False)
    assert arr.shape == (2, 4, 8)
    assert int(arr.max()) == int(U16.max()) != 65535


# ---------------------------------------------------------------------------
# 8: guards
# ---------------------------------------------------------------------------

def test_a_frame_split_across_fragments_is_not_an_excess(tmp_path):
    """One frame may legally span fragments (PS3.5 A.4): two frames, no row."""
    ds = _no_table(J2K_LOSSLESS, [U[:40], U[40:], U2], frames=2)
    assert _fragments(ds) == 3
    assert imagecodecs_handler.offset_table_frame_count(ds) is None
    path = write(tmp_path, ds)
    got = _pipeline(tmp_path, path)
    assert not got["rows"], got["rows"]
    assert got["array"].tolist() == np.stack([U16, U16B]).tolist()
    assert "PASS" in got["grade"] and "REVIEW" not in got["grade"]


def test_as_many_fragments_as_frames_is_not_an_excess():
    ds = _no_table(J2K_LOSSLESS, [U, U2, U3], frames=3)
    assert imagecodecs_handler.offset_table_frame_count(ds) is None


def test_one_declared_frame_over_many_fragments_is_the_stated_limit():
    """S12: pydicom joins every fragment into one frame under NOF 1.

    No pre-decode count can see an excess here, and none is claimed.
    Pinned so nobody believes this shape is covered.
    """
    for frames in (1, None):
        ds = _no_table(J2K_LOSSLESS, [U, U2, U3], frames=frames)
        assert len(list(generate_fragmented_frames(
            ds.PixelData, number_of_frames=1))) == 1
        assert imagecodecs_handler.offset_table_frame_count(ds) is None

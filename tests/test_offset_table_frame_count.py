"""An offset table that disagrees with NumberOfFrames is reported, not obeyed (#418).

An encapsulated `PixelData` states its frame count twice: once in
`NumberOfFrames` (0028,0008), and once in its Basic Offset Table (or the
Extended Offset Table beside it), which carries one offset per frame. No
code compared the two. When they disagreed:

* `imagecodecs_handler.get_pixel_data` decoded frame 0 of a two-frame table
  under a one-frame header and said nothing (the single-frame arm asks
  `generate_frames(..., number_of_frames=1)` for exactly one frame), and a
  table naming *fewer* frames than declared came back as a silent short
  read;
* ingest stored every frame the table named under a header that declared
  one, reported `ingested=1` with no audit row, and the instance could
  never be read back -- `get_pixel_data()` raised an Integrity Error and
  export raised `ExportError`, both against the wrong cause;
* the reverse (fewer frames than declared) was rejected at ingest with the
  reason `Decompression Failed: ` -- empty, because pydicom's
  `StopIteration` has no message.

Every test here asserts on a decoded array, an audit row or a refusal
message, never on the mere absence of an exception, and every fixture is
checked (B0) to carry the table the test is about: a fixture whose offset
table was silently empty would never enter the check at all.

The one mismatch nothing can see is an **empty** BOT with no EOT: the table
then names no frames and the fragments do not say where frames begin.
`test_an_empty_offset_table_is_the_documented_limit_and_decodes_as_before`
pins that limit rather than pretending it is closed.
"""
import copy
import os
import sqlite3
import zlib

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.encaps import (encapsulate, encapsulate_extended,
                            parse_basic_offsets)
from pydicom.pixels import get_decoder
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRLittleEndian, JPEG2000Lossless,
                         generate_uid)

import imagecodecs
from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from isocenter.io_handlers import (LOSS_SCOPE_SIGNAL, LOSS_SCOPE_STANDARD,
                                   _decode_pixels)
from isocenter.session import DicomSession
from support.decode_doors import through_the_fallback

#: Two distinguishable 4x4 8-bit frames. Frame 1 is not frame 0 shifted
#: into an overlapping range, so "frame 0" and "frame 1" cannot be
#: confused by an equality check.
FRAMES = [np.arange(16, dtype=np.uint8).reshape(4, 4),
          (np.arange(16, dtype=np.uint8) + 100).reshape(4, 4),
          (np.arange(16, dtype=np.uint8) + 200).reshape(4, 4)]

#: GE's private transfer syntax: a real UID pydicom cannot classify.
PRIVATE_TS = "1.2.840.113619.5.2"


def _codestream(frame):
    return imagecodecs.jpeg2k_encode(frame, level=0, codecformat="J2K")


def _dataset(n_frames, number_of_frames, table="bot"):
    """A J2K-lossless dataset whose offset table names `n_frames` frames.

    `number_of_frames=None` leaves (0028,0008) absent. `table` is "bot"
    (a populated Basic Offset Table, what `encapsulate` writes by
    default), "eot" (an Extended Offset Table with an empty BOT, what
    `encapsulate_extended` writes) or "empty" (an empty BOT, no EOT).
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = JPEG2000Lossless
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT418", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    if number_of_frames is not None:
        ds.NumberOfFrames = number_of_frames
    streams = [_codestream(f) for f in FRAMES[:n_frames]]
    if table == "bot":
        ds.PixelData = encapsulate(streams, has_bot=True)
    elif table == "empty":
        ds.PixelData = encapsulate(streams, has_bot=False)
    elif table == "eot":
        pixel_data, eot, eot_lengths = encapsulate_extended(streams)
        ds.PixelData = pixel_data
        ds.ExtendedOffsetTable = eot
        ds.ExtendedOffsetTableLengths = eot_lengths
    else:
        raise AssertionError(table)
    # Encapsulated pixel data is written undefined-length.
    ds["PixelData"].is_undefined_length = True
    return ds


def _write(folder, ds, name="one.dcm"):
    path = os.path.join(folder, name)
    ds.save_as(path, enforce_file_format=True)
    return path


def _only_instance(session):
    for pt in session.store.patients:
        for st in pt.studies:
            for se in st.series:
                for inst in se.instances:
                    return inst
    raise AssertionError("the fixture ingested no instance")


def _audit_rows(db, action_type):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT entity_uid, details, loss_scope FROM audit_log "
            "WHERE action_type=?", (action_type,)).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B0 -- the fixtures carry the tables the tests are about
# ---------------------------------------------------------------------------

def test_the_fixtures_carry_the_offset_tables_they_are_named_for():
    """The fixture-never-enters-the-check guard, and it is load-bearing.

    A `_dataset` that quietly wrote an empty BOT would make every refusal
    test below vacuous in the passing direction and every "decodes
    unchanged" test pass for the wrong reason.
    """
    bot2 = _dataset(2, 1)
    offsets = parse_basic_offsets(bot2.PixelData)
    assert len(offsets) == 2 and offsets[0] == 0 and offsets[1] > 0

    assert len(parse_basic_offsets(_dataset(3, 2).PixelData)) == 3
    assert len(parse_basic_offsets(_dataset(2, 3).PixelData)) == 2

    eot = _dataset(2, 1, table="eot")
    assert parse_basic_offsets(eot.PixelData) == []
    assert len(eot.ExtendedOffsetTable) // 8 == 2

    empty = _dataset(2, 1, table="empty")
    assert parse_basic_offsets(empty.PixelData) == []
    assert "ExtendedOffsetTable" not in empty


def test_the_helper_reports_the_table_and_both_counts():
    """The one comparison both halves of the fix read (#418)."""
    h = imagecodecs_handler.offset_table_frame_count
    # The third slot is NumberOfFrames as the file states it, None when
    # absent -- so a 0 can be reported as a 0, not as the 1 it is read as.
    assert h(_dataset(2, 1)) == (2, 1, 1, "Basic Offset Table")
    assert h(_dataset(2, None)) == (2, 1, None, "Basic Offset Table")
    assert h(_dataset(2, 0)) == (2, 1, 0, "Basic Offset Table")
    assert h(_dataset(2, 1, table="eot")) == (
        2, 1, 1, "Extended Offset Table")
    # Undetectable, and said so: None, not a guess.
    assert h(_dataset(2, 1, table="empty")) is None
    # Native pixel data is not encapsulated: no table to compare.
    native = _dataset(1, 1)
    native.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    native.PixelData = FRAMES[0].tobytes()
    assert h(native) is None
    # A `force=True` read of a header-less file has an empty file_meta
    # (#281's population). The helper must not be what refuses it.
    bare = Dataset()
    bare.PixelData = FRAMES[0].tobytes()
    assert h(bare) is None


@pytest.mark.parametrize("ts", [
    PRIVATE_TS,                     # GE's private syntax
    "1.2.840.10008.5.1.4.1.1.7",    # a SOP Class UID in the TS slot
    "",                             # an empty UID
])
def test_the_helper_declines_a_transfer_syntax_it_cannot_classify(ts):
    """None, never a raise: the check must not be what refuses these files.

    pydicom's `UID.is_encapsulated` raises `ValueError("UID is not a
    transfer syntax.")` for all three, and the helper runs outside
    `ingest_worker`'s decode `try` -- so a raise here replaced the
    decoder's own reason, which names the UID, with one that does not.
    """
    ds = _dataset(2, 1)
    ds.file_meta.TransferSyntaxUID = ts
    assert imagecodecs_handler.offset_table_frame_count(ds) is None
    assert imagecodecs_handler.frame_count_mismatch(ds) is None


def test_the_helper_declines_a_pixel_element_with_no_value():
    """`PixelData = None` is present and holds nothing to parse."""
    ds = _dataset(2, 1)
    ds.PixelData = None
    assert imagecodecs_handler.offset_table_frame_count(ds) is None


def _private_ts_file(folder):
    """A native 4x4 file whose transfer syntax no decoder here knows."""
    ds = _dataset(1, None)
    ds.file_meta.TransferSyntaxUID = PRIVATE_TS
    # A fresh element: the one `_dataset` built is marked undefined-length
    # (encapsulated), and reassigning its value keeps that flag.
    del ds.PixelData
    ds.PixelData = FRAMES[0].tobytes()
    path = os.path.join(folder, "private_ts.dcm")
    ds.save_as(path, implicit_vr=False, little_endian=True,
               enforce_file_format=True)
    return path


def test_a_private_transfer_syntax_is_still_refused_in_the_decoders_words(
        tmp_path):
    """Ingest and the file arm both still name the UID they cannot decode.

    Before #418 the ingest reason was `Decompression Failed: No pixel data
    decoders have been implemented for '1.2.840.113619.5.2'` and the file
    read raised `... '1.2.840.113619.5.2' is not supported`. The pre-check
    raising first turned both into `UID is not a transfer syntax.`
    """
    src = tmp_path / "src"
    src.mkdir()
    path = _private_ts_file(str(src))

    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        summary = session.ingest(str(src))
    finally:
        session.close()
    assert summary.ingested == 0
    assert len(summary.failures) == 1
    reason = summary.failures[0][1]
    assert PRIVATE_TS in reason, reason
    assert reason.startswith("Decompression Failed:"), reason

    inst = Instance(generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                    file_path=path)
    with pytest.raises(RuntimeError) as exc:
        inst.get_pixel_data()
    assert PRIVATE_TS in str(exc.value), str(exc.value)


def test_an_explicit_zero_number_of_frames_is_reported_as_zero():
    """The message says what the file says, not the 1 it is read as."""
    with pytest.raises(RuntimeError) as exc:
        through_the_fallback(_dataset(2, 0))
    msg = str(exc.value)
    assert "names 2 frames" in msg
    assert "NumberOfFrames is 0 (read as 1)" in msg
    assert "declares 1" not in msg


def test_a_negative_number_of_frames_is_reported_as_invalid():
    """pydicom's decoder refuses a negative count; it reads nothing as 1.

    Measured on pydicom 3.0.2: "must be greater than or equal to 1". So
    "(read as 1)" would describe a reading no decoder makes.

    Asked of `frame_count_mismatch`, the words the Instance door raises
    before it decodes. `_decode_pixels` never reaches them for this file:
    since #453 it runs pydicom's validation first, which refuses the
    count in the words quoted above.
    """
    msg = imagecodecs_handler.frame_count_mismatch(_dataset(2, -1))
    assert "names 2 frames" in msg
    assert "NumberOfFrames is -1 (invalid)" in msg
    assert "read as" not in msg


@pytest.mark.parametrize("empty", ["", None], ids=["in-memory", "from-file"])
def test_an_empty_number_of_frames_is_reported_as_empty_not_absent(empty):
    """Present with no value is not absent, and it is not read as 1.

    Measured on pydicom 3.0.2: its decoder refuses an empty count
    ("invalid literal for int()"), so "absent (read as 1)" was wrong
    twice. Both spellings: an empty value assigned in memory is `""`, and
    the same element written and read back from a file is `None` -- still
    present (`"NumberOfFrames" in ds`), so presence cannot be read off
    the value.

    Asked of `frame_count_mismatch`, as the negative count above is, and
    for the same reason: pydicom's validation refuses `""` first at
    `_decode_pixels`.
    """
    ds = _dataset(2, 1)
    ds.NumberOfFrames = empty
    assert "NumberOfFrames" in ds
    msg = imagecodecs_handler.frame_count_mismatch(ds)
    assert "names 2 frames" in msg
    assert "NumberOfFrames is empty" in msg
    assert "absent" not in msg
    assert "read as" not in msg


# ---------------------------------------------------------------------------
# T1-T8 -- the imagecodecs route (`imagecodecs_handler.get_pixel_data` until
# #453 deleted it; `_decode_pixels` with pydicom unable to decode since)
# ---------------------------------------------------------------------------

def test_the_handler_refuses_a_two_frame_table_under_one_declared_frame():
    """T1, the issue's case: frame 0 came back and nothing said why."""
    with pytest.raises(RuntimeError) as exc:
        through_the_fallback(_dataset(2, 1))
    msg = str(exc.value)
    assert "Basic Offset Table names 2 frames" in msg
    assert "NumberOfFrames declares 1" in msg


def test_the_handler_says_when_number_of_frames_is_absent():
    """T2: absent is read as 1, and the message says that is what happened."""
    with pytest.raises(RuntimeError) as exc:
        through_the_fallback(_dataset(2, None))
    msg = str(exc.value)
    assert "names 2 frames" in msg
    assert "NumberOfFrames is absent (read as 1)" in msg


def test_the_handler_refuses_in_the_multi_frame_arm_too():
    """T3: three offsets under a two-frame header (the old multi-frame arm)."""
    with pytest.raises(RuntimeError) as exc:
        through_the_fallback(_dataset(3, 2))
    msg = str(exc.value)
    assert "names 3 frames" in msg
    assert "NumberOfFrames declares 2" in msg


def test_the_handler_refuses_a_table_naming_fewer_frames_than_declared():
    """T4, the reverse direction: this was a silent short read of (2,4,4)."""
    with pytest.raises(RuntimeError) as exc:
        through_the_fallback(_dataset(2, 3))
    msg = str(exc.value)
    assert "names 2 frames" in msg
    assert "NumberOfFrames declares 3" in msg


def test_the_handler_reads_the_extended_offset_table():
    """T5: with an EOT the BOT is empty, so a BOT-only check sees nothing."""
    with pytest.raises(RuntimeError) as exc:
        through_the_fallback(_dataset(2, 1, table="eot"))
    msg = str(exc.value)
    assert "Extended Offset Table names 2 frames" in msg
    assert "NumberOfFrames declares 1" in msg


def test_an_empty_offset_table_is_the_documented_limit_and_decodes_as_before():
    """T6: nothing can count frames here, so nothing refuses.

    Pinned so that a check which refused every multi-fragment empty-BOT
    file -- one frame may legally span several fragments -- goes red.
    """
    out, _label = through_the_fallback(_dataset(2, 1, table="empty"))
    assert out.shape == (4, 4)
    assert np.array_equal(out, FRAMES[0])


def test_a_consistent_basic_offset_table_decodes_unchanged():
    """T7: no false positive, single- and multi-frame."""
    out, _label = through_the_fallback(_dataset(1, 1))
    assert np.array_equal(out.reshape(4, 4), FRAMES[0])
    out, _label = through_the_fallback(_dataset(2, 2))
    assert out.shape == (2, 4, 4)
    assert np.array_equal(out[1], FRAMES[1])


def test_a_consistent_extended_offset_table_decodes_unchanged():
    """T8: an EOT that agrees with NumberOfFrames is not a mismatch."""
    out, _label = through_the_fallback(_dataset(2, 2, table="eot"))
    assert out.shape == (2, 4, 4)
    assert np.array_equal(out[0], FRAMES[0])
    assert np.array_equal(out[1], FRAMES[1])


# ---------------------------------------------------------------------------
# B1 -- Instance.get_pixel_data from a file
# ---------------------------------------------------------------------------

def test_a_file_backed_instance_refuses_the_mismatch_and_names_it(tmp_path):
    """B1: the file_path arm, and the three wording traps on its way out.

    Without a check ahead of the pydicom read, pydicom returned (2,4,4)
    for a one-frame header; and a refusal raised only by the handler is
    swallowed by the arm's fallback, which re-raises the *original* error.
    The message must also avoid the phrases the arm rewrites: "no pixel
    data" (turned into `return None`, a silent nothing) and "decompress"
    / "missing dependencies" (turned into "Missing image codecs").
    """
    path = _write(str(tmp_path), _dataset(2, 1))
    inst = Instance(generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                    file_path=path)

    result = "not raised"
    with pytest.raises(RuntimeError) as exc:
        result = inst.get_pixel_data()
    assert result == "not raised"  # never None: a refusal, not "no pixels"
    msg = str(exc.value)
    assert "Basic Offset Table names 2 frames" in msg
    assert "NumberOfFrames declares 1" in msg
    assert "Missing image codecs" not in msg
    assert inst.pixel_array is None


def test_a_consistent_file_backed_instance_still_reads(tmp_path):
    """The pre-check is not a new refusal of good files."""
    path = _write(str(tmp_path), _dataset(2, 2))
    inst = Instance(generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                    file_path=path)
    out = inst.get_pixel_data()
    assert out.shape == (2, 4, 4)
    assert np.array_equal(out[1], FRAMES[1])


# ---------------------------------------------------------------------------
# B2-B5 -- ingest
# ---------------------------------------------------------------------------

def _ingest(tmp_path, ds):
    src = tmp_path / "src"
    src.mkdir()
    _write(str(src), ds)
    db = str(tmp_path / "s.db")
    session = DicomSession(persistence_file=db)
    summary = session.ingest(str(src))
    session.store_backend.flush_audit_queue()
    return session, summary, db


def _assert_truncated_to_frame_0_with_one_signal_row(tmp_path, ds, table,
                                                     declared_words):
    session, summary, db = _ingest(tmp_path, ds)
    try:
        assert summary.ingested == 1
        assert summary.failures == []

        rows = _audit_rows(db, "DATA_LOSS")
        assert len(rows) == 1, rows
        _uid, details, scope = rows[0]
        assert scope == LOSS_SCOPE_SIGNAL
        assert f"{table} names 2 frames" in details
        assert declared_words in details

        inst = _only_instance(session)
        # The pixels must come from the store, not a resident array.
        assert inst.unload_pixel_data() is True
        got = inst.get_pixel_data()
        assert got.shape == (4, 4)
        assert np.array_equal(got, FRAMES[0])

        out = tmp_path / "out"
        session.export(str(out), use_compression=False)
        files = [os.path.join(d, f) for d, _, fs in os.walk(out)
                 for f in fs if f.endswith(".dcm")]
        assert len(files) == 1
        written = pydicom.dcmread(files[0])
        assert int(getattr(written, "NumberOfFrames", 1) or 1) == 1
        assert np.array_equal(written.pixel_array, FRAMES[0])
    finally:
        session.close()


def test_ingest_keeps_the_declared_frame_and_reports_the_excess(tmp_path):
    """B2: this ingested cleanly with no row and could never be read back."""
    _assert_truncated_to_frame_0_with_one_signal_row(
        tmp_path, _dataset(2, 1), "Basic Offset Table",
        "NumberOfFrames declares 1")


def test_ingest_says_when_number_of_frames_was_absent(tmp_path):
    """B3: absent is read as 1 by pydicom too, and the row says so.

    The same words the read-path refusal uses: one spelling of the
    mismatch, whichever channel carries it.
    """
    _assert_truncated_to_frame_0_with_one_signal_row(
        tmp_path, _dataset(2, None), "Basic Offset Table",
        "NumberOfFrames is absent (read as 1)")


def test_ingest_reads_the_extended_offset_table_too(tmp_path):
    """B5: the EOT excess takes the same route as the BOT one."""
    _assert_truncated_to_frame_0_with_one_signal_row(
        tmp_path, _dataset(2, 1, table="eot"), "Extended Offset Table",
        "NumberOfFrames declares 1")


def test_ingest_refuses_a_table_naming_fewer_frames_and_says_why(tmp_path):
    """B4: the reason was an empty `Decompression Failed: `."""
    session, summary, db = _ingest(tmp_path, _dataset(2, 3))
    try:
        assert summary.ingested == 0
        assert len(summary.failures) == 1
        reason = summary.failures[0][1]
        assert "names 2 frames" in reason
        assert "NumberOfFrames declares 3" in reason

        errors = _audit_rows(db, "ERROR")
        assert len(errors) == 1
        assert "names 2 frames" in errors[0][1]
        assert _audit_rows(db, "DATA_LOSS") == []
    finally:
        session.close()


def test_a_consistent_multi_frame_ingest_writes_no_row(tmp_path):
    """No false positive on the ingest path."""
    session, summary, db = _ingest(tmp_path, _dataset(2, 2))
    try:
        assert summary.ingested == 1
        assert _audit_rows(db, "DATA_LOSS") == []
        inst = _only_instance(session)
        assert inst.unload_pixel_data() is True
        got = inst.get_pixel_data()
        assert got.shape == (2, 4, 4)
        assert np.array_equal(got[1], FRAMES[1])
    finally:
        session.close()


# ---------------------------------------------------------------------------
# B6 -- the default decode is unchanged
# ---------------------------------------------------------------------------

def test_the_default_decode_is_still_every_frame_pydicom_returns():
    """B6: only a caller that asks, and only on an excess, truncates.

    On an excess fixture `as_array` with its defaults returns *every*
    frame the table names. `_decode_pixels(ds)` with no keyword must
    return the same: truncation is the decision of a caller that has
    counted the table and will write the row -- `ingest_worker` for the
    top level (#418), `_decode_nested_pixels` for an icon (#433) -- and
    no caller gets it by default. Also pins that the keyword is not
    forwarded as `None`: measured on pydicom 3.0.2, passing
    `allow_excess_frames=None` truncates exactly as `False` does.
    """
    ds = _dataset(2, 1)
    expected, _meta = get_decoder(ds.file_meta.TransferSyntaxUID).as_array(ds)
    assert expected.shape == (2, 4, 4)  # the fixture really has an excess

    got, _pi = _decode_pixels(ds)
    assert got.shape == expected.shape
    assert np.array_equal(got, expected)

    truncated, _pi = _decode_pixels(ds, allow_excess_frames=False)
    assert truncated.shape == (4, 4)
    assert np.array_equal(truncated, FRAMES[0])


# ---------------------------------------------------------------------------
# B7 -- a nested pixel item (an icon) whose offset table disagrees (#433)
# ---------------------------------------------------------------------------
#
# #418 fixed the top level and deliberately left nested items alone, so an
# Icon Image Sequence item whose Basic Offset Table named two frames under a
# one-frame header was carried whole -- 8 bytes under a 2x2 8-bit header --
# with no row, and export then dropped the icon blaming an Integrity Error.
# Scoped STANDARD, not SIGNAL as at the top level: an icon is a derived
# thumbnail, every other icon loss is STANDARD, and truncating an icon's
# frames must not grade worse than losing the icon outright.

#: Two distinguishable 2x2 icon frames.
ICON_FRAMES = [np.array([[10, 11], [12, 13]], dtype=np.uint8),
               np.array([[50, 51], [52, 53]], dtype=np.uint8)]
ICON_PATH_WORDS = "0088,0200[0]"


def _icon(n_frames, number_of_frames):
    """An encapsulated J2K icon item whose BOT names `n_frames` frames."""
    item = Dataset()
    item.Rows = item.Columns = 2
    item.BitsAllocated = item.BitsStored = 8
    item.HighBit = 7
    item.SamplesPerPixel = 1
    item.PhotometricInterpretation = "MONOCHROME2"
    item.PixelRepresentation = 0
    if number_of_frames is not None:
        item.NumberOfFrames = number_of_frames
    item.PixelData = encapsulate(
        [_codestream(f) for f in ICON_FRAMES[:n_frames]], has_bot=True)
    item["PixelData"].is_undefined_length = True
    return item


def _with_icon(icon):
    """A consistent one-frame top level carrying `icon` at depth 1."""
    ds = _dataset(1, 1)
    ds.IconImageSequence = Sequence([icon])
    return ds


def _carried_icon_bytes(inst):
    """The raw bytes the store holds for the icon, or None when not carried."""
    refs = [ref for (path, tag), ref in inst._nested_pixel_refs.items()
            if tag == "7fe0,0010" and path == (("0088,0200", 0),)]
    if not refs:
        return None
    (ref,) = refs
    with open(ref.sidecar_path, "rb") as fh:
        fh.seek(ref.offset)
        blob = fh.read(ref.length)
    return zlib.decompress(blob) if ref.alg == "zlib" else blob


def test_the_icon_fixtures_carry_the_offset_tables_they_are_named_for():
    """B7-0: an icon whose BOT were silently empty would enter no check."""
    assert len(parse_basic_offsets(_icon(2, 1).PixelData)) == 2
    assert len(parse_basic_offsets(_icon(1, 2).PixelData)) == 1
    top = _with_icon(_icon(2, None))
    assert len(parse_basic_offsets(top.PixelData)) == 1
    assert "NumberOfFrames" not in top.IconImageSequence[0]


@pytest.mark.parametrize("number_of_frames,declared_words", [
    (None, "NumberOfFrames is absent (read as 1)"),
    (1, "NumberOfFrames declares 1"),
], ids=["absent", "one"])
def test_an_icon_keeps_its_declared_frame_and_reports_the_excess(
        tmp_path, number_of_frames, declared_words):
    """N1, the issue's case: 8 bytes under a 2x2 8-bit header, and no row."""
    session, summary, db = _ingest(
        tmp_path, _with_icon(_icon(2, number_of_frames)))
    try:
        assert summary.ingested == 1
        assert _carried_icon_bytes(_only_instance(session)) == \
            ICON_FRAMES[0].tobytes()

        rows = _audit_rows(db, "DATA_LOSS")
        assert len(rows) == 1, rows
        _uid, details, scope = rows[0]
        assert scope == LOSS_SCOPE_STANDARD
        assert details.startswith(
            f"Standard tag 7fe0,0010 (OB) at {ICON_PATH_WORDS}: "), details
        assert "Basic Offset Table names 2 frames" in details, details
        assert declared_words in details, details
        assert "Kept the first 1 and discarded 1" in details, details

        out = tmp_path / "out"
        session.export(str(out), use_compression=False)
        session.store_backend.flush_audit_queue()
        files = [os.path.join(d, f) for d, _, fs in os.walk(out)
                 for f in fs if f.endswith(".dcm")]
        assert len(files) == 1
        written = pydicom.dcmread(files[0])
        assert written.IconImageSequence[0].PixelData == \
            ICON_FRAMES[0].tobytes()
        assert not any("could not be restored" in d
                       for _u, d, _s in _audit_rows(db, "DATA_LOSS"))
    finally:
        session.close()


def test_a_consistent_multi_frame_icon_is_carried_whole_with_no_row(tmp_path):
    """N2: no false positive -- a 2/2 icon is 8 bytes, as it always was."""
    session, summary, db = _ingest(tmp_path, _with_icon(_icon(2, 2)))
    try:
        assert summary.ingested == 1
        assert _carried_icon_bytes(_only_instance(session)) == (
            ICON_FRAMES[0].tobytes() + ICON_FRAMES[1].tobytes())
        assert _audit_rows(db, "DATA_LOSS") == []
    finally:
        session.close()


def test_an_icon_naming_fewer_frames_is_not_carried_and_says_why(tmp_path):
    """N3: one row naming the item and both counts, not the generic one.

    The generic row said "unrouted pixel elements are not held in the
    object graph", which is #194's wrong-reason shape: the element was
    routed, and its offset table is why it was not carried.
    """
    session, summary, db = _ingest(tmp_path, _with_icon(_icon(1, 2)))
    try:
        assert summary.ingested == 1
        assert _carried_icon_bytes(_only_instance(session)) is None

        rows = _audit_rows(db, "DATA_LOSS")
        assert len(rows) == 1, rows
        _uid, details, scope = rows[0]
        assert scope == LOSS_SCOPE_STANDARD
        assert details.startswith(
            f"Standard tag 7fe0,0010 (OB) at {ICON_PATH_WORDS} was not "
            f"ingested: "), details
        assert "Basic Offset Table names 1 frames" in details, details
        assert "NumberOfFrames declares 2" in details, details
        assert "unrouted" not in details, details
    finally:
        session.close()


#: Two distinguishable 2x2 RGB 16-bit icon frames: a cell Pillow, the only
#: JPEG 2000 plugin pydicom has here, will not decode.
RGB16_ICON_FRAMES = [
    (np.arange(12, dtype=np.int64) * 3000).astype(np.uint16).reshape(2, 2, 3),
    (np.arange(12, dtype=np.int64) * 3000 + 5).astype(np.uint16)
    .reshape(2, 2, 3)]


def test_a_16_bit_colour_icon_is_truncated_through_the_fallback(tmp_path):
    """N4: the nested caller's excess reaches `imagecodecs` too (#416, #433).

    pydicom cannot decode this icon at all, so the decode goes through
    `_decode_pixels`' fallback, which refuses an excess unless its caller
    asked for it to be dropped. The nested caller asks; without that the
    icon would not be carried.
    """
    icon = Dataset()
    icon.Rows = icon.Columns = 2
    icon.BitsAllocated = icon.BitsStored = 16
    icon.HighBit = 15
    icon.SamplesPerPixel = 3
    icon.PlanarConfiguration = 0
    icon.PhotometricInterpretation = "RGB"
    icon.PixelRepresentation = 0
    icon.NumberOfFrames = 1
    icon.PixelData = encapsulate(
        [_codestream(f) for f in RGB16_ICON_FRAMES], has_bot=True)
    icon["PixelData"].is_undefined_length = True
    assert len(parse_basic_offsets(icon.PixelData)) == 2

    # The precondition: pydicom cannot decode it, so this is the fallback.
    probe = copy.deepcopy(icon)
    probe.file_meta = FileMetaDataset()
    probe.file_meta.TransferSyntaxUID = JPEG2000Lossless
    with pytest.raises(RuntimeError):
        get_decoder(JPEG2000Lossless).as_array(probe,
                                               allow_excess_frames=False)

    session, summary, db = _ingest(tmp_path, _with_icon(icon))
    try:
        assert summary.ingested == 1
        assert _carried_icon_bytes(_only_instance(session)) == \
            RGB16_ICON_FRAMES[0].tobytes()
        rows = _audit_rows(db, "DATA_LOSS")
        assert len(rows) == 1, rows
        _uid, details, scope = rows[0]
        assert scope == LOSS_SCOPE_STANDARD
        assert ICON_PATH_WORDS in details, details
        assert "Basic Offset Table names 2 frames" in details, details
    finally:
        session.close()


def test_an_icon_two_items_deep_is_named_by_its_whole_path(tmp_path):
    """N5: the row names every item on the way down, in order.

    An icon inside the second item of a Referenced Image Sequence. The
    path is the only identity a nested item has, and a depth-1 fixture
    cannot tell a path from its last step: joining the steps with a
    comma instead of " > " survived every depth-1 test.
    """
    outer = Dataset()
    outer.IconImageSequence = Sequence([_icon(2, 1)])
    ds = _dataset(1, 1)
    ds.ReferencedImageSequence = Sequence([Dataset(), outer])
    session, summary, db = _ingest(tmp_path, ds)
    try:
        assert summary.ingested == 1
        rows = _audit_rows(db, "DATA_LOSS")
        assert len(rows) == 1, rows
        _uid, details, scope = rows[0]
        assert scope == LOSS_SCOPE_STANDARD
        assert details.startswith(
            "Standard tag 7fe0,0010 (OB) at "
            "0008,1140[1] > 0088,0200[0]: "), details
        assert "Kept the first 1 and discarded 1" in details, details
    finally:
        session.close()

"""`ingest()` decodes what `Instance.get_pixel_data()` can, and no more (#416).

`ingest_worker` decoded through pydicom's own backend alone. In the
environment this package installs, pydicom has **no** plugin for JPEG
Lossless (1.2.840.10008.1.2.4.57, .70) or JPEG-LS (.80, .81), and its
only JPEG 2000 plugin is Pillow, which refuses 16-bit multi-sample data.
So every such file was refused at the door ("all plugins are missing
dependencies", "Pillow cannot decode 16-bit multi-sample data
correctly") while `Instance.get_pixel_data()` read it through
`imagecodecs`. The ruling: ingest accepts what `get_pixel_data()` can
decode. That opens four whole syntaxes, not one cell, and
`io_handlers._IMAGECODECS_FALLBACK_SYNTAXES` is the one place to narrow
it; `test_every_fallback_syntax_ingests` pins each member.

pydicom is always tried first, and only its plugin failures
(`RuntimeError`) fall through, so a file that ingested before decodes to
the same bytes and label, and a header pydicom rejects stays refused in
its words (F7). The fallback's output is checked against the header,
because a decode can disagree with it: F11 and F12 are the size and
shape halves, and a signedness mismatch is the dtype half. A signed JPEG
Lossless or JPEG-LS frame decoded unsigned, -800 read as 3296, and this
file refused it (F4) until #446 taught the handler to sign-extend from
BitsStored; those files are now pinned as read, at both doors, in
`tests/test_signed_lossless_jpeg_decode.py`.

**Every fixture first asserts that `pydicom.dcmread(p).pixel_array`
raises.** Without that, a test passes through pydicom's door and says
nothing about the fallback. Every value assertion is against a module
literal, never against an array the production code produced.

The export -> re-ingest round trip for 16-bit colour (F3) lives in
`tests/test_signed_pixels_survive_a_compressed_export.py`, where the
refusal it replaces was pinned. Its nested-depth twin (N4) is in
`tests/test_offset_table_frame_count.py`.
"""
import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.pixels import convert_color_space
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid

from isocenter.io_handlers import (LOSS_SCOPE_SIGNAL,
                                   _FALLBACK_PHOTOMETRICS,
                                   _IMAGECODECS_FALLBACK_SYNTAXES,
                                   _decode_pixels)
from isocenter.session import DicomSession

J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"
J2K = "1.2.840.10008.1.2.4.91"
LJPEG = "1.2.840.10008.1.2.4.57"
LJPEG_SV1 = "1.2.840.10008.1.2.4.70"
JPEGLS = "1.2.840.10008.1.2.4.80"
JPEGLS_NEAR = "1.2.840.10008.1.2.4.81"

#: 4x4 RGB, 16-bit, every sample above 255 somewhere so a byte-wide
#: decode could not pass. Literals, built once here.
RGB16 = {
    "uint16": (np.arange(48, dtype=np.int64) * 1000).astype(np.uint16)
    .reshape(4, 4, 3),
    "int16": (np.arange(48, dtype=np.int64) * 1000 - 20000).astype(np.int16)
    .reshape(4, 4, 3),
}
#: A second RGB frame, distinct from the first everywhere.
RGB16_FRAME1 = (np.arange(48, dtype=np.int64) * 1000 + 7).astype(
    np.uint16).reshape(4, 4, 3)
#: 4x4 unsigned 16-bit monochrome, values above 255.
MONO16 = (np.arange(16, dtype=np.int64) * 4000).astype(np.uint16).reshape(4, 4)
#: Palette indices.
PALETTE8 = np.arange(16, dtype=np.uint8).reshape(4, 4)


def _j2k(arr):
    return imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K")


ENCODERS = {J2K_LOSSLESS: _j2k, J2K: _j2k,
            LJPEG: imagecodecs.ljpeg_encode,
            LJPEG_SV1: imagecodecs.ljpeg_encode,
            JPEGLS: imagecodecs.jpegls_encode,
            JPEGLS_NEAR: imagecodecs.jpegls_encode}


def _dataset(ts, frames, photometric="RGB", pixel_representation=0,
             bits_stored=None, number_of_frames=None, planar=0,
             encode_as=None):
    """An encapsulated dataset whose BOT names `len(frames)` frames.

    `encode_as` is the array handed to the encoder when it differs from
    what the header describes (the signed 12-bit pattern).
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ts
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT416", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    first = frames[0]
    ds.Rows, ds.Columns = first.shape[0], first.shape[1]
    ds.SamplesPerPixel = first.shape[2] if first.ndim == 3 else 1
    ds.PhotometricInterpretation = photometric
    if ds.SamplesPerPixel > 1 and planar is not None:
        ds.PlanarConfiguration = planar
    ds.BitsAllocated = first.dtype.itemsize * 8
    ds.BitsStored = bits_stored or ds.BitsAllocated
    ds.HighBit = ds.BitsStored - 1
    ds.PixelRepresentation = pixel_representation
    if number_of_frames is not None:
        ds.NumberOfFrames = number_of_frames
    encode = ENCODERS[ts]
    ds.PixelData = encapsulate(
        [encode(f) for f in (encode_as or frames)], has_bot=True)
    ds["PixelData"].is_undefined_length = True
    return ds


def _write(folder, ds, name="one.dcm"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    ds.save_as(path, enforce_file_format=True)
    return path


def _assert_pydicom_cannot(path, expect=RuntimeError):
    """The precondition: the test is about the fallback, not pydicom.

    `RuntimeError` is pydicom's plugin failure, the one the fallback
    answers. F7 passes `AttributeError`, pydicom's validation failure,
    which the fallback must not answer.
    """
    with pytest.raises(expect):
        _ = pydicom.dcmread(path).pixel_array


def _only_instance(session):
    insts = [i for p in session.store.patients for st in p.studies
             for se in st.series for i in se.instances]
    assert len(insts) == 1, insts
    return insts[0]


def _rows(db, action_type):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT details, loss_scope FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


@pytest.fixture
def ingest(tmp_path):
    """Write `ds`, ingest its folder, and hand back what the caller reads."""
    sessions = []

    def _run(ds, name="one", expect=RuntimeError):
        src = tmp_path / f"src_{name}"
        path = _write(str(src), ds)
        _assert_pydicom_cannot(path, expect)
        db = str(tmp_path / f"{name}.db")
        session = DicomSession(persistence_file=db)
        sessions.append(session)
        summary = session.ingest(str(src))
        session.store_backend.flush_audit_queue()
        return session, summary, db

    yield _run
    for session in sessions:
        session.close()


def _stored(session):
    """The pixels as the store holds them, not a resident array."""
    inst = _only_instance(session)
    assert inst.unload_pixel_data() is True
    return inst, inst.get_pixel_data()


# ---------------------------------------------------------------------------
# F1 -- 16-bit colour JPEG 2000, the cell the issue was filed for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [J2K_LOSSLESS, J2K])
@pytest.mark.parametrize("dtype_name", ["uint16", "int16"])
def test_a_16_bit_rgb_j2k_file_ingests_bit_exactly(ingest, ts, dtype_name):
    """F1: refused with `Pillow cannot decode 16-bit multi-sample data`."""
    want = RGB16[dtype_name]
    session, summary, _db = ingest(_dataset(
        ts, [want], pixel_representation=int(dtype_name == "int16")))
    assert summary.failures == []
    assert summary.ingested == 1
    inst, got = _stored(session)
    assert got.dtype == np.dtype(dtype_name)
    assert got.tolist() == want.tolist()
    assert inst.attributes["0028,0004"] == "RGB"


# ---------------------------------------------------------------------------
# F2 -- the fallback keeps #418's truncation and its row
# ---------------------------------------------------------------------------

def test_the_fallback_keeps_the_declared_frames_and_the_418_row(ingest):
    """F2: a 2-frame BOT under NumberOfFrames 1, through the fallback.

    The handler's public `get_pixel_data` refuses a mismatch, so a
    fallback calling it would reject the file; one that asked for the
    declared frames without `islice` would store both, because
    `generate_frames(buf, number_of_frames=1)` yields every frame a
    populated table names.
    """
    session, summary, db = ingest(_dataset(
        J2K_LOSSLESS, [RGB16["uint16"], RGB16_FRAME1], number_of_frames=1))
    assert summary.failures == []
    assert summary.ingested == 1
    rows = _rows(db, "DATA_LOSS")
    assert len(rows) == 1, rows
    details, scope = rows[0]
    assert scope == LOSS_SCOPE_SIGNAL
    assert "Basic Offset Table names 2 frames" in details, details
    assert "Kept the first 1 and discarded 1" in details, details
    _inst, got = _stored(session)
    assert got.shape == (4, 4, 3)
    assert got.tolist() == RGB16["uint16"].tolist()


def test_a_two_frame_16_bit_rgb_file_ingests_both_frames(ingest):
    """The multi-frame arm of the fallback, with a consistent table."""
    session, summary, db = ingest(_dataset(
        J2K_LOSSLESS, [RGB16["uint16"], RGB16_FRAME1], number_of_frames=2))
    assert (summary.ingested, summary.failures) == (1, [])
    assert _rows(db, "DATA_LOSS") == []
    _inst, got = _stored(session)
    assert got.shape == (2, 4, 4, 3)
    assert got[0].tolist() == RGB16["uint16"].tolist()
    assert got[1].tolist() == RGB16_FRAME1.tolist()


# ---------------------------------------------------------------------------
# F5 -- the widening, stated: every syntax in the constant now ingests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1, JPEGLS, JPEGLS_NEAR])
def test_an_unsigned_16_bit_lossless_file_now_ingests(ingest, ts):
    """F5: every JPEG Lossless and JPEG-LS file was refused at the door."""
    session, summary, _db = ingest(_dataset(ts, [MONO16],
                                            photometric="MONOCHROME2"))
    assert summary.failures == []
    assert summary.ingested == 1
    inst, got = _stored(session)
    assert got.dtype == np.dtype("uint16")
    assert got.tolist() == MONO16.tolist()
    assert inst.attributes["0028,0004"] == "MONOCHROME2"


def test_every_fallback_syntax_ingests():
    """The constant is exactly the syntaxes this file shows ingesting.

    Narrowing it is the owner's one-line lever; this is the test that
    names what that line currently opens. RLE (.5) is out: pydicom
    decodes RLE with no dependency, and the handler has no RLE arm
    (#447). JPEG Baseline and Extended (.50, .51) are in since #604, for
    the 12-bit Extended files Pillow refuses (`test_jpeg_extended_decode`).
    """
    assert _IMAGECODECS_FALLBACK_SYNTAXES == frozenset(
        {"1.2.840.10008.1.2.4.50", "1.2.840.10008.1.2.4.51",
         LJPEG, LJPEG_SV1, JPEGLS, JPEGLS_NEAR, J2K_LOSSLESS, J2K,
         "1.2.840.10008.1.2.4.201", "1.2.840.10008.1.2.4.202",
         "1.2.840.10008.1.2.4.203"})


# ---------------------------------------------------------------------------
# Y1 (was F6) -- YBR under JPEG 2000 is stored as the RGB it decodes to;
# F7 -- what the fallback does not decode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts,photometric,mct", [
    (J2K_LOSSLESS, "YBR_RCT", True),
    (J2K_LOSSLESS, "YBR_RCT", False),
    (J2K, "YBR_ICT", True),
], ids=["rct-mct1", "rct-mct0", "ict-mct1"])
def test_a_16_bit_ybr_j2k_ingests_as_the_rgb_it_decodes_to(ingest, ts,
                                                         photometric, mct):
    """Y1: openjpeg undoes the transform, so the stored label is RGB (#448).

    This was F6, which refused the file naming `'YBR_RCT'`: the fallback
    could only repeat the declared label, and repeating `YBR_RCT` over the
    RGB samples `jpeg2k_decode` returns would be #372's defect through a
    new door. The decode is RGB whatever the codestream's
    multiple-component-transform flag says (both states here) and whatever
    the label says, which is also what pydicom's own plugins return and
    label. So the label is rewritten, as the top level does for pydicom's
    door. `.91` permits a reversible codestream, and the decode does not
    read the label, so its case is exact too.
    """
    want = RGB16["uint16"]
    ds = _dataset(ts, [want], photometric=photometric)
    ds.PixelData = encapsulate([imagecodecs.jpeg2k_encode(
        want, level=0, codecformat="J2K", mct=mct)], has_bot=True)
    ds["PixelData"].is_undefined_length = True
    session, summary, _db = ingest(ds)
    assert (summary.ingested, summary.failures) == (1, [])
    inst, got = _stored(session)
    assert got.dtype == np.dtype("uint16")
    assert got.tolist() == want.tolist()
    assert inst.attributes["0028,0004"] == "RGB"


def test_a_header_pydicom_rejects_is_not_decoded_by_the_fallback(ingest):
    """F7: validation is pydicom's to refuse, in its own words.

    imagecodecs ignores PlanarConfiguration, so a fallback that caught
    every exception would ingest this file with a missing Type 1
    element. pydicom raises `AttributeError` for it, not `RuntimeError`.
    """
    ds = _dataset(J2K_LOSSLESS, [RGB16["uint16"]], planar=None)
    _session, summary, _db = ingest(ds, expect=AttributeError)
    assert summary.ingested == 0
    reason = summary.failures[0][1]
    assert reason.startswith("Decompression Failed:"), reason
    assert "Missing required element: (0028,0006)" in reason, reason
    assert "imagecodecs" not in reason, reason


# ---------------------------------------------------------------------------
# F8 -- an excess the caller did not ask to drop is never stored
# ---------------------------------------------------------------------------

def test_the_fallback_never_stores_an_excess_it_was_not_told_to_drop():
    """F8: `_decode_pixels` by default returns what the table names.

    pydicom does that for the syntaxes it can decode (B6). The fallback
    cannot return every frame under a header declaring fewer without
    storing an array nothing can read back, so without the keyword it
    refuses, naming the table; with it, it keeps the declared frame.
    """
    ds = _dataset(J2K_LOSSLESS, [RGB16["uint16"], RGB16_FRAME1],
                  number_of_frames=1)
    with pytest.raises(RuntimeError) as exc:
        _decode_pixels(ds)
    assert "Basic Offset Table names 2 frames" in str(exc.value)

    arr, pi = _decode_pixels(ds, allow_excess_frames=False)
    assert pi == "RGB"
    assert arr.tolist() == RGB16["uint16"].tolist()


# ---------------------------------------------------------------------------
# F9 -- PALETTE COLOR, the one allow-listed label no other test reaches
# ---------------------------------------------------------------------------

def test_a_palette_colour_jpeg_lossless_file_ingests_its_indices(ingest):
    """F9: indices, bit-exact, under the label they came with.

    Measured: `imagecodecs.ljpeg_decode` returns the index array, and
    pydicom's `as_array` does not apply a palette either, so the two
    doors agree on what a PALETTE COLOR array is.
    """
    session, summary, _db = ingest(_dataset(
        LJPEG_SV1, [PALETTE8], photometric="PALETTE COLOR"))
    assert (summary.ingested, summary.failures) == (1, [])
    inst, got = _stored(session)
    assert got.dtype == np.dtype("uint8")
    assert got.tolist() == PALETTE8.tolist()
    assert inst.attributes["0028,0004"] == "PALETTE COLOR"


# ---------------------------------------------------------------------------
# F10 -- icons under JPEG Lossless and JPEG-LS are carried now
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1, JPEGLS])
def test_a_lossless_jpeg_icon_is_now_carried(ingest, ts):
    """F10: allow-listed in `_CARRIABLE_TRANSFER_SYNTAXES`, never decoded.

    These three are in the nested allow-list, but pydicom had no plugin
    for them here, so every such icon fell to the loss row. Through the
    fallback they are carried, and exported raw.
    """
    icon_pixels = np.array([[10, 11], [12, 13]], dtype=np.uint8)
    icon = Dataset()
    icon.Rows = icon.Columns = 2
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit = 7
    icon.SamplesPerPixel = 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.PixelRepresentation = 0
    icon.PixelData = encapsulate([ENCODERS[ts](icon_pixels)], has_bot=True)
    icon["PixelData"].is_undefined_length = True
    ds = _dataset(ts, [MONO16], photometric="MONOCHROME2")
    ds.IconImageSequence = Sequence([icon])

    session, summary, db = ingest(ds)
    assert (summary.ingested, summary.failures) == (1, [])
    assert _rows(db, "DATA_LOSS") == []
    inst = _only_instance(session)
    assert [tag for (_path, tag) in inst._nested_pixel_refs] == ["7fe0,0010"]

    out = session.store_backend.db_path + "_out"
    session.export(out, use_compression=False)
    files = [os.path.join(d, f) for d, _, fs in os.walk(out)
             for f in fs if f.endswith(".dcm")]
    assert len(files) == 1
    written = pydicom.dcmread(files[0])
    assert written.IconImageSequence[0].PixelData == icon_pixels.tobytes()


# ---------------------------------------------------------------------------
# F2b, F11 -- the two guards a single-frame fixture cannot see
# ---------------------------------------------------------------------------

#: A third RGB frame, for a table naming three frames.
RGB16_FRAME2 = (np.arange(48, dtype=np.int64) * 1000 + 13).astype(
    np.uint16).reshape(4, 4, 3)


def test_a_multi_frame_excess_keeps_exactly_the_declared_frames(ingest):
    """F2b: three frames named, two declared, two kept.

    F2's single declared frame cannot tell whether the fallback decoded
    only the declared frames: `generate_frames(buf, number_of_frames=1)`
    yields every frame the table names, and taking `[0]` of them hides
    the rest. At two declared frames it shows -- without `islice` the
    decode stacks three, and the file is refused instead of truncated.
    """
    session, summary, db = ingest(_dataset(
        J2K_LOSSLESS, [RGB16["uint16"], RGB16_FRAME1, RGB16_FRAME2],
        number_of_frames=2))
    assert summary.failures == []
    assert summary.ingested == 1
    rows = _rows(db, "DATA_LOSS")
    assert len(rows) == 1, rows
    assert "Basic Offset Table names 3 frames" in rows[0][0]
    assert "Kept the first 2 and discarded 1" in rows[0][0]
    _inst, got = _stored(session)
    assert got.shape == (2, 4, 4, 3)
    assert got[0].tolist() == RGB16["uint16"].tolist()
    assert got[1].tolist() == RGB16_FRAME1.tolist()


def test_a_decode_smaller_than_its_header_is_refused_with_both_sizes(ingest):
    """F11: a 4x4 codestream under a 4x8 header (brief §9 attack 1).

    The geometry check has two halves, and this is the half where the
    sample counts differ. Here the size guard buys only the reason:
    without it the file is still refused, by numpy's `cannot reshape`,
    so what it adds is the counts a reader can check against the
    header, in this library's words. The other half is not like that.
    A decode with the *right* count in the wrong shape reshapes without
    complaint, so there the shape guard is what stops a different image
    being stored at all (F12).
    """
    ds = _dataset(J2K_LOSSLESS, [RGB16["uint16"]])
    ds.Columns = 8
    _session, summary, _db = ingest(ds)
    assert summary.ingested == 0
    reason = summary.failures[0][1]
    assert reason.startswith("Decompression Failed:"), reason
    assert "decoded 48 samples" in reason, reason
    assert "1 frame(s) of 4x8x3 need 96" in reason, reason


# ---------------------------------------------------------------------------
# F12 -- the right number of samples in the wrong shape is another image
# ---------------------------------------------------------------------------

#: 8x4 frames, for a header declaring 4x8: the same number of samples.
TALL_RGB16 = np.concatenate([RGB16["uint16"], RGB16_FRAME1])
TALL_MONO16 = np.concatenate([MONO16, MONO16 + 1])


@pytest.mark.parametrize("ts,frame,photometric", [
    (J2K_LOSSLESS, TALL_RGB16, "RGB"),
    (JPEGLS, TALL_MONO16, "MONOCHROME2"),
    (LJPEG_SV1, TALL_MONO16, "MONOCHROME2"),
], ids=["j2k-rgb16", "jpegls-mono16", "ljpeg-mono16"])
def test_an_8x4_decode_under_a_4x8_header_is_refused(ingest, ts, frame,
                                                     photometric):
    """F12: same dtype, same sample count, transposed geometry.

    A size check cannot see it. 8x4 and 4x8 hold the same number of
    samples, so the decode was reshaped into the header's geometry and
    stored as a different image. Each of these was refused on main,
    where only pydicom decoded, and was ingested by the fallback until
    the shape check. Found in review of #451, on both interpreters.
    """
    ds = _dataset(ts, [frame], photometric=photometric)
    ds.Rows, ds.Columns = 4, 8
    _session, summary, _db = ingest(ds)
    assert summary.ingested == 0
    assert len(summary.failures) == 1
    reason = summary.failures[0][1]
    assert reason.startswith("Decompression Failed:"), reason
    assert f"decoded to shape {frame.shape}" in reason, reason
    assert f"the header declares {(4, 8) + frame.shape[2:]}" in reason, \
        reason


def test_three_samples_under_a_one_sample_header_are_refused(ingest):
    """F12, the colour case: 4x4 RGB JPEG-LS under a 4x12 MONOCHROME2 header.

    That is 48 samples either way, so the size check passed and a colour
    image was stored as a grey one of another width, under a label that
    was never true of it.
    """
    ds = _dataset(JPEGLS, [RGB16["uint16"]], photometric="MONOCHROME2",
                  planar=None)
    ds.SamplesPerPixel = 1
    ds.Columns = 12
    _session, summary, _db = ingest(ds)
    assert summary.ingested == 0
    reason = summary.failures[0][1]
    assert reason.startswith("Decompression Failed:"), reason
    assert "decoded to shape (4, 4, 3)" in reason, reason
    assert "the header declares (4, 12)" in reason, reason


# ---------------------------------------------------------------------------
# Y4, Y5, F13 -- which colour spaces are labelled is decided per syntax
# ---------------------------------------------------------------------------

#: A 4x12 greyscale frame: 48 samples, the count a 4x4 RGB header needs.
WIDE_MONO16 = np.concatenate([MONO16, MONO16 + 1, MONO16 + 2], axis=1)
#: 4x4 RGB, 8-bit.
RGB8 = (np.arange(48, dtype=np.int64) * 5).astype(np.uint8).reshape(4, 4, 3)
_GREY = {"MONOCHROME1", "MONOCHROME2", "PALETTE COLOR"}


def _fallback_reason(summary):
    """The fallback's own clause: pydicom's message may name the syntax."""
    reason = summary.failures[0][1]
    assert reason.startswith("Decompression Failed:"), reason
    marker = "imagecodecs could not decode it either: "
    assert marker in reason, reason
    return reason.split(marker, 1)[1]


def test_the_colour_spaces_the_fallback_labels_are_chosen_per_syntax():
    """Y4: declared label -> stored label, per syntax; the one place to widen.

    Every syntax has a row. A label maps to itself where the decode is
    measured to leave it true: greyscale and palette everywhere, RGB under
    all three families (JPEG Lossless since #387, Y5). It maps to another
    where the stored samples are not in the declared space: JPEG 2000's
    `YBR_RCT` and `YBR_ICT` come back RGB from the decoder (Y1, #448), and
    8-bit `YBR_FULL` JPEG-LS is converted to RGB by the fallback (Y2).
    JPEG Baseline and Extended take monochrome alone (#604): no palette,
    and no colour row, which `jpeg_decode` was measured to answer
    differently from pydicom.
    """
    grey = {label: label for label in _GREY}
    assert set(_FALLBACK_PHOTOMETRICS) == _IMAGECODECS_FALLBACK_SYNTAXES
    j2k = {**grey, "RGB": "RGB", "YBR_RCT": "RGB", "YBR_ICT": "RGB"}
    jpegls = {**grey, "RGB": "RGB", "YBR_FULL": "RGB"}
    ljpeg = {**grey, "RGB": "RGB"}
    jpeg = {"MONOCHROME1": "MONOCHROME1", "MONOCHROME2": "MONOCHROME2"}
    assert {ts: dict(labels) for ts, labels in
            _FALLBACK_PHOTOMETRICS.items()} == {
        "1.2.840.10008.1.2.4.50": jpeg, "1.2.840.10008.1.2.4.51": jpeg,
        LJPEG: ljpeg, LJPEG_SV1: ljpeg,
        JPEGLS: jpegls, JPEGLS_NEAR: jpegls,
        J2K_LOSSLESS: j2k, J2K: j2k,
        # HTJ2K is JPEG 2000 to openjpeg, row for row (#459).
        "1.2.840.10008.1.2.4.201": j2k, "1.2.840.10008.1.2.4.202": j2k,
        "1.2.840.10008.1.2.4.203": j2k}


def _colour_ljpeg(ts, want, photometric="RGB"):
    """A 3-component JPEG Lossless stream, written by libjpeg-turbo.

    `imagecodecs.ljpeg_encode` refuses three components, which is why
    #416 concluded no colour JPEG Lossless stream could be built here.
    `jpeg8_encode(lossless=True)` writes one. Predictor 1 under .70,
    which allows only that one; 5 under .57, any other.
    """
    ds = _dataset(ts, [WIDE_MONO16], photometric=photometric)
    ds.SamplesPerPixel, ds.Columns = 3, want.shape[1]
    ds.Rows = want.shape[0]
    ds.PlanarConfiguration = 0
    ds.BitsAllocated = ds.BitsStored = want.dtype.itemsize * 8
    ds.HighBit = ds.BitsStored - 1
    ds.PixelData = encapsulate([imagecodecs.jpeg8_encode(
        want, lossless=True, predictor=1 if ts == LJPEG_SV1 else 5,
        bitspersample=ds.BitsStored)], has_bot=True)
    ds["PixelData"].is_undefined_length = True
    return ds


@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1])
@pytest.mark.parametrize("want", [RGB8, RGB16["uint16"]],
                         ids=["uint8", "uint16"])
def test_a_colour_jpeg_lossless_file_ingests_exactly(ingest, ts, want):
    """Y5 (was F13's refusal): RGB under JPEG Lossless, measured (#387).

    F13 refused it, on the premise that no colour JPEG Lossless stream
    could be built here to measure a decode against. That premise was
    false: this stream is one. It decodes exactly at 8 and 16 bits, at
    imagecodecs 2024.6.1 and 2026.8.16, on 3.12 and 3.14t. Every sample
    in `RGB8` and `RGB16` differs from its neighbours in the pixel, so a
    plane swap or a planar/interleaved mix-up after the decode would not
    compare equal.
    """
    session, summary, _db = ingest(_colour_ljpeg(ts, want))
    assert (summary.ingested, summary.failures) == (1, [])
    inst, got = _stored(session)
    assert got.dtype == want.dtype
    assert got.shape == want.shape
    assert got.tolist() == want.tolist()
    assert inst.attributes["0028,0004"] == "RGB"


@pytest.mark.parametrize("ts", [LJPEG, LJPEG_SV1])
def test_a_ybr_jpeg_lossless_file_is_refused_naming_space_and_syntax(
        ingest, ts):
    """F13, what stays: YBR under JPEG Lossless is still refused.

    No YBR JPEG Lossless stream was measured. JPEG Lossless has no colour
    transform of its own, so a decoder would hand back the stored YBR
    samples, and whether this fallback should convert them is a decision
    nobody has measured for this syntax. Refused before the decode,
    naming the declared space and the syntax.
    """
    _session, summary, _db = ingest(_colour_ljpeg(ts, RGB8,
                                                  photometric="YBR_FULL"))
    assert summary.ingested == 0
    why = _fallback_reason(summary)
    assert "'YBR_FULL'" in why, why
    assert ts in why, why


#: 8-bit YBR_FULL samples of `RGB8`, and what pydicom's own conversion
#: makes of them: the bytes pydicom's door stores for this file with
#: pyjpegls installed (measured), and the reference Y2 compares with.
YBR8 = convert_color_space(RGB8, "RGB", "YBR_FULL")
YBR8_AS_RGB = convert_color_space(YBR8, "YBR_FULL", "RGB")


@pytest.mark.parametrize("ts", [JPEGLS, JPEGLS_NEAR])
def test_an_8_bit_ybr_full_jpeg_ls_frame_is_stored_as_rgb(ingest, ts):
    """Y2: converted and relabelled, as pydicom's door does (#448, Q2).

    A JPEG-LS stream carries no colour transform, and `jpegls_decode`
    returns the YBR samples as stored. pydicom with pyjpegls converts them
    with `convert_color_space` and labels the result RGB, and so does the
    native door since #372; this stores the same bytes under the same
    label. Owner question Q2, answered with the recommendation pending
    confirmation. Near-lossless is encoded at NEAR 0, so exact.
    """
    ds = _dataset(ts, [YBR8], photometric="YBR_FULL")
    session, summary, _db = ingest(ds)
    assert (summary.ingested, summary.failures) == (1, [])
    inst, got = _stored(session)
    assert got.dtype == np.dtype("uint8")
    assert got.tolist() == YBR8_AS_RGB.tolist()
    assert int(np.abs(got.astype(int) - RGB8.astype(int)).max()) <= 2
    assert inst.attributes["0028,0004"] == "RGB"


def test_a_16_bit_ybr_full_jpeg_ls_frame_is_refused_naming_its_depth(ingest):
    """Y3: pydicom cannot convert 16-bit YBR either, and neither does this.

    `convert_color_space` refuses `uint16` ("Invalid ndarray.dtype 'uint16'
    for color space conversion"), at pydicom's door as here. Refused before
    the decode, in this library's words, naming the space and the depth.
    """
    _session, summary, _db = ingest(_dataset(JPEGLS, [RGB16["uint16"]],
                                             photometric="YBR_FULL"))
    assert summary.ingested == 0
    why = _fallback_reason(summary)
    assert "'YBR_FULL'" in why, why
    assert "16-bit" in why, why


@pytest.mark.parametrize("ts", [JPEGLS, JPEGLS_NEAR])
@pytest.mark.parametrize("want", [RGB8, RGB16["uint16"]],
                         ids=["uint8", "uint16"])
def test_a_colour_jpeg_ls_file_ingests_bit_exactly(ingest, ts, want):
    """RGB stays open under JPEG-LS, where it is measured exact.

    `jpegls_decode` returns the samples interleaved, `(rows, cols, 3)`,
    matching PlanarConfiguration 0.
    """
    session, summary, _db = ingest(_dataset(ts, [want]))
    assert (summary.ingested, summary.failures) == (1, [])
    inst, got = _stored(session)
    assert got.dtype == want.dtype
    assert got.tolist() == want.tolist()
    assert inst.attributes["0028,0004"] == "RGB"


# ---------------------------------------------------------------------------
# #444 -- an unavailable imagecodecs says why, at both doors
# ---------------------------------------------------------------------------

#: A broken wheel's shape: installed, with a shared library missing.
_BROKEN_IMPORT = ImportError(
    "libjpeg.so.8: cannot open shared object file: No such file or directory")


def _without_imagecodecs(monkeypatch):
    from isocenter import imagecodecs_handler
    monkeypatch.setattr(imagecodecs_handler, "imagecodecs", None)
    monkeypatch.setattr(imagecodecs_handler, "IMPORT_ERROR", _BROKEN_IMPORT)


def test_ingest_names_why_imagecodecs_is_unavailable(tmp_path, monkeypatch):
    """I2: the ingest row carries the import failure's own words.

    In-process through `ingest_worker`, the function that writes the
    row: a Session hands ingest to its own process pool, which a
    monkeypatch does not reach.
    """
    from isocenter.io_handlers import ingest_worker
    path = _write(str(tmp_path), _dataset(LJPEG_SV1, [MONO16],
                                          photometric="MONOCHROME2"))
    _assert_pydicom_cannot(path)
    _without_imagecodecs(monkeypatch)
    reason = ingest_worker(path)[-1]
    assert reason.startswith("Decompression Failed:"), reason
    assert "imagecodecs is not available: ImportError" in reason, reason
    assert "libjpeg.so.8" in reason, reason


def test_the_lazy_load_error_carries_the_true_reason(tmp_path):
    """I4: the read door's second raise says why, with no second decoder (#444).

    `Instance.get_pixel_data()` has two final raises, and I3 reaches only
    the "Failed to decompress" one. This file reaches the other: its
    offset table names one frame where NumberOfFrames declares two, and
    PlanarConfiguration is absent too. The door's own frame-count check
    refuses before any decode, and the error is `Lazy load failed`.

    Until #453 that refusal went on to the handler, which refused the
    same table again, and #444 added an `imagecodecs fallback:` line so
    the handler's reason -- the only true one, since pydicom's said
    nothing about frames -- was not lost. The door no longer asks a
    second decoder, so the reason is the message itself: exactly the
    table's words, with nothing about imagecodecs beside them. The tail
    is asserted, not the prefix, which names the instance.
    Found in review of #463.
    """
    from isocenter.entities import Instance
    ds = _dataset(J2K_LOSSLESS, [RGB16["uint16"]], planar=None,
                  number_of_frames=2)
    path = _write(str(tmp_path), ds)
    _assert_pydicom_cannot(path, expect=AttributeError)
    inst = Instance(generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                    file_path=path)
    with pytest.raises(RuntimeError) as exc:
        inst.get_pixel_data()
    msg = str(exc.value)
    assert msg.startswith("Lazy load failed"), msg
    assert msg.endswith(": Basic Offset Table names 1 frames; "
                        "NumberOfFrames declares 2"), msg
    assert "imagecodecs" not in msg, msg


def test_the_read_door_names_why_imagecodecs_is_unavailable(tmp_path,
                                                            monkeypatch):
    """I3: `Instance.get_pixel_data()` says why, and keeps its advice.

    Its fallback tested `is_available()` first, so a missing imagecodecs
    was never asked and the caller got pydicom's error alone; and when
    the handler was asked and refused, the refusal was dropped for
    pydicom's. The advice line is pinned elsewhere
    (`tests/test_compression_deps.py`) and must survive the addition.
    """
    from isocenter.entities import Instance
    path = _write(str(tmp_path), _dataset(LJPEG_SV1, [MONO16],
                                          photometric="MONOCHROME2"))
    _assert_pydicom_cannot(path)
    _without_imagecodecs(monkeypatch)
    inst = Instance(generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                    file_path=path)
    with pytest.raises(RuntimeError) as exc:
        inst.get_pixel_data()
    msg = str(exc.value)
    # The fallback's own framing (#453): the door decodes through
    # `_decode_pixels`, whose refusal carries the handler's words as the
    # reason imagecodecs could not decode the file either. It read
    # `imagecodecs fallback: ...` when the door asked the handler itself.
    assert ("imagecodecs could not decode it either: RuntimeError: "
            "imagecodecs is not available: ImportError") in msg, msg
    assert "libjpeg.so.8" in msg, msg
    assert "Missing image codecs" in msg, msg

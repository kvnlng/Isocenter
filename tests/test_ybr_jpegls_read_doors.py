"""8-bit YBR_FULL JPEG-LS reads as RGB at every door, labelled RGB (#464).

A JPEG-LS stream carries no colour transform, and `jpegls_decode` returns
the YBR samples exactly as stored. Since #448, `ingest()` converts them
with pydicom's `convert_color_space` and stores RGB under an RGB label.
The two read doors -- `Instance.get_pixel_data()` from a file and
`imagecodecs_handler.get_pixel_data()` -- returned the unconverted YBR
samples. So one file had two answers.

The owner's ruling (2026-09-11) is to **convert and relabel**. The
handler converts, as ingest does, and the decode says RGB (the handler
rewrote the dataset's PhotometricInterpretation until #453 deleted its
`get_pixel_data`; `_decode_pixels` returns the label instead). `Instance.get_pixel_data()` rewrites
the instance's label to match. Converting without relabelling is #372's
defect: RGB bytes under a YBR label. That must not ship at a new door,
so every conversion assertion here sits beside a label assertion.

A **signed** 8-bit YBR_FULL file is refused at every door, before the
decode. `convert_color_space` has no `int8` path. YBR_FULL's chroma is
defined with a +128 offset over unsigned samples, so there is no signed
layout a conversion could honour. pydicom refuses the file at both its
doors (pyjpegls, and native). Before this change ingest refused it in
`convert_color_space`'s words, and the read doors returned `int8` YBR.

A **16-bit** YBR_FULL file is refused at every door, naming its depth
(#461's ruling, Q5; Y3 in `test_ingest_imagecodecs_fallback.py`). #464
left the read doors returning its samples as stored; since #453 the
Instance door decodes through ingest's own `_decode_pixels`.

Every fixture first asserts that pydicom cannot decode it here, so no
case passes through pydicom's door and tells us nothing about this one.
"""
import os

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.pixels import convert_color_space
from pydicom.uid import generate_uid

from isocenter import imagecodecs_handler
from isocenter.entities import Instance
from isocenter.io_handlers import (_FALLBACK_DECODER_CONVERTS,
                                   _FALLBACK_PHOTOMETRICS)
from isocenter.session import DicomSession
from support.decode_doors import through_the_fallback

JPEGLS = "1.2.840.10008.1.2.4.80"
JPEGLS_NEAR = "1.2.840.10008.1.2.4.81"
SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"

#: Every sample differs from its neighbours in the pixel, so a plane swap
#: or a partial conversion would not compare equal.
RGB8 = (np.arange(48, dtype=np.int64) * 5).astype(np.uint8).reshape(4, 4, 3)
#: The YBR_FULL samples a file stores for `RGB8`, and pydicom's own
#: conversion of them back: the bytes ingest stores, and pydicom with
#: pyjpegls returns, for this file (measured).
YBR8 = convert_color_space(RGB8, "RGB", "YBR_FULL")
YBR8_AS_RGB = convert_color_space(YBR8, "YBR_FULL", "RGB")
#: A second frame, so a multi-frame read that converted frame 0 alone
#: would show.
YBR8_B = convert_color_space(RGB8[::-1].copy(), "RGB", "YBR_FULL")
YBR8_B_AS_RGB = convert_color_space(YBR8_B, "YBR_FULL", "RGB")
RGB16 = (np.arange(48, dtype=np.int64) * 1000 + 7).astype(
    np.uint16).reshape(4, 4, 3)


def _dataset(ts, frames, *, photometric="YBR_FULL", bits=8,
             pixel_representation=0):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SOP_CLASS
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ts
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT464", "DOE^JANE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = SOP_CLASS
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows, ds.Columns = frames[0].shape[:2]
    ds.SamplesPerPixel, ds.PlanarConfiguration = 3, 0
    ds.PhotometricInterpretation = photometric
    ds.BitsAllocated = ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = pixel_representation
    if len(frames) > 1:
        ds.NumberOfFrames = len(frames)
    ds.PixelData = encapsulate(
        [imagecodecs.jpegls_encode(f) for f in frames], has_bot=True)
    ds["PixelData"].is_undefined_length = True
    return ds


def _instance(path, label=None):
    inst = Instance(generate_uid(), SOP_CLASS, 1, file_path=path)
    if label is not None:
        inst.attributes["0028,0004"] = label
    return inst


def _read(read):
    try:
        return read()
    except Exception as exc:  # pylint: disable=broad-except
        return exc


@pytest.fixture
def doors(tmp_path):
    """Write `ds`; return what each door makes of it, and the labels.

    `ingest` is `(ingested, failures, stored, label)`. `stored` is the
    store's array after `unload_pixel_data()` -- the sidecar's answer --
    and `label` is the instance's PhotometricInterpretation. For
    `_decode_pixels`, `(array, label)` or the exception: the column the
    handler's `get_pixel_data` held until #453 deleted it (Q10). For the Instance door, twice: a bare instance, which
    carries no label, and one labelled as the file is, as a hand-built
    graph would be. Each comes with its label and how far its revision
    moved.
    """
    sessions = []

    def _run(ds, name="one"):
        src = tmp_path / f"src_{name}"
        os.makedirs(src)
        path = str(src / "one.dcm")
        ds.save_as(path, enforce_file_format=True)
        with pytest.raises(RuntimeError):
            _ = pydicom.dcmread(path).pixel_array
        session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
        sessions.append(session)
        summary = session.ingest(str(src))
        stored = label = None
        if summary.ingested:
            inst = [i for p in session.store.patients for st in p.studies
                    for se in st.series for i in se.instances][0]
            assert inst.unload_pixel_data() is True
            stored = inst.get_pixel_data()
            label = inst.attributes.get("0028,0004")
        out = {"ingest": (summary.ingested, summary.failures, stored, label)}
        out["decode_pixels"] = _read(
            lambda: through_the_fallback(pydicom.dcmread(path)))
        for door, label in (("bare", None),
                            ("labelled", str(ds.PhotometricInterpretation))):
            inst = _instance(path, label)
            before = inst._revision  # pylint: disable=protected-access
            got = _read(inst.get_pixel_data)
            out[door] = (got, inst.attributes.get("0028,0004"),
                         inst._revision - before)  # pylint: disable=protected-access
        return out

    yield _run
    for session in sessions:
        session.close()


def _same(arr, want):
    return (isinstance(arr, np.ndarray) and arr.dtype == want.dtype
            and arr.shape == want.shape and arr.tolist() == want.tolist())


# ---------------------------------------------------------------------------
# R1 -- converted and relabelled, at all three doors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [JPEGLS, JPEGLS_NEAR])
@pytest.mark.parametrize("frames,want", [
    ([YBR8], YBR8_AS_RGB),
    ([YBR8, YBR8_B], np.stack([YBR8_AS_RGB, YBR8_B_AS_RGB])),
], ids=["one-frame", "two-frame"])
def test_an_8_bit_ybr_full_jpeg_ls_file_reads_as_rgb_at_every_door(
        doors, ts, frames, want):
    """R1: the same RGB bytes, under an RGB label, at every door.

    Before, ingest stored `want` labelled RGB. The handler and the
    Instance door returned the YBR samples, and the file's and the
    instance's labels still said YBR_FULL. Near-lossless is encoded at
    NEAR 0, so exact.

    The labelled instance's revision moves, because a new label is a
    change the store should hold. The bare instance carries no label, so
    nothing on it is false and nothing is written. The pydicom arm of the
    same door never adds a descriptor either.
    """
    assert not np.array_equal(YBR8, YBR8_AS_RGB)
    got = doors(_dataset(ts, frames))
    ingested, failures, stored, label = got["ingest"]
    assert (ingested, failures) == (1, [])
    assert _same(stored, want), stored
    assert label == "RGB"
    arr, decoded_label = got["decode_pixels"]
    assert _same(arr, want), arr
    assert decoded_label == "RGB"
    arr, inst_label, moved = got["labelled"]
    assert _same(arr, want), arr
    assert (inst_label, moved) == ("RGB", 1)
    arr, inst_label, moved = got["bare"]
    assert _same(arr, want), arr
    assert (inst_label, moved) == (None, 0)


def test_an_instance_label_is_rewritten_only_when_the_handler_converted(
        tmp_path):
    """R2: the Instance door relabels from the conversion, not from the file.

    An RGB JPEG-LS file under an instance a hand-built graph labelled
    YBR_FULL. The handler converts nothing here, so the samples come back
    as stored. The door has made no statement about colour, so it leaves
    the instance's label (wrong, but not the door's) and its revision
    alone. A door that copied the file's label over any label that
    differed would write RGB here.
    """
    src = tmp_path / "src"
    os.makedirs(src)
    path = str(src / "one.dcm")
    _dataset(JPEGLS, [RGB8], photometric="RGB").save_as(
        path, enforce_file_format=True)
    inst = _instance(path, "YBR_FULL")
    before = inst._revision  # pylint: disable=protected-access
    assert _same(inst.get_pixel_data(), RGB8)
    assert inst.attributes["0028,0004"] == "YBR_FULL"
    assert inst._revision == before  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# R3 -- signed 8-bit YBR_FULL: refused at every door, before the decode
# ---------------------------------------------------------------------------

def test_a_signed_8_bit_ybr_full_jpeg_ls_file_is_refused_at_every_door(doors):
    """R3: one refusal, in one set of words, at all three doors.

    Before, ingest refused it in `convert_color_space`'s own words
    (`Invalid ndarray.dtype 'int8' for color space conversion`), and
    both read doors returned `int8` YBR samples. Now the handler refuses
    before it decodes. Ingest reaches the same check through
    `_decode_with_imagecodecs`, and so does the Instance door, which
    decodes through ingest's own `_decode_pixels` since #453. Neither
    label moves.
    """
    got = doors(_dataset(JPEGLS, [YBR8], pixel_representation=1))
    ingested, failures, _stored, _label = got["ingest"]
    assert ingested == 0
    words = "its declared colour space 'YBR_FULL' is signed 8-bit"
    # Ingest's framing is the one the CHANGELOG quotes: the handler's
    # refusal reaches the row as the reason imagecodecs could not decode
    # it either, not as a bare error escaping `_decode_with_imagecodecs`.
    assert f"imagecodecs could not decode it either: {words}" \
        in failures[0][1], failures
    exc = got["decode_pixels"]
    assert isinstance(exc, RuntimeError), exc
    assert f"imagecodecs could not decode it either: {words}" in str(exc), \
        str(exc)
    for door, label in (("bare", None), ("labelled", "YBR_FULL")):
        exc, inst_label, moved = got[door]
        assert isinstance(exc, RuntimeError), f"{door}: {exc!r}"
        # Ingest's framing, since the door decodes through
        # `_decode_pixels` (#453); it was `imagecodecs fallback: <words>`.
        assert f"imagecodecs could not decode it either: {words}" \
            in str(exc), str(exc)
        assert (inst_label, moved) == (label, 0)


# ---------------------------------------------------------------------------
# R7 -- a decode that fails leaves every label where it was
# ---------------------------------------------------------------------------

def test_a_truncated_8_bit_ybr_full_jpeg_ls_file_changes_no_label(doors):
    """R7: the relabel follows a conversion that happened, never precedes it.

    #372's defect was a label without its conversion. Here the stream is
    cut two bytes short, so CharLS reads the header and then fails the
    decode. A door that relabelled before decoding would leave `RGB` on
    an instance whose samples were never converted. The labelled
    instance's label and its revision stay put. (`_decode_pixels` never
    writes to the dataset it is given, so the handler's dataset-label
    half of this test went with the handler's `get_pixel_data`, #453.)
    """
    ds = _dataset(JPEGLS, [YBR8])
    whole = imagecodecs.jpegls_encode(YBR8)
    ds.PixelData = encapsulate([whole[:-2]], has_bot=True)
    ds["PixelData"].is_undefined_length = True
    got = doors(ds)
    assert got["ingest"][0] == 0, got["ingest"]
    exc = got["decode_pixels"]
    assert isinstance(exc, RuntimeError), exc
    assert "imagecodecs could not decode it either" in str(exc), str(exc)
    for door, label in (("bare", None), ("labelled", "YBR_FULL")):
        exc, inst_label, moved = got[door]
        assert isinstance(exc, RuntimeError), f"{door}: {exc!r}"
        assert (inst_label, moved) == (label, 0)


# ---------------------------------------------------------------------------
# R4 -- 16-bit YBR_FULL: refused at the read door too (#461, Q5)
# ---------------------------------------------------------------------------

def test_a_16_bit_ybr_full_jpeg_ls_file_is_refused_at_the_read_door_too(
        doors):
    """R4: #464 converts unsigned 8-bit only; #461 recorded 16-bit as a limit.

    `convert_color_space` refuses `uint16`, and pydicom refuses the native
    form as well, so there is no reference conversion to agree with. #464
    left the read doors returning the samples as stored, under the file's
    YBR_FULL label, while ingest refused the file naming its depth. #461's
    ruling (Q5) is to refuse at every door, and since #453 the Instance
    door decodes through ingest's own `_decode_pixels`, so it refuses in
    ingest's words and its label does not move. The handler column is
    gone: `imagecodecs_handler.get_pixel_data` is no longer a door.
    """
    got = doors(_dataset(JPEGLS, [RGB16], bits=16))
    ingested, failures, _stored, _label = got["ingest"]
    assert ingested == 0
    words = ("its declared colour space 'YBR_FULL' is 16-bit, and the "
             "conversion to RGB this fallback would make")
    assert words in failures[0][1], failures
    for door, label in (("bare", None), ("labelled", "YBR_FULL")):
        exc, inst_label, moved = got[door]
        assert isinstance(exc, RuntimeError), f"{door}: {exc!r}"
        assert words in str(exc), str(exc)
        assert (inst_label, moved) == (label, 0)


# ---------------------------------------------------------------------------
# R5 -- one rule: the handler's conversions are the ingest table's
# ---------------------------------------------------------------------------

def test_the_handler_converts_exactly_the_relabels_ingest_leaves_to_it():
    """R5: `_FALLBACK_PHOTOMETRICS` and the handler cannot drift apart.

    Ingest's table says which declared label is stored under which. A
    relabel under a syntax whose decoder has already converted
    (`_FALLBACK_DECODER_CONVERTS`, JPEG 2000) is a change of label only.
    Every other relabel is a conversion, and the handler makes it, for
    ingest and for both read doors alike. So the table's conversion rows
    and the handler's own table must be the same set. A row added to
    either one alone would be a conversion made at one door and not at
    the other, which is this issue again.
    """
    from_table = {ts: {declared: stored
                       for declared, stored in labels.items()
                       if declared != stored}
                  for ts, labels in _FALLBACK_PHOTOMETRICS.items()
                  if ts not in _FALLBACK_DECODER_CONVERTS}
    from_table = {ts: rows for ts, rows in from_table.items() if rows}
    assert from_table == {
        ts: dict(rows)
        for ts, rows in imagecodecs_handler.CONVERTS_TO.items()}


# ---------------------------------------------------------------------------
# R6 -- a set landing during the fallback read keeps its pixels and label
# ---------------------------------------------------------------------------

def test_a_set_landing_during_the_fallback_read_keeps_its_pixels_and_label(
        tmp_path, monkeypatch):
    """R6: the fallback arm publishes, and relabels, only into an empty slot.

    `get_pixel_data()` loads with no lock held and publishes under the
    pixel-state lock, only while the array is still absent; otherwise it
    returns what is resident (#465). The relabel is made in the same
    hold, on the publishing branch only. Here the set is a grey frame, so
    it corrects the label to MONOCHROME2 and SamplesPerPixel to 1: a
    relabel made anyway would write RGB beside a SamplesPerPixel of 1, a
    label no array can carry, and a publish made anyway would put the
    stale RGB frame over the set's pixels and mark them written.

    The set is injected inside the decode call, which is the interleaving
    a second thread produces, made deterministic (#465's `SetDuringRead`
    shape). The decode is `io_handlers._decode_pixels` since #453, which
    the door imports at call time, so that is where it is patched; it was
    `imagecodecs_handler.get_pixel_data`.
    """
    src = tmp_path / "src"
    os.makedirs(src)
    path = str(src / "one.dcm")
    _dataset(JPEGLS, [YBR8]).save_as(path, enforce_file_format=True)
    inst = _instance(path, "YBR_FULL")
    from isocenter import io_handlers
    real = io_handlers._decode_pixels  # pylint: disable=protected-access

    grey = np.zeros((4, 4), dtype=np.uint8)
    fired = []

    def set_during_read(ds, **kwargs):
        got = real(ds, **kwargs)
        fired.append(1)
        inst.set_pixel_data(grey)
        return got

    monkeypatch.setattr(io_handlers, "_decode_pixels", set_during_read)
    got = inst.get_pixel_data()
    assert fired == [1], "the fallback arm was never reached"
    assert inst.attributes["0028,0004"] == "MONOCHROME2"
    assert int(inst.attributes["0028,0002"]) == 1
    assert got is inst.pixel_array
    assert got.shape == grey.shape and np.array_equal(got, grey)
    assert inst._pixel_array_unwritten, "the set's pixels were marked written"

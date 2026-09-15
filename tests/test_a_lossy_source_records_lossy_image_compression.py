"""A lossy source that does not declare it has LossyImageCompression recorded
at ingest (#601).

(0028,2110) is Type 3 in the General Image Module. PS3.3 C.7.6.1.1.5 gives
it two values, `00` and `01`; `01` "conveys that the Image has undergone
lossy compression", and "once this value has been set to 01 it shall not
be reset". **Nothing requires it under a lossy transfer syntax**: the v0.9.8
triage read PS3.5 8.2 as saying `01` shall be set there, and PS3.5 8.2.3 and
8.2.4 never mention the element. A cohort that omits it is conformant.

What made the omission a defect here is that the transfer syntax was the
only record of the loss, and an export replaces it: measured on ce5b2b2,
JPEG-LS near-lossless (`JPEGLSNearLossless_16`, NEAR 2), HTJ2K with the
irreversible wavelet (`HTJ2K_08_RGB`), a built `.91` irreversible
codestream and a JPEG Baseline file stripped of the element all exported
natively and under JPEG 2000 Lossless with no 2110, graded PASS, and wrote
no row; a JPEG Baseline file declaring `00` exported `00`.

Ingest now stamps `01` when **the pixel data proves it** -- a JPEG frame
header of a DCT process (SOF0, 1, 2, 5, 6, 9, 10, 13 or 14) under JPEG
Baseline or Extended, a JPEG-LS scan with NEAR above 0, or a JPEG 2000
codestream using the 9-7 irreversible wavelet -- and writes a WARNING
naming the evidence (owner ruling Q1: kept for an absent value and for a
declared `00`). A syntax alone is not evidence: a NEAR 0 JPEG-LS stream, a
reversible JPEG 2000 codestream and a lossless (SOF3) JPEG frame are
bit-exact whatever their syntax is called (built and measured), and a
false `01` can never be withdrawn. Review J2 M1 found the JPEG arm taking
`.50`/`.51` as evidence by syntax alone, which stamped a lossless SOF3
frame.
"""
import logging
import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate, generate_frames
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid

from isocenter.session import DicomSession

#: The fixtures, resolved at import so a missing one fails loudly.
JPEGLS_NEAR_16 = get_testdata_file("JPEGLSNearLossless_16.dcm")
JLSN_RGB_ILV0 = get_testdata_file("JLSN_RGB_ILV0.dcm")
HTJ2K_08_RGB = get_testdata_file("HTJ2K_08_RGB.dcm")
SC_RGB_JPEG_DCMTK = get_testdata_file("SC_rgb_jpeg_dcmtk.dcm")
JPEG_LOSSY = get_testdata_file("JPEG-lossy.dcm")
J2KI_693 = get_testdata_file("693_J2KI.dcm")
MR_SMALL = get_testdata_file("MR_small.dcm")
MR_SMALL_JLS = get_testdata_file("MR_small_jpeg_ls_lossless.dcm")
JPEG_LL = get_testdata_file("JPEG-LL.dcm")
CT_SMALL = get_testdata_file("CT_small.dcm")
FIXTURES = (JPEGLS_NEAR_16, JLSN_RGB_ILV0, HTJ2K_08_RGB, SC_RGB_JPEG_DCMTK,
            JPEG_LOSSY, J2KI_693, MR_SMALL, MR_SMALL_JLS, JPEG_LL, CT_SMALL)
assert all(FIXTURES), FIXTURES

JPEGLS_NEAR = "1.2.840.10008.1.2.4.81"
JPEGLS_LOSSLESS = "1.2.840.10008.1.2.4.80"
JPEG_BASELINE = "1.2.840.10008.1.2.4.50"
JPEG_EXTENDED = "1.2.840.10008.1.2.4.51"
J2K = "1.2.840.10008.1.2.4.91"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"
LEAD = "LossyImageCompression (0028,2110) "
TAIL = (". Recorded as 01 at ingest, so an export carries it (PS3.3 "
        "C.7.6.1.1.5: 01 conveys that the image has undergone lossy "
        "compression, and once set it shall not be reset). The samples are "
        "unchanged.")


def _mr16():
    return pydicom.dcmread(MR_SMALL).pixel_array.astype(np.uint16)


def _save(folder, ds, name="in.dcm"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(str(folder), name)
    ds.save_as(path, enforce_file_format=True)
    return path


def _fixture(path, *, declared="keep"):
    """A pydicom fixture, with its 0028,2110 kept, deleted or replaced."""
    # `force`, and identity filled in: JLSN_RGB_ILV0 carries no preamble,
    # no SOP Instance UID and a partial File Meta Information group.
    ds = pydicom.dcmread(path, force=True)
    if "SOPInstanceUID" not in ds:
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
        ds.SOPInstanceUID = generate_uid()
        ds.Modality = "OT"
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    if declared != "keep":
        if "LossyImageCompression" in ds:
            del ds.LossyImageCompression
        if declared is not None:
            ds.LossyImageCompression = declared
    return ds


def _built(ts, codestream, **attrs):
    """MR_small's header over one encapsulated `codestream` under `ts`."""
    ds = pydicom.dcmread(MR_SMALL)
    ds.file_meta.TransferSyntaxUID = ts
    ds.PixelData = encapsulate([codestream])
    ds["PixelData"].is_undefined_length = True
    ds.PixelRepresentation = 0
    for keyword, value in attrs.items():
        setattr(ds, keyword, value)
    return ds


def _near(level):
    return _built(JPEGLS_NEAR, imagecodecs.jpegls_encode(_mr16(), level=level))


def _j2k91(reversible):
    kwargs = {} if reversible else {"level": 40, "reversible": False}
    if reversible:
        kwargs["level"] = 0
    return _built(J2K, imagecodecs.jpeg2k_encode(_mr16(), codecformat="J2K",
                                                 **kwargs))


def _lossy_rows(db):
    with sqlite3.connect(db) as conn:
        return [(a, d) for a, d in conn.execute(
            "SELECT action_type, details FROM audit_log "
            "WHERE details LIKE 'LossyImageCompression (0028,2110)%'")]


def _only(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _run(tmp_path, ds, *, compression=False, name="s", pixels=False):
    """Ingest `ds`, export it, report. Returns a dict of what was seen.

    `pixels`: also return the stored frame, read before the export.
    """
    src = tmp_path / f"src_{name}"
    _save(src, ds)
    db = str(tmp_path / f"{name}.db")
    out = tmp_path / f"out_{name}"
    report = tmp_path / f"{name}.md"
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(str(src))
        instances = _only(session)
        graph = instances[0].attributes.get("0028,2110") if instances else None
        array = (np.array(instances[0].get_pixel_data())
                 if pixels and instances else None)
        session.export(str(out), use_compression=compression,
                       show_progress=False)
        session.generate_report(str(report))
    written = [os.path.join(r, f) for r, _d, fs in os.walk(str(out))
               for f in fs if f.endswith(".dcm")]
    file_value = (pydicom.dcmread(written[0]).get("LossyImageCompression")
                  if written else None)
    grade = [line for line in report.read_text(encoding="utf-8").splitlines()
             if "Validation Status" in line]
    return {"summary": summary, "graph": graph, "file": file_value,
            "rows": _lossy_rows(db), "grade": grade, "db": db,
            "written": written, "array": array}


# ---------------------------------------------------------------------------
# L1: a lossy source without 01 is recorded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [False, True],
                         ids=["native", "j2k"])
@pytest.mark.parametrize("build, lead, evidence", [
    (lambda: _fixture(JPEGLS_NEAR_16), "is absent",
     f"its JPEG-LS scan declares NEAR 2, where 0 is lossless ({JPEGLS_NEAR})"),
    (lambda: _fixture(JLSN_RGB_ILV0), "is absent",
     f"its JPEG-LS scan declares NEAR 3, where 0 is lossless ({JPEGLS_NEAR})"),
    (lambda: _fixture(HTJ2K_08_RGB), "is absent",
     "its JPEG 2000 codestream uses the irreversible 9-7 wavelet "
     "(1.2.840.10008.1.2.4.203)"),
    (lambda: _j2k91(reversible=False), "is absent",
     f"its JPEG 2000 codestream uses the irreversible 9-7 wavelet ({J2K})"),
    (lambda: _built(JPEGLS_LOSSLESS,
                    imagecodecs.jpegls_encode(_mr16(), level=2)), "is absent",
     f"its JPEG-LS scan declares NEAR 2, where 0 is lossless "
     f"({JPEGLS_LOSSLESS})"),
    (lambda: _fixture(SC_RGB_JPEG_DCMTK, declared=None), "is absent",
     f"its JPEG frame header is SOF0, a DCT process, which is lossy by "
     f"definition ({JPEG_BASELINE})"),
    (lambda: _fixture(JPEG_LOSSY, declared=None), "is absent",
     f"its JPEG frame header is SOF1, a DCT process, which is lossy by "
     f"definition ({JPEG_EXTENDED})"),
    (lambda: _fixture(SC_RGB_JPEG_DCMTK, declared="00"), "declares '00'",
     f"its JPEG frame header is SOF0, a DCT process, which is lossy by "
     f"definition ({JPEG_BASELINE})"),
], ids=["jpegls-near2", "jpegls-near3-ilv0", "htj2k-irreversible",
        "j2k91-irreversible", "jpegls80-near2", "jpeg50-absent",
        "jpeg51-absent", "jpeg50-00"])
def test_a_lossy_source_without_01_is_recorded(tmp_path, build, lead,
                                               evidence, compression):
    """L1: `01` in the graph and the file, one WARNING naming the evidence.

    Killing mutations: (m18) the DCT arm deleted (the `.50`/`.51`
    parameters); (m20) the stamp deleted with the row kept (graph and
    file); (m24) `_first_frame` returning the undivided PixelData (the
    JPEG-LS and JPEG 2000 parameters); (r11) NEAR read only under `.81`
    (`jpegls80-near2`: a `.80` stream with NEAR 2 is lossy too, review J2
    F3).
    """
    got = _run(tmp_path, build(), compression=compression)

    assert got["summary"].ingested == 1, got["summary"]
    assert got["graph"] == "01"
    assert got["file"] == "01"
    assert got["rows"] == [
        ("WARNING", f"{LEAD}{lead}, and this file's pixel data is "
                    f"lossy-compressed: {evidence}{TAIL}")], got["rows"]
    assert got["grade"] == [
        "| **Validation Status** | **REVIEW_REQUIRED** |"], got["grade"]


# ---------------------------------------------------------------------------
# L2: a lossless stream under a lossy-named syntax claims nothing
# ---------------------------------------------------------------------------

def _rgb_native():
    """A native 8x8 RGB Secondary Capture, for this library's J2K export."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT601", "DOE^JANE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = 8
    ds.SamplesPerPixel, ds.PlanarConfiguration = 3, 0
    ds.PhotometricInterpretation = "RGB"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PixelData = (np.arange(192) % 251).astype(np.uint8).tobytes()
    return ds


@pytest.mark.parametrize("build", [
    lambda _tmp: _near(0),
    lambda _tmp: _j2k91(reversible=True),
    lambda _tmp: _fixture(MR_SMALL_JLS),
    lambda _tmp: _fixture(JPEG_LL),
    lambda _tmp: _fixture(CT_SMALL),
], ids=["jpegls-near0", "j2k91-reversible", "jpegls-lossless",
        "jpeg-lossless", "native"])
def test_a_lossless_stream_under_a_lossy_syntax_claims_nothing(tmp_path,
                                                             build):
    """L2: no stamp and no row, whatever the syntax is called.

    Green before #601; it pins the evidence rule. Killing mutations: (m16)
    `near > 0` read as `near >= 0` (the NEAR 0 stream); (m17) any COD taken
    as irreversible (the reversible `.91`).
    """
    ds = build(tmp_path)
    declared = ds.get("LossyImageCompression")
    got = _run(tmp_path, ds)

    assert got["summary"].ingested == 1, got["summary"]
    assert got["rows"] == [], got["rows"]
    assert got["graph"] == declared


@pytest.mark.parametrize("build", [lambda: _fixture(CT_SMALL), _rgb_native],
                         ids=["ct-small", "rgb"])
def test_this_librarys_own_compressed_export_claims_nothing(tmp_path, build):
    """L2b: `_compress_j2k` writes a reversible codestream, so re-ingesting
    this library's `use_compression=True` output stamps nothing.

    Measured, not assumed: the written frame's COD is read and must say
    5-3 reversible, or #601 would brand every compressed export lossy.
    """
    from isocenter.imagecodecs_handler import _j2k_irreversible
    first = _run(tmp_path, build(), compression=True, name="first")
    assert len(first["written"]) == 1, first["written"]
    exported = pydicom.dcmread(first["written"][0])
    assert str(exported.file_meta.TransferSyntaxUID) == J2K_LOSSLESS
    frame = next(generate_frames(exported.PixelData, number_of_frames=1))
    assert _j2k_irreversible(frame) is False

    again = _run(tmp_path, exported, name="again")
    assert again["rows"] == [], again["rows"]
    assert again["graph"] is None


@pytest.mark.parametrize("ts, bits", [(JPEG_BASELINE, 8), (JPEG_EXTENDED, 16)],
                         ids=["jpeg50-sof3-8bit", "jpeg51-sof3-16bit"])
def test_a_lossless_jpeg_frame_under_a_dct_syntax_claims_nothing(tmp_path, ts,
                                                                  bits):
    """L2c: a lossless (SOF3) frame labelled `.50` or `.51` is not stamped.

    Review J2 M1: the JPEG arm took the transfer syntax as the evidence
    and never opened the stream, so this frame -- decoded through the
    imagecodecs fallback, bit-exact -- was stamped `01` in the graph and
    the export, with a row saying its pixel data is lossy-compressed,
    and graded REVIEW_REQUIRED. The frame header is the evidence: SOF3
    is process 14, lossless (ITU-T T.81 Table B.1). Killing mutation
    (m29): every JPEG frame type taken as DCT.
    """
    source = _mr16() if bits == 16 else (_mr16() >> 4).astype(np.uint8)
    ds = _built(ts, imagecodecs.ljpeg_encode(source), BitsStored=bits,
                BitsAllocated=bits, HighBit=bits - 1)
    got = _run(tmp_path, ds, pixels=True)

    assert got["summary"].ingested == 1, got["summary"]
    assert np.array_equal(got["array"].reshape(source.shape), source)
    assert got["rows"] == [], got["rows"]
    assert got["graph"] is None
    assert got["file"] is None
    assert got["grade"] == ["| **Validation Status** | **PASS** |"], got["grade"]


def test_a_dct_syntax_is_read_by_its_frame_header():
    """L2d: the evidence is the first frame header, and no header is none.

    A SOF0 and a SOF1 frame are evidence; a SOF3 frame, a scan with no
    frame header before it, an SOI and nothing more, and a frame that is
    not JPEG at all are not. Killing mutations: (m29) any frame type taken as DCT; (m30)
    an absent frame header taken as DCT.
    """
    from isocenter.io_handlers import _lossy_compression_evidence
    grey8 = (_mr16() >> 4).astype(np.uint8)
    sof0 = imagecodecs.jpeg8_encode(grey8, level=90)
    sof1 = imagecodecs.jpeg8_encode(_mr16() & 0xFFF, level=90,
                                    bitspersample=12)
    sof3 = imagecodecs.ljpeg_encode(grey8)

    def evidence(ts, frame):
        return _lossy_compression_evidence(_built(ts, bytes(frame)))

    assert evidence(JPEG_BASELINE, sof0) == {
        "declared": None, "syntax": JPEG_BASELINE, "evidence": "dct",
        "value": 0}
    assert evidence(JPEG_EXTENDED, sof1)["value"] == 1
    assert evidence(JPEG_BASELINE, sof3) is None
    assert evidence(JPEG_EXTENDED, sof3) is None
    no_header = b"\xff\xd8" + _seg(0xDA, bytes([1, 1, 0, 0, 63, 0])) + b"\x00"
    assert evidence(JPEG_BASELINE, no_header) is None
    assert evidence(JPEG_BASELINE, b"\xff\xd8") is None
    assert evidence(JPEG_EXTENDED, b"\x00\x00\x00\x00") is None


#: T.81 Table B.1: the DCT processes, and the lossless ones. C4, C8 and CC
#: sit in the same range and are not frame headers (L2e covers those).
_DCT_SOFS = (0, 1, 2, 5, 6, 9, 10, 13, 14)
_LOSSLESS_SOFS = (3, 7, 11, 15)


@pytest.mark.parametrize("ts", [JPEG_BASELINE, JPEG_EXTENDED],
                         ids=["jpeg50", "jpeg51"])
@pytest.mark.parametrize("n", _DCT_SOFS + _LOSSLESS_SOFS,
                         ids=[f"sof{n}" for n in _DCT_SOFS + _LOSSLESS_SOFS])
def test_every_frame_process_is_read_by_its_own_header(ts, n):
    """L2d, per SOFn: each documented DCT process is evidence, and no lossless one is.

    L2d reaches SOF0 and SOF1 through a real encoder. This walks every
    frame type in T.81 Table B.1 over the synthetic `SOI + SOFn + SOS`
    shape, because SOF2, SOF9 and SOF10 are streams libjpeg-turbo writes
    and pydicom decodes under `.50`/`.51`, while no installed encoder
    writes the rest. Killing mutations (review round 2): (n1) SOF2
    dropped from the DCT set; (n2) the frame-header range narrowed to
    `C0`-`C3`; (n6) SOF7, SOF11 and SOF15 taken as DCT.
    """
    from isocenter.io_handlers import _lossy_compression_evidence
    frame = (b"\xff\xd8"
             + _seg(0xC0 + n, bytes([8, 0, 4, 0, 4, 1, 1, 0x11, 0]))
             + _seg(0xDA, bytes([1, 1, 0, 1, 0, 0])))

    got = _lossy_compression_evidence(_built(ts, frame))

    if n in _DCT_SOFS:
        assert got == {"declared": None, "syntax": ts, "evidence": "dct",
                       "value": n}
    else:
        assert got is None


def test_jpeg_frame_type_walks_not_searches():
    """L2e: the SOFn of the first frame header, reached by walking.

    A COM payload holding `FF C0` ahead of a SOF3 header is where a search
    would read SOF0; DHT (`FF C4`), JPG (`FF C8`) and DAC (`FF CC`) sit in
    the `C0`-`CF` range and are not frame headers; fill bytes may precede a
    marker; a scan reached first ends the walk. Killing mutation (m31): a
    search for the first `FF Cn`.
    """
    from isocenter.imagecodecs_handler import _jpeg_frame_type
    soi = b"\xff\xd8"
    com = _seg(0xFE, b"\xff\xc0\x00\x0b")
    sof3 = _seg(0xC3, bytes([8, 0, 4, 0, 4, 1, 1, 0x11, 0]))
    sos = _seg(0xDA, bytes([1, 1, 0, 1, 0, 0]))

    assert _jpeg_frame_type(soi + com + sof3 + sos) == 3
    assert _jpeg_frame_type(soi + _seg(0xC4, bytes(4)) + _seg(0xCC, bytes(2))
                            + b"\xff\xff" + sof3 + sos) == 3
    assert _jpeg_frame_type(soi + _seg(0xC8, bytes(2)) + sof3) == 3
    assert _jpeg_frame_type(soi + sos + sof3) is None
    assert _jpeg_frame_type(soi + com) is None
    assert _jpeg_frame_type(b"\x00\x00" + sof3) is None
    grey8 = (_mr16() >> 4).astype(np.uint8)
    assert _jpeg_frame_type(imagecodecs.jpeg8_encode(grey8, level=90)) == 0
    assert _jpeg_frame_type(imagecodecs.ljpeg_encode(grey8)) == 3


# ---------------------------------------------------------------------------
# L3-L7
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [SC_RGB_JPEG_DCMTK, J2KI_693],
                         ids=["jpeg50", "j2k91-693"])
def test_a_declared_01_is_left_alone(tmp_path, path):
    """L3: `01` is already the record; no row. Killing mutation (m15): the
    `01` early return deleted."""
    got = _run(tmp_path, _fixture(path))

    assert got["rows"] == [], got["rows"]
    assert got["graph"] == "01"


def test_a_refused_decode_records_nothing(tmp_path):
    """L4: a NEAR 2 source pydicom refuses before any decode (no
    BitsAllocated) writes its ERROR row and nothing else."""
    ds = _near(2)
    del ds.BitsAllocated
    src = tmp_path / "src"
    _save(src, ds)
    db = str(tmp_path / "refused.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(str(src))
    assert summary.ingested == 0, summary
    assert len(summary.failures) == 1, summary
    assert _lossy_rows(db) == []


def _lossless_and_lossy(uid):
    """A NEAR 0 and a NEAR 2 dataset sharing one SOP Instance UID.

    Both carry DeviceSerialNumber `SN1` and a block of 200 in rows and
    columns 0-7, so a redaction rule on that serial changes the frame.
    """
    frame = _mr16()
    frame[0:8, 0:8] = 200
    out = []
    for level in (0, 2):
        ds = _built(JPEGLS_NEAR, imagecodecs.jpegls_encode(frame, level=level))
        ds.SOPInstanceUID = uid
        ds.file_meta.MediaStorageSOPInstanceUID = uid
        ds.DeviceSerialNumber = "SN1"
        out.append(ds)
    return out


def test_a_declined_duplicate_writes_no_lossy_row(tmp_path, monkeypatch):
    """L4b: the row is for an instance the store holds, not a file it declined.

    A lossless file and a lossy one with the same SOP Instance UID: the
    first in path order is kept, unstamped, and the second is declined
    with #431's WARNING. A lossy row for the declined file would be keyed
    to the kept instance's UID and say its pixel data is lossy -- false,
    and it bars PASS. `test_a_declined_duplicate_writes_no_high_bit_row` is
    the HighBit row's twin. Killing mutation (r3, review J2 F2): the row
    recorded above the two declined `continue`s.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    lossless, lossy = _lossless_and_lossy(generate_uid())
    src = tmp_path / "src"
    _save(src, lossless, name="a.dcm")
    _save(src, lossy, name="b.dcm")
    db = str(tmp_path / "dup.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(str(src))
        kept = _only(session)
    assert (summary.ingested, summary.declined) == (1, 1), summary
    assert len(kept) == 1 and "0028,2110" not in kept[0].attributes
    assert _lossy_rows(db) == []


def test_a_declined_superseded_source_writes_no_lossy_row(tmp_path,
                                                           monkeypatch):
    """L4c: the other declined branch, #238's un-redacted original.

    A lossless file is ingested and redacted, which gives the instance a
    new SOP Instance UID; a lossy file carrying the pre-redaction UID, at
    another path, is then declined. No lossy row: the file never entered
    the store. Killing mutation (r3): the row recorded above the
    superseded-source `continue`.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    uid = generate_uid()
    lossless, lossy = _lossless_and_lossy(uid)
    src, elsewhere = tmp_path / "src", tmp_path / "elsewhere"
    _save(src, lossless)
    _save(elsewhere, lossy)
    db = str(tmp_path / "superseded.db")
    with DicomSession(persistence_file=db) as session:
        session.ingest(str(src))
        session.configuration.rules = [
            {"serial_number": "SN1", "redaction_zones": [[0, 8, 0, 8]]}]
        assert session.redact(show_progress=False) == 1
        summary = session.ingest(str(elsewhere))
        live = _only(session)
    assert (summary.ingested, summary.declined) == (0, 1), summary
    assert len(live) == 1 and live[0].sop_instance_uid != uid
    assert "0028,2110" not in live[0].attributes
    assert _lossy_rows(db) == []


def test_the_lossy_row_quotes_a_declared_value_as_plain_data():
    """L4d: a multi-valued declaration rides `meta` as a list, and a `|`
    in a declared value cannot break the report's table.

    Killing mutations: (r4) the MultiValue left as pydicom's type; (r5)
    the `|` escape removed.
    """
    from isocenter.io_handlers import (_lossy_compression_evidence,
                                       _lossy_compression_words)
    ds = _near(2)
    ds.LossyImageCompression = ["00", "02"]
    facts = _lossy_compression_evidence(ds)
    assert type(facts["declared"]) is list  # pylint: disable=unidiomatic-typecheck
    assert facts["declared"] == ["00", "02"]
    assert _lossy_compression_words(facts).startswith(
        f"{LEAD}declares ['00', '02'], and ")
    piped = dict(facts, declared="0|")
    assert _lossy_compression_words(piped).startswith(
        f"{LEAD}declares '0\\|', and "), _lossy_compression_words(piped)


def test_the_lossy_row_log_is_capped(tmp_path, monkeypatch, caplog):
    """L5: seven NEAR 2 instances, seven rows, five lines and one
    suppression line. Killing mutation (m23): the lossy rows counted on the
    HighBit counter (a HighBit line would then be suppressed by them)."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    src = tmp_path / "src"
    for index in range(7):
        ds = _near(2)
        ds.SOPInstanceUID = generate_uid()
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        _save(src, ds, name=f"i{index}.dcm")
    db = str(tmp_path / "cap.db")
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with DicomSession(persistence_file=db) as session:
            summary = session.ingest(str(src))
    assert summary.ingested == 7, summary
    assert len(_lossy_rows(db)) == 7
    messages = [record.getMessage() for record in caplog.records]
    assert len([m for m in messages if LEAD in m]) == 5, messages
    assert len([m for m in messages if "suppressing further per-instance "
                "messages for LossyImageCompression" in m]) == 1, messages


def test_the_lossy_and_high_bit_caps_are_separate(tmp_path, monkeypatch,
                                                  caplog):
    """L5b: six lossy instances do not use up the HighBit log cap.

    Killing mutation (m23): one counter for both rows, under which a
    HighBit mismatch on a seventh instance is suppressed unseen.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    src = tmp_path / "src"
    for index in range(6):
        ds = _near(2)
        ds.SOPInstanceUID = generate_uid()
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        _save(src, ds, name=f"a{index}.dcm")
    ds = _fixture(CT_SMALL)
    ds.BitsStored = 12  # HighBit stays 15: ingest's #455 row
    _save(src, ds, name="z-high-bit.dcm")
    db = str(tmp_path / "caps.db")
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with DicomSession(persistence_file=db) as session:
            session.ingest(str(src))
    messages = [record.getMessage() for record in caplog.records]
    assert len([m for m in messages
                if "PS3.5 8.1.1 requires HighBit" in m]) == 1, messages


def test_the_recorded_value_survives_a_reload(tmp_path):
    """L6: the stamp is in the store, not only in the graph that made it."""
    got = _run(tmp_path, _near(2))
    with DicomSession(persistence_file=got["db"]) as session:
        assert _only(session)[0].attributes.get("0028,2110") == "01"


def test_an_icon_is_not_the_image(tmp_path):
    """L7: a pixel-less SR under `.81` carrying a NEAR 2 icon is not stamped.

    0028,2110 belongs to the General Image Module; an icon is a thumbnail,
    not the image. Killing mutation: the evidence asked in the nested path.
    """
    frame = (np.arange(16, dtype=np.int64) * 16).astype(np.uint8).reshape(4, 4)
    icon = Dataset()
    icon.Rows = icon.Columns = 4
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit, icon.SamplesPerPixel = 7, 1
    icon.PhotometricInterpretation, icon.PixelRepresentation = "MONOCHROME2", 0
    icon.PixelData = encapsulate([imagecodecs.jpegls_encode(frame, level=2)])
    icon["PixelData"].is_undefined_length = True

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.11"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = JPEGLS_NEAR
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT601", "DOE^JANE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "SR", 1, 1
    ds.StudyDate = "20230101"
    ds.IconImageSequence = Sequence([icon])
    src = tmp_path / "src"
    _save(src, ds)
    db = str(tmp_path / "icon.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(str(src))
        inst = _only(session)[0]
        assert "0028,2110" not in inst.attributes
        item = inst.sequences["0088,0200"].items[0]
        assert "0028,2110" not in item.attributes
    assert summary.ingested == 1, summary
    assert _lossy_rows(db) == []


# ---------------------------------------------------------------------------
# L8-L9: the stream readers walk; they do not search
# ---------------------------------------------------------------------------

def _seg(marker, payload):
    return (b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big")
            + payload)


#: A JPEG-LS frame header (SOF55) for a 4x4 3-component 8-bit image.
_SOF55 = _seg(0xF7, bytes([8, 0, 4, 0, 4, 3, 1, 0x11, 0, 2, 0x11, 0,
                           3, 0x11, 0]))


def test_jpegls_near_walks_not_searches():
    """L8: NEAR is the SOS byte after the Ns component specs, found by walking.

    A COM payload holding `FF DA 01 00 00 05` is where a search would read
    NEAR 5; a scan with three components puts NEAR six bytes further than
    one does; fill bytes may precede a marker. Killing mutations: (m21)
    `2 * Ns` read as `2`; (m22-JLS) a search for `FF DA`.
    """
    from isocenter.imagecodecs_handler import _jpegls_near
    com = _seg(0xFE, b"\xff\xda\x01\x00\x00\x05")
    sos3 = _seg(0xDA, bytes([3, 1, 0, 2, 0, 3, 0, 4, 0]))
    sos1 = _seg(0xDA, bytes([1, 1, 0, 3, 0]))
    soi = b"\xff\xd8"

    assert _jpegls_near(soi + com + _SOF55 + sos3 + b"\x00" * 8) == 4
    assert _jpegls_near(soi + _SOF55 + b"\xff\xff" + sos1 + b"\x00" * 8) == 3
    assert _jpegls_near(soi + com + _SOF55) is None
    assert _jpegls_near(b"\x00\x00" + sos1) is None
    assert _jpegls_near(imagecodecs.jpegls_encode(_mr16(), level=2)) == 2
    assert _jpegls_near(imagecodecs.jpegls_encode(_mr16(), level=0)) == 0


def _cod_split(codestream):
    """`(through SIZ, the rest)` of a raw J2K codestream."""
    assert codestream[:4] == b"\xff\x4f\xff\x51"
    lsiz = int.from_bytes(codestream[4:6], "big")
    return codestream[:4 + lsiz], codestream[4 + lsiz:]


def _without_cod(codestream):
    head, rest = _cod_split(codestream)
    out, pos = head, 0
    while rest[pos:pos + 2] != b"\xff\x90":
        length = int.from_bytes(rest[pos + 2:pos + 4], "big")
        if rest[pos + 1] != 0x52:
            out += rest[pos:pos + 2 + length]
        pos += 2 + length
    return out + rest[pos:]


def test_j2k_irreversible_reads_cod():
    """L9: COD's transform byte, reached by walking the main header.

    Raw codestream, JP2 box, XLBox `jp2c`; a COM whose payload holds
    `FF 52` and the opposite transform at the offset a search would read;
    no COD at all. Killing mutation (m22): a search for `FF 52`.
    """
    from isocenter.imagecodecs_handler import _j2k_irreversible
    arr = _mr16()
    reversible = bytes(imagecodecs.jpeg2k_encode(arr, level=0,
                                                 codecformat="J2K"))
    irreversible = bytes(imagecodecs.jpeg2k_encode(
        arr, level=40, codecformat="J2K", reversible=False))
    assert _j2k_irreversible(reversible) is False
    assert _j2k_irreversible(irreversible) is True

    jp2 = bytes(imagecodecs.jpeg2k_encode(arr, level=40, codecformat="JP2",
                                          reversible=False))
    assert _j2k_irreversible(jp2) is True
    i = jp2.find(b"jp2c")
    lbox = int.from_bytes(jp2[i - 4:i], "big")
    payload = jp2[i + 4:i - 4 + lbox] if lbox else jp2[i + 4:]
    xl = (jp2[:i - 4] + (1).to_bytes(4, "big") + b"jp2c"
          + (16 + len(payload)).to_bytes(8, "big") + payload)
    assert imagecodecs.jpeg2k_decode(xl).shape == arr.shape
    assert _j2k_irreversible(xl) is True

    # A COM (FF 64) right after SIZ: Lcme, Rcme, then `FF 52` and bytes
    # whose 13th after the pair says "irreversible" (0).
    decoy = b"\x00\x01" + b"\xff\x52" + bytes(16)
    head, rest = _cod_split(reversible)
    with_com = head + _seg(0x64, decoy) + rest
    assert imagecodecs.jpeg2k_decode(with_com).tolist() == arr.tolist()
    assert _j2k_irreversible(with_com) is False

    assert _j2k_irreversible(_without_cod(reversible)) is None
    assert _j2k_irreversible(b"") is None

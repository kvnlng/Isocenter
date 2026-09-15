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

Ingest now stamps `01` when **the pixel data proves it** -- a DCT process
(JPEG Baseline or Extended), a JPEG-LS scan with NEAR above 0, or a JPEG
2000 codestream using the 9-7 irreversible wavelet -- and writes a WARNING
naming the evidence (owner ruling Q1: kept for an absent value and for a
declared `00`). A syntax alone is not evidence: a NEAR 0 JPEG-LS stream and
a reversible JPEG 2000 codestream are bit-exact whatever their syntax is
called (built and measured), and a false `01` can never be withdrawn.
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


def _run(tmp_path, ds, *, compression=False, name="s"):
    """Ingest `ds`, export it, report. Returns a dict of what was seen."""
    src = tmp_path / f"src_{name}"
    _save(src, ds)
    db = str(tmp_path / f"{name}.db")
    out = tmp_path / f"out_{name}"
    report = tmp_path / f"{name}.md"
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(str(src))
        instances = _only(session)
        graph = instances[0].attributes.get("0028,2110") if instances else None
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
            "written": written}


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
    (lambda: _fixture(SC_RGB_JPEG_DCMTK, declared=None), "is absent",
     "JPEG Baseline (1.2.840.10008.1.2.4.50) is a DCT process, which is "
     "lossy by definition"),
    (lambda: _fixture(JPEG_LOSSY, declared=None), "is absent",
     "JPEG Extended (1.2.840.10008.1.2.4.51) is a DCT process, which is "
     "lossy by definition"),
    (lambda: _fixture(SC_RGB_JPEG_DCMTK, declared="00"), "declares '00'",
     "JPEG Baseline (1.2.840.10008.1.2.4.50) is a DCT process, which is "
     "lossy by definition"),
], ids=["jpegls-near2", "jpegls-near3-ilv0", "htj2k-irreversible",
        "j2k91-irreversible", "jpeg50-absent", "jpeg51-absent", "jpeg50-00"])
def test_a_lossy_source_without_01_is_recorded(tmp_path, build, lead,
                                               evidence, compression):
    """L1: `01` in the graph and the file, one WARNING naming the evidence.

    Killing mutations: (m18) the DCT-process arm deleted (the `.50`/`.51`
    parameters); (m20) the stamp deleted with the row kept (graph and
    file); (m24) `_first_frame` returning the undivided PixelData (the
    JPEG-LS and JPEG 2000 parameters).
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

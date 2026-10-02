"""A value too long for its VR's 2-byte Explicit VR length is said, and our own export re-ingests it (#692).

An Explicit VR element of a VR with a 2-byte length (US, SS, UL, SL, FL, FD,
DS, IS, AT, the short text VRs) holds at most 65535 bytes. Implicit VR has a
4-byte length for every element, so a long value -- a Real World Value LUT
Data `(0040,9212)` FD list from an Implicit VR source, or a long list a
caller sets -- is legal there. pydicom writes such an element as `UN` under
an Explicit VR syntax (PS3.5 6.2.2), which is conformant. Measured on `main`
at 7579d4df:

- the compressed export, the default, wrote it `UN` with no row and no note
  (pydicom's warning fires in a spawned worker and never reaches the
  caller);
- re-ingesting that export dropped it: pydicom does not decode a `UN` of
  known VR above 0xFFFF bytes, and `populate_attrs`' `UN` size gate took it,
  with a `DATA_LOSS` row. This library could not re-ingest its own output.

The owner's rulings (Q4, Q5): an INFO note at export, and ingest decodes a
standard `UN` under its dictionary VR, so the round trip holds.
"""
import logging
import shutil

import numpy as np
import pydicom
import pytest
from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.filebase import DicomBytesIO
from pydicom.filewriter import write_dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

import isocenter.io_handlers as io_handlers
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
RWV = 0x00409212           # Real World Value LUT Data, FD 1-n
RWV_TEXT = "0040,9212"
VALUES = [float(i) / 4 for i in range(9000)]    # 72000 bytes


def _source(folder, *, syntax=ImplicitVRLittleEndian, extra=None):
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.692.1"
    meta.TransferSyntaxUID = syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = SC, meta.MediaStorageSOPInstanceUID
    ds.PatientID, ds.PatientName = "P692", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.692.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.692.3"
    ds.Modality, ds.StudyDate, ds.StudyTime = "OT", "20200101", "120000"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(16, dtype=np.uint16).tobytes()
    if extra is not None:
        extra(ds)
    ds.save_as(str(folder / "a.dcm"), enforce_file_format=True,
               implicit_vr=syntax == ImplicitVRLittleEndian, little_endian=True)


def _with_rwv(ds):
    ds.add_new(RWV, "FD", VALUES)


def _export(src, db, out, *, compress, mutate=None):
    with DicomSession(str(db)) as s:
        s.ingest(str(src))
        if mutate is not None:
            (p,) = s.store.patients
            mutate(p.studies[0].series[0].instances[0])
        s.export(str(out), use_compression=compress, show_progress=False)
        losses = s.store_backend.get_audit_losses()
        rows = s.store_backend.get_audit_errors()
    (written,) = list(out.rglob("*.dcm"))
    return written, losses, rows


def _notes(caplog):
    return [r.getMessage() for r in caplog.records if "is written as UN" in r.getMessage()]


def test_an_implicit_source_fd_list_round_trips_through_our_own_compressed_export(tmp_path):
    _source(tmp_path / "src", extra=_with_rwv)
    first, _losses, _rows = _export(tmp_path / "src", tmp_path / "a.db",
                                    tmp_path / "out1", compress=True)
    assert pydicom.dcmread(str(first)).get_item(RWV).VR == "UN"

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    second, losses, _rows = _export(tmp_path / "again", tmp_path / "b.db",
                                    tmp_path / "out2", compress=False)

    assert not [r for r in losses if RWV_TEXT in r[2]], losses
    ds = pydicom.dcmread(str(second))
    assert ds[RWV].VR == "FD"
    assert list(ds[RWV].value) == VALUES


def test_the_relabel_to_un_is_said(tmp_path, caplog):
    _source(tmp_path / "src", extra=_with_rwv)
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _written, _losses, rows = _export(tmp_path / "src", tmp_path / "a.db",
                                          tmp_path / "out", compress=True)
    notes = _notes(caplog)
    assert len(notes) == 1, notes
    assert RWV_TEXT in notes[0] and "UN" in notes[0] and "72000" in notes[0]
    assert not [r for r in rows if "is written as UN" in r[2]], rows


def test_an_implicit_export_writes_it_under_its_own_vr(tmp_path, caplog):
    _source(tmp_path / "src", extra=_with_rwv)
    with caplog.at_level(logging.INFO, logger="isocenter"):
        written, _losses, _rows = _export(tmp_path / "src", tmp_path / "a.db",
                                          tmp_path / "out", compress=False)
    ds = pydicom.dcmread(str(written))
    assert str(ds.file_meta.TransferSyntaxUID) == ImplicitVRLittleEndian
    assert ds[RWV].VR == "FD"
    assert _notes(caplog) == []


def test_a_short_value_is_not_relabelled(tmp_path, caplog):
    _source(tmp_path / "src", extra=lambda ds: ds.add_new(RWV, "FD", VALUES[:100]))
    with caplog.at_level(logging.INFO, logger="isocenter"):
        written, _losses, _rows = _export(tmp_path / "src", tmp_path / "a.db",
                                          tmp_path / "out", compress=True)
    assert pydicom.dcmread(str(written)).get_item(RWV).VR == "FD"
    assert _notes(caplog) == []


#: Noise, so a lossless J2K stream of it stays above 65535 bytes.
NOISE = np.random.default_rng(692).integers(
    -32768, 32768, size=(256, 256), dtype=np.int16)


@pytest.mark.parametrize("compress", [True, False])
def test_pixel_data_over_the_threshold_is_never_relabelled(tmp_path, caplog, compress):
    """Pixel Data's VR is a 4-byte-length VR whatever it is spelled as.

    Under compression `_compress_j2k` assigns the stream to a dataset with
    no Pixel Data element, so pydicom gives it the dictionary's `OB or OW`,
    a spelling outside `EXPLICIT_VR_LENGTH_32`, and pydicom settles it only
    inside `dcmwrite`. Read as "has a 2-byte length", a stream over 65535
    bytes was relabelled `UN` and written with an undefined length, a file
    pydicom cannot read back. Found by the fingerprint retake: 72 files,
    every compressed export whose stream passed 64 KiB. The relabel now
    weighs only the VRs pydicom names as 2-byte (`EXPLICIT_VR_LENGTH_16`).
    Noise, so the stream stays that long.
    """
    def big(ds):
        ds.Rows = ds.Columns = 256
        ds.PixelRepresentation = 1
        ds.PixelData = NOISE.tobytes()

    _source(tmp_path / "src", syntax=ExplicitVRLittleEndian, extra=big)
    with caplog.at_level(logging.INFO, logger="isocenter"):
        written, _losses, _rows = _export(tmp_path / "src", tmp_path / "a.db",
                                          tmp_path / "out", compress=compress)
    ds = pydicom.dcmread(str(written))
    assert ds["PixelData"].VR in ("OB", "OW")
    assert len(ds.PixelData) > 65535
    assert np.array_equal(ds.pixel_array, NOISE)
    assert _notes(caplog) == []


def test_a_private_un_over_the_threshold_is_still_dropped(tmp_path):
    """The decode is for standard tags: a private `UN` has no dictionary VR
    to decode under, and keeps the size gate."""
    def extra(ds):
        ds.add_new(0x00090010, "LO", "ACME")
        ds.add_new(0x00091001, "UN", bytes(70000))

    _source(tmp_path / "src", syntax=ExplicitVRLittleEndian, extra=extra)
    with DicomSession(str(tmp_path / "a.db")) as s:
        s.ingest(str(tmp_path / "src"))
        (p,) = s.store.patients
        attrs = p.studies[0].series[0].instances[0].attributes
        losses = s.store_backend.get_audit_losses()
    assert "0009,1001" not in attrs
    assert [r for r in losses if "0009,1001" in r[2]], losses


def test_a_caller_us_list_is_said_and_round_trips(tmp_path, caplog):
    values = list(range(40000))
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src")
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True,
            mutate=lambda i: i.set_attr("0018,1310", values))
    notes = _notes(caplog)
    assert len(notes) == 1 and "0018,1310" in notes[0], notes

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    second, losses, _rows = _export(tmp_path / "again", tmp_path / "b.db",
                                    tmp_path / "out2", compress=False)
    assert not [r for r in losses if "0018,1310" in r[2]], losses
    assert list(pydicom.dcmread(str(second))[0x00181310].value) == values


def test_a_caller_lut_data_list_is_said_and_dropped_on_re_ingest_as_ow_is(tmp_path, caplog):
    """LUT Data (0028,3006) is `US or OW` in the dictionary.

    A caller's 80,000-byte `US` list is written `UN` (#692). Re-ingest
    weighs that `UN` against the binary retention limit, exactly as it
    weighs the same bytes spelled `OW`, and drops it with a `DATA_LOSS`
    row (owner ruling on the review of #900, F6); the note says so rather
    than promising a read-back. Whether LUT Data should be exempt from the
    limit is #902.
    """
    values = [(i * 7) % 65536 for i in range(40000)]
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src")
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True,
            mutate=lambda i: (i.set_attr("0028,3002", [0, 0, 16]),
                              i.set_attr("0028,3006", values)))
    notes = _notes(caplog)
    assert len(notes) == 1 and "0028,3006" in notes[0], notes
    assert "reads them back" not in notes[0], notes[0]
    assert "65534" in notes[0], notes[0]

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    _second, losses, _rows = _export(tmp_path / "again", tmp_path / "b.db",
                                     tmp_path / "out2", compress=False)
    rows = [r for r in losses if "Standard tag 0028,3006 (UN)" in r[2]]
    assert len(rows) == 1, losses


def _lut_source(folder, vr, nbytes):
    """An Explicit VR file whose LUT Data is `nbytes` spelled `vr`."""
    folder.mkdir(parents=True, exist_ok=True)

    def lut(ds):
        ds.add_new(0x00283002, "US", [nbytes // 2 % 65536, 0, 16])
        ds.add_new(0x00283006, vr, (bytes(range(256)) * (nbytes // 256 + 1))[:nbytes])

    _source(folder, syntax=ExplicitVRLittleEndian, extra=lut)


@pytest.mark.parametrize("nbytes,kept", [(65532, True), (65534, True), (65536, False)],
                         ids=["under", "at", "over"])
@pytest.mark.parametrize("vr", ["OW", "UN"])
def test_lut_data_meets_the_retention_limit_whichever_way_it_is_spelled(tmp_path, vr, nbytes, kept):
    """The owner's ruling on F6: a `UN` LUT Data is gated as its `OW` twin.

    At or below 65534 bytes both are kept, byte for byte; above it both
    are dropped with one `DATA_LOSS` row naming the tag and the limit.
    Before, the `UN` spelling of 65536 bytes was decoded on its `US` arm
    and kept while the `OW` one was dropped.
    """
    _lut_source(tmp_path / "src", vr, nbytes)
    with DicomSession(str(tmp_path / "s.db")) as s:
        s.ingest(str(tmp_path / "src"))
        (p,) = s.store.patients
        attrs = dict(p.studies[0].series[0].instances[0].attributes)
        losses = s.store_backend.get_audit_losses()
    rows = [r for r in losses if "Standard tag 0028,3006" in r[2]]
    if kept:
        assert rows == [], losses
        held = attrs["0028,3006"]
        held = (bytes(held) if isinstance(held, (bytes, bytearray))
                else np.asarray(held, dtype="<u2").tobytes())
        assert held == (bytes(range(256)) * (nbytes // 256 + 1))[:nbytes]
    else:
        assert "0028,3006" not in attrs
        assert len(rows) == 1, losses
        assert "65534-byte retention threshold" in rows[0][2]


def _uc_notes(caplog):
    return [r.getMessage() for r in caplog.records if "is written as UC" in r.getMessage()]


def _private_list(i):
    i.set_attr("0009,0010", "ACME")
    # No recorded VR: the fallback writes the list as multi-valued `LO`
    # text, about 230 KB of it.
    i.set_attr("0009,1001", list(range(40000)))


def test_a_caller_private_list_round_trips_as_uc(tmp_path, caplog):
    """A private `LO` over 65535 bytes is written `UC` under Explicit VR (#901, Q3 A).

    #692 wrote it `UN`, and a private `UN` has no dictionary VR to decode
    under, so this library's re-ingest dropped it with a PRIVATE
    `DATA_LOSS` row, grading the run `REVIEW_REQUIRED`. `UC` holds the
    same values with a 4-byte length, and reads back as text.
    """
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src")
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True, mutate=_private_list)
    elem = pydicom.dcmread(str(first))[0x00091001]
    assert elem.VR == "UC"
    assert elem.VM == 40000
    notes = _uc_notes(caplog)
    assert len(notes) == 1, notes
    assert notes[0].endswith(
        "0009,1001 (LO, 228890 bytes) is written as UC: an Explicit VR LO "
        "element can hold at most 65535 bytes (PS3.5 6.2.2), and UC holds "
        "the same values with a 4-byte length; this library reads them "
        "back as UC."), notes
    assert _notes(caplog) == []

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    with DicomSession(str(tmp_path / "b.db")) as s:
        s.ingest(str(tmp_path / "again"))
        (p,) = s.store.patients
        held = p.studies[0].series[0].instances[0].attributes["0009,1001"]
        assert [str(v) for v in held] == [str(i) for i in range(40000)]
        s.export(str(tmp_path / "out2"), show_progress=False)
        losses = s.store_backend.get_audit_losses()
        rows = s.store_backend.get_audit_errors()
    assert not [r for r in losses if "0009,1001" in r[2]], losses
    assert not [r for r in rows if "0009,1001" in r[2]], rows
    (second,) = list((tmp_path / "out2").rglob("*.dcm"))
    again = pydicom.dcmread(str(second))[0x00091001]
    assert again.VR == "UC" and again.VM == 40000


def test_a_recorded_private_us_list_is_still_un_and_said(tmp_path, caplog):
    """A recorded private numeric VR keeps #692's `UN` (Q3 A): writing it
    as text would change the source's own VR. The note says re-ingest
    drops it, and it does."""
    def extra(ds):
        ds.add_new(0x00090010, "LO", "ACME")
        ds.add_new(0x00091002, "US", [1, 2])

    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src", syntax=ExplicitVRLittleEndian, extra=extra)
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True,
            mutate=lambda i: i.set_attr("0009,1002", list(range(40000))))
    assert pydicom.dcmread(str(first))[0x00091002].VR == "UN"
    notes = _notes(caplog)
    assert len(notes) == 1 and "0009,1002 (US, 80000 bytes)" in notes[0], notes
    assert "drops a UN over 65534 bytes with a DATA_LOSS row" in notes[0]
    assert _uc_notes(caplog) == []

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    _second, losses, _rows = _export(tmp_path / "again", tmp_path / "b.db",
                                     tmp_path / "out2", compress=False)
    assert [r for r in losses if "Private tag 0009,1002 (UN)" in r[2]], losses


def test_a_recorded_private_sh_list_is_still_un(tmp_path, caplog):
    """The `UC` arm is for `LO` alone, the one multi-valued text VR the
    fallback writes. A recorded private `SH` whose values still fit it, over
    65535 bytes in all, keeps #692's `UN`: it is the source's VR, and
    writing it `UC` would change it."""
    def extra(ds):
        ds.add_new(0x00090010, "LO", "ACME")
        ds.add_new(0x00091003, "SH", ["A", "B"])

    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src", syntax=ExplicitVRLittleEndian, extra=extra)
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True,
            mutate=lambda i: i.set_attr("0009,1003", ["abcdefghijklmn"] * 5000))
    assert pydicom.dcmread(str(first))[0x00091003].VR == "UN"
    assert _uc_notes(caplog) == []
    assert len([n for n in _notes(caplog) if "0009,1003 (SH" in n]) == 1


def test_a_standard_lo_over_the_limit_is_still_un(tmp_path, caplog):
    """Software Versions (0018,1020), LO 1-n: a standard tag keeps #692's
    `UN`, which re-ingest decodes under the dictionary's `LO`. Writing it
    `UC` would put a standard tag under a VR its dictionary does not
    give."""
    values = ["v" * 60] * 1200
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src")
        first, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=True, mutate=lambda i: i.set_attr("0018,1020", values))
    assert pydicom.dcmread(str(first)).get_item(0x00181020).VR == "UN"
    assert _uc_notes(caplog) == []
    assert len([n for n in _notes(caplog) if "0018,1020" in n]) == 1


def test_an_implicit_export_of_a_long_private_list_is_unchanged(tmp_path, caplog):
    """Implicit VR names no VR on the wire, so there is nothing to relabel:
    the value is the backslash-joined text, as before, and nothing is
    said. Re-ingest of it still drops it (a follow-up)."""
    with caplog.at_level(logging.INFO, logger="isocenter"):
        _source(tmp_path / "src")
        written, _losses, _rows = _export(
            tmp_path / "src", tmp_path / "a.db", tmp_path / "out1",
            compress=False, mutate=_private_list)
    ds = pydicom.dcmread(str(written))
    assert str(ds.file_meta.TransferSyntaxUID) == ImplicitVRLittleEndian
    raw = ds.get_item(0x00091001).value
    joined = "\\".join(str(i) for i in range(40000)).encode()
    assert raw == joined + b" " * (len(joined) % 2)
    assert _uc_notes(caplog) == [] and _notes(caplog) == []


def _explicit_bytes(ds):
    buffer = DicomBytesIO()
    buffer.is_little_endian = True
    buffer.is_implicit_VR = False
    write_dataset(buffer, ds)
    return buffer.getvalue()


@pytest.mark.parametrize("vr,value", [
    ("FD", VALUES),
    ("US", list(range(40000))),
    ("SL", [-i for i in range(20000)]),
    ("LO", ["x" * 60] * 1200),
])
def test_our_relabel_writes_the_bytes_pydicom_would(vr, value):
    """Nothing in the golden cohort exercises the relabel, so this is the
    check that it encodes as pydicom itself does: same tag, VR, length and
    value bytes."""
    tag = {"FD": RWV, "US": 0x00181310, "SL": 0x00189219,
           "LO": 0x00081030}[vr]
    theirs = Dataset()
    theirs.add(DataElement(tag, vr, value))
    ours = Dataset()
    ours.add(DataElement(tag, vr, value))
    notes = []
    io_handlers._relabel_long_short_length_values(ours, notes)
    assert ours[tag].VR == "UN"
    assert len(notes) == 1
    with pytest.warns(UserWarning, match="64 kByte"):
        expected = _explicit_bytes(theirs)
    assert _explicit_bytes(ours) == expected

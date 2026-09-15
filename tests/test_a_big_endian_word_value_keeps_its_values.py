"""A big-endian source's word values keep their values (#657).

#648 fixed the pixels; everything else a big-endian file holds in words wider
than a byte was still stored as the bytes it was read in. Ingest retains an
unrouted `OW`/`OL`/`OF`/`OD`/`OV` value verbatim and `ingest_worker` copies
Waveform Data verbatim to the sidecar; the export writes both back verbatim
under a little-endian syntax. Measured on 448eb75: a palette LUT of
`[0, 1000, 40000, 65535]` exported as `[0, 59395, 16540, 65535]`, an `SS`
waveform `[0, 100, -200, 3000]` as `[0, 25600, 14591, -18421]` in the DICOM
export and the WFDB `.dat` alike -- no row, `verify_readback=True` passing,
and export -> ingest -> export identical, so the wrong values were stable.

The graph's contract is now little-endian for these values. What cannot be
converted whole -- a `UN` value, whose word size nobody knows; a length that
is not a whole number of words; a waveform with no usable Waveform Bits
Allocated -- is kept and said, by a WARNING row.

Every expected value is a literal, and every one is a value a byte swap
changes. Nothing here compares against pydicom's reading of the source:
pydicom returns these values as the file's bytes, and its own `dcmwrite` of a
big-endian dataset to a little-endian syntax leaves them unswapped too.

Real `Session` ingests throughout: the ingest pool spawns, so a monkeypatch
here would not reach the worker under test.
"""
import logging
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRBigEndian, ExplicitVRLittleEndian, generate_uid

from isocenter.session import DicomSession

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"


def _dataset(big=True):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRBigEndian if big else ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT657", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    ds.SliceThickness, ds.KVP = "1.0", "120"
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = bytes(4)
    return ds


def _save(tmp_path, ds, big=True, name="src"):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    path = str(folder / f"{ds.SOPInstanceUID}.dcm")
    pydicom.dcmwrite(path, ds, implicit_vr=False, little_endian=not big,
                     force_encoding=True)
    return str(folder)


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _files(folder):
    return sorted(os.path.join(r, f) for r, _d, fs in os.walk(str(folder))
                  for f in fs if f.endswith(".dcm"))


def _rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT action_type, details FROM audit_log "
            "WHERE action_type IN ('WARNING', 'DATA_LOSS', 'ERROR')").fetchall()


def _find(ds, path, tag):
    for seq, index in path:
        ds = ds[seq].value[index]
    return ds[tag]


def _key(tag):
    return f"{tag >> 16:04x},{tag & 0xFFFF:04x}"


def _graph(inst, path, tag):
    item = inst
    for seq, index in path:
        item = item.sequences[_key(seq)].items[index]
    return item.attributes[_key(tag)]


# (id, builder path, tag, VR, numpy code, literal values)
WORDS = [
    ("OW-palette", (), 0x00281201, "OW", "u2", [0, 1000, 40000, 65535]),
    ("OW-overlay", (), 0x60003000, "OW", "u2", [0, 1000, 40000, 65535]),
    ("OL", (), 0x00660040, "OL", "u4", [0, 1, 70000, 4000000000]),
    ("OF", (), 0x00660016, "OF", "f4", [0.5, -1.25, 1000.0, 3.0]),
    ("OD", (), 0x00660022, "OD", "f8", [0.5, -1.25, 1e10, 3.0]),
    ("OV", (), 0x00720081, "OV", "u8", [0, 1, 2**40, 2**63 + 5]),
    ("OF-nested", ((0x00660002, 0), (0x00660011, 0)), 0x00660016, "OF", "f4",
     [0.5, -1.25, 1000.0, 3.0]),
    ("OW-private", (), 0x00091010, "OW", "u2", [0, 1000, 40000, 65535]),
]


def _place(ds, path, tag, vr, payload):
    if tag >> 16 == 0x0009:
        ds.add_new(0x00090010, "LO", "J7 PROBE")
    parent = ds
    for seq, _index in path:
        item = Dataset()
        parent.add_new(seq, "SQ", Sequence([item]))
        parent = item
    parent.add_new(tag, vr, payload)


@pytest.mark.parametrize("path, tag, vr, code, values",
                         [pytest.param(*w[1:], id=w[0]) for w in WORDS])
@pytest.mark.parametrize("compress", [False, True], ids=["native", "j2k"])
def test_a_big_endian_word_value_is_stored_and_exported_as_its_values(
        tmp_path, path, tag, vr, code, values, compress):
    ds = _dataset()
    _place(ds, path, tag, vr, np.array(values, ">" + code).tobytes())
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = _instances(session)
        stored = np.frombuffer(_graph(inst, path, tag), "<" + code).tolist()
        session.export(str(out), use_compression=compress)
    with DicomSession(persistence_file=db) as session:
        (inst,) = _instances(session)
        reopened = np.frombuffer(_graph(inst, path, tag), "<" + code).tolist()
    (written,) = _files(out)
    exported = np.frombuffer(
        bytes(_find(pydicom.dcmread(written), path, tag).value),
        "<" + code).tolist()

    assert stored == values
    assert reopened == values
    assert exported == values
    assert _rows(db) == []


@pytest.mark.parametrize("bits, interpretation, code, values", [
    pytest.param(16, "SS", "i2", [0, 100, -200, 3000], id="16-SS"),
    pytest.param(32, "SL", "i4", [0, 100, -200, 70000], id="32-SL"),
])
def test_a_big_endian_waveform_is_stored_and_exported_as_its_samples(
        tmp_path, bits, interpretation, code, values):
    ds = _dataset()
    ds.WaveformSequence = Sequence([_waveform_item(
        bits, interpretation, np.array(values, ">" + code).tobytes())])
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    out, wfdb = tmp_path / "out", tmp_path / "wfdb"
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = _instances(session)
        stored = np.frombuffer(inst.get_waveform_bytes(), "<" + code).tolist()
        session.export(str(out), use_compression=False)
        if interpretation == "SS":
            decoded = np.asarray(inst.get_waveform_data()).ravel().tolist()
            session.export(str(wfdb), format="wfdb")
    (written,) = _files(out)
    exported = np.frombuffer(bytes(pydicom.dcmread(written)
                                   .WaveformSequence[0].WaveformData),
                             "<" + code).tolist()

    assert stored == values
    assert exported == values
    if interpretation == "SS":
        assert decoded == values
        (dat,) = [os.path.join(r, f) for r, _d, fs in os.walk(str(wfdb))
                  for f in fs if f.endswith(".dat")]
        assert np.fromfile(dat, "<i2").tolist() == values
    assert _rows(db) == []


def _waveform_item(bits, interpretation, payload, channel=()):
    """One Waveform Sequence item; `channel` adds `(tag, vr, bytes)` to its channel."""
    wf = Dataset()
    wf.MultiplexGroupTimeOffset = "0"
    wf.WaveformOriginality = "ORIGINAL"
    wf.NumberOfWaveformChannels = 1
    wf.NumberOfWaveformSamples = 4
    wf.SamplingFrequency = "500"
    ch = Dataset()
    ch.ChannelSourceSequence = Sequence([Dataset()])
    ch.ChannelSourceSequence[0].CodeValue = "5.6.3-9-1"
    ch.ChannelSourceSequence[0].CodingSchemeDesignator = "SCPECG"
    ch.ChannelSourceSequence[0].CodeMeaning = "Lead I"
    ch.WaveformBitsStored = bits or 16
    for tag, vr, value in channel:
        ch.add_new(tag, vr, value)
    wf.ChannelDefinitionSequence = Sequence([ch])
    if bits is not None:
        wf.WaveformBitsAllocated = bits
    wf.WaveformSampleInterpretation = interpretation
    wf.add_new(0x54001010, "OB" if bits == 8 else "OW", payload)
    return wf


def test_what_has_no_byte_order_is_unchanged(tmp_path):
    """OB, an 8-bit waveform, and any little-endian source: bytes as read, no row."""
    be = _dataset()
    be.add_new(0x00090010, "LO", "J7 PROBE")
    be.add_new(0x00091010, "OB", b"\x01\x02\x03\x04")
    be.WaveformSequence = Sequence([_waveform_item(
        8, "SB", b"\x00\x01\xfe\x64",
        channel=[(0x54000110, "OB", b"\xfe\x01"), (0x54000112, "OB", b"\x64\x02")])])
    le = _dataset(big=False)
    le.add_new(0x00281201, "OW", np.array([0, 1000], "<u2").tobytes())
    folder = _save(tmp_path, be)
    _save(tmp_path, le, big=False)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        by_patient = {i.sop_instance_uid: i for i in _instances(session)}
        b, l = by_patient[be.SOPInstanceUID], by_patient[le.SOPInstanceUID]
        assert b.attributes["0009,1010"] == b"\x01\x02\x03\x04"
        assert b.get_waveform_bytes() == b"\x00\x01\xfe\x64"
        assert _channel(b)["5400,0110"] == b"\xfe\x01"
        assert _channel(b)["5400,0112"] == b"\x64\x02"
        assert l.attributes["0028,1201"] == b"\x00\x00\xe8\x03"
    assert _rows(db) == []


def test_a_bare_dataset_is_not_taken_for_big_endian():
    """`populate_attrs` on a hand-built Dataset: (None, None) is not big-endian."""
    from isocenter.entities import DicomItem
    from isocenter.io_handlers import populate_attrs

    ds = Dataset()
    ds.add_new(0x00281201, "OW", b"\x00\x01\x02\x03")
    item = DicomItem()
    populate_attrs(ds, item)
    assert item.attributes["0028,1201"] == b"\x00\x01\x02\x03"


def test_a_big_endian_un_value_is_kept_as_read_and_said(tmp_path):
    ds = _dataset()
    item = Dataset()
    item.add_new(0x00130010, "LO", "J7 PROBE")
    item.add_new(0x00131010, "UN", b"\x00\x01\x00\x02")
    ds.add_new(0x00110010, "LO", "J7 PROBE")
    ds.add_new(0x00111010, "SQ", Sequence([item]))
    ds.add_new(0x00090010, "LO", "J7 PROBE")
    ds.add_new(0x00091010, "UN", b"\x00\x01\x00\x02\x00\x03")
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        session.export(str(out), use_compression=True)
        session.generate_report(str(report))
    (written,) = _files(out)
    back = pydicom.dcmread(written)

    # The WARNING bars PASS: on 448eb75 this source graded PASS with no row.
    assert [line for line in report.read_text(encoding="utf-8").splitlines()
            if "Validation Status" in line] == [
        "| **Validation Status** | **REVIEW_REQUIRED** |"]
    assert bytes(back[0x00091010].value) == b"\x00\x01\x00\x02\x00\x03"
    assert bytes(_find(back, ((0x00111010, 0),), 0x00131010).value) == \
        b"\x00\x01\x00\x02"
    assert sorted(_rows(db)) == [
        ("WARNING",
         "Private tag 0009,1010 (UN): 6 bytes read from a big-endian source "
         "whose value representation is UN, so the word size and byte "
         "order are unknown. The bytes were kept in the byte order they "
         "were read in."),
        ("WARNING",
         "Private tag 0013,1010 (UN) at 0011,1010[0]: 4 bytes read from a "
         "big-endian source whose value representation is UN, so the word "
         "size and byte order are unknown. The bytes were kept in the byte "
         "order they were read in."),
    ]


def test_a_ragged_big_endian_value_converts_its_whole_words(tmp_path):
    ds = _dataset()
    # 0.5 as a big-endian float, then two bytes that are not a word.
    ds.add_new(0x00660016, "OF", b"\x3f\x00\x00\x00\xbf\xa0")
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        session.export(str(out), use_compression=False)
    (written,) = _files(out)

    assert bytes(pydicom.dcmread(written)[0x00660016].value) == \
        b"\x00\x00\x00\x3f\xbf\xa0"
    assert _rows(db) == [
        ("WARNING",
         "Standard tag 0066,0016 (OF): 6 bytes read from a big-endian source "
         "are not a whole number of 4-byte words. The whole words were "
         "converted to little-endian; the trailing 2 byte(s) were kept as "
         "read."),
    ]


def test_a_big_endian_waveform_without_bits_allocated_is_kept_and_said(tmp_path):
    ds = _dataset()
    ds.WaveformSequence = Sequence([_waveform_item(
        None, "SS", b"\x00\x00\x00\x64\xff\x38\x0b\xb8")])
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = _instances(session)
        assert inst.get_waveform_bytes() == b"\x00\x00\x00\x64\xff\x38\x0b\xb8"
    assert _rows(db) == [
        ("WARNING",
         "Standard tag 5400,1010 (OW) at 5400,0100[0]: 8 bytes read from a "
         "big-endian source with no usable Waveform Bits Allocated, so the "
         "sample width and byte order are unknown. The bytes were kept in "
         "the byte order they were read in."),
    ]


def test_a_waveform_bits_allocated_that_is_not_one_number_is_no_width(tmp_path):
    """(5400,1004) is `US` VM1; a file that gives it two values gives no width.

    Kept and said like an absent one, and the file still ingests: the width
    lookup must not raise on a `MultiValue`, which cannot key a dict.
    """
    ds = _dataset()
    wf = _waveform_item(None, "SS", b"\x00\x00\x00\x64")
    wf.add_new(0x54001004, "US", [16, 16])
    ds.WaveformSequence = Sequence([wf])
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = _instances(session)
        assert inst.get_waveform_bytes() == b"\x00\x00\x00\x64"
    assert _rows(db) == [
        ("WARNING",
         "Standard tag 5400,1010 (OW) at 5400,0100[0]: 4 bytes read from a "
         "big-endian source with no usable Waveform Bits Allocated, so the "
         "sample width and byte order are unknown. The bytes were kept in "
         "the byte order they were read in."),
    ]


def _channel(inst, group=0):
    """The first Channel Definition item's attributes, of Waveform Sequence item `group`."""
    return (inst.sequences["5400,0100"].items[group]
            .sequences["003a,0200"].items[0].attributes)


@pytest.mark.parametrize("bits, interpretation, vr, code, low, high", [
    pytest.param(16, "SS", "OW", "i2", -200, 3000, id="16-OW"),
    pytest.param(32, "SL", "OW", "i4", -200, 70000, id="32-OW"),
    pytest.param(16, "SS", "OB", "i2", -200, 3000, id="16-OB"),
])
def test_channel_minimum_and_maximum_are_one_sample_wide(
        tmp_path, bits, interpretation, vr, code, low, high):
    """(5400,0110)/(5400,0112) hold one sample, as wide as Waveform Bits Allocated.

    Not as wide as the wire VR's word: an `OW` Channel Minimum Value at 32
    bits is one 4-byte sample, and a 2-byte-word conversion would store
    `-200` as `ff ff 38 ff`. The width is the enclosing Waveform Sequence
    item's, because that is where (5400,1004) lives -- the channel item
    that holds these two has none.

    The graph is asserted, fresh and reopened, and not the export: pydicom
    resolves these elements' `OB or OW` from the channel item, finds no
    Waveform Bits Allocated there, and fails the file's export (#674).
    """
    ds = _dataset()
    ds.WaveformSequence = Sequence([_waveform_item(
        bits, interpretation, np.array([0, 1, 2, 3], ">" + code).tobytes(),
        channel=[(0x54000110, vr, np.array([low], ">" + code).tobytes()),
                 (0x54000112, vr, np.array([high], ">" + code).tobytes())])])
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = _instances(session)
        fresh = dict(_channel(inst))
    with DicomSession(persistence_file=db) as session:
        (inst,) = _instances(session)
        reopened = dict(_channel(inst))

    for attrs in (fresh, reopened):
        assert np.frombuffer(attrs["5400,0110"], "<" + code).tolist() == [low]
        assert np.frombuffer(attrs["5400,0112"], "<" + code).tolist() == [high]
    assert _rows(db) == []


def test_channel_minimum_with_no_waveform_bits_allocated_is_kept_and_said(
        tmp_path):
    ds = _dataset()
    ds.WaveformSequence = Sequence([_waveform_item(
        None, "SS", b"\x00\x00\x00\x64",
        channel=[(0x54000110, "OW", b"\xff\x38")])])
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = _instances(session)
        assert _channel(inst)["5400,0110"] == b"\xff\x38"

    assert sorted(_rows(db)) == [
        ("WARNING",
         "Standard tag 5400,0110 (OW) at 5400,0100[0] > 003a,0200[0]: 2 "
         "bytes read from a big-endian source with no usable Waveform Bits "
         "Allocated, so the sample width and byte order are unknown. The "
         "bytes were kept in the byte order they were read in."),
        ("WARNING",
         "Standard tag 5400,1010 (OW) at 5400,0100[0]: 4 bytes read from a "
         "big-endian source with no usable Waveform Bits Allocated, so the "
         "sample width and byte order are unknown. The bytes were kept in "
         "the byte order they were read in."),
    ]


def test_a_discarded_multiplex_group_draws_no_byte_order_row(tmp_path):
    """Groups 1..n are dropped at ingest (#36, #160), so nothing about them was kept.

    A byte-order row for an element of a dropped group would say its bytes
    "were kept"; the group's own DATA_LOSS row is the true account of it.
    Both groups lack Waveform Bits Allocated, so group 0, which is kept,
    still draws its two rows: the filter takes the discarded groups only.
    """
    ds = _dataset()
    ds.WaveformSequence = Sequence([
        _waveform_item(None, "SS", b"\x00\x00\x00\x64",
                       channel=[(0x54000110, "OW", b"\xff\x38")]),
        _waveform_item(None, "SS", b"\x00\x00\x00\x64",
                       channel=[(0x54000110, "OW", b"\xff\x38")]),
    ])
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = _instances(session)
        assert len(inst.sequences["5400,0100"].items) == 1
    rows = _rows(db)

    assert sorted(kind for kind, _ in rows) == ["DATA_LOSS", "WARNING", "WARNING"]
    assert sorted(d for kind, d in rows if kind == "WARNING") == [
        "Standard tag 5400,0110 (OW) at 5400,0100[0] > 003a,0200[0]: 2 "
        "bytes read from a big-endian source with no usable Waveform Bits "
        "Allocated, so the sample width and byte order are unknown. The "
        "bytes were kept in the byte order they were read in.",
        "Standard tag 5400,1010 (OW) at 5400,0100[0]: 4 bytes read from a "
        "big-endian source with no usable Waveform Bits Allocated, so the "
        "sample width and byte order are unknown. The bytes were kept in "
        "the byte order they were read in.",
    ]


def test_a_value_the_size_gate_drops_draws_no_byte_order_row(tmp_path):
    """Dropped with DATA_LOSS above BINARY_RETENTION_MAX_BYTES; no byte order to speak of.

    A ragged `OF` and a private `UN`, both over the gate: each earns its
    DATA_LOSS row and nothing else, because the conversion (and its row)
    comes after the gate, for the values that are kept.
    """
    ds = _dataset()
    ds.add_new(0x00660016, "OF", b"\x3f\x00\x00\x00" * 16384 + b"\xbf\xa0")
    ds.add_new(0x00090010, "LO", "J7 PROBE")
    ds.add_new(0x00091010, "UN", b"\x01\x02" * 32768)
    folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
    rows = _rows(db)

    assert [kind for kind, _ in rows] == ["DATA_LOSS", "DATA_LOSS"]
    assert not [d for _, d in rows if "big-endian" in d]


@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview],
                         ids=["bytes", "bytearray", "memoryview"])
def test_every_bytes_like_value_is_converted(wrap):
    """`_process_safe` hands back `bytes` today; the conversion must not depend on it."""
    from isocenter.io_handlers import _stored_byte_order

    stored = _stored_byte_order(wrap(b"\x03\xe8\x9c\x40"), "OW", "0028,1201",
                                (), True, None)
    assert bytes(stored) == b"\xe8\x03\x40\x9c"
    assert isinstance(stored, bytes)


def test_byte_order_rows_are_one_per_element_and_the_log_is_capped(
        tmp_path, caplog):
    for number in range(7):
        ds = _dataset()
        ds.InstanceNumber = number + 1
        ds.add_new(0x00090010, "LO", "J7 PROBE")
        ds.add_new(0x00091010, "UN", b"\x00\x01\x00\x02")
        folder = _save(tmp_path, ds)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            assert not session.ingest(folder).failures
    lines = [r.getMessage() for r in caplog.records
             if "big-endian" in r.getMessage()]

    assert sum(1 for kind, _ in _rows(db) if kind == "WARNING") == 7
    assert len(lines) == 6
    assert lines[-1] == (
        "... (suppressing further per-element messages for big-endian "
        "values whose byte order could not be converted) ...")


def test_the_corpus_big_endian_palette_exports_as_its_little_endian_twin(
        tmp_path):
    """pydicom's own OBXXXX1A pair: the BE file's palette, exported, is the LE file's."""
    big = get_testdata_file("OBXXXX1A_expb.dcm")
    little = pydicom.dcmread(get_testdata_file("OBXXXX1A.dcm"))
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "one.dcm").write_bytes(open(big, "rb").read())
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(str(folder)).failures
        session.export(str(out), use_compression=False)
    (written,) = _files(out)
    back = pydicom.dcmread(written)

    for tag in (0x00281201, 0x00281202, 0x00281203):
        assert bytes(back[tag].value) == bytes(little[tag].value)
    # A value the swap changes, as a literal: the red table's second entry.
    assert np.frombuffer(bytes(back[0x00281201].value), "<u2")[1] == 256

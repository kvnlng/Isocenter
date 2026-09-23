"""LUT Data read as numbers exports (#653).

LUT Data (0028,3006) is `US or OW`. An Explicit VR source that wrote `US`
hands back ints -- a `MultiValue`, and a list once the store has held it
(#651) -- and every export of it failed the whole file:
`TypeError: With tag (0028,3000) got exception: With tag (0028,3006) got
exception: a bytes-like object is required, not 'MultiValue'`. pydicom
resolves the ambiguous VR at write from the sibling LUT Descriptor, picks
`OW` for any table longer than one entry, and `write_OWvalue` accepts only
bytes. Measured on 448eb75 in the Modality, VOI and Presentation LUT
Sequences, two sequences deep and at the top level, from Explicit LE and
Explicit BE sources, under both `use_compression` values, fresh and
reopened. An Implicit VR source was unaffected: pydicom resolves to `OW`
bytes at read.

The value now decides the arm: numbers are written under `US` (or `SS`),
bytes keep pydicom's resolution.

Every expected value is a literal.
"""
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian, generate_uid)

from isocenter.session import DicomSession

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
TABLE = [0, 1000, 40000, 65535]


def _dataset(syntax):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = syntax
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT653", "DOE^JOHN"
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


def _lut_item(value, vr="US", descriptor=(4, 0, 16)):
    item = Dataset()
    if descriptor is not None:
        item.add_new(0x00283002, "US", list(descriptor))
    item.add_new(0x00283003, "LO", "J7")
    item.add_new(0x00283006, vr, value)
    return item


PLACES = [
    pytest.param(((0x00283000, 0),), id="ModalityLUTSequence"),
    pytest.param(((0x00283010, 0),), id="VOILUTSequence"),
    pytest.param(((0x20500010, 0),), id="PresentationLUTSequence"),
    pytest.param(((0x00283110, 0), (0x00283010, 0)), id="two-deep"),
]


def _place(ds, path, item):
    parent = ds
    for depth, (seq, _index) in enumerate(path):
        child = item if depth == len(path) - 1 else Dataset()
        parent.add_new(seq, "SQ", Sequence([child]))
        parent = child


def _save(tmp_path, ds, syntax):
    folder = tmp_path / "src"
    folder.mkdir()
    pydicom.dcmwrite(str(folder / "one.dcm"), ds,
                     implicit_vr=syntax == ImplicitVRLittleEndian,
                     little_endian=syntax != ExplicitVRBigEndian,
                     force_encoding=True)
    return str(folder)


def _files(folder):
    return sorted(os.path.join(r, f) for r, _d, fs in os.walk(str(folder))
                  for f in fs if f.endswith(".dcm"))


def _raw_lut(path, lut_path):
    """The exported element's VR and entries, without pydicom's read-time resolution."""
    ds = pydicom.dcmread(path)
    for seq, index in lut_path:
        ds = ds[seq].value[index]
    raw = ds.get_item(0x00283006)
    value = raw.value
    return getattr(raw, "VR", None), np.frombuffer(value, "<u2").tolist()


def _errors(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT details FROM audit_log "
                            "WHERE action_type = 'ERROR'").fetchall()


@pytest.mark.parametrize("path", PLACES)
@pytest.mark.parametrize("syntax", [ExplicitVRLittleEndian, ExplicitVRBigEndian],
                         ids=["explicit-le", "explicit-be"])
@pytest.mark.parametrize("compress", [False, True], ids=["native", "j2k"])
def test_lut_data_read_as_numbers_exports(tmp_path, path, syntax, compress):
    ds = _dataset(syntax)
    _place(ds, path, _lut_item(TABLE))
    folder = _save(tmp_path, ds, syntax)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        session.export(str(tmp_path / "fresh"), use_compression=compress)
    with DicomSession(persistence_file=db) as session:
        session.export(str(tmp_path / "reopened"), use_compression=compress)

    for out in ("fresh", "reopened"):
        (written,) = _files(tmp_path / out)
        vr, entries = _raw_lut(written, path)
        assert entries == TABLE
        # Explicit VR on the wire under JPEG 2000: the arm the value chose.
        assert vr == ("US" if compress else None)
    assert _errors(db) == []


def test_top_level_lut_data_read_as_numbers_exports(tmp_path):
    ds = _dataset(ExplicitVRLittleEndian)
    ds.add_new(0x00283002, "US", [4, 0, 16])
    ds.add_new(0x00283006, "US", TABLE)
    folder = _save(tmp_path, ds, ExplicitVRLittleEndian)
    with DicomSession(persistence_file=str(tmp_path / "s.db")) as session:
        assert not session.ingest(folder).failures
        session.export(str(tmp_path / "out"), use_compression=True)
    (written,) = _files(tmp_path / "out")
    assert _raw_lut(written, ()) == ("US", TABLE)


def test_lut_data_numbers_with_no_descriptor_export(tmp_path):
    """pydicom's resolution reads the descriptor; the value's arm does not need it."""
    ds = _dataset(ExplicitVRLittleEndian)
    _place(ds, ((0x00283010, 0),), _lut_item(TABLE, descriptor=None))
    folder = _save(tmp_path, ds, ExplicitVRLittleEndian)
    with DicomSession(persistence_file=str(tmp_path / "s.db")) as session:
        assert not session.ingest(folder).failures
        session.export(str(tmp_path / "out"), use_compression=True)
    (written,) = _files(tmp_path / "out")
    assert _raw_lut(written, ((0x00283010, 0),)) == ("US", TABLE)


@pytest.mark.parametrize("compress", [False, True], ids=["native", "j2k"])
def test_lut_data_read_as_bytes_keeps_its_words(tmp_path, compress):
    """An Implicit VR source: bytes, `OW`, unchanged by this fix."""
    ds = _dataset(ImplicitVRLittleEndian)
    _place(ds, ((0x00283010, 0),),
           _lut_item(np.array(TABLE, "<u2").tobytes(), vr="OW"))
    folder = _save(tmp_path, ds, ImplicitVRLittleEndian)
    with DicomSession(persistence_file=str(tmp_path / "s.db")) as session:
        assert not session.ingest(folder).failures
        session.export(str(tmp_path / "out"), use_compression=compress)
    (written,) = _files(tmp_path / "out")
    assert _raw_lut(written, ((0x00283010, 0),)) == \
        ("OW" if compress else None, TABLE)


@pytest.mark.parametrize("vr, value, arm", [
    ("US or OW", [0, 65535], "US"),
    ("US or OW", 7, "US"),
    ("US or SS or OW", [0, 65535], "US"),
    # US before SS where both fit, which is ruling Q1: trying SS first
    # writes `SS` here and passes every other case (review finding 4).
    ("US or SS or OW", [0, 100], "US"),
    ("US or SS or OW", [-1, 2], "SS"),
    ("US or SS or OW", [-32768, 32767], "SS"),
    ("US or OW", b"\x00\x01", "US or OW"),
    ("US or OW", bytearray(b"\x00\x01"), "US or OW"),
    ("US or OW", memoryview(b"\x00\x01"), "US or OW"),
    ("US or OW", [], "US or OW"),
    ("US or OW", None, "US or OW"),
    # `US or SS` keeps pydicom's Pixel Representation answer when an arm
    # fits, because the header is the standard's decider there (#674); and
    # `OW` is not ambiguous at all, so nothing is decided for it.
    ("US or SS", [1, 2], "US or SS"),
    ("OW", [1, 2], "OW"),
])
def test_the_value_chooses_the_numeric_arm(vr, value, arm):
    from isocenter.io_handlers import _numeric_arm

    assert _numeric_arm(vr, value) == arm


@pytest.mark.parametrize("vr, value", [
    pytest.param("US or OW", [70000], id="above-US"),
    pytest.param("US or OW", 70000, id="scalar-above-US"),
    pytest.param("US or OW", [-1, 2], id="negative-with-no-SS-arm"),
    pytest.param("US or SS or OW", [-1, 65535], id="fits-neither-US-nor-SS"),
    pytest.param("US or SS or OW", [-40000], id="below-SS"),
    pytest.param("US or OW", [1.5, 2.0], id="floats"),
    pytest.param("US or OW", "0\\1", id="text"),
    # The refusal covers all four ambiguous VRs since #674: a value no arm
    # holds is one behaviour, so it is one raise, whether or not the VR has
    # an `OW` arm to say only bytes fit it.
    pytest.param("US or SS", [70000], id="US-or-SS-above-US"),
    pytest.param("OB or OW", [1, 2], id="OB-or-OW-numbers"),
    # The first value outside each arm, under a VR carrying both, because
    # the cases above pin what is *inside* the bounds and an off-by-one in
    # `_fitting_arm` is not a wrong arm but a whole file lost at
    # `dcmwrite` -- the failure class #674 exists to remove. This one
    # helper is now the decider in three places: here, the export-time
    # pass, and its veto.
    pytest.param("US or SS", [65536], id="one-above-US"),
    pytest.param("US or SS", [-32769], id="one-below-SS"),
])
def test_a_value_that_fits_no_numeric_arm_is_refused(vr, value):
    """pydicom's `OW` writer takes only bytes, so no arm can write these (#653).

    Refused here, inside `_merge`'s per-element `try`, so the element is one
    `DATA_LOSS` row instead of `dcmwrite` failing the whole file. The two
    `match`-sharing messages are why the grammar is one sentence: `US or SS`
    has no `OW` arm to mention and the refusal is otherwise the same (#674).
    """
    from isocenter.io_handlers import _numeric_arm

    with pytest.raises(ValueError, match="fits no numeric arm of "):
        _numeric_arm(vr, value)


def test_lut_data_that_fits_no_numeric_arm_is_one_element_lost(tmp_path):
    """A caller's out-of-range table costs its element, not the file (#653).

    Measured on 448eb75: `set_attr` of `[70000, 1]` failed the whole file's
    export with `TypeError: ... a bytes-like object is required, not
    'MultiValue'`, because the refusal was pydicom's, at `dcmwrite`, past
    `_merge`'s per-element `try`.
    """
    ds = _dataset(ExplicitVRLittleEndian)
    _place(ds, ((0x00283010, 0),), _lut_item(TABLE))
    folder = _save(tmp_path, ds, ExplicitVRLittleEndian)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = [i for p in session.store.patients for st in p.studies
                   for se in st.series for i in se.instances]
        inst.sequences["0028,3010"].items[0].set_attr("0028,3006",
                                                      [70000, 1])
        session.export(str(tmp_path / "out"), use_compression=False)
    (written,) = _files(tmp_path / "out")
    item = pydicom.dcmread(written)[0x00283010].value[0]

    assert 0x00283006 not in item
    assert item[0x00283003].value == "J7"
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT action_type, details FROM audit_log WHERE action_type "
            "IN ('WARNING', 'DATA_LOSS', 'ERROR')").fetchall()
    assert rows == [(
        "DATA_LOSS",
        "Tag 0028,3006 not exported (data loss): ValueError: the value fits "
        "no numeric arm of US or OW, and OW holds only bytes")]


def test_a_caller_set_numpy_table_is_one_element_lost(tmp_path):
    """A `numpy` array under an ambiguous numeric tag now costs its element.

    The behaviour change the review measured (finding 3): `_numeric_arm`
    takes only `bytes`, `bytearray` and `memoryview` for a buffer, so an
    `ndarray` -- which pydicom's `write_OWvalue` accepted, since it takes
    anything `pack` can consume -- raises and becomes one `DATA_LOSS` row.
    The wider refusal is deliberate: "anything the buffer protocol
    accepts" would also swallow a numpy *scalar*, which is a number and
    belongs in the `US` arm.

    No ingest produces an array here, and since #767 a caller's `set_attr`
    of one never reaches the exporter either: a nested `set_attr` marks the
    instance changed, so the save `export()` begins with tries to store the
    array and raises `TypeError` (the store's JSON holds no `ndarray`), as
    a top-level `set_attr` of one already did -- what the save should do
    with it is #775. So the array is written into the item's `attributes`
    directly, a value the store never sees: what is pinned here is the
    exporter's refusal, for any path that still hands it one.
    """
    import numpy as np

    ds = _dataset(ExplicitVRLittleEndian)
    _place(ds, ((0x00283010, 0),), _lut_item(TABLE))
    folder = _save(tmp_path, ds, ExplicitVRLittleEndian)
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        (inst,) = [i for p in session.store.patients for st in p.studies
                   for se in st.series for i in se.instances]
        inst.sequences["0028,3010"].items[0].attributes["0028,3006"] = \
            np.array([1, 2, 3, 4], "<u2")
        session.export(str(tmp_path / "out"), use_compression=False)
    (written,) = _files(tmp_path / "out")
    item = pydicom.dcmread(written)[0x00283010].value[0]

    assert 0x00283006 not in item
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT action_type, details FROM audit_log WHERE action_type "
            "IN ('WARNING', 'DATA_LOSS', 'ERROR')").fetchall()
    assert rows == [(
        "DATA_LOSS",
        "Tag 0028,3006 not exported (data loss): ValueError: the value fits "
        "no numeric arm of US or OW, and OW holds only bytes")]


@pytest.mark.parametrize("name, sequence, count, head", [
    pytest.param("mlut_18.dcm", 0x00283000, 4096, [0, 16, 32, 48], id="mlut_18"),
    pytest.param("vlut_04.dcm", 0x00283010, 256, [0, 257, 514, 771], id="vlut_04"),
])
def test_pydicoms_own_lut_files_export(tmp_path, name, sequence, count, head):
    """The two files in pydicom's corpus that hold LUT Data as US: both failed every export."""
    from pydicom.data import get_testdata_file

    folder = tmp_path / "src"
    folder.mkdir()
    (folder / name).write_bytes(open(get_testdata_file(name), "rb").read())
    db = str(tmp_path / "s.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(str(folder)).failures
        session.export(str(tmp_path / "out"), use_compression=True)
    (written,) = _files(tmp_path / "out")
    vr, entries = _raw_lut(written, ((sequence, 0),))
    assert vr == "US"
    assert len(entries) == count
    assert entries[:4] == head
    assert _errors(db) == []

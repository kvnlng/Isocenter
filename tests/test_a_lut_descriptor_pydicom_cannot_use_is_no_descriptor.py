"""A LUT Descriptor that is present and holds no value, or one, is treated as
no descriptor, at ingest and at export (#703).

LUT Data (0028,3006) is `US or OW`, and pydicom decides between the arms
with `ds.LUTDescriptor[0] == 1`. #691 covered the LUT whose descriptor is
absent, where that raises `AttributeError`. A descriptor that is present
but empty reads as `None`, and one holding a single value reads as an
`int`; subscripting either raises `TypeError`, which neither pydicom's
wrapper nor this library's two `except AttributeError` clauses caught:

- an Implicit VR source was refused at ingest (`TypeError: 'NoneType'
  object is not subscriptable`), because pydicom resolves the VR at read;
- its Explicit VR twin ingested, and then lost LUT Data at export with a
  `DATA_LOSS` row in pydicom's words. A STANDARD loss does not grade, so
  that export could be `PASS` without the table.

Both doors in `isocenter.io_handlers` now take that `TypeError` as they
take the `AttributeError`: both twins ingest, both write LUT Data as `OW`
with the source's bytes, and both carry one `WARNING` clause, the same
words. Each test builds its dataset once and writes it both ways; every
expected row is a literal, whole.
"""
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid

from isocenter.entities import Instance, iter_item_tree
from isocenter.io_handlers import populate_attrs
from isocenter.session import DicomSession

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
LUT_DESCRIPTOR, LUT_DATA = 0x00283002, 0x00283006
MODALITY_LUT, VOI_LUT = 0x00283000, 0x00283010
TABLE = np.array([1, 2], "<u2").tobytes()          # b"\x01\x00\x02\x00"
ABSENT = object()

TAIL = (". The written bytes are the source's; what this library had to "
        "choose is the value representation the file declares, which "
        "decides how a reader interprets those bytes.")


def _unusable(count):
    return ("Ambiguous value representation (0028,3006): the LUT Descriptor "
            f"beside it holds {count} value(s), not the three whose first "
            "decides between US and OW, so OW was written" + TAIL)


NO_DESCRIPTOR = ("Ambiguous value representation (0028,3006): the LUT it "
                 "belongs to declares no LUT Descriptor, whose first value "
                 "decides between US and OW, so OW was written" + TAIL)


def _dataset(descriptor, seq, table=TABLE):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT703", "DOE^JOHN"
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
    item = Dataset()
    if descriptor is not ABSENT:
        item.add_new(LUT_DESCRIPTOR, "US", descriptor)
    item.add_new(LUT_DATA, "OW", table)
    ds.add_new(seq, "SQ", Sequence([item]))
    return ds


def _save(folder, ds, implicit):
    os.makedirs(folder)
    pydicom.dcmwrite(os.path.join(folder, "one.dcm"), ds,
                     implicit_vr=implicit, little_endian=True,
                     force_encoding=True)
    return folder


def _rows(db, kind):
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = ?", (kind,))]


def _graph(session):
    (inst,) = [i for p in session.store.patients for st in p.studies
               for se in st.series for i in se.instances]
    return {path: dict(item.attributes) for item, path in iter_item_tree(inst)}


def _path(seq):
    return ((f"{seq >> 16:04x},{seq & 0xFFFF:04x}", 0),)


def _written_lut_data(out, seq):
    """(VR, bytes) of LUT Data as the one exported file holds it."""
    (path,) = [os.path.join(r, f) for r, _d, fs in os.walk(out)
               for f in fs if f.endswith(".dcm")]
    item = pydicom.dcmread(path)[seq][0]
    # `get_item`: the element as the file holds it. `item[LUT_DATA]` would
    # ask pydicom the very question under test, and raise.
    raw = item.get_item(LUT_DATA)
    if raw is None:
        return None
    return (None if raw.VR is None else str(raw.VR)), bytes(raw.value)


UNUSABLE = [
    pytest.param(None, 0, MODALITY_LUT, id="empty-modality-lut"),
    pytest.param(None, 0, VOI_LUT, id="empty-voi-lut"),
    pytest.param([4], 1, MODALITY_LUT, id="one-value-modality-lut"),
    pytest.param([4], 1, VOI_LUT, id="one-value-voi-lut"),
]
SOURCES = [pytest.param(True, id="implicit-source"),
           pytest.param(False, id="explicit-source")]
# This library's native export is Implicit VR; the compressed one names
# its VRs on the wire.
EXPORTS = [pytest.param(False, None, id="native-export"),
           pytest.param(True, "OW", id="j2k-export")]


@pytest.mark.parametrize("descriptor, _count, seq", UNUSABLE)
def test_both_twins_are_ingested_and_hold_the_same_graph(
        tmp_path, descriptor, _count, seq):
    ds = _dataset(descriptor, seq)
    graphs = {}
    for implicit in (True, False):
        name = "implicit" if implicit else "explicit"
        folder = _save(str(tmp_path / name), ds, implicit)
        db = str(tmp_path / (name + ".db"))
        with DicomSession(persistence_file=db) as session:
            summary = session.ingest(folder)
            # Red on main for the implicit twin: failures == [(path,
            # "TypeError: 'NoneType' object is not subscriptable")], or
            # "'int' object ..." for the one-value descriptor.
            assert (summary.ingested, summary.failures) == (1, [])
            graphs[name] = _graph(session)
        assert _rows(db, "ERROR") == []
    assert graphs["implicit"][_path(seq)]["0028,3006"] == b"\x01\x00\x02\x00"
    assert graphs["implicit"] == graphs["explicit"]


@pytest.mark.parametrize("compress, wire_vr", EXPORTS)
@pytest.mark.parametrize("implicit", SOURCES)
@pytest.mark.parametrize("descriptor, count, seq", UNUSABLE)
def test_lut_data_is_written_ow_with_one_warning_and_no_loss(
        tmp_path, descriptor, count, seq, implicit, compress, wire_vr):
    folder = _save(str(tmp_path / "src"), _dataset(descriptor, seq), implicit)
    db = str(tmp_path / "t.db")
    out = str(tmp_path / "out")
    with DicomSession(persistence_file=db) as session:
        assert session.ingest(folder).failures == []
        assert session.export(out, use_compression=compress).written == 1
    # Red on main in every cell: the implicit source is refused; the
    # explicit one exports with no (0028,3006) and a DATA_LOSS row.
    assert _written_lut_data(out, seq) == (wire_vr, b"\x01\x00\x02\x00")
    assert _rows(db, "DATA_LOSS") == []
    assert _rows(db, "WARNING") == [_unusable(count)]


PRIVATE_CREATOR, PRIVATE_US = 0x00190010, 0x0019100A
PRIVATE_BYTES = b"\x10\x00"                          # 16, as the file holds it


def _with_unstated_private(ds, seq, implicit):
    """Add, at the root and inside the LUT item, a private element of a
    creator pydicom's private dictionary knows as `US`, with no VR stated
    on the wire: Implicit VR names none, and Explicit VR says `UN`."""
    for holder in (ds, ds[seq][0]):
        holder.add_new(PRIVATE_CREATOR, "LO", "SIEMENS MR HEADER")
        if implicit:
            holder.add_new(PRIVATE_US, "US", 16)
        else:
            holder.add_new(PRIVATE_US, "UN", PRIVATE_BYTES)
    return ds


def _written_private(out, seq):
    """The private element's bytes as the exported file holds them, at
    the root and in the LUT item, unconverted."""
    (path,) = [os.path.join(r, f) for r, _d, fs in os.walk(out)
               for f in fs if f.endswith(".dcm")]
    ds = pydicom.dcmread(path)
    return [bytes(holder.get_item(PRIVATE_US, keep_deferred=True).value)
            for holder in (ds, ds[seq][0])]


@pytest.mark.parametrize("compress, wire_vr", EXPORTS)
@pytest.mark.parametrize("implicit", SOURCES)
@pytest.mark.parametrize("descriptor, count, seq", UNUSABLE[::2])
def test_an_unusable_descriptor_beside_an_unstated_private_vr_keeps_both(
        tmp_path, descriptor, count, seq, implicit, compress, wire_vr):
    """#703 beside #740, in one file and in one item. #740 asks each tag's
    raw element whether it is a private element with no stated VR before
    anything reads it, and holds such an element as the file's bytes;
    #703 lets a LUT Data whose descriptor cannot decide be read as `OW`.
    Both read the dataset without converting what they are not about
    (`get_item(..., keep_deferred=True)`), so neither changes what the
    other finds: the file is ingested, LUT Data is `OW` with its bytes
    and its one clause, and the private element is the two bytes the
    file held, at both depths, in the graph and in the export."""
    ds = _with_unstated_private(_dataset(descriptor, seq), seq, implicit)
    folder = _save(str(tmp_path / "src"), ds, implicit)
    db = str(tmp_path / "t.db")
    out = str(tmp_path / "out")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
        assert (summary.ingested, summary.failures) == (1, [])
        graph = _graph(session)
        assert graph[()]["0019,100a"] == PRIVATE_BYTES
        assert graph[_path(seq)]["0019,100a"] == PRIVATE_BYTES
        assert graph[_path(seq)]["0028,3006"] == b"\x01\x00\x02\x00"
        assert session.export(out, use_compression=compress).written == 1
    assert _written_lut_data(out, seq) == (wire_vr, b"\x01\x00\x02\x00")
    assert _written_private(out, seq) == [PRIVATE_BYTES, PRIVATE_BYTES]
    assert _rows(db, "ERROR") == [] and _rows(db, "DATA_LOSS") == []
    assert _unusable(count) in _rows(db, "WARNING")


@pytest.mark.parametrize("descriptor, _count, seq", UNUSABLE)
def test_the_twins_rows_are_the_same_words(tmp_path, descriptor, _count, seq):
    ds = _dataset(descriptor, seq)
    rows = {}
    for implicit in (True, False):
        name = "implicit" if implicit else "explicit"
        folder = _save(str(tmp_path / name), ds, implicit)
        db = str(tmp_path / (name + ".db"))
        with DicomSession(persistence_file=db) as session:
            session.ingest(folder)
            if session.store.patients:
                session.export(str(tmp_path / (name + "-out")),
                               use_compression=True)
        rows[name] = sorted(_rows(db, "WARNING") + _rows(db, "DATA_LOSS")
                            + _rows(db, "ERROR"))
    assert len(rows["explicit"]) == 1
    assert rows["implicit"] == rows["explicit"]


@pytest.mark.parametrize("implicit", SOURCES)
def test_an_absent_descriptor_still_takes_its_own_clause(tmp_path, implicit):
    """#691's sentence says the LUT declares no descriptor. That is true
    of this file and false of the ones above, so there are two clauses."""
    folder = _save(str(tmp_path / "src"), _dataset(ABSENT, MODALITY_LUT),
                   implicit)
    db = str(tmp_path / "t.db")
    out = str(tmp_path / "out")
    with DicomSession(persistence_file=db) as session:
        assert session.ingest(folder).failures == []
        assert session.export(out, use_compression=True).written == 1
    assert _written_lut_data(out, MODALITY_LUT) == ("OW", b"\x01\x00\x02\x00")
    assert _rows(db, "WARNING") == [NO_DESCRIPTOR]


@pytest.mark.parametrize("implicit", SOURCES)
@pytest.mark.parametrize("descriptor, table, vr", [
    ([1, 0, 16], b"\x01\x00", "US"),
    ([2, 0, 16], b"\x01\x00\x02\x00", "OW"),
], ids=["one-entry-is-us", "two-entries-is-ow"])
def test_a_descriptor_pydicom_can_use_decides_and_nothing_is_said(
        tmp_path, descriptor, table, vr, implicit):
    folder = _save(str(tmp_path / "src"),
                   _dataset(descriptor, MODALITY_LUT, table), implicit)
    db = str(tmp_path / "t.db")
    out = str(tmp_path / "out")
    with DicomSession(persistence_file=db) as session:
        assert session.ingest(folder).failures == []
        assert session.export(out, use_compression=True).written == 1
    assert _written_lut_data(out, MODALITY_LUT) == (vr, table)
    assert _rows(db, "WARNING") == _rows(db, "DATA_LOSS") == []


def test_a_type_error_that_is_not_an_ambiguous_vr_still_raises():
    """The read's fallback takes pydicom's resolution failure and nothing
    else: a `TypeError` out of any other element's read refuses the file as
    it always did."""
    class Broken(Dataset):
        def __getitem__(self, key):
            if key == 0x00100010:
                raise TypeError("not an ambiguous VR")
            return super().__getitem__(key)

    ds = Broken()
    ds.PatientName = "DOE^JOHN"
    with pytest.raises(TypeError, match="not an ambiguous VR"):
        populate_attrs(ds, Instance("1.2.9", CT_IMAGE, 1))


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    (line,) = [ln for ln in path.read_text(encoding="utf-8").splitlines()
               if ln.startswith("| **Validation Status** |")]
    return line


@pytest.mark.parametrize("descriptor, grade", [
    ([2, 0, 16], "PASS"), (None, "REVIEW_REQUIRED")],
    ids=["usable-descriptor", "empty-descriptor"])
def test_the_explicit_twins_run_grades_on_the_warning(tmp_path, descriptor, grade):
    """Until #703 the explicit twin lost its table with a STANDARD
    `DATA_LOSS` row, which does not grade, so the run could be `PASS`
    without it. It now keeps the table and says what it chose, and a
    `WARNING` grades. The usable descriptor is the control: the same run
    with nothing to say is `PASS`, so the other's grade is the row's."""
    folder = _save(str(tmp_path / "src"), _dataset(descriptor, MODALITY_LUT),
                   False)
    with DicomSession(persistence_file=str(tmp_path / "t.db")) as session:
        session.ingest(folder)
        session.audit()
        session.anonymize()
        session.export(str(tmp_path / "out"))
        line = _grade(session, tmp_path)
    assert grade in line
    assert ("PASS" in line) == (grade == "PASS")

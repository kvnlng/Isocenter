"""An Implicit VR source whose ambiguous VR pydicom cannot resolve at read is
ingested as its Explicit VR twin is (#691).

pydicom resolves `US or OW`, `US or SS` and the rest when an element is
read, with no ancestors, and raises where the deciding sibling is absent
from the nearest dataset. Under Implicit VR, or for an element carried as
`UN`, nothing on the wire answers instead, so `populate_attrs`' walk ended
at that element and the file was refused, while the copy that names the
arm ingested.

Each test builds its dataset once and writes it both ways, so "as its
explicit twin is" compares one source read through two syntaxes. The
explicit side is the path that already worked before #691, which is what
makes it an independent expectation; everything else is a literal.
"""
import os
import shutil
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid

from isocenter.entities import Instance, iter_item_tree
from isocenter.io_handlers import BINARY_RETENTION_MAX_BYTES, populate_attrs
from isocenter.session import DicomSession

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
LUT_DESCRIPTOR, LUT_DATA = 0x00283002, 0x00283006
MODALITY_LUT, VOI_LUT = 0x00283000, 0x00283010
SMALLEST_PIXEL = 0x00280106


def _dataset(*, pixels=True, pr=0):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT691", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    ds.SliceThickness, ds.KVP = "1.0", "120"
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    if pixels:
        ds.Rows = ds.Columns = 2
        ds.BitsAllocated = ds.BitsStored = 8
        ds.HighBit = 7
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        if pr is not None:
            ds.PixelRepresentation = pr
        ds.PixelData = bytes(4)
    return ds


def _lut(ds, *, seq=MODALITY_LUT, descriptor=None,
         entries=(0, 1000, 40000, 65535)):
    item = Dataset() if seq else ds
    if descriptor is not None:
        item.add_new(LUT_DESCRIPTOR, "US", list(descriptor))
    item.add_new(LUT_DATA, "OW", np.array(entries, "<u2").tobytes())
    if seq:
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


def _ingest(tmp_path, ds, implicit, name):
    """(summary, fresh graph or None, reopened graph or None, db)."""
    folder = _save(str(tmp_path / name), ds, implicit)
    db = str(tmp_path / (name + ".db"))
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
        fresh = _graph(session) if summary.ingested else None
    reopened = None
    if summary.ingested:
        with DicomSession(persistence_file=db) as session:
            reopened = _graph(session)
    return summary, fresh, reopened, db


LUT_SHAPES = [
    pytest.param(dict(seq=MODALITY_LUT), id="modality-lut"),
    pytest.param(dict(seq=VOI_LUT), id="voi-lut"),
    pytest.param(dict(seq=None), id="top-level"),
    pytest.param(dict(seq=MODALITY_LUT, entries=()), id="empty"),
    pytest.param(dict(seq=MODALITY_LUT,
                      entries=tuple(range(BINARY_RETENTION_MAX_BYTES // 2))),
                 id="at-the-retention-threshold"),
]


@pytest.mark.parametrize("shape", LUT_SHAPES)
def test_an_implicit_lut_with_no_descriptor_is_held_as_its_explicit_twin_is(
        tmp_path, shape):
    ds = _lut(_dataset(), **shape)
    implicit = _ingest(tmp_path, ds, True, "implicit")
    explicit = _ingest(tmp_path, ds, False, "explicit")
    # Red on main: failures == [(uid, "AttributeError: Failed to resolve
    # ambiguous VR for tag (0028,3006): 'Dataset' object has no attribute
    # 'LUTDescriptor'")]
    assert implicit[0].failures == []
    assert implicit[1] == explicit[1]
    assert implicit[2] == explicit[2]
    assert _rows(implicit[3], "WARNING") == _rows(explicit[3], "WARNING") == []


def test_an_oversized_implicit_lut_with_no_descriptor_is_dropped_as_its_explicit_twin_is(
        tmp_path):
    ds = _lut(_dataset(),
              entries=tuple(range(BINARY_RETENTION_MAX_BYTES // 2 + 1)))
    implicit = _ingest(tmp_path, ds, True, "implicit")
    explicit = _ingest(tmp_path, ds, False, "explicit")
    loss = ["Standard tag 0028,3006 (OW) was not ingested; its value exceeds "
            "the 65534-byte retention threshold, so it is not held in the "
            "object graph, so it is not in the exported file."]
    assert _rows(explicit[3], "DATA_LOSS") == loss
    # Red on main: the file is refused, and no DATA_LOSS row is written.
    assert _rows(implicit[3], "DATA_LOSS") == loss
    assert implicit[1] == explicit[1]


def test_an_implicit_export_of_a_descriptorless_lut_is_reingested(tmp_path):
    """Replaces J10's `..._cannot_be_reingested`: our own native export is an
    Implicit VR file of exactly the shape #691 refused."""
    ds = _lut(_dataset())
    src = _save(str(tmp_path / "src"), ds, False)
    db = str(tmp_path / "a.db")
    with DicomSession(persistence_file=db) as session:
        assert session.ingest(src).failures == []
        assert session.export(str(tmp_path / "first"),
                              use_compression=False).written == 1
    w2 = ("Ambiguous value representation (0028,3006): the LUT it belongs to "
          "declares no LUT Descriptor, whose first value decides between US "
          "and OW, so OW was written. The written bytes are the source's; "
          "what this library had to choose is the value representation the "
          "file declares, which decides how a reader interprets those bytes.")
    assert _rows(db, "WARNING") == [w2]
    (first,) = [os.path.join(r, f) for r, _d, fs in os.walk(tmp_path / "first")
                for f in fs if f.endswith(".dcm")]
    again = tmp_path / "again"
    again.mkdir()
    shutil.copy(first, str(again / "two.dcm"))
    db2 = str(tmp_path / "b.db")
    with DicomSession(persistence_file=db2) as session:
        summary = session.ingest(str(again))
        # Red on main: ingested == 0, the AttributeError above.
        assert (summary.ingested, summary.failures) == (1, [])
        assert _graph(session)[((f"{MODALITY_LUT >> 16:04x},"
                                 f"{MODALITY_LUT & 0xFFFF:04x}", 0),)][
            "0028,3006"] == np.array([0, 1000, 40000, 65535], "<u2").tobytes()
        assert session.export(str(tmp_path / "second"),
                              use_compression=False).written == 1
    assert _rows(db2, "WARNING") == [w2]


def test_lut_data_that_has_a_descriptor_reads_with_no_warning_row(tmp_path):
    """The negative case: nothing about an unambiguous LUT changes."""
    ds = _lut(_dataset(), descriptor=(4, 0, 16))
    summary, fresh, _reopened, db = _ingest(tmp_path, ds, True, "implicit")
    assert summary.failures == []
    assert _rows(db, "WARNING") == _rows(db, "DATA_LOSS") == []
    assert fresh[(("0028,3000", 0),)]["0028,3006"] == np.array(
        [0, 1000, 40000, 65535], "<u2").tobytes()


def test_an_implicit_icon_with_no_pixel_representation_is_held_as_its_explicit_twin_is(
        tmp_path):
    ds = _dataset(pixels=False)
    icon = Dataset()
    icon.Rows = icon.Columns = 2
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit = 7
    icon.SamplesPerPixel = 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.add_new(SMALLEST_PIXEL, "US", 3)
    icon.PixelData = bytes(4)
    ds.IconImageSequence = Sequence([icon])
    implicit = _ingest(tmp_path, ds, True, "implicit")
    explicit = _ingest(tmp_path, ds, False, "explicit")
    # Red on main: "Failed to resolve ambiguous VR for tag (0028,0106):
    # 'Dataset' object has no attribute 'PixelRepresentation'"
    assert implicit[0].failures == []
    assert implicit[1][(("0088,0200", 0),)]["0028,0106"] == 3
    assert implicit[1] == explicit[1]
    assert implicit[2] == explicit[2]


def test_an_implicit_source_with_pixels_and_no_pixel_representation_is_refused_as_its_explicit_twin_is(
        tmp_path):
    ds = _dataset(pr=None)
    ds.add_new(SMALLEST_PIXEL, "US", 3)
    implicit = _ingest(tmp_path, ds, True, "implicit")
    explicit = _ingest(tmp_path, ds, False, "explicit")
    reason = ("Decompression Failed: AttributeError: Missing required "
              "element: (0028,0103) 'Pixel Representation'")
    assert [r for _u, r in explicit[0].failures] == [reason]
    # Red on main: the reason is pydicom's read-time VR refusal instead.
    assert [r for _u, r in implicit[0].failures] == [reason]


def test_a_private_sequence_holding_a_descriptorless_lut_is_ingested(tmp_path):
    """The `UN`-recovered sequence route (`_sequence_from_un_bytes`)."""
    ds = _dataset()
    block = ds.private_block(0x0009, "J12PROBE", create=True)
    item = Dataset()
    item.add_new(LUT_DATA, "OW", np.array([1, 2], "<u2").tobytes())
    block.add_new(0x10, "SQ", Sequence([item]))
    summary, fresh, _reopened, _db = _ingest(tmp_path, ds, True, "implicit")
    # Red on main: the AttributeError, raised through populate_attrs'
    # private-sequence branch.
    assert summary.failures == []
    assert fresh[(("0009,1010", 0),)]["0028,3006"] == b"\x01\x00\x02\x00"


def test_an_attribute_error_that_is_not_an_ambiguous_vr_still_raises():
    """The fallback catches pydicom's read-time resolution failure and
    nothing else: any other AttributeError out of `ds[tag]` is the refusal
    it always was."""
    class Broken(Dataset):
        def __getitem__(self, key):
            if key == 0x00100010:
                raise AttributeError("not an ambiguous VR")
            return super().__getitem__(key)

    ds = Broken()
    ds.PatientName = "DOE^JOHN"
    with pytest.raises(AttributeError, match="not an ambiguous VR"):
        populate_attrs(ds, Instance("1.2.9", CT_IMAGE, 1))


def test_an_implicit_icon_under_a_root_pixel_representation_reads_signed_without_the_fallback(
        tmp_path, monkeypatch):
    """Why the fallback may hand the resolver `[ds]` and nothing above it.

    pydicom propagates a Pixel Representation declared at the root into
    every sequence item it reads, so an icon's `US or SS` element resolves at
    read and never raises: the fallback is reached only where no ancestor
    declares one, and then walking the real chain would find nothing more.
    Pinned both ways: the value is the signed -5 the root's 1 names (a chain
    with no declarer would read 65531), and the resolver the fallback calls
    is never entered on the read. The second is the one that pins the
    claim: an item pydicom has stamped with `_pixel_rep` would read -5
    through the resolver too.
    """
    ds = _dataset(pr=1)
    icon = Dataset()
    icon.Rows = icon.Columns = 2
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit = 7
    icon.SamplesPerPixel = 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.add_new(SMALLEST_PIXEL, "SS", -5)
    icon.PixelData = bytes(4)
    ds.IconImageSequence = Sequence([icon])
    implicit = _ingest(tmp_path, ds, True, "implicit")
    explicit = _ingest(tmp_path, ds, False, "explicit")
    assert implicit[0].failures == []
    assert implicit[1][(("0088,0200", 0),)]["0028,0106"] == -5
    assert implicit[1] == explicit[1]

    import isocenter.io_handlers as io_handlers

    def entered(*_args, **_kwargs):
        raise AssertionError("the #691 fallback was entered")

    monkeypatch.setattr(io_handlers, "_resolve_one_ambiguous_vr", entered)
    read = pydicom.dcmread(
        os.path.join(str(tmp_path / "implicit"), "one.dcm"), force=True)
    instance = Instance(read.SOPInstanceUID, CT_IMAGE, 1)
    populate_attrs(read, instance)
    ((item, _path),) = [(i, p) for i, p in iter_item_tree(instance)
                        if p == (("0088,0200", 0),)]
    assert item.attributes["0028,0106"] == -5


def _icon(*, value=-5, vr="SS", pixels=True, pr=None):
    item = Dataset()
    item.Rows = item.Columns = 2
    item.BitsAllocated = item.BitsStored = 8
    item.HighBit = 7
    item.SamplesPerPixel = 1
    item.PhotometricInterpretation = "MONOCHROME2"
    if pr is not None:
        item.PixelRepresentation = pr
    item.add_new(SMALLEST_PIXEL, vr, value)
    if pixels:
        item.PixelData = bytes(4)
    return item


def _private_sequence(owner, item, creator="J12PRIV"):
    owner.private_block(0x0009, creator, create=True).add_new(
        0x10, "SQ", Sequence([item]))
    return owner


def _smallest_values(graph):
    return sorted((path, attrs["0028,0106"]) for path, attrs in graph.items()
                  if "0028,0106" in attrs)


def _private_icon(root_pr):
    return _private_sequence(_dataset(pr=root_pr), _icon())


def _pixel_less_private_item(root_pr):
    return _private_sequence(_dataset(pr=root_pr), _icon(pixels=False))


def _standard_icon_inside_a_private_item(root_pr):
    holder = Dataset()
    holder.IconImageSequence = Sequence([_icon()])
    return _private_sequence(_dataset(pr=root_pr), holder)


def _private_sequence_inside_a_private_item(root_pr):
    return _private_sequence(
        _dataset(pr=root_pr),
        _private_sequence(Dataset(), _icon(), creator="J12NEST"))


def _private_sequence_inside_a_standard_item(root_pr):
    ds = _dataset(pr=root_pr)
    holder = Dataset()
    ds.ModalityLUTSequence = Sequence([
        _private_sequence(holder, _icon(), creator="J12NEST")])
    return ds


ROOT_SIGNED_SHAPES = [
    pytest.param(_private_icon, id="private-item-with-pixels"),
    pytest.param(_pixel_less_private_item, id="pixel-less-private-item"),
    pytest.param(_standard_icon_inside_a_private_item,
                 id="standard-sequence-inside-a-private-item"),
    pytest.param(_private_sequence_inside_a_private_item,
                 id="private-sequence-inside-a-private-item"),
    pytest.param(_private_sequence_inside_a_standard_item,
                 id="private-sequence-inside-a-standard-item"),
]


@pytest.mark.parametrize("build", ROOT_SIGNED_SHAPES)
def test_a_private_sequence_inherits_the_root_pixel_representation_as_its_explicit_twin_does(
        tmp_path, build):
    """Under Implicit VR a private sequence arrives as `UN` bytes, and
    re-parsing them (`_sequence_from_un_bytes`) propagated no `_pixel_rep`
    into its items the way pydicom's own `SQ` read does. A root's Pixel
    Representation of 1 then never reached the item: a signed -5 read as
    65531, and the export wrote a `WARNING` row contradicting a header the
    source never contradicted. The pixel-less item is the shape that
    already ingested before #691, reading 65531; it now reads what its
    explicit twin reads."""
    ds = build(1)
    implicit = _ingest(tmp_path, ds, True, "implicit")
    explicit = _ingest(tmp_path, ds, False, "explicit")
    assert implicit[0].failures == explicit[0].failures == []
    assert [v for _p, v in _smallest_values(implicit[1])] == [-5]
    assert implicit[1] == explicit[1]
    assert implicit[2] == explicit[2]


def test_a_private_icon_under_a_root_pixel_representation_exports_with_no_warning_and_no_fallback(
        tmp_path, monkeypatch):
    """The export half of the case above, and the ancestor claim behind it:
    with the root's Pixel Representation propagated, pydicom resolves the
    element at read, so the #691 fallback -- which sees only the item -- is
    never entered."""
    folder = _save(str(tmp_path / "implicit"), _private_icon(1), True)
    db = str(tmp_path / "implicit.db")
    with DicomSession(persistence_file=db) as session:
        assert session.ingest(folder).failures == []
        assert session.export(str(tmp_path / "out"),
                              use_compression=False).written == 1
    assert _rows(db, "WARNING") == []

    import isocenter.io_handlers as io_handlers

    def entered(*_args, **_kwargs):
        raise AssertionError("the #691 fallback was entered")

    monkeypatch.setattr(io_handlers, "_resolve_one_ambiguous_vr", entered)
    read = pydicom.dcmread(os.path.join(folder, "one.dcm"), force=True)
    instance = Instance(read.SOPInstanceUID, CT_IMAGE, 1)
    populate_attrs(read, instance)
    assert [v for _p, v in _smallest_values(
        {p: i.attributes for i, p in iter_item_tree(instance)})] == [-5]


@pytest.mark.parametrize("root_pr, item_pr", [(0, None), (1, 0)],
                         ids=["root-unsigned", "item-declares-its-own"])
def test_a_private_item_takes_the_nearest_declared_pixel_representation(
        tmp_path, root_pr, item_pr):
    """The negative cases of that propagation: an unsigned root stays
    unsigned, and an item's own Pixel Representation outranks the root's."""
    ds = _private_sequence(_dataset(pr=root_pr),
                           _icon(value=65531, vr="US", pr=item_pr))
    implicit = _ingest(tmp_path, ds, True, "implicit")
    explicit = _ingest(tmp_path, ds, False, "explicit")
    assert implicit[0].failures == []
    assert [v for _p, v in _smallest_values(implicit[1])] == [65531]
    assert implicit[1] == explicit[1]


# The (0028,0106) element of `_icon(value=3, vr="US")` as `dcmwrite` puts it
# on the wire, and the same element relabelled `UN` (PS3.5 7.1.2: two
# reserved bytes and a 4-byte length).
_US_ELEMENT = {True: (b"\x28\x00\x06\x01US\x02\x00\x03\x00",
                      b"\x28\x00\x06\x01UN\x00\x00\x02\x00\x00\x00\x03\x00"),
               False: (b"\x00\x28\x01\x06US\x00\x02\x00\x03",
                       b"\x00\x28\x01\x06UN\x00\x00\x00\x00\x00\x02\x00\x03")}


@pytest.mark.parametrize("little_endian", [True, False],
                         ids=["explicit-little-endian", "explicit-big-endian"])
def test_an_ambiguous_element_carried_as_un_is_read_in_the_sources_byte_order(
        tmp_path, little_endian):
    """An element written with VR `UN` reaches the #691 fallback under an
    explicit syntax too, and the fallback converts `US or SS` bytes itself:
    it must read them in the byte order the source was written in. A
    big-endian 3 is not 768."""
    ds = _dataset(pixels=False)
    ds.IconImageSequence = Sequence([_icon(value=3, vr="US")])
    ds.file_meta.TransferSyntaxUID = (
        "1.2.840.10008.1.2.1" if little_endian else "1.2.840.10008.1.2.2")
    graphs = {}
    for label, relabel in (("un", True), ("us", False)):
        folder = tmp_path / label
        folder.mkdir()
        path = str(folder / "one.dcm")
        pydicom.dcmwrite(path, ds, implicit_vr=False,
                         little_endian=little_endian, force_encoding=True)
        if relabel:
            old, new = _US_ELEMENT[little_endian]
            with open(path, "rb") as fh:
                raw = fh.read()
            assert raw.count(old) == 1
            with open(path, "wb") as fh:
                fh.write(raw.replace(old, new))
        with DicomSession(persistence_file=str(tmp_path / (label + ".db"))) \
                as session:
            summary = session.ingest(str(folder))
            assert summary.failures == []
            graphs[label] = _graph(session)
    assert _smallest_values(graphs["un"]) == [((("0088,0200", 0),), 3)]
    assert graphs["un"] == graphs["us"]


def test_a_malformed_root_pixel_representation_reaches_a_private_item_as_a_standard_one(
        tmp_path):
    """The propagation into a re-parsed private item copies the parent's
    Pixel Representation as pydicom's own `_set_pixel_representation` does:
    unconverted. A root carrying a malformed two-valued `[1, 0]` is not a
    number, so converting it raised `TypeError` and refused a file that
    ingested before the propagation existed; and pydicom's arm compares the
    raw value (`[1, 0] == 0` is false), so the item in a standard sequence
    reads signed. -5 rather than 3, because only a value outside 0..32767
    tells a private item that reads as the standard one from one left
    unstamped."""
    ds = _dataset(pixels=False)
    ds.add_new(0x00280103, "US", [1, 0])
    _private_sequence(ds, _icon(pixels=False))
    ds.IconImageSequence = Sequence([_icon(pixels=False)])
    summary, fresh, _reopened, _db = _ingest(tmp_path, ds, True, "implicit")
    assert summary.failures == []
    assert _smallest_values(fresh) == [
        ((("0009,1010", 0),), -5), ((("0088,0200", 0),), -5)]

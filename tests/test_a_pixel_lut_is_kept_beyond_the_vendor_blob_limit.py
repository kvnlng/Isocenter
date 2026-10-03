"""A pixel-interpretation LUT over 65534 bytes is kept and exported (#902).

`BINARY_RETENTION_MAX_BYTES` (65534) gates every unrouted binary value by
size, whatever its tag; it exists for the megabyte vendor blobs. Measured on
`main` at 7e66fec6, it also took the tables that say how an image's pixels
display:
- a VOI LUT of 40000 or 65536 16-bit entries was exported with its
  descriptor and no LUT Data;
- a PALETTE COLOR image with 65536-entry 16-bit palettes was exported
  PALETTE COLOR with no palette data;
- golden-cohort member `gdcm-US-ALOKA-16.dcm` (and `_big`) lost its Red and
  Green segmented palettes and kept Blue.
Each draws a STANDARD `DATA_LOSS` row, which does not grade, so all of them
were PASS.

The owner's ruling (Q4, A): LUT Data, Gray LUT Data and the six palette
data elements are kept up to 393216 bytes (65536 entries x 3 words x 2
bytes), one ceiling for all eight, and the limit is unchanged for everything
else. A conformant non-segmented table is at most 131072 bytes (65536
entries of 16 bits), so it never reaches the ceiling; PS3.3 C.7.9.2 does not
itself bound a segmented encoding, so 393216 is a ruled cap. 131072 was
tried for all eight after review and overruled.
"""
import shutil

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"

#: The ceiling for all eight: 65536 entries x 3 words x 2 bytes.
LUT_MAX = 393216

#: The largest table a LUT Descriptor can declare: 65536 entries of 16 bits.
TABLE_MAX = 131072


def _pattern(nbytes):
    return (bytes(range(256)) * (nbytes // 256 + 1))[:nbytes]


def _source(folder, *, syntax=ExplicitVRLittleEndian, extra=None):
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.902.1"
    meta.TransferSyntaxUID = syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = SC, meta.MediaStorageSOPInstanceUID
    ds.PatientID, ds.PatientName = "P902", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.902.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.902.3"
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


def _voi_lut(nbytes):
    def extra(ds):
        item = Dataset()
        item.add_new(0x00283002, "US", [nbytes // 2 % 65536, 0, 16])
        item.add_new(0x00283006, "OW", _pattern(nbytes))
        ds.add_new(0x00283010, "SQ", [item])
    return extra


def _run(tmp_path, extra, *, syntax=ExplicitVRLittleEndian, compress=True):
    _source(tmp_path / "src", syntax=syntax, extra=extra)
    with DicomSession(str(tmp_path / "s.db")) as s:
        s.ingest(str(tmp_path / "src"))
        (p,) = s.store.patients
        inst = p.studies[0].series[0].instances[0]
        s.export(str(tmp_path / "out"), use_compression=compress,
                 show_progress=False)
        losses = s.store_backend.get_audit_losses()
    (written,) = list((tmp_path / "out").rglob("*.dcm"))
    return inst, pydicom.dcmread(str(written)), losses


def _lut_rows(losses, tag):
    return [r for r in losses if f"tag {tag} (" in r[2]]


def test_a_full_voi_lut_is_exported_as_ow_through_export(tmp_path):
    """First, the wire VR through `export()` itself, at the non-segmented
    most (131072 bytes, descriptor first value 0, meaning 65536 entries):
    `_resolve_ambiguous_vrs` asks pydicom, which decides `US or OW` from
    the descriptor. `OW` has a 4-byte length, so #692's relabel does not
    touch it."""
    _inst, ds, losses = _run(tmp_path, _voi_lut(TABLE_MAX))
    item = ds.VOILUTSequence[0]
    elem = item[0x00283006]
    assert elem.VR == "OW"
    assert bytes(elem.value) == _pattern(TABLE_MAX)
    assert list(item.LUTDescriptor) == [0, 0, 16]
    assert _lut_rows(losses, "0028,3006") == []


@pytest.mark.parametrize("syntax", [ExplicitVRLittleEndian, ImplicitVRLittleEndian],
                         ids=["explicit", "implicit"])
@pytest.mark.parametrize("nbytes", [80000, TABLE_MAX], ids=["40000", "65536"])
def test_a_voi_lut_over_the_vendor_limit_is_kept(tmp_path, syntax, nbytes):
    inst, ds, losses = _run(tmp_path, _voi_lut(nbytes), syntax=syntax)
    (held_item,) = inst.sequences["0028,3010"].items
    held = held_item.attributes["0028,3006"]
    assert bytes(held) == _pattern(nbytes)
    elem = ds.VOILUTSequence[0][0x00283006]
    assert elem.VR == "OW" and bytes(elem.value) == _pattern(nbytes)
    assert _lut_rows(losses, "0028,3006") == []


def _palette(sizes, *, segmented):
    base = 0x1221 if segmented else 0x1201

    def extra(ds):
        ds.PhotometricInterpretation = "PALETTE COLOR"
        for offset in range(3):
            ds.add_new(0x00281101 + offset, "US", [0, 0, 16])
        for offset, nbytes in enumerate(sizes):
            ds.add_new(0x00280000 + base + offset, "OW",
                       _pattern(nbytes)[::-1] if offset % 2 else _pattern(nbytes))
    return extra


def test_full_16_bit_palettes_are_all_exported(tmp_path):
    _inst, ds, losses = _run(tmp_path, _palette([TABLE_MAX] * 3, segmented=False),
                             compress=False)
    assert ds.PhotometricInterpretation == "PALETTE COLOR"
    for offset in range(3):
        tag = 0x00281201 + offset
        expected = _pattern(TABLE_MAX)[::-1] if offset % 2 else _pattern(TABLE_MAX)
        assert bytes(ds[tag].value) == expected, hex(tag)
        assert _lut_rows(losses, f"0028,{0x1201 + offset:04x}") == []


def test_segmented_palettes_of_the_cohorts_sizes_are_kept(tmp_path):
    """ALOKA's sizes (87818, 113784, 55364): before, Red and Green were
    dropped and Blue kept, a colour image with one palette of three."""
    sizes = [87818, 113784, 55364]
    _inst, ds, losses = _run(tmp_path, _palette(sizes, segmented=True),
                             compress=False)
    for offset, nbytes in enumerate(sizes):
        tag = 0x00281221 + offset
        expected = _pattern(nbytes)[::-1] if offset % 2 else _pattern(nbytes)
        assert bytes(ds[tag].value) == expected, hex(tag)
    assert [r for r in losses if "0028,122" in r[2]] == []


def test_a_segmented_palette_at_its_ceiling_is_kept_and_over_it_dropped(tmp_path):
    _inst, ds, losses = _run(
        tmp_path, _palette([LUT_MAX, LUT_MAX + 2, 2], segmented=True),
        compress=False)
    assert bytes(ds[0x00281221].value) == _pattern(LUT_MAX)
    assert 0x00281222 not in ds
    (row,) = _lut_rows(losses, "0028,1222")
    assert f"exceeds the {LUT_MAX}-byte retention threshold" in row[2]
    assert row[3] == "STANDARD"


@pytest.mark.parametrize("tag", ["0028,1201", "0028,3006", "0028,1200"])
def test_a_non_segmented_lut_between_the_table_and_the_ceiling_is_kept(tmp_path, tag):
    """One ceiling for all eight, by ruling: a non-segmented value past a
    conformant table's 131072 bytes, at the ceiling, is still kept. A
    helper that gave the non-segmented tags 131072 is red here."""
    if tag == "0028,3006":
        extra = _voi_lut(LUT_MAX)
    elif tag == "0028,1200":
        def extra(ds):
            ds.add_new(0x00281100, "US", [0, 0, 16])
            ds.add_new(0x00281200, "OW", _pattern(LUT_MAX))
    else:
        extra = _palette([LUT_MAX, 2, 2], segmented=False)
    _inst, ds, losses = _run(tmp_path, extra, compress=False)
    assert _lut_rows(losses, tag) == []
    group, element = (int(x, 16) for x in tag.split(","))
    found = (ds.VOILUTSequence[0] if tag == "0028,3006" else ds)[(group << 16) | element]
    assert bytes(found.value) == _pattern(LUT_MAX)


@pytest.mark.parametrize("tag", ["0028,1201", "0028,3006"])
def test_a_non_segmented_lut_over_its_ceiling_is_dropped_naming_it(tmp_path, tag):
    """The row says the limit that applied, never the vendor-blob one."""
    if tag == "0028,3006":
        extra = _voi_lut(LUT_MAX + 2)
    else:
        extra = _palette([LUT_MAX + 2, 2, 2], segmented=False)
    _inst, ds, losses = _run(tmp_path, extra, compress=False)
    (row,) = _lut_rows(losses, tag)
    assert f"exceeds the {LUT_MAX}-byte retention threshold" in row[2]


def test_the_limit_is_unchanged_for_everything_else(tmp_path):
    """Overlay Data and a private OB of 65536 bytes are still dropped, each
    with its row naming 65534: the exemption is for these eight tags, not a
    higher limit for all."""
    def extra(ds):
        ds.add_new(0x60000010, "US", 256)
        ds.add_new(0x60000011, "US", 256)
        ds.add_new(0x60000100, "US", 1)
        ds.add_new(0x60000102, "US", 0)
        ds.add_new(0x60003000, "OW", bytes(65536))
        ds.add_new(0x00090010, "LO", "ACME")
        ds.add_new(0x00091001, "OB", bytes(65536))

    _inst, ds, losses = _run(tmp_path, extra, compress=False)
    assert 0x60003000 not in ds and 0x00091001 not in ds
    (overlay,) = _lut_rows(losses, "6000,3000")
    assert "exceeds the 65534-byte retention threshold" in overlay[2]
    assert overlay[3] == "STANDARD"
    (private,) = [r for r in losses if "Private tag 0009,1001" in r[2]]
    assert "exceeds the 65534-byte retention threshold" in private[2]
    assert private[3] == "PRIVATE"


def test_a_caller_lut_data_list_round_trips_through_our_own_export(tmp_path):
    """A caller's 40000-entry int list is written `US`, so #692 writes it
    `UN` under Explicit VR. Re-ingest does not decode it (F6), and the
    `UN` gate now reads LUT Data's ceiling, so it is kept as bytes and
    re-exported `OW`: the same words, little-endian, as the `US`
    encoding."""
    values = [(i * 7) % 65536 for i in range(40000)]
    _source(tmp_path / "src")
    with DicomSession(str(tmp_path / "a.db")) as s:
        s.ingest(str(tmp_path / "src"))
        (p,) = s.store.patients
        inst = p.studies[0].series[0].instances[0]
        inst.set_attr("0028,3002", [0, 0, 16])
        inst.set_attr("0028,3006", values)
        s.export(str(tmp_path / "out1"), show_progress=False)
    (first,) = list((tmp_path / "out1").rglob("*.dcm"))
    assert pydicom.dcmread(str(first)).get_item(0x00283006).VR == "UN"

    (tmp_path / "again").mkdir()
    shutil.copy(first, tmp_path / "again" / "a.dcm")
    with DicomSession(str(tmp_path / "b.db")) as s:
        s.ingest(str(tmp_path / "again"))
        s.export(str(tmp_path / "out2"), use_compression=False,
                 show_progress=False)
        losses = s.store_backend.get_audit_losses()
    assert _lut_rows(losses, "0028,3006") == []
    (second,) = list((tmp_path / "out2").rglob("*.dcm"))
    elem = pydicom.dcmread(str(second))[0x00283006]
    assert elem.VR == "OW"
    assert bytes(elem.value) == np.asarray(values, dtype="<u2").tobytes()


def test_the_retired_gray_lut_data_is_kept_too(tmp_path):
    """Gray LUT Data (0028,1200), retired and `US or SS or OW`: the eighth
    tag, so a set that forgot it is red here."""
    def extra(ds):
        ds.add_new(0x00281100, "US", [0, 0, 16])
        ds.add_new(0x00281200, "OW", _pattern(80000))

    inst, _ds, losses = _run(tmp_path, extra, compress=False)
    assert bytes(inst.attributes["0028,1200"]) == _pattern(80000)
    assert _lut_rows(losses, "0028,1200") == []

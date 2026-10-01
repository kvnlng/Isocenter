"""An Implicit VR `US or SS` value with no Pixel Representation is said at ingest (#700).

A `US or SS` element's VR is decided by Pixel Representation (0028,0103).
Under Implicit VR no VR is on the wire, so where nothing in the chain declares
Pixel Representation the bytes are read unsigned: Smallest Image Pixel Value
`(0028,0106)` written `fbff` is held as 65531, which a signed reading takes
as -5. Measured on `main` at 7579d4df: the Explicit VR twin, `SS -5`, draws
the export's `WARNING Ambiguous value representation (0028,0106): no Pixel
Representation is declared anywhere above it ...`, and the Implicit VR file
drew nothing at all.

The owner's ruling (Q6): one ingest `WARNING` row for such a value when bit
15 is set. Below 32768 the signed and unsigned readings agree, so there is
nothing ambiguous to say. No bytes change.
"""
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"


def _source(folder, *, syntax=ImplicitVRLittleEndian, vr="US", value=65531,
            pixel_rep=None, edit=None):
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.700.1"
    meta.TransferSyntaxUID = syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = SC, meta.MediaStorageSOPInstanceUID
    ds.PatientID, ds.PatientName = "P700", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.700.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.700.3"
    ds.Modality, ds.StudyDate, ds.StudyTime = "OT", "20200101", "120000"
    if value is not None:
        ds.add_new(0x00280106, vr, value)
    if pixel_rep is not None:
        ds.PixelRepresentation = pixel_rep
    if edit is not None:
        edit(ds)
    ds.save_as(str(folder / "a.dcm"), enforce_file_format=True,
               implicit_vr=syntax == ImplicitVRLittleEndian, little_endian=True)


def _rows(tmp_path, *, export=False):
    with DicomSession(str(tmp_path / "s.db")) as s:
        s.ingest(str(tmp_path / "src"))
        if export:
            s.export(str(tmp_path / "out"), show_progress=False)
        rows = s.store_backend.get_audit_errors()
    return [r for r in rows if r[1] == "WARNING" and "0028,0106" in r[2]]


def test_an_implicit_value_with_bit_15_set_draws_one_row(tmp_path):
    _source(tmp_path / "src", vr="SS", value=-5)
    rows = _rows(tmp_path)
    assert len(rows) == 1, rows
    detail = rows[0][2]
    assert "Ambiguous value representation" in detail
    assert "65531" in detail and "-5" in detail
    assert "Implicit VR" in detail


def test_a_small_implicit_value_draws_none(tmp_path):
    _source(tmp_path / "src", value=100)
    assert _rows(tmp_path) == []


@pytest.mark.parametrize("value,rows", [(32768, 1), (32767, 0)])
def test_the_boundary_is_bit_15(tmp_path, value, rows):
    _source(tmp_path / "src", value=value)
    assert len(_rows(tmp_path)) == rows


def test_an_explicit_us_with_no_declarer_draws_none(tmp_path):
    """The source said US: there was no ambiguity to report."""
    _source(tmp_path / "src", syntax=ExplicitVRLittleEndian, vr="US",
            value=65531)
    assert _rows(tmp_path, export=True) == []


def test_a_declared_pixel_representation_draws_none(tmp_path):
    _source(tmp_path / "src", value=65531, pixel_rep=0)
    assert _rows(tmp_path) == []


def test_a_nested_value_draws_a_row_naming_its_item(tmp_path):
    """A `US or SS` value one level down, no Pixel Representation anywhere."""
    def edit(ds):
        item = Dataset()
        item.add_new(0x00280106, "US", 65531)
        item.CodeValue = "X"
        ds.add_new(0x00400555, "SQ", Sequence([item]))  # Acquisition Context

    _source(tmp_path / "src", value=None, edit=edit)
    rows = _rows(tmp_path)
    assert len(rows) == 1, rows
    assert "0040,0555[0]" in rows[0][2]


def test_a_nested_value_under_a_declaring_root_draws_none(tmp_path):
    """The root's Pixel Representation reaches the item as `_pixel_rep`."""
    def edit(ds):
        item = Dataset()
        item.add_new(0x00280106, "US", 65531)
        item.CodeValue = "X"
        ds.add_new(0x00400555, "SQ", Sequence([item]))

    _source(tmp_path / "src", value=None, pixel_rep=0, edit=edit)
    assert _rows(tmp_path) == []


def test_a_private_sequence_rebuilt_from_un_bytes_draws_a_row(tmp_path):
    """Implicit VR writes a private sequence as `UN` bytes the ingest
    re-parses (`_sequence_from_un_bytes`). The rebuilt items are read as
    Implicit VR Little Endian and say so in their own `original_encoding`
    (pydicom 3.0.2, measured), so the row fires for them too; pinned in case
    a pydicom stops stamping it."""
    def edit(ds):
        ds.add_new(0x00090010, "LO", "ACME")
        item = Dataset()
        item.add_new(0x00280106, "US", 65531)
        ds.add_new(0x00091010, "SQ", Sequence([item]))

    _source(tmp_path / "src", value=None, edit=edit)
    rows = _rows(tmp_path)
    assert len(rows) == 1, rows
    assert "0009,1010[0]" in rows[0][2]

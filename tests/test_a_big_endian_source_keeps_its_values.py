"""A source in Explicit VR Big Endian keeps its sample values (#648).

pydicom decodes a big-endian native frame to an array in the file's own
byte order -- `>u2`, `>i2`, `>u4` -- whose *values* are right. Ingest then
stored `arr.tobytes()`, which for such an array is big-endian bytes, and
`SidecarPixelLoader` reads the sidecar in native order. Measured on
abcb3aa: a 16-bit BitsStored 12 frame and its icon of `[0, 100, 4000, 4095]`
read back and exported as `[0, 25600, 40975, 65295]` -- and the export
widened BitsStored to 16, because the swapped values no longer fit 12 bits
-- with no row, grade PASS, and `verify_readback=True` passing, because the
readback compares the written file with the array written rather than
with the source.

The decode now returns native byte order at its one exit, as
`Instance.set_pixel_data()` already did for a caller's array. Every value
below is one a byte swap changes: 4095 reads 65295 swapped, and -2000
reads 12536.

These go through a real `Session` ingest. The ingest pool spawns, so a
monkeypatch in this process would not reach the decode being tested.
"""
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRBigEndian, ExplicitVRLittleEndian, generate_uid

from isocenter.session import DicomSession

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"


def _dataset(values, *, bits, stored, representation, samples=1,
             icon=False, element="PixelData"):
    """A 2x2 CT instance whose pixels are `values` (a big-endian array)."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRBigEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT648", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    # `IODValidator` refuses a CT Image without these, which would fail the
    # export on geometry before it reached the pixels under test.
    ds.SliceThickness, ds.KVP = "1.0", "120"
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.Rows = ds.Columns = 2
    ds.SamplesPerPixel = samples
    ds.PhotometricInterpretation = "RGB" if samples > 1 else "MONOCHROME2"
    if samples > 1:
        ds.PlanarConfiguration = 0
    ds.BitsAllocated = bits
    if element == "PixelData":
        ds.BitsStored = stored
        ds.HighBit = stored - 1
        ds.PixelRepresentation = representation
        ds.add_new(0x7FE00010, "OW" if bits > 8 else "OB", values.tobytes())
    else:
        ds.add_new(0x7FE00008, "OF", values.tobytes())
    if icon:
        item = Dataset()
        item.Rows = item.Columns = 2
        item.BitsAllocated, item.BitsStored = bits, stored
        item.HighBit = stored - 1
        item.SamplesPerPixel = 1
        item.PhotometricInterpretation = "MONOCHROME2"
        item.PixelRepresentation = representation
        item.add_new(0x7FE00010, "OW", values.tobytes())
        ds.IconImageSequence = Sequence([item])
    return ds


def _save(tmp_path, ds, name="src"):
    folder = tmp_path / name
    folder.mkdir()
    path = str(folder / "one.dcm")
    # An OW value is written as the bytes it holds, which are big-endian
    # here, under a big-endian syntax; `force_encoding` because pydicom
    # otherwise re-derives the encoding from `file_meta`.
    pydicom.dcmwrite(path, ds, implicit_vr=False, little_endian=False,
                     force_encoding=True)
    assert str(pydicom.dcmread(path).file_meta.TransferSyntaxUID) == str(
        ExplicitVRBigEndian)
    return path


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _ingest_reload_export(tmp_path, path):
    """The sidecar's array in session, after reopening, and the export."""
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        assert not summary.failures, summary.failures
        (inst,) = _instances(session)
        assert inst.unload_pixel_data() is True
        stored = inst.get_pixel_data()
        session.export(str(out), format="dicom", use_compression=False)
    with DicomSession(persistence_file=db) as session:
        (inst,) = _instances(session)
        reopened = inst.get_pixel_data()
    files = [os.path.join(r, f) for r, _d, fs in os.walk(str(out))
             for f in fs if f.endswith(".dcm")]
    assert len(files) == 1, files
    return stored, reopened, pydicom.dcmread(files[0])


def test_a_big_endian_frame_is_stored_as_its_values(tmp_path):
    values = np.array([0, 100, 4000, 4095], ">u2")
    path = _save(tmp_path, _dataset(values, bits=16, stored=12,
                                    representation=0))

    stored, reopened, exported = _ingest_reload_export(tmp_path, path)

    want = [[0, 100], [4000, 4095]]
    assert stored.tolist() == want
    assert reopened.tolist() == want
    assert exported.pixel_array.tolist() == want
    # The swapped values overflowed 12 bits, so the export used to widen
    # BitsStored to 16 in silence; the source's 12 fits its own values.
    assert exported.BitsStored == 12


@pytest.mark.parametrize("values, bits, stored, representation, samples", [
    pytest.param(np.array([-2000, -1, 0, 2047], ">i2"), 16, 16, 1, 1,
                 id="int16"),
    pytest.param(np.array([0, 100, 70000, 4000000000], ">u4"), 32, 32, 0, 1,
                 id="uint32"),
    pytest.param((np.arange(12) * 1000 + 1).astype(">u2"), 16, 16, 0, 3,
                 id="rgb16"),
])
def test_a_big_endian_signed_and_32_bit_frame_keep_their_values(
        tmp_path, values, bits, stored, representation, samples):
    path = _save(tmp_path, _dataset(values, bits=bits, stored=stored,
                                    representation=representation,
                                    samples=samples))

    stored_arr, reopened, exported = _ingest_reload_export(tmp_path, path)

    shape = (2, 2, 3) if samples > 1 else (2, 2)
    want = values.astype(values.dtype.newbyteorder("=")).reshape(shape)
    assert stored_arr.tolist() == want.tolist()
    assert reopened.tolist() == want.tolist()
    assert exported.pixel_array.tolist() == want.tolist()


def test_a_big_endian_icon_is_carried_as_its_values(tmp_path):
    values = np.array([0, 100, 4000, 4095], ">u2")
    path = _save(tmp_path, _dataset(values, bits=16, stored=12,
                                    representation=0, icon=True))

    _stored, _reopened, exported = _ingest_reload_export(tmp_path, path)

    icon = exported.IconImageSequence[0]
    # A native export: the item is read under the file's own syntax.
    icon.file_meta = exported.file_meta
    assert str(exported.file_meta.TransferSyntaxUID) != str(ExplicitVRBigEndian)
    assert icon.pixel_array.tolist() == [[0, 100], [4000, 4095]]


def test_decode_pixels_returns_native_byte_order(tmp_path):
    from isocenter.io_handlers import _decode_pixels
    values = np.array([-2000, -1, 0, 2047], ">i2")
    path = _save(tmp_path, _dataset(values, bits=16, stored=16,
                                    representation=1))

    arr, label = _decode_pixels(pydicom.dcmread(path, force=True))

    assert arr.dtype.isnative
    # The values too: `isnative` alone is satisfied by a decode that swaps
    # the bytes *and* relabels the dtype, which reads 12536 for -2000.
    assert arr.tolist() == [[-2000, -1], [0, 2047]]
    # And the bytes a store would write are the native spelling of them.
    assert np.frombuffer(arr.tobytes(), "=i2").tolist() == [-2000, -1, 0, 2047]
    assert label == "MONOCHROME2"


def test_a_big_endian_float_frame_keeps_its_values(tmp_path):
    values = np.array([0.5, -1.25, 1000.0, 3.0], ">f4")
    path = _save(tmp_path, _dataset(values, bits=32, stored=32,
                                    representation=0,
                                    element="FloatPixelData"))

    stored, reopened, exported = _ingest_reload_export(tmp_path, path)

    want = [[0.5, -1.25], [1000.0, 3.0]]
    assert stored.tolist() == want
    assert reopened.tolist() == want
    assert str(exported.file_meta.TransferSyntaxUID) != str(ExplicitVRBigEndian)
    assert exported.pixel_array.tolist() == want


def test_a_little_endian_source_is_unchanged(tmp_path):
    """Guard: the normalisation is a no-op on an array already native."""
    values = np.array([0, 100, 4000, 4095], "<u2")
    ds = _dataset(values, bits=16, stored=12, representation=0)
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    folder = tmp_path / "src"
    folder.mkdir()
    path = str(folder / "one.dcm")
    ds.save_as(path, enforce_file_format=True)

    stored, reopened, exported = _ingest_reload_export(tmp_path, path)

    assert stored.tolist() == [[0, 100], [4000, 4095]]
    assert reopened.tolist() == stored.tolist()
    assert exported.pixel_array.tolist() == stored.tolist()

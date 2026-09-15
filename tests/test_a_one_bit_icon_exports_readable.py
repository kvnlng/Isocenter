"""A 1-bit icon exports readable (#649).

PS3.3 C.7.6.1.1.6 allows an Icon Image Sequence item with BitsAllocated 1
("Pixel samples shall have a Value of either 1 or 8 for Bits Allocated
(0028,0100) and Bits Stored (0028,0101)"). Ingest decodes such an icon to
one byte per sample, and the export wrote those bytes back under the
declared BitsAllocated 1, so a reader that unpacks bits found 16 bytes
where 2 were packed. Measured on abcb3aa: pydicom's `pixel_array` raised
`TypeError: slice indices must be integers or None or have an __index__
method` on the exported item, for one frame and for two, under both
`use_compression` settings, with no row and the grade unchanged.

The item is now written as the top level writes a 1-bit image (ruling
OQ3 (a)): BitsAllocated 8, BitsStored 1, HighBit 0, one byte per sample.
C.7.6.1.1.6 permits 8, and pydicom reads it back as the source's mask.
The graph keeps the declared BitsAllocated 1; only the file changes.

The mask is asymmetric in both axes, so a transposition or a bit-order
flip reads as a different mask, not as the same one.
"""
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.pixels.utils import pack_bits
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.session import DicomSession

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
MASK = np.array([[1, 0, 0, 1, 1], [0, 1, 0, 0, 0], [1, 1, 1, 0, 1]], np.uint8)


def _dataset():
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT649", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    # `IODValidator` refuses a CT Image without these.
    ds.SliceThickness, ds.KVP = "1.0", "120"
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    return ds


def _with_top_level(ds):
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelData = np.arange(16, dtype=np.uint8).tobytes()
    return ds


def _icon(frames, *, bits=1):
    item = Dataset()
    item.Rows, item.Columns = MASK.shape
    item.BitsAllocated = item.BitsStored = bits
    item.HighBit = bits - 1
    item.SamplesPerPixel = 1
    item.PhotometricInterpretation = "MONOCHROME2"
    item.PixelRepresentation = 0
    if len(frames) > 1:
        item.NumberOfFrames = len(frames)
    if bits == 1:
        payload = pack_bits(np.stack(frames))
    else:
        payload = np.stack(frames).astype(np.uint8).tobytes()
    item.add_new(0x7FE00010, "OB", payload)
    return item


def _save(tmp_path, ds):
    folder = tmp_path / "src"
    folder.mkdir()
    path = str(folder / "one.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _export(tmp_path, path, *, use_compression=False):
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        assert not summary.failures, summary.failures
        session.export(str(out), format="dicom",
                       use_compression=use_compression)
        graph = [i for p in session.store.patients for st in p.studies
                 for se in st.series for i in se.instances]
    files = [os.path.join(r, f) for r, _d, fs in os.walk(str(out))
             for f in fs if f.endswith(".dcm")]
    assert len(files) == 1, files
    return pydicom.dcmread(files[0]), graph


def _exported_icon(exported):
    icon = exported.IconImageSequence[0]
    # The icon is written native whatever the file's syntax (PS3.5 A.4),
    # so it is read as native: under a compressed export's own syntax
    # pydicom would look for fragments that are not there.
    icon.file_meta = FileMetaDataset()
    icon.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return icon


@pytest.mark.parametrize("use_compression", [False, True],
                         ids=["native", "use_compression"])
def test_a_one_bit_icon_reads_back_as_its_mask(tmp_path, use_compression):
    ds = _with_top_level(_dataset())
    ds.IconImageSequence = Sequence([_icon([MASK])])
    path = _save(tmp_path, ds)

    exported, _graph = _export(tmp_path, path,
                               use_compression=use_compression)

    assert _exported_icon(exported).pixel_array.tolist() == MASK.tolist()


def test_a_one_bit_icon_is_written_by_the_top_levels_rule(tmp_path):
    ds = _with_top_level(_dataset())
    ds.IconImageSequence = Sequence([_icon([MASK])])
    path = _save(tmp_path, ds)

    exported, graph = _export(tmp_path, path)

    icon = _exported_icon(exported)
    assert (icon.BitsAllocated, icon.BitsStored, icon.HighBit) == (8, 1, 0)
    # The byte count, not only readability: 15 samples of one byte each,
    # padded to even. A BitsAllocated 8 over 2 packed bytes also fails to
    # read, but pydicom's padding tolerance could hide a wrong count.
    # (No VR assertion: the export writes Implicit VR Little Endian, so the
    # file carries none and pydicom's read-back VR is its own guess.)
    assert len(icon.PixelData) == 16
    # The graph keeps what the source declared; the export writes the
    # consistent header, the #455 precedent.
    (inst,) = graph
    assert inst.sequences["0088,0200"].items[0].attributes["0028,0100"] == 1


def test_a_two_frame_one_bit_icon_reads_back(tmp_path):
    ds = _with_top_level(_dataset())
    inverse = (1 - MASK).astype(np.uint8)
    ds.IconImageSequence = Sequence([_icon([MASK, inverse])])
    path = _save(tmp_path, ds)

    exported, _graph = _export(tmp_path, path)

    icon = _exported_icon(exported)
    assert len(icon.PixelData) == 30
    frames = icon.pixel_array
    assert frames.shape == (2, 3, 5)
    assert frames[0].tolist() == MASK.tolist()
    assert frames[1].tolist() == inverse.tolist()


def test_an_eight_bit_icon_is_untouched(tmp_path):
    values = (MASK * 200 + 7).astype(np.uint8)
    ds = _with_top_level(_dataset())
    ds.IconImageSequence = Sequence([_icon([values], bits=8)])
    path = _save(tmp_path, ds)

    exported, _graph = _export(tmp_path, path)

    icon = _exported_icon(exported)
    assert (icon.BitsAllocated, icon.BitsStored, icon.HighBit) == (8, 8, 7)
    assert bytes(icon.PixelData) == values.tobytes() + b"\0"
    assert icon.pixel_array.tolist() == values.tolist()


def test_a_one_bit_top_level_still_widens(tmp_path):
    """Guard: the top level's answer, which the icon now matches."""
    ds = _dataset()
    ds.Rows, ds.Columns = MASK.shape
    ds.BitsAllocated = ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelData = pack_bits(MASK)
    path = _save(tmp_path, ds)

    exported, _graph = _export(tmp_path, path)

    assert (exported.BitsAllocated, exported.BitsStored,
            exported.HighBit) == (8, 1, 0)
    assert exported.pixel_array.tolist() == MASK.tolist()

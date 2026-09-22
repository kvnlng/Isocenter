"""An `Instance` read from its source file reads under its own pixel descriptors (#595).

`get_pixel_data()` on an `Instance(file_path=...)` with no store behind
it -- a graph built by hand, or `write_tree()` input -- decoded under the
*file's* Rows, Columns, SamplesPerPixel, NumberOfFrames, BitsAllocated
and PixelRepresentation whatever its `attributes` said, so a `set_attr`
on any of them changed nothing it read. Measured on 63a64158 with
`CT_small.dcm`: after PixelRepresentation 1 -> 0 the file-backed
instance read `int16` where an ingested one read `uint16`; after Rows 64
/ Columns 256 it read `(128, 128)`; and `write_tree()` wrote the file's
geometry back over the edit -- Rows and Columns with no log line at all.
The same edit on an ingested instance has been read under since #417.

The owner's ruling (Q1, as recommended): the file arm reads the decoded
samples under the instance's descriptors, by the sidecar loader's one
reading rule and in its words, and an edit the samples cannot be read
under raises. An instance that holds no descriptors reads the file as the
file declares.

The fixture is int16 with negative samples, so a `uint16` reading and an
`int16` one are told apart by value, not only by dtype. Every UID is
literal, and the source file is written into `tmp_path`.
"""
from datetime import date
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

import isocenter.entities as entities_module
from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import DicomExporter, populate_attrs
from isocenter.session import DicomSession

SOP = "1.2.826.0.1.595.1"
SC = "1.2.840.10008.5.1.4.1.1.7"
PARAMETRIC_MAP = "1.2.840.10008.5.1.4.1.1.30"
SAMPLES = (np.arange(16, dtype=np.int16) - 8).reshape(4, 4)
ROWS, COLS, PR, BITS = "0028,0010", "0028,0011", "0028,0103", "0028,0100"


def _source(folder: Path, floats=False) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = PARAMETRIC_MAP if floats else SC
    meta.MediaStorageSOPInstanceUID = SOP
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = meta.MediaStorageSOPClassUID, SOP
    ds.PatientID, ds.PatientName = "P595", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.595.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.595.3"
    ds.Modality, ds.StudyDate = "OT", "20230101"
    ds.SeriesNumber, ds.InstanceNumber = 1, 1
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    if floats:
        ds.BitsAllocated = ds.BitsStored = 32
        ds.HighBit = 31
        ds.PixelRepresentation = 0
        ds.FloatPixelData = SAMPLES.astype(np.float32).tobytes()
    else:
        ds.BitsAllocated = ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1
        ds.PixelData = SAMPLES.tobytes()
    path = folder / "src.dcm"
    ds.save_as(str(path), enforce_file_format=True)
    return path


def _bare(path: Path) -> Instance:
    inst = Instance(SOP, SC, 1, file_path=str(path))
    populate_attrs(pydicom.dcmread(str(path)), inst, [])
    return inst


def _edit(inst, edits):
    for tag, value in edits:
        inst.set_attr(tag, value)


def test_a_pixel_representation_edit_is_read_under_the_edit(tmp_path):
    """Killing mutation: the arm as it stood (`int16`)."""
    inst = _bare(_source(tmp_path))
    inst.set_attr(PR, 0)
    arr = inst.get_pixel_data()
    assert arr.dtype == np.uint16
    assert arr.tobytes() == SAMPLES.tobytes()


def test_a_geometry_the_samples_hold_is_read_in_the_new_shape(tmp_path):
    """Killing mutation: comparing the dtype only."""
    inst = _bare(_source(tmp_path))
    _edit(inst, [(ROWS, 2), (COLS, 8)])
    arr = inst.get_pixel_data()
    assert (arr.dtype, arr.shape) == (np.int16, (2, 8))
    assert arr.tobytes() == SAMPLES.tobytes()


def test_a_geometry_the_samples_cannot_hold_is_refused_in_the_sidecars_words(tmp_path):
    """Killing mutations: truncating to the new geometry; returning the
    file's reading; a path in the words (they reach an audit row)."""
    path = _source(tmp_path)
    inst = _bare(path)
    _edit(inst, [(ROWS, 2), (COLS, 2)])
    with pytest.raises(RuntimeError) as raised:
        inst.get_pixel_data()
    words = str(raised.value)
    assert f"Lazy load failed for instance {SOP}: " in words
    assert f"frame for {SOP} holds 16 samples; geometry (2, 2) needs 4" in words
    assert str(path) not in words and str(tmp_path) not in words
    assert inst.pixel_array is None


#: §1.3's seven edits, at this fixture's size.
EDITS = {
    "none": [],
    "pixel representation": [(PR, 0)],
    "geometry the samples hold": [(ROWS, 2), (COLS, 8)],
    "geometry they cannot hold": [(ROWS, 2), (COLS, 2)],
    "photometric": [("0028,0004", "MONOCHROME1")],
    "bits stored": [("0028,0101", 12)],
    "bits allocated": [(BITS, 8)],
}


def _reading(inst):
    try:
        arr = inst.get_pixel_data()
    except RuntimeError as exc:
        words = str(exc)
        return ("raised", words[words.index("Integrity Error: "):])
    return (arr.dtype, arr.shape, arr.tobytes())


@pytest.mark.parametrize("edit", sorted(EDITS))
def test_the_file_and_the_store_read_every_edit_alike(tmp_path, edit):
    """One rule on both arms: the same dtype, shape and bytes, or the same
    refusal after "Integrity Error: ". Killing mutation: any divergence."""
    path = _source(tmp_path / "src")
    bare = _bare(path)
    _edit(bare, EDITS[edit])
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "src"))
        (patient,) = session.store.patients
        stored = patient.studies[0].series[0].instances[0]
        stored.unload_pixel_data()
        assert stored.pixel_array is None
        _edit(stored, EDITS[edit])
        expected = _reading(stored)
    assert _reading(bare) == expected


def test_an_instance_holding_no_descriptors_reads_the_file_as_declared(tmp_path):
    """Killing mutation: an absent descriptor defaulted (Rows 0, "declares
    no pixel geometry") instead of taken from the file."""
    inst = Instance(SOP, SC, 1, file_path=str(_source(tmp_path)))
    arr = inst.get_pixel_data()
    assert (arr.dtype, arr.shape) == (np.int16, (4, 4))
    assert np.array_equal(arr, SAMPLES)


def test_an_edited_float_instance_stays_float(tmp_path):
    """Float Pixel Data records no dtype carrier on a bare instance, so the
    decoded dtype supplies it. Killing mutation: the carrier not taken from
    the decoded array (its float bytes read as `int32`)."""
    inst = _bare(_source(tmp_path, floats=True))
    _edit(inst, [(ROWS, 2), (COLS, 8)])
    arr = inst.get_pixel_data()
    assert (arr.dtype, arr.shape) == (np.float32, (2, 8))
    assert arr.tobytes() == SAMPLES.astype(np.float32).tobytes()


def test_a_resident_file_frame_follows_a_later_edit(tmp_path):
    """A file frame is reloadable, so `set_attr` releases it on an edit that
    changes its reading; the next read goes back to the file. Killing
    mutation: the file arm ignoring the edit after that release."""
    inst = _bare(_source(tmp_path))
    assert inst.get_pixel_data().dtype == np.int16
    inst.set_attr(PR, 0)
    assert inst.get_pixel_data().dtype == np.uint16


def test_an_edit_during_the_decode_is_not_published_under_the_old_reading(
        tmp_path, monkeypatch):
    """An edit landing while the file is decoded (seconds, for a JPEG 2000
    frame) is not published under the reading taken before it: the publish
    refuses a stale capture and the arm reinterprets the decode it already
    has. Deterministic: the edit is made from inside the decode. Killing
    mutations: no capture passed from the file arm; no retry on
    `_STALE_CAPTURE`."""
    inst = _bare(_source(tmp_path))
    real = entities_module._decode_from_file
    calls = []

    def decode_then_edit(ds):
        result = real(ds)
        if not calls:
            inst.set_attr(PR, 0)
        calls.append(1)
        return result

    monkeypatch.setattr(entities_module, "_decode_from_file", decode_then_edit)
    arr = inst.get_pixel_data()
    assert arr.dtype == np.uint16
    assert inst.pixel_array.dtype == np.uint16
    assert len(calls) == 1


def _graph(inst):
    patient = Patient("P595", "Doe^John")
    study = Study("1.2.826.0.1.595.2", date(2023, 1, 1))
    series = Series("1.2.826.0.1.595.3", "OT", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _write(tmp_path, inst):
    out = tmp_path / "out"
    DicomExporter.write_tree(_graph(inst), str(out), show_progress=False)
    (path,) = list(out.rglob("*.dcm"))
    return pydicom.dcmread(str(path))


def test_write_tree_writes_the_edit(tmp_path):
    """Killing mutation: the writer reading under the file's descriptors,
    which wrote the file's geometry back over the edit."""
    path = _source(tmp_path / "src")

    inst = _bare(path)
    inst.set_attr(PR, 0)
    written = _write(tmp_path / "pr", inst)
    assert written.PixelRepresentation == 0
    assert written.PixelData == SAMPLES.tobytes()

    inst = _bare(path)
    _edit(inst, [(ROWS, 2), (COLS, 8)])
    written = _write(tmp_path / "geometry", inst)
    assert (written.Rows, written.Columns) == (2, 8)
    assert written.PixelData == SAMPLES.tobytes()

    inst = _bare(path)
    _edit(inst, [(ROWS, 2), (COLS, 2)])
    with pytest.raises(RuntimeError):
        _write(tmp_path / "refused", inst)

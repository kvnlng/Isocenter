"""A geometry descriptor the export rewrites from the array is named (#736).

The export writes Rows, Columns, SamplesPerPixel and NumberOfFrames from
the one resolved geometry of the array it is writing (the #217 magnitude
authority), over whatever the instance declared. Measured at de5b26d9 on
3.12.14 and 3.14.7t: a hand-built instance holding a 4x4 array under
`attributes["0028,0010"] = 2` and `attributes["0028,0011"] = 8` was
written 4x4 with no line at all, where the same rewrite of
PixelRepresentation is named at INFO. So were a declared SamplesPerPixel 3
and a declared NumberOfFrames 2.

Only a write that goes around the entity reaches it: `set_attr` refuses a
geometry the resident array cannot be read under, and an ingested
instance is re-read under the edit or fails the export (#595).

Ruled (Q6 A, 2026-10-06): one INFO note per instance naming each of the
four descriptors the array overrides, on `ExportOutcome.corrections`, as
PixelRepresentation's rewrite is said. No audit row.

Every test goes through `write_tree` on a hand-built graph and reads the
parent's INFO lines, each asserted whole.
"""
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import DicomExporter, populate_attrs

SOP = "1.2.826.0.1.736.1"
SC = "1.2.840.10008.5.1.4.1.1.7"
PARAMETRIC_MAP = "1.2.840.10008.5.1.4.1.1.30"
SAMPLES = (np.arange(16, dtype=np.int16) - 8).reshape(4, 4)
SPP, FRAMES, ROWS, COLS, PR = ("0028,0002", "0028,0008", "0028,0010",
                               "0028,0011", "0028,0103")
ARRAY = "which holds 1 frame of 4 x 4 at 1 sample per pixel"


def _source(folder: Path, floats=False) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = PARAMETRIC_MAP if floats else SC
    meta.MediaStorageSOPInstanceUID = SOP
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = meta.MediaStorageSOPClassUID, SOP
    ds.PatientID, ds.PatientName = "P736", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.736.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.736.3"
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


def _instance(tmp_path, array, floats=False):
    """A hand-built instance holding `array` resident, declared as the file is."""
    path = _source(tmp_path / "src", floats)
    inst = Instance(SOP, PARAMETRIC_MAP if floats else SC, 1,
                    file_path=str(path))
    populate_attrs(pydicom.dcmread(str(path)), inst, [])
    inst.set_pixel_data(array)
    return inst


def _write(tmp_path, inst, caplog):
    """`write_tree` the instance; the file as read back, and its INFO notes."""
    patient = Patient("P736", "Doe^John")
    study = Study("1.2.826.0.1.736.2", date(2023, 1, 1))
    series = Series("1.2.826.0.1.736.3", "OT", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    out = tmp_path / "out"
    with caplog.at_level(logging.INFO, logger="isocenter"):
        DicomExporter.write_tree(patient, str(out), show_progress=False)
    (path,) = list(out.rglob("*.dcm"))
    prefix = f"{inst.sop_instance_uid}: "
    notes = [r.getMessage()[len(prefix):] for r in caplog.records
             if r.levelno == logging.INFO
             and r.getMessage().startswith(prefix)]
    return pydicom.dcmread(str(path)), notes


def test_rows_and_columns_the_array_overrides_are_named(tmp_path, caplog):
    """The issue's own case: 4x4 under a declared 2x8.

    Killing mutation: the comparison deleted (no note, as on main).
    """
    inst = _instance(tmp_path, SAMPLES.copy())
    inst.attributes[ROWS] = 2
    inst.attributes[COLS] = 8

    ds, notes = _write(tmp_path, inst, caplog)

    assert (ds.Rows, ds.Columns) == (4, 4)
    assert ds.pixel_array.tobytes() == SAMPLES.tobytes()
    assert notes == [
        f"Rows 2 and Columns 8 do not describe the array, {ARRAY}; written "
        f"with Rows 4 and Columns 4, the array's own"]


@pytest.mark.parametrize("tag, declared, keyword, sentence", [
    (SPP, 3, "SamplesPerPixel",
     f"SamplesPerPixel 3 does not describe the array, {ARRAY}; written with "
     f"SamplesPerPixel 1, the array's own"),
    (FRAMES, 2, "NumberOfFrames",
     f"NumberOfFrames 2 does not describe the array, {ARRAY}; written with "
     f"NumberOfFrames 1, the array's own"),
])
def test_a_sample_or_frame_count_the_array_overrides_is_named(
        tmp_path, caplog, tag, declared, keyword, sentence):
    """SamplesPerPixel and NumberOfFrames are rewritten the same way.

    Killing mutation: the note raised for Rows and Columns only (Q6 C).
    """
    inst = _instance(tmp_path, SAMPLES.copy())
    inst.attributes[tag] = declared

    ds, notes = _write(tmp_path, inst, caplog)

    assert int(ds.get(keyword)) == 1
    assert notes == [sentence]


def test_all_four_are_named_in_one_sentence_in_tag_order(tmp_path, caplog):
    """One note per instance, however many descriptors disagree.

    Killing mutation: a note per descriptor.
    """
    inst = _instance(tmp_path, SAMPLES.copy())
    inst.attributes[COLS] = 8
    inst.attributes[ROWS] = 2
    inst.attributes[FRAMES] = 2
    inst.attributes[SPP] = 3

    _, notes = _write(tmp_path, inst, caplog)

    assert notes == [
        f"SamplesPerPixel 3, NumberOfFrames 2, Rows 2 and Columns 8 do not "
        f"describe the array, {ARRAY}; written with SamplesPerPixel 1, "
        f"NumberOfFrames 1, Rows 4 and Columns 4, the array's own"]


def test_a_declaration_that_agrees_is_not_noted(tmp_path, caplog):
    """Control: an untouched instance, and a declared NumberOfFrames of 1."""
    inst = _instance(tmp_path, SAMPLES.copy())
    inst.attributes[FRAMES] = 1

    ds, notes = _write(tmp_path, inst, caplog)

    assert (ds.Rows, ds.Columns, ds.SamplesPerPixel, ds.NumberOfFrames) == (
        4, 4, 1, 1)
    assert notes == []


def test_a_frame_count_never_declared_is_not_a_correction(tmp_path, caplog):
    """Two frames with no NumberOfFrames declared: written 2, and no note.

    The export writes NumberOfFrames whenever there is more than one
    frame; nothing declared is not a correction of anything. Killing
    mutation: NumberOfFrames noted when undeclared (compared with 1, say).
    """
    frames = np.stack([SAMPLES, SAMPLES + 1])
    inst = _instance(tmp_path, frames)
    inst.attributes.pop(FRAMES, None)
    assert FRAMES not in inst.attributes

    ds, notes = _write(tmp_path, inst, caplog)

    assert int(ds.NumberOfFrames) == 2
    assert ds.pixel_array.tobytes() == frames.tobytes()
    assert notes == []


def test_the_float_arm_says_it_too(tmp_path, caplog):
    """Float Pixel Data goes through the same geometry write.

    Killing mutation: `corrections` passed at the integer call site only.
    """
    floats = SAMPLES.astype(np.float32)
    inst = _instance(tmp_path, floats, floats=True)
    inst.attributes[ROWS] = 2
    inst.attributes[COLS] = 8

    ds, notes = _write(tmp_path, inst, caplog)

    assert (ds.Rows, ds.Columns) == (4, 4)
    assert bytes(ds.FloatPixelData) == floats.tobytes()
    assert notes == [
        f"Rows 2 and Columns 8 do not describe the array, {ARRAY}; written "
        f"with Rows 4 and Columns 4, the array's own"]


def test_the_pixel_representation_note_is_still_its_own(tmp_path, caplog):
    """Two rewrites, two notes; the older one word for word.

    Killing mutation: the new note folded into PixelRepresentation's, or
    replacing it.
    """
    inst = _instance(tmp_path, SAMPLES.copy())
    inst.attributes[ROWS] = 2
    inst.attributes[COLS] = 8
    inst.attributes[PR] = 0

    ds, notes = _write(tmp_path, inst, caplog)

    assert (ds.Rows, ds.Columns, ds.PixelRepresentation) == (4, 4, 1)
    assert notes == [
        f"Rows 2 and Columns 8 do not describe the array, {ARRAY}; written "
        f"with Rows 4 and Columns 4, the array's own",
        "PixelRepresentation 0 (unsigned) is not the signedness of int16 "
        "samples; written with PixelRepresentation 1 (signed), the array's "
        "own"]

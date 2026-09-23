"""A JPEG 2000 file pydicom's Pillow plugin refuses is written, and named (#670).

Since #416 a 16-bit image with more than one sample is written under
`use_compression=True` as JPEG 2000 Lossless. The file is conformant and
its codestream is bit-exact: `imagecodecs.jpeg2k_decode`, this library's
`ingest()` and pydicom 3.0.2 with `pylibjpeg-openjpeg` 2.5.0 read it
sample for sample. pydicom's Pillow plugin, the only one of pydicom's JPEG
2000 plugins that installs with this package, does not: it refuses every codestream
with a precision above 8 bits and more than one sample ("Pillow cannot
decode 16-bit multi-sample data correctly"), because Pillow narrows such
data to 8 bits. Measured at 82e82386 on 3.12.14 and 3.14.7t over a
120-cell matrix: that cell, and only that cell, is refused; and the
export said nothing -- no row, no log line, and `verify_readback=True`
passed, because it reads through this library's decoder.

Ruled (2026-09-23): the file stays as it is, and the export says so. One
INFO line per instance on `ExportOutcome.corrections`, no audit row, the
grade unmoved -- the channel #596 uses for 16-bit `YBR_FULL`, because
this is a limit of a reader and not a defect in the user's data.

Worker-level arms call `_export_instance_worker` in this process, as
`tests/test_export_photometric_admissibility.py` does; the session and
`write_tree` arms check the parent's half. No test here names a source
SOP Instance UID as a literal: the expected UID is read off the instance
after the export, so a change to how UIDs are written (#544) cannot turn
these red for a reason that is not theirs.
"""
import itertools
import logging
import os
from datetime import date

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.encaps import generate_frames

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import (DicomExporter, ExportContext,
                                   _export_instance_worker)
from isocenter.session import DicomSession

SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"
CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
SR_STORAGE = "1.2.840.10008.5.1.4.1.1.88.11"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"
#: pydicom 3.0.2 `pixels/decoders/pillow.py`'s own words.
PILLOW_REFUSAL = "Pillow cannot decode"

_serial = itertools.count(1)
_LEVERS = ("ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
           "ISOCENTER_MAX_TASKS_PER_CHILD")


def _literal(dtype, samples, frames=1, bits_stored=None):
    """Samples of `dtype`, above a byte where the width allows.

    A 16-bit value that fits in a byte would not tell a 16-bit frame from
    an 8-bit one, so the 16-bit literals reach past 255; both extremes of
    the signed range are forced in for the signed 16-bit cells.
    """
    dtype = np.dtype(dtype)
    shape = (8, 8) if samples == 1 else (8, 8, samples)
    if frames > 1:
        shape = (frames,) + shape
    n = int(np.prod(shape))
    base = np.arange(n, dtype=np.int64)
    if dtype == np.bool_:
        return (base % 3 == 0).reshape(shape)
    if bits_stored is not None:
        return (base * 21 % (1 << bits_stored)).astype(dtype).reshape(shape)
    if dtype == np.uint8:
        return (base * 7 % 256).astype(dtype).reshape(shape)
    if dtype == np.int8:
        return (base % 200 - 100).astype(dtype).reshape(shape)
    if dtype == np.uint16:
        return (base * 331 % 65536).astype(dtype).reshape(shape)
    values = base * 331 % 65536 - 32768
    values[0], values[-1] = -32768, 32767
    return values.astype(dtype).reshape(shape)


def _image(arr, label, *, samples, frames=1, after=()):
    """A hand-built SC instance carrying `arr` under `label`.

    NumberOfFrames goes on before the pixels, because it decides how the
    array's shape is read; `after` (BitsStored, say) goes on after them,
    because `set_pixel_data()` rewrites the width from the array.
    """
    inst = Instance(f"1.2.826.0.1.670.{next(_serial)}", SC_STORAGE, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "OT"), ("0028,0002", samples)):
        inst.set_attr(tag, value)
    if frames > 1:
        inst.set_attr("0028,0008", frames)
    inst.set_pixel_data(arr)
    inst.set_attr("0028,0004", label)
    for tag, value in after:
        inst.set_attr(tag, value)
    return inst


def _export(tmp_path, inst, **kwargs):
    return _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        **kwargs))


def _graph(instances):
    patient = Patient("PAT1", "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "OT", 1)
    series.instances.extend(instances)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _grade(report):
    return [line.strip() for line in report.read_text().splitlines()
            if "Grade Basis" in line][0]


def _notes(corrections):
    return [c for c in corrections if "#670" in c]


def _codestream_samples(path):
    """Every frame decoded by imagecodecs, straight off the codestream."""
    ds = pydicom.dcmread(path)
    frames = int(ds.get("NumberOfFrames", 1) or 1)
    decoded = [imagecodecs.jpeg2k_decode(f) for f in
               generate_frames(ds.PixelData, number_of_frames=frames)]
    return np.stack(decoded) if frames > 1 else decoded[0]


# ---------------------------------------------------------------------------
# The cell: written as before, and named.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("frames", [1, 2])
@pytest.mark.parametrize("dtype", [np.uint16, np.int16])
def test_the_16_bit_colour_cell_is_written_and_noted(tmp_path, dtype, frames):
    """The file is JPEG 2000 and exact, Pillow refuses it, and it is noted.

    The syntax assertion pins #416's export half: writing this cell
    uncompressed (design U, costed and not taken) would reverse it, and
    must not slip in unasked.

    The premise -- `pixel_array` raises with Pillow -- is held
    unconditionally, as P8x in `test_signed_pixels_survive_a_compressed_
    export.py` holds it. If this environment gains `pylibjpeg` with
    `pylibjpeg-openjpeg`, pydicom decodes the file and this test goes
    red. That is deliberate: the note would still be true of a reader
    with only Pillow, but the suite would no longer be measuring the
    reader the note is about, and someone should look.

    Killing mutations: the note block deleted; the note routed to
    `warnings`.
    """
    arr = _literal(dtype, 3, frames)
    outcome = _export(tmp_path, _image(arr, "RGB", samples=3, frames=frames),
                      compression="j2k")

    assert outcome.ok, outcome.error
    assert outcome.warnings == [], outcome.warnings
    notes = _notes(outcome.corrections)
    assert len(notes) == 1, outcome.corrections
    for phrase in ("BitsAllocated 16", "3 samples per pixel",
                   "pylibjpeg-openjpeg", "use_compression=False",
                   PILLOW_REFUSAL):
        assert phrase in notes[0], (phrase, notes[0])

    ds = pydicom.dcmread(outcome.output_path)
    assert ds.file_meta.TransferSyntaxUID == J2K_LOSSLESS
    assert ds.BitsAllocated == 16
    got = _codestream_samples(outcome.output_path)
    assert got.dtype == np.dtype(dtype), got.dtype
    assert got.tolist() == arr.tolist()

    with pytest.raises(RuntimeError, match=PILLOW_REFUSAL):
        _ = ds.pixel_array


# ---------------------------------------------------------------------------
# Only that cell: the two-sided matrix.
# ---------------------------------------------------------------------------

MATRIX = [
    # (id, dtype, samples, label, bits_stored, noted)
    ("uint8-mono", np.uint8, 1, "MONOCHROME2", None, False),
    ("int8-mono", np.int8, 1, "MONOCHROME2", None, False),
    ("uint16-mono", np.uint16, 1, "MONOCHROME2", None, False),
    ("int16-mono", np.int16, 1, "MONOCHROME2", None, False),
    ("uint16-mono-bs12", np.uint16, 1, "MONOCHROME2", 12, False),
    ("bool-mono", np.bool_, 1, "MONOCHROME2", None, False),
    ("uint8-rgb", np.uint8, 3, "RGB", None, False),
    ("int8-rgb", np.int8, 3, "RGB", None, False),
    ("uint8-ybr-full", np.uint8, 3, "YBR_FULL", None, False),
    ("uint16-rgb", np.uint16, 3, "RGB", None, True),
    ("int16-rgb", np.int16, 3, "RGB", None, True),
]


@pytest.mark.parametrize("dtype, samples, label, bits_stored, noted",
                         [row[1:] for row in MATRIX],
                         ids=[row[0] for row in MATRIX])
def test_only_that_cell_is_noted(tmp_path, dtype, samples, label,
                                 bits_stored, noted):
    """Noted if and only if the file is 16-bit with more than one sample.

    That is Pillow's own predicate (precision above 8, more than one
    sample), and the file's precision is its container width, because
    `jpeg2k_encode` writes BitsAllocated as the precision. Each silent
    cell also asserts that Pillow decodes it, so this is a measurement of
    the matrix and not only of the note: a silent cell Pillow could not
    read would be a second #670.

    The `YBR_FULL` cell is compared with the uncompressed export of the
    same instance, not with the literal: pydicom converts `YBR_FULL` to
    RGB in both files and the literal is YBR (the trap in §1.2 of the
    spec). Every other silent cell is compared with the literal; pydicom
    undoes `YBR_RCT`, so an `RGB` file comes back `RGB`.

    Killing mutations: the `SamplesPerPixel` guard dropped (16-bit
    monochrome is noted); the `BitsAllocated` guard dropped or widened to
    `>= 8` (8-bit colour is noted).
    """
    arr = _literal(dtype, samples, bits_stored=bits_stored)
    after = (() if bits_stored is None else
             (("0028,0101", bits_stored), ("0028,0102", bits_stored - 1)))
    outcome = _export(tmp_path, _image(arr, label, samples=samples,
                                       after=after), compression="j2k")

    assert outcome.ok, outcome.error
    assert outcome.warnings == [], outcome.warnings
    ds = pydicom.dcmread(outcome.output_path)
    assert ds.file_meta.TransferSyntaxUID == J2K_LOSSLESS
    if noted:
        assert len(_notes(outcome.corrections)) == 1, outcome.corrections
        with pytest.raises(RuntimeError, match=PILLOW_REFUSAL):
            _ = ds.pixel_array
        return

    assert _notes(outcome.corrections) == [], outcome.corrections
    got = ds.pixel_array
    if label == "YBR_FULL":
        native = _export(tmp_path / "native",
                         _image(arr, label, samples=samples))
        assert native.ok, native.error
        expected = pydicom.dcmread(native.output_path).pixel_array
        assert got.dtype == np.dtype(np.uint8), got.dtype
        assert got.tolist() == expected.tolist()
        return
    written = arr.astype(np.uint8) if arr.dtype == np.bool_ else arr
    assert got.dtype == written.dtype, got.dtype
    assert got.tolist() == written.tolist()


def test_a_16_bit_ybr_full_file_keeps_the_596_note_and_not_this_one(
        tmp_path):
    """The two notes make opposite claims, so one instance carries one.

    A 16-bit `YBR_FULL` file under `j2k` meets every other condition of
    the #670 note. Ingest refuses the shape (#461), which is why the
    matrix probe could not reach it; a hand-built graph can, and
    `write_tree` writes one. It carries the #596 note, "this library
    cannot read such a file back", which is the true one -- no reader
    here, this library included, converts 16-bit YBR. The #670 note says
    this library reads the file back, so beside it that would be false.

    Killing mutation: the `_PYDICOM_CONVERTS` exclusion dropped.
    """
    arr = _literal(np.uint16, 3)
    outcome = _export(tmp_path, _image(arr, "YBR_FULL", samples=3),
                      compression="j2k")

    assert outcome.ok, outcome.error
    assert len(outcome.corrections) == 1, outcome.corrections
    assert "#461" in outcome.corrections[0], outcome.corrections
    assert _notes(outcome.corrections) == [], outcome.corrections


@pytest.mark.parametrize("dtype", [np.uint16, np.int16])
def test_an_uncompressed_16_bit_colour_export_is_not_noted(tmp_path, dtype):
    """Native bytes are Pillow's to read, so there is nothing to say.

    Killing mutation: the transfer syntax guard dropped.
    """
    arr = _literal(dtype, 3)
    outcome = _export(tmp_path, _image(arr, "RGB", samples=3))

    assert outcome.ok, outcome.error
    assert _notes(outcome.corrections) == [], outcome.corrections
    ds = pydicom.dcmread(outcome.output_path)
    assert ds.file_meta.TransferSyntaxUID != J2K_LOSSLESS
    got = ds.pixel_array
    assert got.dtype == np.dtype(dtype), got.dtype
    assert got.tolist() == arr.tolist()


# ---------------------------------------------------------------------------
# INFO, not WARNING, and both public doors say it.
# ---------------------------------------------------------------------------

def test_the_note_writes_no_row_and_the_grade_stays_pass(
        tmp_path, monkeypatch, caplog):
    """The parent logs the note at INFO; no row, and the run reads PASS.

    A `WARNING` row would grade a correct file `REVIEW_REQUIRED`: the
    file is exact and this library reads it back. The SOP UID the line
    carries is read off the instance after the export, never a literal.

    Killing mutation: the note routed to `warnings` (the parent half --
    a row appears and the grade moves).
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    inst = _image(_literal(np.uint16, 3), "RGB", samples=3)
    report = tmp_path / "report.md"

    with caplog.at_level(logging.INFO, logger="isocenter"), \
            DicomSession(str(tmp_path / "n.db")) as session:
        session.store.patients.append(_graph([inst]))
        session.save()
        session.export(str(tmp_path / "out"), use_compression=True,
                       show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
        session.generate_report(str(report))
        uid = session.store.patients[0].studies[0].series[0] \
            .instances[0].sop_instance_uid

    assert rows == [], rows
    assert "PASS" in _grade(report), _grade(report)
    lines = [r for r in caplog.records if "#670" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    assert lines[0].levelno == logging.INFO
    assert lines[0].getMessage().startswith(f"{uid}: "), lines[0].getMessage()


def test_write_tree_notes_it_too(tmp_path, caplog):
    """The serializer door logs the same line (#670).

    Killing mutation: the note added on a session path only.
    """
    inst = _image(_literal(np.int16, 3), "RGB", samples=3)

    with caplog.at_level(logging.INFO, logger="isocenter"):
        DicomExporter.write_tree(_graph([inst]), str(tmp_path / "out"),
                                 compression="j2k", show_progress=False)

    lines = [r for r in caplog.records if "#670" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    assert lines[0].levelno == logging.INFO
    assert lines[0].getMessage().startswith(f"{inst.sop_instance_uid}: ")


# ---------------------------------------------------------------------------
# Keyed on the written file: no pixels, or an icon, is not noted.
# ---------------------------------------------------------------------------

def test_a_pixel_less_file_is_not_noted(tmp_path):
    """Declared 16-bit, 3-sample descriptors with no pixels: no note.

    A pixel-less file stays native under `compression="j2k"`, where the
    worker's `written_syntax` still says JPEG 2000; the note reads the
    file's own syntax after `_finalize_dataset`, and the integer arm's
    having written.

    Killing mutation: the note keyed on declared attributes, or on
    `written_syntax`, rather than on the written file.
    """
    # SR-shaped, as #534's `_pixel_less` is: an image IOD with no pixels
    # is refused by `IODValidator` before the note could be reached.
    inst = Instance(f"1.2.826.0.1.670.{next(_serial)}", SR_STORAGE, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "SR"), ("0028,0002", 3),
                       ("0028,0004", "RGB"), ("0028,0010", 8),
                       ("0028,0011", 8), ("0028,0100", 16),
                       ("0028,0101", 16), ("0028,0102", 15),
                       ("0028,0103", 0)):
        inst.set_attr(tag, value)

    outcome = _export(tmp_path, inst, compression="j2k")

    assert outcome.ok, outcome.error
    assert _notes(outcome.corrections) == [], outcome.corrections
    ds = pydicom.dcmread(outcome.output_path)
    assert "PixelData" not in ds
    assert ds.file_meta.TransferSyntaxUID != J2K_LOSSLESS


def _icon_source(folder):
    """A CT file: a 4x4 16-bit monochrome frame, one 16-bit 3-sample icon."""
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_STORAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_STORAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    # `IODValidator` refuses a CT Image without these.
    ds.SliceThickness, ds.KVP = "1.0", "120"
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = (np.arange(16, dtype=np.uint16) * 1000).tobytes()

    icon = Dataset()
    icon.Rows = icon.Columns = 2
    icon.BitsAllocated = icon.BitsStored = 16
    icon.HighBit = 15
    icon.SamplesPerPixel = 3
    icon.PhotometricInterpretation = "RGB"
    icon.PixelRepresentation = 0
    icon.PlanarConfiguration = 0
    icon.add_new(0x7FE00010, "OW",
                 (np.arange(12, dtype=np.uint16) * 1000).tobytes())
    ds.IconImageSequence = Sequence([icon])
    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)


def test_an_icon_is_not_noted(tmp_path, monkeypatch, caplog):
    """A 16-bit, 3-sample icon under a monochrome image: no note.

    The icon is always written raw inside the item, whatever the file's
    syntax, and the note reads the top-level descriptors of the file as
    written, so an icon cannot reach it. The top-level image is 16-bit
    monochrome and JPEG 2000, which Pillow reads.

    Killing mutation: the note keyed on any 16-bit, 3-sample descriptor
    set the instance carries, the icon's included.
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    src = tmp_path / "src"
    src.mkdir()
    _icon_source(str(src))
    out = tmp_path / "out"

    with caplog.at_level(logging.INFO, logger="isocenter"), \
            DicomSession(str(tmp_path / "icon.db")) as session:
        session.ingest(str(src))
        session.export(str(out), use_compression=True, show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]

    assert rows == [], rows
    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, written
    ds = pydicom.dcmread(written[0])
    assert ds.file_meta.TransferSyntaxUID == J2K_LOSSLESS
    icon = ds.IconImageSequence[0]
    assert (icon.BitsAllocated, icon.SamplesPerPixel) == (16, 3)
    assert ds.pixel_array.tolist() == (
        np.arange(16, dtype=np.uint16) * 1000).reshape(4, 4).tolist()
    assert [r.getMessage() for r in caplog.records
            if "#670" in r.getMessage()] == []

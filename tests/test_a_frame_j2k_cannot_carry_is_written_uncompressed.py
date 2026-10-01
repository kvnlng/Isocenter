"""A 32- or 64-bit integer frame is written uncompressed under the default compression (#771).

`session.export(folder)` compresses by default (`use_compression=True`), and
JPEG 2000 Lossless here is exact only to 25 bits: `_J2K_ENCODABLE_FRAMES`
refuses every 32- and 64-bit integer frame by name (#404), because the codec
encodes a 32-bit frame silently wrong and refuses a 64-bit one. Measured on
`main` at 7579d4df, that refusal failed the instance: an RT Dose, a 32-bit
secondary capture, or any `set_pixel_data()` of a 32/64-bit array exported
nothing under the default, with one `ERROR` row each, and an export whose
instances were all such images raised `ExportError`. The same pixels were
written exactly with `use_compression=False`.

The owner's ruling (Q1): write such a frame uncompressed, Implicit VR Little
Endian, with one INFO note on `ExportOutcome.corrections` and no audit row.
The decision is per instance, from the frame, so a 16-bit instance beside it
in the same export is still compressed. The guard inside `_compress_j2k` stays
as the backstop for a direct caller, and both read one predicate,
`_j2k_encodable`, so they cannot drift.

Worker-level arms call `_export_instance_worker` in this process; the session
arms check the parent's half. Values are asserted against literals and
dtypes against absolute dtypes.
"""
import itertools
import logging

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

import isocenter.io_handlers as io_handlers
from isocenter.entities import Instance
from isocenter.io_handlers import ExportContext, _export_instance_worker
from isocenter.session import DicomSession

SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"
IMPLICIT = "1.2.840.10008.1.2"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"

_serial = itertools.count(1)


def _literal(dtype, samples=1):
    """Samples of `dtype` that reach past 25 bits wherever the width allows."""
    dtype = np.dtype(dtype)
    shape = (4, 4) if samples == 1 else (4, 4, samples)
    n = int(np.prod(shape))
    info = np.iinfo(dtype)
    # Both extremes, then values spread over the positive half.
    values = [int(info.min), int(info.max), 0, 1] + [
        (i * 2654435761) % (2 ** (info.bits - 1)) for i in range(n - 4)]
    return np.array(values[:n], dtype=dtype).reshape(shape)


def _image(arr, *, samples=1, label=None):
    inst = Instance(f"1.2.826.0.1.771.{next(_serial)}", SC_STORAGE, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "OT"), ("0028,0002", samples)):
        inst.set_attr(tag, value)
    inst.set_pixel_data(arr)
    if label is not None:
        inst.set_attr("0028,0004", label)
    return inst


def _export(tmp_path, inst, compression="j2k"):
    return _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        compression=compression))


def _notes(corrections):
    return [c for c in corrections if "#771" in c]


@pytest.mark.parametrize("dtype,samples", [
    ("uint32", 1), ("int32", 1), ("uint32", 3),
    ("uint64", 1), ("int64", 1),
])
def test_a_wide_frame_is_written_uncompressed_under_the_default(tmp_path, dtype, samples):
    arr = _literal(dtype, samples)
    inst = _image(arr, samples=samples, label="RGB" if samples == 3 else None)
    outcome = _export(tmp_path, inst)

    assert outcome.ok, outcome.error
    ds = pydicom.dcmread(outcome.output_path)
    assert str(ds.file_meta.TransferSyntaxUID) == IMPLICIT
    assert "PixelData" in ds
    bits = np.dtype(dtype).itemsize * 8
    assert int(ds.BitsAllocated) == bits
    got = ds.pixel_array
    assert got.dtype == np.dtype(dtype), got.dtype
    assert got.tolist() == arr.tolist()
    notes = _notes(outcome.corrections)
    assert len(notes) == 1, outcome.corrections
    assert f"BitsAllocated {bits}" in notes[0]
    assert dtype in notes[0]
    assert "exactly" in notes[0]
    assert outcome.warnings == [], outcome.warnings


@pytest.mark.parametrize("dtype", ["uint16", "int16", "uint8"])
def test_a_frame_j2k_can_carry_is_still_compressed(tmp_path, dtype):
    outcome = _export(tmp_path, _image(_literal(dtype)))
    assert outcome.ok, outcome.error
    ds = pydicom.dcmread(outcome.output_path)
    assert str(ds.file_meta.TransferSyntaxUID) == J2K_LOSSLESS
    assert _notes(outcome.corrections) == []


def test_an_uncompressed_export_writes_no_note(tmp_path):
    arr = _literal("uint32")
    outcome = _export(tmp_path, _image(arr), compression=None)
    assert outcome.ok, outcome.error
    assert _notes(outcome.corrections) == []
    assert pydicom.dcmread(outcome.output_path).pixel_array.tolist() == arr.tolist()


def test_the_pixel_data_provider_url_is_dropped_on_the_fallback_too(tmp_path):
    inst = _image(_literal("int32"))
    inst.set_attr("0028,7fe0", "https://example.invalid/pixels")
    outcome = _export(tmp_path, inst)
    assert outcome.ok, outcome.error
    ds = pydicom.dcmread(outcome.output_path)
    assert "PixelDataProviderURL" not in ds
    assert "PixelData" in ds
    assert any("Pixel Data Provider URL" in detail
               for _scope, detail in outcome.losses), outcome.losses


def test_the_decision_and_the_guard_read_one_table(tmp_path, monkeypatch):
    """Widen the table and the worker sends a 32-bit frame to the encoder.

    Red if the worker decides from a set of its own: it would still fall
    back and never reach `_compress_j2k`.
    """
    monkeypatch.setattr(io_handlers, "_J2K_ENCODABLE_FRAMES",
                        io_handlers._J2K_ENCODABLE_FRAMES | {(4, False)})
    reached = []

    def spy(ds, pixel_array=None):
        reached.append(pixel_array.dtype)
        raise RuntimeError("spy: the encoder was reached")

    monkeypatch.setattr(io_handlers, "_compress_j2k", spy)
    outcome = _export(tmp_path, _image(_literal("uint32")))
    assert reached == [np.dtype("uint32")]
    assert not outcome.ok
    assert _notes(outcome.corrections) == []


def test_a_wide_ybr_rct_label_is_not_told_to_compress(tmp_path):
    """The #502 label WARNING's ICT/RCT remedy says "export with
    use_compression=True", which is false for a frame that compression
    writes uncompressed anyway."""
    arr = _literal("uint32", 3)
    outcome = _export(tmp_path, _image(arr, samples=3, label="YBR_RCT"))
    assert outcome.ok, outcome.error
    label = [w for w in outcome.warnings if "YBR_RCT" in w]
    assert len(label) == 1, outcome.warnings
    assert "Export with use_compression=True" not in label[0], label[0]
    assert "#771" in label[0], label[0]


# ---------------------------------------------------------------------------
# The session door: ingest a real 32-bit file and export it by default.
# ---------------------------------------------------------------------------

def _write_src(path, arr):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC_STORAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT771", "DOE^JOHN"
    ds.StudyInstanceUID = "1.2.826.0.1.771.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.771.3"
    ds.SOPClassUID = SC_STORAGE
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Modality, ds.StudyDate, ds.StudyTime = "OT", "20200101", "120000"
    ds.Rows, ds.Columns = arr.shape[:2]
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = arr.itemsize * 8
    ds.HighBit = arr.itemsize * 8 - 1
    ds.PixelRepresentation = 1 if arr.dtype.kind == "i" else 0
    ds.PixelData = arr.tobytes()
    ds.save_as(str(path), enforce_file_format=True)


def test_one_export_writes_each_instance_in_its_own_syntax(tmp_path, caplog):
    src = tmp_path / "src"
    src.mkdir()
    wide = _literal("uint32")
    narrow = _literal("uint16")
    _write_src(src / "wide.dcm", wide)
    _write_src(src / "narrow.dcm", narrow)

    db = str(tmp_path / "s.db")
    with caplog.at_level(logging.INFO, logger="isocenter"), \
            DicomSession(db) as session:
        session.ingest(str(src))
        summary = session.export(str(tmp_path / "out"), show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]

    assert summary.failures == []
    assert not [r for r in rows if r[1] == "ERROR"], rows
    syntaxes = {}
    for path in (tmp_path / "out").rglob("*.dcm"):
        ds = pydicom.dcmread(str(path))
        syntaxes[int(ds.BitsAllocated)] = str(ds.file_meta.TransferSyntaxUID)
        want = wide if int(ds.BitsAllocated) == 32 else narrow
        assert ds.pixel_array.tolist() == want.tolist()
    assert syntaxes == {32: IMPLICIT, 16: J2K_LOSSLESS}
    lines = [r for r in caplog.records if "#771" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    assert lines[0].levelno == logging.INFO


def test_an_export_of_only_wide_frames_writes_with_no_row(tmp_path):
    """On main this raised `ExportError`: wrote 0 of 1."""
    src = tmp_path / "src"
    src.mkdir()
    _write_src(src / "wide.dcm", _literal("int32"))

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        summary = session.export(str(tmp_path / "out"), show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]

    assert summary.failures == []
    assert len(list((tmp_path / "out").rglob("*.dcm"))) == 1
    assert not [r for r in rows if r[1] == "ERROR"], rows
    assert not [r for r in rows if "#771" in r[2]], rows

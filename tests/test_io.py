import os
import pydicom
import numpy as np
import pytest
from unittest.mock import patch
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from isocenter.entities import Instance, Study, Series, Patient
from isocenter.builders import DicomBuilder
from isocenter.io_handlers import DicomExporter, DicomImporter, DicomStore


def test_export_import_roundtrip(tmp_path, dummy_patient):
    # 1. Export
    export_dir = tmp_path / "export_test"
    DicomExporter.write_tree(dummy_patient, str(export_dir))

    DicomExporter.write_tree(dummy_patient, str(export_dir))

    files = list(export_dir.rglob("*.dcm"))
    assert len(files) == 1

    # 2. Import into new store
    store = DicomStore()
    DicomImporter.import_files([str(f) for f in files], store)

    assert len(store.patients) == 1
    imported_inst = store.patients[0].studies[0].series[0].instances[0]

    # Check if file path was captured (for lazy loading)
    assert imported_inst.file_path is not None
    assert str(export_dir) in imported_inst.file_path

def test_persistence_priority(tmp_path):
    """
    Ensure that explicit Object Model fields (e.g. StudyDate) take precedence
    over legacy attributes in DicomExporter.
    """
    # 1. Create dummy instance
    inst = Instance("1.2.3.4", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.attributes["0008,0020"] = "20000101" # Stale date
    inst.set_pixel_data(np.zeros((10,10), dtype=np.uint8)) # Valid pixels

    study = Study("1.2.3.99", "20230101") # New/Remediated date
    series = Series("1.2.3.98", "OT", 1)

    study.series.append(series)
    series.instances.append(inst)

    pat = Patient("P1", "Test")
    pat.studies.append(study)

    # 2. Export
    out_dir = tmp_path / "export_prio"

    # Mock validator to accept sparse dummy data
    # AND Mock run_parallel to run synchronously so the patch applies!
    with patch('isocenter.validation.IODValidator.validate', return_value=[]), \
         patch('isocenter.io_handlers.run_parallel', side_effect=lambda func, items, *a, **k: [func(i) for i in items]):
        DicomExporter.write_tree(pat, str(out_dir))

    # 3. Read back
    exported_files = list(out_dir.rglob("*.dcm"))
    assert len(exported_files) > 0
    exported_file = exported_files[0]
    ds = pydicom.dcmread(exported_file)

    # 4. Assert
    assert ds.StudyDate == "20230101", "Export should prioritize Study object field over attributes dict"


def _write_minimal(path, **overrides):
    """A single ordinary DICOM file: identifiers only, no pixels.

    Only the fields named in `overrides` are added on top of the
    identifying set. That is load-bearing for #282: `ingest_worker`
    reads `ds.get("ManufacturerModelName", "")`, so "the model is
    absent" has to mean the element is not in the file at all. Writing
    it as an empty element would exercise pydicom's empty-value
    handling instead of the default, which is not what the `or` guards.

    No PixelData: the linkage arm under test runs before anything looks
    at pixels, and a file with none cannot fail decompression.
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber = "OT", 1
    ds.StudyDate = "20230101"

    for keyword, value in overrides.items():
        setattr(ds, keyword, value)

    ds.save_as(str(path), enforce_file_format=True)
    return str(path)


@pytest.mark.parametrize("man, model", [("ACME", None), (None, "Scanner")])
def test_a_series_with_a_manufacturer_and_no_model_still_gets_its_equipment(
        tmp_path, man, model):
    """Partial equipment metadata is equipment (#282).

    `import_files`' linkage arm builds `Equipment` under
    `if meta['man'] or meta['model']`. Both directions of the partial
    case are parametrized to document the issue's "or the reverse";
    either one alone kills the `or -> and` mutant, so this buys
    documentation rather than extra kill power.

    The two field assertions are deliberate: `is not None` alone would
    survive an argument swap in the `Equipment(...)` construction.
    """
    overrides = {}
    if man is not None:
        overrides["Manufacturer"] = man
    if model is not None:
        overrides["ManufacturerModelName"] = model
    path = _write_minimal(tmp_path / "one.dcm", **overrides)

    store = DicomStore()
    DicomImporter.import_files([path], store)

    series = store.patients[0].studies[0].series[0]
    assert series.equipment is not None
    assert series.equipment.manufacturer == (man or "")
    assert series.equipment.model_name == (model or "")


def _write_without_preamble(path):
    """The same identifying set, written with no preamble and no file meta.

    A plain `Dataset` rather than a `FileDataset`: that is what makes the
    file preamble-less and `DICM`-less, which is the shape `force=True`
    exists to accept (raw exports, some vendor dumps).

    Two traps. `SOPClassUID` is set on the dataset on purpose --
    `ingest_worker` only consults `ds.file_meta` when `sop_class` is
    empty, and with `force=True` on a header-less file `ds.file_meta` is
    an EMPTY `FileMetaDataset`; a truthy `sop_class` short-circuits that
    lookup and keeps this test about the preamble alone. And no
    PixelData: the file carries no transfer syntax of its own, so there
    would be nothing to decode the pixels against.
    """
    ds = pydicom.Dataset()
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    ds.Modality, ds.SeriesNumber = "OT", 1
    ds.StudyDate = "20230101"

    pydicom.dcmwrite(str(path), ds, implicit_vr=True, little_endian=True,
                     enforce_file_format=False)
    return ds.SOPInstanceUID


def test_a_file_with_no_preamble_is_accepted_by_the_eager_ingest(tmp_path):
    """`force=True` on the one ingest read is behaviour, not decoration (#281).

    There is exactly one ingest path -- `Session.ingest()` ->
    `DicomImporter.import_files()` -> `run_parallel(ingest_worker, ...)`
    -> `pydicom.dcmread(fp, stop_before_pixels=False, force=True)` --
    so calling `import_files` is calling the eager arm. (The other
    `dcmread` on a source file, in `Instance.get_pixel_data()`, is the
    lazy PIXEL load, not a second ingest.)
    """
    path = tmp_path / "raw.dcm"
    sop = _write_without_preamble(path)

    # Honest-fixture guard, and load-bearing: without it, a fixture that
    # quietly regained its preamble would make everything below pass
    # under the mutant and this test would pin nothing.
    with pytest.raises(pydicom.errors.InvalidDicomError):
        pydicom.dcmread(str(path))

    store = DicomStore()
    summary = DicomImporter.import_files([str(path)], store)

    # `IngestSummary` (#211) rather than a bare graph check: under the
    # mutant the reason is named here instead of surfacing as an empty
    # store with no explanation.
    assert summary.failures == []
    assert summary.ingested == 1
    assert (store.patients[0].studies[0].series[0]
            .instances[0].sop_instance_uid) == sop


def _write_headerless_with_pixels(path):
    """The same shape as `_write_without_preamble`, but carrying pixels.

    A `FileDataset` with a complete `file_meta` -- including a
    `TransferSyntaxUID`, which is what the pixels have to be decoded
    against -- written with `enforce_file_format=False` so no preamble
    and no `DICM` prefix reach the file. That is the combination the
    eager ingest's `force=True` accepts and a plain `dcmread` refuses,
    and it is the ordinary shape of a raw vendor dump.
    """
    arr = np.arange(16, dtype=np.uint16).reshape(4, 4)

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()

    ds = FileDataset(str(path), pydicom.Dataset(), file_meta=fm, preamble=None)
    ds.PatientID, ds.PatientName = "PAT289", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    ds.Modality, ds.SeriesNumber, ds.StudyDate = "OT", 1, "20230101"
    ds.Rows, ds.Columns, ds.SamplesPerPixel = 4, 4, 1
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
    ds.PixelRepresentation = 0
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = arr.tobytes()

    pydicom.dcmwrite(str(path), ds, enforce_file_format=False)
    return ds.SOPInstanceUID, arr


def _ingest_headerless(path):
    """Ingest with no sidecar manager, so `file_path` is the only route back.

    `import_files` binds a `_pixel_loader` only when a sidecar is
    supplied; without one the instance carries neither a loader nor a
    resident array, and `get_pixel_data()` reaches its third step -- the
    `file_path` re-read -- on the ordinary path rather than after some
    explicit clearing.
    """
    store = DicomStore()
    summary = DicomImporter.import_files([str(path)], store)
    assert summary.failures == []
    return store.patients[0].studies[0].series[0].instances[0]


def test_a_file_the_eager_ingest_accepted_can_reload_its_own_pixels(tmp_path):
    """The two reads of a source file have to accept the same files (#289).

    `ingest_worker` reads with `force=True` (#281); `get_pixel_data()`'s
    third step re-read did not. A header-less file therefore ingested
    cleanly -- metadata indexed, `IngestSummary(ingested=1,
    failures=[])`, nothing audited -- and then could not produce its own
    pixels: `RuntimeError: Lazy load failed for ...: File is missing
    DICOM File Meta Information header or the 'DICM' prefix is missing`.
    A file accepted at one boundary and refused at the next is the
    defect, whichever boundary is "right" in isolation.
    """
    path = tmp_path / "raw_pixels.dcm"
    sop, arr = _write_headerless_with_pixels(path)

    # Honest-fixture guard, as in the ingest test above: without it a
    # fixture that quietly regained its preamble would make everything
    # below pass under the mutant, and this test would pin nothing.
    with pytest.raises(pydicom.errors.InvalidDicomError):
        pydicom.dcmread(str(path))

    inst = _ingest_headerless(path)
    assert inst.sop_instance_uid == sop
    # No sidecar, so no loader: the assertion below is about step 3.
    assert inst._pixel_loader is None

    got = inst.get_pixel_data()
    assert got is not None
    assert got.shape == (4, 4)
    assert np.array_equal(got, arr)


def test_unload_reports_a_pixel_array_it_can_actually_bring_back(tmp_path):
    """The silent half of #289: the loud one is a `RuntimeError`, this is not.

    `unload_pixel_data()`'s contract is that it clears the resident array
    **only when it can be brought back**, and it accepts `file_path` as
    one of the two routes back. On a header-less file that route did not
    work, so the unload returned `True` -- the caller's evidence that the
    pixels are safe -- and the pixels were then unreachable. No
    exception, no audit row, nothing in the report.

    The array is assigned rather than set through `set_pixel_data()` on
    purpose: assignment is exactly what `get_pixel_data()`'s three
    success arms do, so this is an array that has been read and is
    resident. `set_pixel_data()` would set `_pixel_array_unwritten` and
    the unload would refuse for #293's reason instead, which is a
    different question.
    """
    path = tmp_path / "raw_pixels.dcm"
    _sop, arr = _write_headerless_with_pixels(path)
    inst = _ingest_headerless(path)

    inst.pixel_array = arr.copy()
    assert inst.unload_pixel_data() is True
    assert inst.pixel_array is None

    # ...and the promise the True stood for.
    back = inst.get_pixel_data()
    assert back is not None
    assert np.array_equal(back, arr)

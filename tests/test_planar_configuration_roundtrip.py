"""Isocenter holds and stores pixels interleaved, always -- two sites.

Nothing in the tree recorded that invariant, and both halves of the
planar-configuration handling were built on the opposite assumption.

pydicom **de-planarises on read**. A planar-configuration-1 file
(`R R R ... G G G ... B B B ...`) comes back from `ds.pixel_array` in
`(rows, cols, samples)` order with the samples interleaved. Measured on
pydicom 3.0.2, a 2-frame 3x3 RGB source written planar:

    ds.PlanarConfiguration = 1 ; PixelData = 0..53 planar
    ds.pixel_array.shape == (2, 3, 3, 3), ravel() == [0 9 18 1 10 19 ...]

So the array `set_pixel_data()` receives is interleaved, the bytes
`persist_pixel_data` writes with `arr.tobytes()` are interleaved, and a
(0028,0006) of `1` sitting in `attributes` describes nothing isocenter
holds. `ingest_worker` normalises that value from 1 to 0 for exactly
this reason, but `set_pixel_data()` deliberately does not (#217 narrowed
`planar_configuration_default` so it would stop overwriting a *declared*
value), so a hand-built or reloaded graph can carry the 1 into both
sites below.

**Site A, the loader.** `SidecarPixelLoader.__call__` reshaped
single-frame colour data as `(samples, rows, cols)` when it saw a
declared 1, then transposed. Fed interleaved bytes, that returns a
transposed image. The multi-frame arm never had the branch and was
therefore correct -- #210's issue text has this exactly inverted, and
also calls the single-frame arm unreachable, which it is not.

**Site B, the exporter.** `DicomExporter.write_tree()` on the same graph
wrote a file whose pixel bytes are interleaved and whose
`PlanarConfiguration` element says `1`, because `_merge` stamps
`attributes` onto the dataset and `_write_pixel_geometry` only corrected
the value when the instance had *not* declared one. That is a corrupt
exported DICOM file produced by the serializer, with no error, no
warning and no `DATA_LOSS` row -- the higher-risk of the two, and on the
export path rather than the loader. (#210)
"""
import numpy as np
import pydicom
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import DicomExporter
from isocenter.persistence import SqliteStore

SC = "1.2.840.10008.5.1.4.1.1.7"


def _colour_instance(uid, arr, *, frames=None):
    """An instance declaring planar configuration 1 over interleaved pixels.

    Exactly the state `set_pixel_data()` leaves behind for a caller who
    declared 1: the descriptors are written from the array's own shape,
    and (0028,0006) is left as the caller declared it.
    """
    inst = Instance(sop_instance_uid=uid)
    inst.set_attr("0028,0002", 3)
    inst.set_attr("0028,0006", 1)
    inst.set_attr("0028,0100", 8)
    inst.set_attr("0028,0101", 8)
    inst.set_attr("0028,0102", 7)
    inst.set_attr("0028,0103", 0)
    inst.set_attr("0028,0004", "RGB")
    inst.set_attr("0008,0016", SC)
    if frames is not None:
        inst.set_attr("0028,0008", frames)
    inst.set_pixel_data(arr)
    assert inst.attributes["0028,0006"] == 1, (
        "set_pixel_data() must leave a declared planar configuration "
        "alone (#217); if it stops doing so, these tests stop testing "
        "what they say they test")
    return inst


def _graph(inst):
    pat = Patient("P210", "PLANAR^TEST")
    st = Study("1.2.210.1", "20230101")
    ser = Series("1.2.210.2", "OT", 1)
    ser.instances.append(inst)
    st.series.append(ser)
    pat.studies.append(st)
    return pat


def _round_trip_through_the_sidecar(inst, tmp_path, name):
    """Persist, drop the resident array, and read it back through the loader."""
    store = SqliteStore(str(tmp_path / f"{name}.db"))
    try:
        store.persist_pixel_data(inst)
        inst.pixel_array = None
        return inst.get_pixel_data()
    finally:
        store.stop()


def test_a_declared_planar_one_single_frame_survives_the_sidecar(tmp_path):
    """RED before the fix: the loader transposed a single-frame image.

    `(samples, rows, cols)` is the shape of *planar* bytes, and the
    sidecar never holds planar bytes.
    """
    arr = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    inst = _colour_instance("1.2.210.10", arr)

    back = _round_trip_through_the_sidecar(inst, tmp_path, "single")

    assert back.shape == arr.shape
    assert np.array_equal(back, arr), (
        f"the sidecar round trip returned {back.ravel()[:9].tolist()} "
        f"where {arr.ravel()[:9].tolist()} went in (#210)")


def test_a_declared_planar_one_single_frame_exports_the_image_it_holds(tmp_path):
    """RED before the fix: `write_tree` wrote interleaved bytes labelled planar.

    Asserted on `ds.pixel_array`, not on `ds.PixelData`. The raw bytes
    were already right; it is the *meaning* the file gives them that was
    wrong, and a byte comparison passes on both sides of the defect.
    """
    arr = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    inst = _colour_instance("1.2.210.11", arr)

    out = tmp_path / "export"
    DicomExporter.write_tree(_graph(inst), str(out))
    written = list(out.rglob("*.dcm"))
    assert len(written) == 1

    ds = pydicom.dcmread(str(written[0]))
    assert ds.PlanarConfiguration == 0, (
        "the exported file declares planar configuration "
        f"{ds.PlanarConfiguration} over interleaved pixel bytes (#210)")
    assert np.array_equal(ds.pixel_array, arr), (
        "the exported file decodes to a different image from the one "
        "the graph holds (#210)")


def test_a_declared_planar_one_multi_frame_still_survives_the_sidecar(tmp_path):
    """GREEN on both sides, and the record of which arm was already right.

    The multi-frame arm never had a planar branch, so it reshaped
    interleaved bytes as `(frames, rows, cols, samples)` and was correct
    all along. This test states that, so a future change that "fixes the
    multi-frame arm to honour planar 1" -- which is what #210's issue
    text asks for -- turns it red instead of reading as an improvement.
    """
    arr = np.arange(2 * 3 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3, 3)
    inst = _colour_instance("1.2.210.12", arr, frames=2)

    back = _round_trip_through_the_sidecar(inst, tmp_path, "multi")

    assert back.shape == arr.shape
    assert np.array_equal(back, arr)


def test_ingest_normalises_a_real_planar_one_file(tmp_path):
    """GREEN on both sides: `ingest_worker`'s normalisation, pinned.

    It is the reason a planar 1 is rare rather than impossible on the
    graph, and it is also the measurement the invariant rests on -- the
    bytes pydicom hands back for a planar source are interleaved, so
    recording 0 beside them is the honest label rather than a
    convenience.
    """
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    from isocenter.io_handlers import DicomImporter, DicomStore

    interleaved = np.arange(2 * 3 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3, 3)
    # Written planar: the source file's byte order really is R.., G.., B..
    planar_bytes = np.ascontiguousarray(
        interleaved.transpose(0, 3, 1, 2)).tobytes()

    path = tmp_path / "planar.dcm"
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = SC
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds = FileDataset(str(path), pydicom.Dataset(), file_meta=fm, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "P210", "PLANAR^TEST"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.SOPClassUID = SC
    ds.Modality, ds.SeriesNumber, ds.StudyDate = "OT", 1, "20230101"
    ds.Rows, ds.Columns, ds.SamplesPerPixel = 3, 3, 3
    ds.NumberOfFrames = 2
    ds.PlanarConfiguration = 1
    ds.PhotometricInterpretation = "RGB"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
    ds.PixelRepresentation = 0
    ds.PixelData = planar_bytes
    ds.save_little_endian = True
    ds.save_implicit_vr = False
    pydicom.dcmwrite(str(path), ds, enforce_file_format=True)

    # The measurement the invariant rests on: pydicom de-planarises.
    reread = pydicom.dcmread(str(path))
    assert reread.PlanarConfiguration == 1
    assert np.array_equal(reread.pixel_array, interleaved)

    store = DicomStore()
    summary = DicomImporter.import_files([str(path)], store)
    assert summary.failures == []
    inst = store.patients[0].studies[0].series[0].instances[0]
    assert inst.attributes["0028,0006"] == 0, (
        "ingest holds pixels interleaved, so the descriptor beside them "
        "must say 0")

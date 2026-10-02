"""A dtype-only `set_pixel_data()` is not written off by the dedup (#406).

`SidecarPixelLoader` snapshots rows, columns, samples, frames,
BitsAllocated, PixelRepresentation and the `_ISOCENTER_PIXEL_DTYPE`
carrier **once**, at construction, and rebuilds every frame from that
snapshot. `_persist_pixels`' deduplication arm -- "these exact bytes are
already in the sidecar" -- returned the loader's own frame without
rebuilding it, so a `set_pixel_data()` that changed only the *type* of
the pixels, leaving the bytes bit-identical, was committed by a save that
never re-read the instance. The loader went on describing the frame it
had replaced.

Every test here asserts on a **decoded array or a written file**, never
on the absence of an exception, and every one proves it entered the dedup
arm -- because the arm is the only place the defect lives, and a fixture
that appends a fresh frame never reaches it.

Nothing in this repository could see this defect: with the fix applied
and nothing else changed the whole suite was green, exactly as it was
without it. `tests/conftest.py`'s shared pixel fixture is all-zero
`uint16` and its users route through `write_tree`/`execute_config` rather
than `session.export()`, so it can construct neither half. These tests
build their own sources for that reason.
"""
import glob
import logging
import os
import re

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import SidecarPixelLoader
from isocenter.session import DicomSession


def _skeleton():
    """The metadata every source here shares; the pixel element differs."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT406", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    return ds


def _write_int_src(folder, arr, name="one.dcm"):
    """An ordinary (7fe0,0010) source carrying `arr`."""
    ds = _skeleton()
    ds.Rows, ds.Columns = arr.shape
    ds.BitsAllocated = ds.BitsStored = arr.dtype.itemsize * 8
    ds.HighBit = ds.BitsAllocated - 1
    ds.PixelRepresentation = 1 if arr.dtype.kind == "i" else 0
    ds.PixelData = arr.tobytes()
    ds.save_as(os.path.join(folder, name), enforce_file_format=True)


def _write_float_src(folder, arr, name="one.dcm"):
    """A source whose only pixel element is (7fe0,0008) Float Pixel Data.

    This is the severe carrier: the export picks its pixel container from
    the *array's* dtype rather than from `attributes`, so a stale loader
    handing back `float32` writes `FloatPixelData` no matter what the
    descriptors say.
    """
    ds = _skeleton()
    ds.Rows, ds.Columns = arr.shape
    ds.BitsAllocated = ds.BitsStored = 32
    ds.HighBit = 31
    ds.FloatPixelData = arr.tobytes()
    ds.save_as(os.path.join(folder, name), enforce_file_format=True)


def _only_instance(session):
    for pt in session.store.patients:
        for st in pt.studies:
            for se in st.series:
                for inst in se.instances:
                    return inst
    raise AssertionError("the fixture ingested no instance")


def _sidecar(db):
    return db.replace(".db", "_pixels.bin")


def test_the_second_save_appends_nothing_so_this_is_the_dedup_arm(tmp_path):
    """The fixture-never-enters-the-arm guard, and it is load-bearing.

    Without it, a "fix" that simply defeats the deduplication -- clearing
    `_pixel_hash` in `set_pixel_data()`, say -- passes every other test in
    this file, because the *write* arm below the dedup rebuilds the loader
    anyway. It also grows the sidecar by a full frame on every save of
    unchanged bytes, which is the thing the dedup exists to prevent and
    the only thing this assertion can see.
    """
    src = tmp_path / "src"
    src.mkdir()
    arr = (np.arange(16, dtype=np.uint16) + 40000).reshape(4, 4)
    _write_int_src(str(src), arr)
    db = str(tmp_path / "s.db")

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        inst = _only_instance(session)
        session.save(sync=True)
        before_size = os.path.getsize(_sidecar(db))
        before_hash = inst._pixel_hash
        assert isinstance(inst._pixel_loader, SidecarPixelLoader)

        inst.set_pixel_data(arr.view(np.int16))
        session.save(sync=True)

        assert os.path.getsize(_sidecar(db)) == before_size, (
            "the second save appended a frame, so the deduplication arm "
            "was never entered and the rest of this file proves nothing")
        assert inst._pixel_hash == before_hash, (
            "the bytes were meant to be bit-identical across the "
            "replacement; a changed digest means the fixture changed them")
    finally:
        session.close()


def test_a_dtype_only_replacement_reloads_as_the_dtype_the_caller_set(tmp_path):
    """Signedness: `uint16` at 40000+ read as `int16` is negative.

    The source values are deliberately >= 32768. Below that the two
    readings agree and the comparison collapses to `0 == 0`, which is why
    `got.min() < 0` is asserted separately from the array equality.
    """
    src = tmp_path / "src"
    src.mkdir()
    arr = (np.arange(16, dtype=np.uint16) + 40000).reshape(4, 4)
    replacement = arr.view(np.int16)
    db = str(tmp_path / "s.db")
    _write_int_src(str(src), arr)

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        inst = _only_instance(session)
        session.save(sync=True)
        before_size = os.path.getsize(_sidecar(db))

        inst.set_pixel_data(replacement)
        session.save(sync=True)

        # Every test in this file carries this guard, on its own fixture.
        # Test 1 proves the shape is reachable; it cannot speak for a
        # fixture it does not use, and a replacement whose byte length
        # differed would quietly take the write arm below the dedup and
        # pass anyway.
        assert os.path.getsize(_sidecar(db)) == before_size, (
            "the dedup arm was not entered")

        # A precondition, not a courtesy: `unload_pixel_data()` refuses an
        # array replaced through `set_pixel_data()` and not since written
        # (#293). If the save did not clear that flag it returns False, the
        # array stays resident, and `get_pixel_data()` below hands back the
        # resident array without ever consulting the loader -- so the test
        # would pass on unfixed code.
        assert inst.unload_pixel_data() is True
        assert inst.pixel_array is None

        got = inst.get_pixel_data()

        assert got.dtype == np.int16
        assert got.min() < 0
        assert np.array_equal(got.reshape(replacement.shape), replacement)
    finally:
        session.close()


def test_a_geometry_only_replacement_reloads_at_the_geometry_the_caller_set(tmp_path):
    """The same window is open on Rows/Columns, which is why the fix rebuilds.

    A `pixel_dtype`-only patch answers the signedness case and leaves this
    one: the loader reshapes to its own snapshotted 4x4 while `attributes`
    say 2x8.
    """
    src = tmp_path / "src"
    src.mkdir()
    arr = np.arange(16, dtype=np.uint8).reshape(4, 4)
    replacement = arr.reshape(2, 8)
    db = str(tmp_path / "s.db")
    _write_int_src(str(src), arr)

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        inst = _only_instance(session)
        session.save(sync=True)
        before_size = os.path.getsize(_sidecar(db))

        inst.set_pixel_data(replacement)
        session.save(sync=True)

        assert os.path.getsize(_sidecar(db)) == before_size, (
            "the dedup arm was not entered")

        assert inst.unload_pixel_data() is True
        assert inst.pixel_array is None

        got = inst.get_pixel_data()

        assert got.shape == (2, 8)
        assert np.array_equal(got, replacement)
    finally:
        session.close()


def test_a_float_instance_whose_pixels_become_integers_exports_as_integers(tmp_path):
    """The severe case: what reaches the file.

    `use_compression=False` deliberately. At the frozen default the same
    graph is now *refused* -- a 32-bit integer frame is outside
    `_J2K_ENCODABLE_FRAMES` -- which is the next test.
    """
    src = tmp_path / "src"
    src.mkdir()
    floats = (np.arange(16, dtype=np.float32) + 0.5).reshape(4, 4)
    want = floats.view(np.int32)
    db = str(tmp_path / "s.db")
    out = str(tmp_path / "out")
    _write_float_src(str(src), floats)

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        inst = _only_instance(session)
        session.save(sync=True)
        before_size = os.path.getsize(_sidecar(db))

        inst.set_pixel_data(want)
        session.save(sync=True)
        assert os.path.getsize(_sidecar(db)) == before_size, (
            "the dedup arm was not entered")

        session.export(out, format="dicom", show_progress=False,
                       use_compression=False)
    finally:
        session.close()

    files = glob.glob(os.path.join(out, "**", "*.dcm"), recursive=True)
    assert len(files) == 1
    written = pydicom.dcmread(files[0])

    assert "FloatPixelData" not in written
    assert "PixelData" in written
    assert written.PixelRepresentation == 1
    assert written.BitsAllocated == 32
    # The container being right with the wrong numbers in it is the same
    # defect one layer down, so the values are asserted too.
    assert np.array_equal(written.pixel_array.reshape(want.shape), want)


def test_the_same_export_compressed_writes_integers_instead_of_a_float_file(
        tmp_path, caplog):
    """`use_compression=True` writes the int32 frame, uncompressed.

    It once "succeeded", writing a `FloatPixelData` file with float
    values beside one `EXPORT` audit row reading `wrote 1 of 1`. With the
    frame correctly seen as 32-bit integer, `_J2K_ENCODABLE_FRAMES`
    refused it, and the export raised; since #771 such a frame is written
    uncompressed with an INFO note naming its dtype -- the same as any
    other 32-bit integer frame.
    """
    src = tmp_path / "src"
    src.mkdir()
    floats = (np.arange(16, dtype=np.float32) + 0.5).reshape(4, 4)
    want = floats.view(np.int32)
    db = str(tmp_path / "s.db")
    out = str(tmp_path / "out")
    _write_float_src(str(src), floats)

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        inst = _only_instance(session)
        session.save(sync=True)
        before_size = os.path.getsize(_sidecar(db))

        inst.set_pixel_data(want)
        session.save(sync=True)
        assert os.path.getsize(_sidecar(db)) == before_size, (
            "the dedup arm was not entered")

        with caplog.at_level(logging.INFO, logger="isocenter"):
            summary = session.export(out, format="dicom", show_progress=False)
    finally:
        session.close()

    assert summary.failures == []
    (written,) = glob.glob(os.path.join(out, "**", "*.dcm"), recursive=True)
    ds = pydicom.dcmread(written)
    assert "FloatPixelData" not in ds
    assert ds.pixel_array.dtype == np.dtype("int32")
    assert np.array_equal(ds.pixel_array.reshape(want.shape), want)
    notes = [r.getMessage() for r in caplog.records
             if "written uncompressed" in r.getMessage()]
    assert len(notes) == 1, [r.getMessage() for r in caplog.records]
    # `\b` on purpose, and this one assertion is the whole kill for a
    # `pixel_dtype`-only fix: `"int32" in "uint32"` is True, so a bare
    # substring check passes on that mutant and it survives.
    #
    # What moves to `uint32` is the `{dtype}` token of the #771 note
    # (`_J2K_FALLBACK_NOTE`) -- `arr` is the frame the loader rebuilt, and
    # its dtype comes from `_integer_dtype(self.bits,
    # self.pixel_representation)`, the loader's own stale snapshot. The
    # loader's state itself is pinned independently, and directly, by
    # tests 2 and 3.
    assert re.search(r"\bint32\b", notes[0]), notes[0]

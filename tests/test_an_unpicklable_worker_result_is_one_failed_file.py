"""One file whose parsed result cannot be pickled never costs the pass (#651).

pydicom 3.0.2 holds a LUT Descriptor -- Red, Green or Blue Palette Color
(0028,1101-1103) or LUT Descriptor (0028,3002) -- as a `MultiValue` whose
item constructor is a closure local to `DataElement._convert_value`, and
only when the element's VR had to be looked up, which is to say under
Implicit VR. `populate_attrs` stored that value as it was, so the
`Instance` a worker returned could not be pickled. The pickling happens in
`ProcessPoolExecutor`'s result hand-back, outside `ingest_worker`'s `try`,
and `executor.map` re-raises it in the parent and cancels everything
queued behind it: `ingest()` raised (`AttributeError` on 3.12,
`PicklingError` on 3.14t -- so nothing here asserts the type), every file
after the offending one in path order was lost, the ones before it were
linked and never saved, and no row was written.

Implicit VR is the syntax `export(use_compression=False)` writes, so an
exported palette or LUT file could not be ingested again.

Two changes, and the tests are split the same way:

- **the root fix** -- a `MultiValue` that cannot be pickled is kept as a
  plain `list`, which is what a reopened store holds anyway -- is pinned by
  the ingest tests, which build their fixtures by hand rather than using
  pydicom-data's `gdcm-US-ALOKA-16.dcm`: that file is downloaded on first
  use and CI has no cache;
- **the guarantee** -- a worker result that still cannot cross the process
  boundary is returned as one failed file -- is pinned in-process with a
  planted unpicklable value, and once across a real spawned process pool
  with the root fix switched off in the child, so the guarantee is what is
  measured there rather than the fix.
"""
import glob
import os
import pickle
import shutil
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                         generate_uid)

import isocenter.io_handlers as io_handlers
from isocenter.entities import Instance
from isocenter.io_handlers import DicomImporter, ingest_worker
from isocenter.session import DicomSession
from isocenter.store import DicomStore

#: Where each variant carries its descriptor, and the value it carries.
VARIANTS = {
    "top-level": ("0028,1101", [0, 0, 16]),
    "in-sequence": ("0028,3002", [256, 0, 16]),
}


def _write_lut_file(path, where, syntax=ImplicitVRLittleEndian):
    """CT_small carrying a LUT Descriptor, saved under `syntax`.

    The in-sequence item deliberately carries no LUT Data (0028,3006):
    that element fails the export on its own (#653), which would make a
    test here fail for a reason #651 does not touch.
    """
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    if where == "top-level":
        ds.add_new(0x00281101, "US", [0, 0, 16])
    else:
        item = Dataset()
        item.add_new(0x00283002, "US", [256, 0, 16])
        ds.ModalityLUTSequence = Sequence([item])
    ds.file_meta.TransferSyntaxUID = syntax
    ds.save_as(path, enforce_file_format=True)


def _write_second_ct(path):
    """CT_small under a fresh SOP Instance UID, so #431 does not decline it."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    uid = generate_uid()
    ds.SOPInstanceUID = uid
    ds.file_meta.MediaStorageSOPInstanceUID = uid
    ds.save_as(path, enforce_file_format=True)


def _folder(tmp_path, where):
    """`a_lut.dcm` first in path order, then two good neighbours."""
    src = tmp_path / "src"
    src.mkdir()
    _write_lut_file(str(src / "a_lut.dcm"), where)
    shutil.copy(get_testdata_file("MR_small.dcm"), src / "b_mr.dcm")
    _write_second_ct(str(src / "c_ct2.dcm"))
    return src


def _instances(session_or_store):
    store = getattr(session_or_store, "store", session_or_store)
    return [i for p in store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _lut_instance(session_or_store):
    uid = pydicom.dcmread(get_testdata_file("CT_small.dcm")).SOPInstanceUID
    matches = [i for i in _instances(session_or_store)
               if i.sop_instance_uid == uid]
    assert len(matches) == 1, "the LUT file did not reach the graph"
    return matches[0]


def _descriptor(inst, where):
    tag, _ = VARIANTS[where]
    if where == "top-level":
        return inst.attributes.get(tag)
    seq = inst.sequences.get("0028,3000")
    assert seq is not None and len(seq.items) == 1, (
        "the Modality LUT Sequence did not reach the graph")
    return seq.items[0].attributes.get(tag)


@pytest.mark.parametrize("where", sorted(VARIANTS))
def test_an_implicit_vr_lut_descriptor_ingests_beside_its_neighbours(
        tmp_path, where):
    """The root fix: the file ingests, its value is a list, and it pickles.

    `a_lut.dcm` sorts first, so on main nothing at all reached the graph.
    The value is compared to a `list` on purpose: `[0, 0, 16] ==
    MultiValue(...)` is True, so equality alone would not tell the fixed
    value from the one that cannot be pickled -- `pickle.dumps(inst)` is
    what does. The reopen half pins that the fresh value and the stored
    one agree.
    """
    _, expected = VARIANTS[where]
    src = _folder(tmp_path, where)
    db = str(tmp_path / "lut.db")

    session = DicomSession(db)
    try:
        summary = session.ingest(str(src))
        assert summary.failures == []
        assert summary.ingested == 3
        inst = _lut_instance(session)
        value = _descriptor(inst, where)
        assert type(value) is list and value == expected
        pickle.dumps(inst)
        session.save(sync=True)
    finally:
        session.close()

    reopened = DicomSession(db)
    try:
        value = _descriptor(_lut_instance(reopened), where)
        assert list(value) == expected
    finally:
        reopened.close()


def test_our_own_uncompressed_export_of_it_can_be_ingested_again(tmp_path):
    """The round trip: Explicit VR in, Implicit VR out, and back in again.

    The source ingests on main -- Explicit VR hands pydicom the VR, so no
    closure -- and the export writes Implicit VR Little Endian. It was the
    re-ingest of that output that raised.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_lut_file(str(src / "ct_lut.dcm"), "top-level",
                    syntax=ExplicitVRLittleEndian)
    out = tmp_path / "out"

    first = DicomSession(str(tmp_path / "first.db"))
    try:
        assert first.ingest(str(src)).ingested == 1
        summary = first.export(str(out), show_progress=False,
                               use_compression=False, verify_readback=True)
        assert summary.failures == []
    finally:
        first.close()

    written, = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    w = pydicom.dcmread(written)
    assert w.file_meta.TransferSyntaxUID == ImplicitVRLittleEndian
    assert list(w.RedPaletteColorLookupTableDescriptor) == [0, 0, 16]

    again = tmp_path / "again"
    again.mkdir()
    shutil.copy(written, again / "a_reexport.dcm")
    shutil.copy(get_testdata_file("MR_small.dcm"), again / "b_mr.dcm")
    second = DicomSession(str(tmp_path / "second.db"))
    try:
        summary = second.ingest(str(again))
        assert summary.failures == []
        assert summary.ingested == 2
    finally:
        second.close()


class _Local:  # pragma: no cover - only ever pickled, and that fails
    """Stands in for any value a future pydicom hands back unpicklable."""

    def __reduce__(self):
        raise pickle.PicklingError("planted: this value cannot be pickled")


@pytest.mark.parametrize("slot", ["attribute", "meta"])
def test_a_result_that_cannot_be_pickled_is_returned_as_a_failure(
        tmp_path, monkeypatch, slot):
    """The guarantee, in-process: the worker returns one failure, not a raise.

    Planted in each of the two slots the worker pickles, because both
    carry live objects: an attribute on the instance, and a value in
    `meta` (which carries the nested pixel candidates, among others). A
    check that pickled the instance alone passes `attribute` and fails
    `meta`.

    The failure result must itself pickle -- it is what crosses instead.
    """
    fp = str(tmp_path / "ct.dcm")
    shutil.copy(get_testdata_file("CT_small.dcm"), fp)

    if slot == "attribute":
        real = io_handlers.populate_attrs

        def planted(ds, item, *args, **kwargs):
            real(ds, item, *args, **kwargs)
            if isinstance(item, Instance):
                item.attributes["0009,1010"] = _Local()

        monkeypatch.setattr(io_handlers, "populate_attrs", planted)
    else:
        monkeypatch.setattr(io_handlers, "_decode_nested_pixels",
                            lambda *args, **kwargs: [_Local()])

    result = ingest_worker(fp)

    assert result[1] is None, (
        "the worker returned the instance, so the pool's hand-back raises "
        "and the whole pass is lost")
    assert result[0] == {"path": fp}
    assert result[7].startswith(io_handlers._UNCROSSABLE_RESULT)
    pickle.dumps(result)


def _disable_process_safe():
    """Pool initializer: switch the root fix off inside the child.

    Module scope so a spawned child imports it by name. With the fix off,
    the LUT file's result really is unpicklable in the child, so what the
    parent sees is the guarantee and nothing else.
    """
    io_handlers._process_safe = lambda value: value


def test_one_uncrossable_result_does_not_cost_the_pass_on_processes(
        tmp_path):
    """The guarantee across a real process boundary, with the fix isolated.

    A spawned pool, because that is the boundary the result could not
    cross. On main `import_files` raised out of the result iterator and
    both good files behind the LUT file were lost.
    """
    src = _folder(tmp_path, "top-level")
    lut_path = os.path.join(str(src), "a_lut.dcm")
    executor = ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn"),
        initializer=_disable_process_safe)
    try:
        summary = DicomImporter.import_files([str(src)], DicomStore(),
                                             executor=executor)
    finally:
        executor.shutdown(wait=True)

    assert summary.ingested == 2
    assert len(summary.failures) == 1
    path, reason = summary.failures[0]
    assert path == lut_path
    assert reason.startswith(io_handlers._UNCROSSABLE_RESULT)


def test_on_the_threads_path_it_ingests_pickles_and_exports(
        tmp_path, monkeypatch):
    """Where the symptom was not: threads never pickle the result.

    `import_files(executor=None)` under `ISOCENTER_FORCE_THREADS` runs the
    worker on a thread pool (and is the default on a free-threaded build),
    so on main the file ingested with an unpicklable instance in the
    graph -- one that any later process hop would fail on. Here the
    instance pickles, and a full `session.export()` of that store writes
    the file.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    src = tmp_path / "src"
    src.mkdir()
    _write_lut_file(str(src / "a_lut.dcm"), "top-level")
    out = tmp_path / "out"

    session = DicomSession(str(tmp_path / "threads.db"))
    try:
        summary = DicomImporter.import_files(
            [str(src)], session.store, executor=None,
            sidecar_manager=session.store_backend.sidecar,
            store_backend=session.store_backend)
        assert summary.failures == []
        assert summary.ingested == 1
        inst = _lut_instance(session)
        pickle.dumps(inst)
        exported = session.export(str(out), show_progress=False,
                                  use_compression=False,
                                  verify_readback=True)
        assert exported.failures == []
        assert exported.written_uids == [inst.sop_instance_uid]
    finally:
        session.close()

    written, = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert list(pydicom.dcmread(written).RedPaletteColorLookupTableDescriptor
                ) == [0, 0, 16]

"""A background save must never rewire a redacted instance to stale pixels.

`Session.save()` without `sync=True` returns immediately and leaves
`SqliteStore.save_all` running on the persistence manager's thread,
against the same live instances the caller keeps using. A redaction pass
started in that window is a second writer to each instance's pixel
state: both the save (`_persist_pixels`) and the redaction swap
(`persist_pixel_data`) read the resident array, write a sidecar frame,
and rebind `_pixel_loader`/`_pixel_hash` -- and last write wins. When the
save reads the bytes *before* the redaction zeroes them and rebinds
*after* the redaction's rebind, the instance thereafter reads back its
pre-redaction pixels while carrying a full redaction attestation: a new
SOP UID, `DERIVED` flags, and an `_ISOCENTER_REDACTION_HASH` that makes
the next `redact()` skip it. An `export()` then ships the burned-in
identifier under an attestation saying it is gone (#274).

These tests force that interleaving deterministically by parking the
save thread inside its sidecar `write_frame` until the redaction pass
has completed, so on unfixed code the save's rebind always lands last.
They key on the seam -- `write_frame` reached from the persistence
manager's thread -- not on any lock's existence, so a fix that repairs
the loader but lets the stale hash through still fails the hash half.
"""
import hashlib
import pickle
import threading

import numpy as np
import pytest

from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore
from isocenter.services import RedactionService
from isocenter.session import DicomSession

SERIAL = "SERIAL_123"
ROIS = [[0, 10, 0, 10]]

#: What every instance's pixels must be after redaction, and its digest.
#: The hash assertion is the half that catches a fix repairing the
#: loader while leaving `_pixel_hash` pointing at the pristine frame.
_REDACTED = np.ones((100, 100), dtype=np.uint8) * 255
_REDACTED[0:10, 0:10] = 0
_REDACTED_HASH = hashlib.sha256(_REDACTED.tobytes()).hexdigest()


def _populate(session, count=10):
    """The same 10-instance graph as `test_redaction_memory_swap`."""
    patient = Patient("P_MEM", "Memory Test")
    study = Study("S_MEM", "20230101")
    series = Series("SE_MEM", "OT", 1,
                    Equipment("Isocenter", "MemTest", SERIAL))
    instances = []
    for i in range(count):
        inst = Instance(f"I_{i}", "1.2.840.10008.5.1.4.1.1.2", i + 1)
        inst.set_pixel_data(np.ones((100, 100), dtype=np.uint8) * 255)
        instances.append(inst)
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return instances


def _park_save_thread_in_write_frame(session, monkeypatch):
    """Gate the save thread's first sidecar write until the test says go.

    Returns (bg_in_write, main_done). The first `write_frame` reached
    from the persistence manager's thread sets `bg_in_write` and waits
    up to 2s for `main_done` before delegating; every other call --
    including redaction's own writes, from the main thread or worker
    threads -- delegates straight through.

    On unfixed code the redaction pass runs unimpeded while the save is
    parked, so the save's loader rebind always lands last: the race is
    forced, every run. With the store-level lock, the save holds it
    through the parked write, the redaction's `persist_pixel_data`
    blocks on it, `main_done` is never set inside the window, and the
    2s timeout releases everything -- so the only false outcome on a
    pathological machine is a false PASS, never a false failure.
    """
    save_thread = session.persistence_manager.thread
    sidecar = session.store_backend.sidecar
    real_write = sidecar.write_frame
    bg_in_write = threading.Event()
    main_done = threading.Event()

    def gated_write(data, compression='zlib'):
        if (threading.current_thread() is save_thread
                and not bg_in_write.is_set()):
            bg_in_write.set()
            main_done.wait(2.0)
        return real_write(data, compression)

    monkeypatch.setattr(sidecar, "write_frame", gated_write)
    return bg_in_write, main_done


def _assert_every_instance_reads_back_redacted(instances):
    for i, inst in enumerate(instances):
        data = inst.get_pixel_data()
        assert data is not None
        assert data[0, 0] == 0, (
            f"Instance {i} reads back unredacted pixels: the background "
            "save rebound its loader to the pre-redaction frame (#274)")
        assert inst._pixel_hash == _REDACTED_HASH, (
            f"Instance {i} carries the pre-redaction pixel hash: "
            "self-consistent with the stale frame, so the loader's "
            "integrity check cannot catch it (#274)")


def test_a_concurrent_background_save_cannot_rewire_the_loader_to_stale_pixels(
        tmp_path, monkeypatch):
    """The serial path: `RedactionService` racing `save_all` directly.

    This is `test_redaction_memory_swap`'s exact shape -- async save,
    then the serial redaction loop -- with the straddle forced instead
    of left to the scheduler. That test is the canary (intermittent on
    unfixed code, ~1 in 3000 natively); this one is the pin.
    """
    session = DicomSession(str(tmp_path / "race_serial.db"))
    try:
        instances = _populate(session)
        bg_in_write, main_done = _park_save_thread_in_write_frame(
            session, monkeypatch)

        session.save()  # async: save_all runs on the manager's thread
        assert bg_in_write.wait(10), "the background save never started"

        # It also read the pristine bytes already: `_persist_pixels`
        # hashes before it writes, so parking inside `write_frame`
        # parks it *between* its read and its rebind.
        service = RedactionService(session.store, session.store_backend)
        service.redact_machine_instances(SERIAL, ROIS, show_progress=False)
        main_done.set()

        # Let the parked save finish its rebind before asserting;
        # otherwise the corruption would land after the assert ran.
        session.persistence_manager.flush()

        _assert_every_instance_reads_back_redacted(instances)
    finally:
        main_done.set()
        session.close()


def test_the_session_redact_pipeline_survives_a_save_still_in_flight(
        tmp_path, monkeypatch):
    """The pipeline path: `session.save(); session.redact()` under threads.

    3.14t's default executor runs `execute_redaction_task` in-process on
    the shared objects -- the same `persist_pixel_data` writer as the
    serial path, racing the same background save. `ISOCENTER_FORCE_THREADS`
    selects that path structurally on any build.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    session = DicomSession(str(tmp_path / "race_pipeline.db"))
    try:
        instances = _populate(session)
        session.configuration.rules = [
            {"serial_number": SERIAL, "redaction_zones": ROIS}]
        bg_in_write, main_done = _park_save_thread_in_write_frame(
            session, monkeypatch)

        session.save()
        assert bg_in_write.wait(10), "the background save never started"

        session.redact(show_progress=False)
        main_done.set()

        session.persistence_manager.flush()

        _assert_every_instance_reads_back_redacted(instances)
    finally:
        main_done.set()
        session.close()


def test_redact_drains_the_pending_save_before_dispatching(
        tmp_path, monkeypatch):
    """`Session.redact()` must flush the persistence manager first.

    Belt to the lock's braces, for the pipeline path only: a redaction
    pass must not run concurrently with a save that is serializing the
    very pixels it is about to replace. Direct `RedactionService` users
    have no such gate -- the store lock is what protects them -- but the
    pipeline can simply refuse to open the window (#274).
    """
    from isocenter import session as session_module
    from isocenter.persistence_manager import PersistenceManager

    session = DicomSession(str(tmp_path / "race_flush.db"))
    try:
        _populate(session, count=1)
        session.configuration.rules = [
            {"serial_number": SERIAL, "redaction_zones": ROIS}]

        order = []
        real_flush = PersistenceManager.flush

        def recording_flush(self):
            order.append("flush")
            return real_flush(self)

        def recording_dispatch(*args, **kwargs):
            order.append("dispatch")
            return iter([])

        monkeypatch.setattr(PersistenceManager, "flush", recording_flush)
        monkeypatch.setattr(session_module, "run_parallel",
                            recording_dispatch)

        session.redact(show_progress=False)

        assert "dispatch" in order, "redaction never dispatched its tasks"
        assert "flush" in order, (
            "redact() dispatched without draining the persistence "
            "manager; a save still in flight races the redaction (#274)")
        assert order.index("flush") < order.index("dispatch"), (
            "redact() flushed only after dispatching, which leaves the "
            "whole race window open (#274)")
    finally:
        session.close()


def test_the_store_still_pickles_with_its_pixel_swap_lock(tmp_path):
    """The #218 contract, extended to the pixel-swap lock.

    `SqliteStore` is pickled whole into process-pool workers. A lock
    raises `TypeError: cannot pickle '_thread.lock' object`, so the new
    lock must be dropped in `__getstate__` and recreated in
    `__setstate__` -- and the recreated one must be a real, usable lock,
    or the worker's first pixel write dies far from the cause.
    """
    store = SqliteStore(str(tmp_path / "pickle_lock.db"))
    try:
        assert store._pixel_swap_lock is not None

        clone = pickle.loads(pickle.dumps(store))
        try:
            assert clone._pixel_swap_lock is not None
            assert clone._pixel_swap_lock is not store._pixel_swap_lock
            # Usable, not merely present.
            with clone._pixel_swap_lock:
                pass
        finally:
            clone.stop()
    finally:
        store.stop()

"""`_export_dicom` completes its save before it frees memory (#343).

The export path saves and then sweeps: `self.save()` followed by
`self.release_memory()`, under a comment saying "flush before the walk".
The save was asynchronous -- `persistence_manager.save_async()` enqueues
and returns -- so it did not flush before the walk, it flushed
*concurrently with* it. `release_memory()` frees an instance only once
the background save has attached a `_pixel_loader` to it, so which
instances were swept depended on which thread got there first:

- worker loses (idle machine): no loader yet, the unload is refused, the
  resident array reaches the export worker. Green.
- worker wins (loaded machine): loader attached, array nulled, the export
  worker reloads through the loader.

For an ingested instance the reload is correct and the race was benign,
which is why it survived: nondeterministic memory behaviour, same output.
For an instance whose `pixel_array` was assigned directly -- no
Rows/Columns written -- the reload produced an empty image and the export
failed (`tests/test_pixel_geometry_pipeline.py` pins the loader half).
Measured: 0/200 sweeps idle, 2/300 under two concurrent full suites, and
`tests/test_wfdb_writer.py`'s colocation test failing 4 runs in 15 under
the same load. CHANGELOG's #183 entry is the first sighting ("once the
save won that race"); it fixed the dtype the reload came back with and
left the ordering alone.

The fix is `save(sync=True)`: drain the manager, then `save_all` on the
exporting thread, then the sweep. `audit()` and `redact()` already drain
on entry; export was the third verb and did not.

**What is deliberately not written here.** `save(sync=True)` inherits
`flush()`'s never-return-early property, so a wedged worker now wedges
the export rather than racing it. That is the trade `save()`'s docstring
argues for, and a test that waits forever is not a test.
"""
import threading
import time
from datetime import date
from unittest.mock import patch

import numpy as np

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

#: Everything `IODValidator` demands of a CT image, so the export plan is
#: non-empty and the measurement is about the sweep rather than a refusal.
#: Same set as `tests/test_close_warns_about_unsaved_instances.py`.
CT_REQUIRED = (
    ("0008,0020", "20230101"), ("0008,0030", "120000"),
    ("0008,0060", "CT"),
    ("0018,0050", "1.0"), ("0018,0060", "120"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
)


def test_the_export_save_has_landed_before_release_memory_runs(
        tmp_path, monkeypatch):
    """By the time the sweep runs, the save is done and the queue is empty.

    The worker is slowed, not the caller: `SqliteStore.save_all` sleeps
    half a second before delegating, on whichever thread calls it. With
    the asynchronous save that thread is `PersistenceWorker` and the
    sweep runs first, so the record reads `(False, False, 1)` -- no
    loader, array still resident, one job still queued. With
    `save(sync=True)` the manager is drained and `save_all` runs on the
    exporting thread, so the sweep sees `(True, True, 0)` and the export
    worker takes the reload path, which has to produce a real image for
    `written` to be 1.

    The events list is asserted in order, and it names the thread each
    ran on, so a green here cannot be a worker that happened to win:
    the save that landed must be the export's own, on the export's
    thread, and it must have returned before the sweep began. `run_parallel`
    is patched inline the way the colocation test does it -- the point is
    the parent's ordering, not the workers.

    **The spelling is pinned on purpose, not only the order.**
    `self.save(); self.persistence_manager.flush(); self.release_memory()`
    would give the same ordering and turn this red, because the recorded
    save would then return on `PersistenceWorker`. That is deliberate:
    `save(sync=True)` already means "drain, then save on the caller's
    thread", and a second spelling of that behaviour at this one call
    site is the duplicate CLAUDE.md's "one spelling per behaviour" rule
    exists to refuse (the brief rejected it as such). If the call site
    ever legitimately changes spelling, change the thread named here
    with it, and say why in the same commit.
    """
    events = []
    real_save_all = SqliteStore.save_all

    def slow_save_all(self, *args, **kwargs):
        time.sleep(0.5)
        result = real_save_all(self, *args, **kwargs)
        events.append(("save_all returned", threading.current_thread().name))
        return result

    monkeypatch.setattr(SqliteStore, "save_all", slow_save_all)

    with DicomSession(str(tmp_path / "sweep.db")) as session:
        patient = Patient("PAT1", "Anon")
        study = Study("ST_1", date(2023, 1, 1))
        study.study_time = "120000"
        series = Series("SE_1", "CT", 1)
        inst = Instance("1.2.826.0.1.sweep", CT_STORAGE, 1)
        inst.file_path = None
        for tag, value in CT_REQUIRED:
            inst.set_attr(tag, value)
        inst.set_pixel_data(np.full((8, 8), 7, dtype=np.uint8))
        series.instances.append(inst)
        study.series.append(series)
        patient.studies.append(study)
        session.store.patients.append(patient)

        # `_release_memory`, the sweep with the export's `show_progress`
        # (#540); `release_memory()` is its parameterless public face, and
        # the export no longer goes through it.
        real_release = session._release_memory

        def observing_release(show_progress):
            events.append(("release_memory", threading.current_thread().name))
            real_release(show_progress)
            events.append((
                "after sweep",
                inst._pixel_loader is not None,
                inst.pixel_array is None,
                session.persistence_manager.queue.unfinished_tasks))

        monkeypatch.setattr(session, "_release_memory", observing_release)

        with patch("isocenter.io_handlers.run_parallel",
                   side_effect=lambda func, items, *a, **k: [func(i) for i in items]):
            summary = session.export(str(tmp_path / "out"),
                                     show_progress=False,
                                     check_burned_in=False)

    exporting = threading.current_thread().name
    assert events == [
        ("save_all returned", exporting),
        ("release_memory", exporting),
        ("after sweep", True, True, 0),
    ], (
        f"events were {events!r}; the export's save has to return, on the "
        "exporting thread, before the sweep runs -- otherwise which "
        "instances are freed depends on which thread got there first (#343)")

    assert summary.written == 1, (
        f"the export wrote {summary.written}; after the sweep the worker "
        "reloads through the loader, and that reload has to be a real image")
    assert inst.get_pixel_data().shape == (8, 8)

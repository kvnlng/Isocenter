"""What `run_parallel` promises, across its three execution paths.

It dispatches to a shared executor, a recycling `multiprocessing.Pool`,
or a fresh executor, and each path had its own copy of the progress-bar
setup and its own reading of the environment. These tests state the
behaviour those copies were supposed to share, so the copies can be
removed.
"""
import collections
import concurrent.futures
import contextlib
import functools
import logging
import multiprocessing
import multiprocessing.connection
import multiprocessing.pool
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures.process import BrokenProcessPool

import pytest

from isocenter import parallel


def identity(value):
    return value


def _threads_chosen(force_threads, maxtasksperchild, lever=None):
    """The one field these assertions are about (#384, #400).

    `_resolve_execution_choice` answers three questions where
    `_use_threads` answered one; the two attribution fields have their
    own table below (`test_the_choice_records_which_lever_asked_for_processes`).
    Reading `.use_threads` here keeps each assertion about the thing it
    was written about.
    """
    return parallel._resolve_execution_choice(
        force_threads, maxtasksperchild, lever).use_threads



def double_or_raise(value):
    """Module scope: it has to pickle into a process-pool worker."""
    if value < 0:
        raise ValueError(f"no negatives here: {value}")
    return value * 2


def double_or_die(value):
    """Module scope for the same reason. A negative kills the worker
    outright -- `os._exit` skips every handler and `finally`, which is
    the closest a test can get to the OOM-kill this path exists for."""
    if value < 0:
        os._exit(13)
    return value * 2


def _collector_enabled(_):
    """Module scope: it pickles into a worker and reports *that* process's
    collector. Imported inside because it runs there, like `_worker_init`."""
    import gc  # pylint: disable=import-outside-toplevel
    return gc.isenabled()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No test should inherit another's tuning variables."""
    for name in ("ISOCENTER_MAX_WORKERS", "ISOCENTER_CHUNKSIZE",
                 "ISOCENTER_MAX_TASKS_PER_CHILD", "ISOCENTER_DISABLE_GC",
                 "ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
                 "ISOCENTER_SHOW_PROGRESS", "ISOCENTER_WORKER_FAULTHANDLER"):
        monkeypatch.delenv(name, raising=False)


def test_a_malformed_tuning_variable_is_reported(monkeypatch, caplog):
    """A mistyped setting must not look like an applied one.

    Every environment read was wrapped in `except ValueError: pass`, so
    `ISOCENTER_MAX_WORKERS=banana` reverted to the default in silence.
    The symptom is a cohort that runs at the wrong width with nothing
    anywhere saying why.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "banana")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    with caplog.at_level(logging.WARNING):
        assert parallel.run_parallel(
            identity, [1, 2, 3], show_progress=False) == [1, 2, 3]

    assert any("ISOCENTER_MAX_WORKERS" in record.message
               for record in caplog.records), (
        "the malformed value was ignored without a word")


def test_results_come_back_in_order_on_the_standard_path(monkeypatch):
    """`map` preserves input order; callers rely on it for zip-style joins."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    assert parallel.run_parallel(
        identity, list(range(20)), show_progress=False) == list(range(20))


def test_return_generator_defers_the_work(monkeypatch):
    """Streaming mode must not have run anything before it is consumed."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    seen = []

    def record(value):
        seen.append(value)
        return value

    result = parallel.run_parallel(
        record, [1, 2, 3], show_progress=False, return_generator=True)

    assert seen == [], "the generator ran before anything asked it to"
    assert list(result) == [1, 2, 3]
    # Sorted: workers finish in whatever order they finish. The contract is
    # that every item ran and results come back in input order, not that
    # the pool scheduled them in it.
    assert sorted(seen) == [1, 2, 3]


def test_an_empty_workload_is_not_an_error(monkeypatch):
    """Zero items is an ordinary outcome of a filter, not a failure."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    assert parallel.run_parallel(identity, [], show_progress=False) == []


def test_progress_can_be_switched_off_globally(monkeypatch):
    """ISOCENTER_SHOW_PROGRESS=0 silences a caller that asked for a bar."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")

    drawn = []
    real_tqdm = parallel.tqdm
    monkeypatch.setattr(parallel, "tqdm",
                        lambda *a, **k: drawn.append(k) or real_tqdm(*a, **k))

    parallel.run_parallel(identity, [1, 2], show_progress=True)

    assert not drawn


def test_the_progress_bar_is_told_how_many_items_to_expect(monkeypatch):
    """A sized iterable needs no explicit total; a bar without one is useless."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    seen = {}
    real_tqdm = parallel.tqdm

    def capture(iterable, **kwargs):
        seen.update(kwargs)
        return real_tqdm(iterable, **kwargs)

    monkeypatch.setattr(parallel, "tqdm", capture)

    parallel.run_parallel(identity, [1, 2, 3, 4], show_progress=True)

    assert seen.get("total") == 4


def test_an_explicit_total_reaches_the_progress_bar(monkeypatch):
    """The `total=` argument is for generators, which have no `__len__` (#365).

    The test above covers the sized-iterable arm of `_progress_total`;
    the explicit-`total` arm was covered by nothing, so `return
    strategy.total` could become `return None` and every bar over a
    generator would lose its denominator. Killing mutation: that
    `return` -> `return None`.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    seen = {}
    real_tqdm = parallel.tqdm

    def capture(iterable, **kwargs):
        seen.update(kwargs)
        return real_tqdm(iterable, **kwargs)

    monkeypatch.setattr(parallel, "tqdm", capture)

    parallel.run_parallel(identity, (i for i in range(4)), total=4,
                          show_progress=True)

    assert seen.get("total") == 4


def test_the_default_path_is_processes_under_a_gil_and_threads_without_one(
        monkeypatch):
    """What `_use_threads` answers when no lever is set, on both builds (#365).

    The gate -- local before a push, `test-floor` at release -- runs 3.14t
    precisely because `run_parallel()` takes the threads path there, and nothing had ever said so in a test: the last
    line of `_use_threads` could drop its `not`, turn its `and` into
    `or`, or become `return None`, and the suite stayed green on
    whichever build happened to be running. Pinned by patching
    `sys._is_gil_enabled` both ways and deleting it (the builds that have
    always had a GIL), so each arm is exercised on any interpreter.

    **Identity assertions, not truthiness.** `None` is falsy, so `assert
    not result` lets the `return None` mutant through on a GIL build
    while it silently turns the free-threaded default into processes.
    The three levers are cleared by the autouse fixture.
    """
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
    assert _threads_chosen(False, None) is False, (
        "with a GIL and no lever, the default must be processes")

    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: False, raising=False)
    assert _threads_chosen(False, None) is True, (
        "without a GIL and no lever, the default must be threads")

    monkeypatch.delattr(sys, "_is_gil_enabled", raising=False)
    assert _threads_chosen(False, None) is False, (
        "a build that cannot be asked has always had a GIL: processes")


def test_the_per_call_process_pool_pins_spawn(monkeypatch):
    """Every process pool here starts workers by spawn, never by fork.

    `_run_on_recycling_pool` has pinned spawn since it existed, with the
    reason in place: a forked worker inherits the parent's open SQLite
    handles and its sidecar file position. `_run_on_new_executor` -- the
    pool the redaction path actually uses, and the one that pickles the
    store (#220) -- took the platform default, which is fork on Linux
    3.12: exactly the population where CI intermittently stalled 900
    seconds in a forked worker's `persist_pixel_data` and died with
    `sqlite3.OperationalError: database is locked`, while spawn
    platforms never once reproduced it (#250). macOS defaults to spawn,
    which is why this divergence was invisible to every local run --
    and why this test asserts the constructor argument rather than the
    platform-dependent effect.
    """
    import concurrent.futures

    captured = {}
    real = concurrent.futures.ProcessPoolExecutor

    class Recording(real):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Recording)
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

    result = list(parallel.run_parallel(
        identity, [1, 2], show_progress=False, max_workers=2))

    assert sorted(result) == [1, 2]
    ctx = captured.get("mp_context")
    assert ctx is not None and ctx.get_start_method() == "spawn", (
        f"the per-call process pool was built with mp_context={ctx!r}; "
        "without an explicit spawn context it forks on Linux 3.12 and "
        "the worker inherits the parent's open SQLite handles (#220, "
        "#250)")


def test_the_shared_session_executor_pins_spawn(tmp_path, monkeypatch):
    """`Session._executor` makes the same promise, including after restart.

    Asserted on the constructor argument, not on the executor's
    resulting context: macOS resolves the default to spawn anyway, so an
    effect-shaped assertion is green there with or without the pin --
    vacuous on every machine this suite runs on locally, and red only
    on the Linux runner it exists to protect.
    """
    import concurrent.futures

    from isocenter.session import DicomSession

    calls = []
    real = concurrent.futures.ProcessPoolExecutor

    class Recording(real):
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Recording)

    with DicomSession(str(tmp_path / "spawn_pin.db")) as session:
        session._restart_executor(max_workers=1)

    assert len(calls) >= 2, "expected the init pool and the restart pool"
    for kwargs in calls:
        ctx = kwargs.get("mp_context")
        assert ctx is not None and ctx.get_start_method() == "spawn", (
            f"a Session pool was built with mp_context={ctx!r}; without "
            "an explicit spawn context it forks on Linux 3.12 and the "
            "worker inherits the parent's open SQLite handles (#220, "
            "#250)")


def _record_session_pools(monkeypatch):
    """Records the keyword arguments of every `ProcessPoolExecutor(...)` built."""
    import concurrent.futures

    calls = []
    real = concurrent.futures.ProcessPoolExecutor

    class Recording(real):
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Recording)
    return calls


def test_the_shared_session_executor_is_sized_by_isocenter_max_workers(
        tmp_path, monkeypatch):
    """`ISOCENTER_MAX_WORKERS` sizes the pool `ingest()` runs on (#501).

    `Session.__init__` built `ProcessPoolExecutor(max_workers=None)`, so
    the stdlib chose the width, one per CPU. The variable every
    worker-count doc points at was read by `run_parallel` and by
    `redact()`, and never by the pool that `ingest()` is handed. Measured
    on 49e135a with 14 CPUs: 14 workers with the variable at `2`, on
    3.12.14 and on 3.14.7t alike. The shared pool is processes on both
    builds; only `run_parallel`'s own pool takes threads on 3.14t.

    Asserted on the constructor argument, as the spawn test above is.
    Reading `_max_workers` afterwards would see the stdlib's default
    under the mutant, and the test would only be right on a machine
    whose CPU count happened to match. Killing mutation: the
    constructor's `max_workers` reverted to `None`.
    """
    from isocenter.session import DicomSession

    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    calls = _record_session_pools(monkeypatch)

    with DicomSession(str(tmp_path / "s.db")):
        pass

    assert calls, "Session() built no ProcessPoolExecutor at all"
    assert calls[0].get("max_workers") == 2, (
        f"the session's shared pool was built with {calls[0]!r} under "
        "ISOCENTER_MAX_WORKERS=2. ingest() runs on that pool, so the "
        "documented way to narrow a run did not narrow ingest")


def test_the_shared_session_executor_defaults_to_one_worker_per_cpu(
        tmp_path, monkeypatch):
    """Unset, the shared pool gets `run_parallel`'s default (#501).

    The CPU count is pinned to 3, which is neither this box's count nor
    a runner's, so the number cannot be right by coincidence. Killing
    mutations: the kwarg reverted to `None` (it is then `None`, not 3);
    the default computed as the `CPU * 1.5` the stale comment at the
    constructor claimed (4, not 3).
    """
    from isocenter.session import DicomSession

    monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 3)
    calls = _record_session_pools(monkeypatch)

    with DicomSession(str(tmp_path / "s.db")):
        pass

    assert calls and calls[0].get("max_workers") == 3, (
        f"with ISOCENTER_MAX_WORKERS unset and 3 CPUs the shared pool was "
        f"built with {calls[0] if calls else None!r}; run_parallel's "
        "default is one worker per CPU")


def test_a_restarted_shared_executor_is_sized_the_same_way(
        tmp_path, monkeypatch):
    """`_restart_executor()` resolves the width construction does; an argument wins.

    The OOM-recovery path rebuilds with `max_workers=None` by default, so
    even after construction honoured the variable, a restart would have
    undone it. An explicit argument, the "fewer workers" its docstring
    promises, still wins. Killing mutations: `_restart_executor`'s
    default passed through as `None`; the explicit argument ignored in
    favour of the resolver.
    """
    from isocenter.session import DicomSession

    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    calls = _record_session_pools(monkeypatch)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session._restart_executor()
        session._restart_executor(max_workers=1)

    assert [kwargs.get("max_workers") for kwargs in calls] == [2, 2, 1], (
        f"pool widths across construction, a bare restart and a "
        f"restart(max_workers=1): {[k.get('max_workers') for k in calls]}")


def test_a_zero_worker_count_is_reported_when_the_session_opens(
        tmp_path, monkeypatch, caplog):
    """`0` warns at `Session()`, and the shared pool gets the default (#501).

    Now that construction reads the variable, it has to read it through
    the same floor `_resolve_strategy` does (#335, #341). Otherwise `0`
    reaches `ProcessPoolExecutor`, which raises `ValueError: max_workers
    must be greater than 0` without naming any variable, and the session
    does not open. Killing mutation: the helper reading the variable
    with `minimum=None`.
    """
    from isocenter.session import DicomSession

    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "0")
    monkeypatch.setattr(os, "cpu_count", lambda: 3)
    calls = _record_session_pools(monkeypatch)

    with caplog.at_level(logging.WARNING):
        with DicomSession(str(tmp_path / "s.db")):
            pass

    assert calls and calls[0].get("max_workers") == 3, (
        f"a rejected worker count must fall back to the default, got "
        f"{calls[0] if calls else None!r}")
    assert any("ISOCENTER_MAX_WORKERS" in record.getMessage()
               and "0" in record.getMessage()
               for record in caplog.records), (
        "0 was discarded at Session() without a warning naming the "
        "variable and the value")


def _write_ct(folder, name):
    """A minimal ingestable CT file, with its own study and series UIDs.

    Fresh UIDs per call, so two of these are never declined as duplicates
    (#431).
    """
    # pylint: disable=import-outside-toplevel
    import numpy as np
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    ct_storage = "1.2.840.10008.5.1.4.1.1.2"
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = ct_storage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "P_POOL", "DOE^POOL"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = ct_storage
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate = "20230101"
    ds.Manufacturer, ds.ManufacturerModelName = "ACME", "SCAN"
    ds.DeviceSerialNumber = "SN_POOL"
    ds.Rows = ds.Columns = 32
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.random.default_rng(len(name)).integers(
        0, 256, (32, 32), dtype=np.uint8).tobytes()
    path = os.path.join(folder, name)
    ds.save_as(path, enforce_file_format=True)
    return path


def _folder(tmp_path, tag, files=0):
    folder = tmp_path / tag
    folder.mkdir()
    for i in range(files):
        _write_ct(str(folder), f"{tag}_{i}.dcm")
    return str(folder)


def _instance_count(session):
    """Instances in the session's graph, the count an ingest is measured by."""
    return sum(len(series.instances)
               for patient in session.store.patients
               for study in patient.studies
               for series in study.series)


def _wait_until(predicate, what, timeout=60.0):
    """Bounded wait on a condition, never a sleep on a guess."""
    import time  # pylint: disable=import-outside-toplevel

    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def test_each_ingest_re_resolves_the_shared_pool_width(tmp_path, monkeypatch):
    """`ingest()` reads `ISOCENTER_MAX_WORKERS` again and rebuilds on a change (#511).

    #501 made `Session()` size the shared pool from the variable, and
    stopped there: a value set after the session opened reached the next
    internal restart of that pool and nothing else, so an operator who
    narrowed the lever in a notebook narrowed `audit()` and `redact()`
    and not the next `ingest()`. Every other reader is per call --
    `run_parallel` on each call, `redact()` in `_redaction_worker_count`
    on each pass -- and #504 made the argument for closing this one: a
    construction-time read is ignored in silence when the variable is set
    later.

    The CPU count is pinned to 3 so the construction width is neither
    this box's nor a runner's and cannot be right by coincidence.
    Killing mutation: `ingest()` dispatching to `self._executor` again
    instead of re-resolving, which builds one pool, not two.
    """
    from isocenter.session import DicomSession

    monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 3)
    calls = _record_session_pools(monkeypatch)

    with DicomSession(str(tmp_path / "s.db")) as session:
        monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
        session.ingest(_folder(tmp_path, "src", files=1))

    assert [kwargs.get("max_workers") for kwargs in calls] == [3, 2], (
        f"pool widths across Session() with 3 CPUs and an ingest() under "
        f"ISOCENTER_MAX_WORKERS=2: "
        f"{[k.get('max_workers') for k in calls]}. The variable was set "
        f"after the session opened, so an ingest that never re-reads it "
        f"runs at the construction width")


def test_an_ingest_at_an_unchanged_width_keeps_the_pool_it_has(
        tmp_path, monkeypatch):
    """Re-resolving is not rebuilding: an unchanged width costs nothing (#511).

    The rebuild is gated on the width having actually moved, so the
    ordinary session -- which never touches the variable -- keeps one
    pool for its lifetime and pays no teardown or respawn per ingest.
    Killing mutation: the width comparison dropped, so every `ingest()`
    rebuilds.
    """
    from isocenter.session import DicomSession

    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    calls = _record_session_pools(monkeypatch)

    with DicomSession(str(tmp_path / "s.db")) as session:
        before = session._executor
        session.ingest(_folder(tmp_path, "one", files=1))
        session.ingest(_folder(tmp_path, "two", files=1))
        assert session._executor is before, (
            "the shared pool was replaced by an ingest() that asked for "
            "the width it already had")

    assert [kwargs.get("max_workers") for kwargs in calls] == [2], (
        f"two ingests at an unchanged width built "
        f"{len(calls)} pools: {[k.get('max_workers') for k in calls]}")


def test_a_restarted_pool_records_the_width_it_was_built_with(
        tmp_path, monkeypatch):
    """The recorded width follows an OOM restart, not just construction (#511).

    `_restart_executor(max_workers=1)` is the memory-recovery path. If
    the recorded width stayed at the construction number, the next
    `ingest()` would resolve that same number, find it unchanged, and run
    on the 1-worker recovery pool believing it had the full width -- the
    silent direction. Killing mutation: `_restart_executor` not moving
    `_executor_width`, which makes the third width absent.
    """
    from isocenter.session import DicomSession

    monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 3)
    calls = _record_session_pools(monkeypatch)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session._restart_executor(max_workers=1)
        session.ingest(_folder(tmp_path, "src", files=1))

    assert [kwargs.get("max_workers") for kwargs in calls] == [3, 1, 3], (
        f"pool widths across Session(), a restart at 1 and an ingest() "
        f"with the variable unset and 3 CPUs: "
        f"{[k.get('max_workers') for k in calls]}. The ingest must see "
        f"the restart's width as the current one and rebuild back to the "
        f"resolved 3")


def _slow_identity(value):
    """Module scope: it pickles into a worker.

    Slow enough that the tasks queued behind it are still queued when the
    caller retires the pool.
    """
    import time  # pylint: disable=import-outside-toplevel
    time.sleep(0.3)
    return value


def test_retiring_the_shared_pool_cancels_nothing_that_is_queued(
        tmp_path, monkeypatch):
    """`_retire_shared_executor()` is the opposite of the OOM teardown (#511).

    The resize path retires a pool no `ingest()` is running on, so
    `shutdown(wait=True)` with no `cancel_futures` is both correct and
    prompt. `_restart_executor()`'s `shutdown(wait=False,
    cancel_futures=True)` is the *broken*-pool contract -- that pool is
    assumed unusable, so what is queued on it is lost either way -- and
    the two are one line apart in spelling. This is what goes red if the
    healthy path is ever "modernised" into the recovery path's teardown:
    with one worker and four slow tasks, three are still queued when the
    retire lands, and cancelling them raises `CancelledError` at
    `result()`.

    Killing mutation: `_retire_shared_executor`'s shutdown given
    `cancel_futures=True`.

    It is called with the pool, not with nothing: the resize path swaps
    the replacement in first and retires the pool it swapped out
    afterwards, outside `_ingest_lock`, so `self._executor` is already
    the new one by then.
    """
    from isocenter.session import DicomSession

    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "1")

    with DicomSession(str(tmp_path / "s.db")) as session:
        pool = session._executor
        futures = [pool.submit(_slow_identity, i) for i in range(4)]
        session._retire_shared_executor(pool)
        assert [f.result(timeout=60) for f in futures] == [0, 1, 2, 3], (
            "retiring the shared pool dropped work that was queued on it; "
            "only the broken-pool restart may cancel futures")


def test_a_pool_rebuild_that_fails_leaves_the_session_usable(
        tmp_path, monkeypatch, caplog):
    """A failed resize costs one `ingest()`, not the session (#511).

    `ProcessPoolExecutor(...)` raises `OSError` when the box has no file
    descriptors or memory left for the queue pipes -- EMFILE, ENOMEM --
    which is the failure mode of exactly the memory-short machine an
    operator narrows `ISOCENTER_MAX_WORKERS` for. The rebuild is
    therefore ordered so that a raise mutates nothing: the replacement
    is constructed **before** the old pool is retired, and the in-flight
    counter is incremented **after** the swap. The ruling is that the
    session keeps the live pool it had and the exception reaches the
    caller.

    Get either order wrong and the session is bricked permanently, which
    is what the three post-conditions here measure, two of them by using
    the session afterwards rather than by reading its attributes:

    - Retired first: `self._executor` points at a pool already shut
      down, so the **next `ingest()` at an unchanged width** -- which
      rebuilds nothing and dispatches to what is there -- raises
      `cannot schedule new futures after shutdown` or silently indexes
      nothing. It is permanent, because the width left at the old
      number means no later call finds a rebuild to do.
    - Incremented first: `_ingest_executor` is a `@contextmanager`, so
      an exception before the `yield` escapes `__enter__` and the
      `finally` that decrements never runs. The counter left at 1 is a
      phantom peer, so the **next lone `ingest()`** takes the peer
      branch: it warns and runs at the old width instead of rebuilding.

    The environment is healthy again for both probes -- the real
    constructor is restored -- so nothing here is about the failure
    persisting. It is about the session's own state after it.

    Killing mutations: the retire moved back above the construction; the
    increment moved back above the rebuild.
    """
    import concurrent.futures  # pylint: disable=import-outside-toplevel
    import errno  # pylint: disable=import-outside-toplevel

    from isocenter.session import DicomSession

    monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 3)

    attempts, built, fail_next = [], [], []
    real = concurrent.futures.ProcessPoolExecutor

    class Arming(real):
        """Records every construction; raises EMFILE for an armed one."""

        def __init__(self, *args, **kwargs):
            attempts.append(kwargs.get("max_workers"))
            if fail_next:
                fail_next.pop()
                raise OSError(errno.EMFILE, "Too many open files")
            super().__init__(*args, **kwargs)
            built.append(kwargs.get("max_workers"))

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Arming)

    with DicomSession(str(tmp_path / "s.db")) as session:
        assert built == [3], f"the session's own pool was not built: {built}"
        before = session._executor

        monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
        fail_next.append(True)
        with pytest.raises(OSError) as failure:
            session.ingest(_folder(tmp_path, "doomed", files=1))
        assert failure.value.errno == errno.EMFILE

        assert attempts == [3, 2], (
            f"the failed rebuild was not attempted at the requested "
            f"width: {attempts}")
        assert built == [3], f"a second pool was built after all: {built}"
        assert session._ingests_in_flight == 0, (
            f"the failed rebuild leaked the in-flight counter at "
            f"{session._ingests_in_flight}; every later ingest() sees a "
            f"peer that is not there and never resizes again")
        assert session._executor_width == 3, (
            f"the recorded width moved to {session._executor_width} for a "
            f"pool that was never built")
        assert session._executor is before, (
            "the failed rebuild left the session pointing at something "
            "other than the pool it still has")

        # Probe 1 -- the width is unchanged, so nothing is rebuilt and
        # this dispatches to the pool the session kept. Red if that pool
        # was retired before the replacement failed.
        monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "3")
        session.ingest(_folder(tmp_path, "healthy", files=1))
        assert built == [3], (
            f"an ingest() at an unchanged width rebuilt the pool: {built}")
        assert _instance_count(session) == 1, (
            f"the session's kept pool no longer runs an ingest(): "
            f"{_instance_count(session)} instance(s) indexed")

        # Probe 2 -- a real resize, alone. Red if the counter leaked,
        # because the peer branch warns and skips instead.
        monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            session.ingest(_folder(tmp_path, "resized", files=1))
        assert built == [3, 2], (
            f"the ingest() after a failed rebuild did not resize: {built}")
        assert _instance_count(session) == 2, (
            f"the rebuilt pool did not run its ingest(): "
            f"{_instance_count(session)} instance(s) indexed")
        skipped = [record.getMessage() for record in caplog.records
                   if record.levelno >= logging.WARNING
                   and "ISOCENTER_MAX_WORKERS" in record.getMessage()]
        assert not skipped, (
            f"a lone ingest() reported a peer that is not there: {skipped}")


def test_a_retirement_that_raises_does_not_leak_the_in_flight_counter(
        tmp_path, monkeypatch, caplog):
    """Nothing between the increment and the decrement is outside the `try` (#511).

    The sibling of the failed-rebuild case, and the same defect class:
    `_ingest_executor` is a `@contextmanager`, so anything that raises
    before the `yield` escapes `__enter__`, and a counter left at 1 is a
    phantom peer that makes every later `ingest()` skip the resize for a
    call that is not there. The rebuild is ordered so it cannot leak;
    what is left outside the lock after the increment is the log line and
    the retirement of the pool that was swapped out, and the retirement
    is the one that can block -- it waits out a worker wedged in a C
    library -- so `KeyboardInterrupt` there is the realistic raise.
    `_retire_shared_executor` catches `RuntimeError` and `OSError` and
    not that.

    The `try` therefore opens the instant the lock is released rather
    than just before the `yield`. A retirement that raises still leaves
    the swap in place, so the session keeps a live pool at the recorded
    width and only this `ingest()` is lost -- which is what the second
    half measures, by resizing again to a third width and requiring the
    rebuild rather than the peer branch's warning.

    Killing mutation: the `try` moved back below the retire.
    """
    from isocenter.session import DicomSession

    monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 3)
    calls = _record_session_pools(monkeypatch)

    with DicomSession(str(tmp_path / "s.db")) as session:
        real_retire = session._retire_shared_executor
        interrupt = {"armed": True}

        def maybe_retire(executor):
            if interrupt["armed"]:
                raise KeyboardInterrupt("Ctrl-C while the old pool drained")
            return real_retire(executor)

        monkeypatch.setattr(session, "_retire_shared_executor", maybe_retire)
        monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
        with pytest.raises(KeyboardInterrupt):
            session.ingest(_folder(tmp_path, "interrupted", files=1))

        assert session._ingests_in_flight == 0, (
            f"a retirement that raised left the in-flight counter at "
            f"{session._ingests_in_flight}; every later ingest() would "
            f"see a peer that is not there and never resize again")
        # The swap stands: the pool is the new one, at the new width.
        assert session._executor_width == 2
        assert [kwargs.get("max_workers") for kwargs in calls] == [3, 2]

        interrupt["armed"] = False
        monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "1")
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            session.ingest(_folder(tmp_path, "after", files=1))
        assert [kwargs.get("max_workers") for kwargs in calls] == [3, 2, 1], (
            f"the ingest() after an interrupted retirement did not resize: "
            f"{[k.get('max_workers') for k in calls]}")
        assert _instance_count(session) == 1, (
            f"the session could not ingest after an interrupted "
            f"retirement: {_instance_count(session)} instance(s)")
        skipped = [record.getMessage() for record in caplog.records
                   if record.levelno >= logging.WARNING
                   and "ISOCENTER_MAX_WORKERS" in record.getMessage()]
        assert not skipped, (
            f"a lone ingest() reported a peer that is not there: {skipped}")


def test_a_peer_ingest_keeps_its_pool_and_the_skipped_resize_is_reported(
        tmp_path, monkeypatch, caplog):
    """A resize never touches a pool another `ingest()` is running on (#511).

    Two `ingest()` calls on two threads of one session can overlap: the
    sidecar pass-lock is taken *shared* for an ingest, so the lock design
    admits it, and the shared pool is the only thing the two calls share.
    Rebuilding it under a peer would shut down the pool holding that
    peer's queued files. They are all submitted in one `executor.map`
    inside `_run_on_shared_executor`, so cancelling them drops the files
    the peer has not started yet with nothing raised at the caller --
    #232's shape. So the second call runs on the pool as it stands and
    says so, in #471's shape: one `WARNING` naming the variable, the
    pool's width and the width asked for. The next `ingest()` that starts
    with no peer gets the new width, which the third phase here asserts
    rather than taking on trust from the message.

    The park is the seam `test_compact_refuses_during_a_pass.py` uses for
    the same window: the first `Equipment.from_parts`, reached while
    linking the first result, inside the pass and after the counter has
    been incremented. The peer waits on the counter reading 1, a bounded
    condition, never on a sleep. It ingests an **empty** folder on
    purpose: this test is about which pool the second call is handed, and
    a second real ingest mutating the graph while the first is parked
    mid-link would be measuring something else.

    Run on both gate interpreters. On the free-threaded build two
    threads in one session is the ordinary case, not a corner.

    Killing mutation: the peer check dropped, so the second call rebuilds
    the pool under the first.
    """
    import threading  # pylint: disable=import-outside-toplevel

    from isocenter.entities import Equipment
    from isocenter.session import DicomSession

    monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 3)
    calls = _record_session_pools(monkeypatch)

    parked, released = threading.Event(), threading.Event()
    real_from_parts = Equipment.from_parts
    seen = []

    def parking_from_parts(*args, **kwargs):
        seen.append(True)
        if len(seen) == 1:
            parked.set()
            assert released.wait(60.0), "the peer never released the park"
        return real_from_parts(*args, **kwargs)

    monkeypatch.setattr(Equipment, "from_parts", parking_from_parts)

    with DicomSession(str(tmp_path / "s.db")) as session:
        first = threading.Thread(
            target=session.ingest,
            args=(_folder(tmp_path, "first", files=2),))
        first.start()
        try:
            _wait_until(parked.is_set, "the first ingest to park")
            _wait_until(lambda: session._ingests_in_flight == 1,
                        "the first ingest to be counted in flight")

            monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
            before = session._executor
            with caplog.at_level(logging.WARNING):
                session.ingest(_folder(tmp_path, "peer"))

            assert session._executor is before, (
                "an ingest() resized the shared pool while another "
                "ingest() was running on it; the peer's queued files "
                "would have been cancelled")
            assert [kwargs.get("max_workers") for kwargs in calls] == [3], (
                f"a second pool was built under a peer ingest: "
                f"{[k.get('max_workers') for k in calls]}")
            skipped = [record.getMessage() for record in caplog.records
                       if record.levelno >= logging.WARNING
                       and "ISOCENTER_MAX_WORKERS" in record.getMessage()]
            assert len(skipped) == 1, (
                f"expected exactly one WARNING about the skipped resize, "
                f"got {skipped}")
            # The widths in the order the sentence promises, not just
            # present somewhere in it: `"2" in msg and "3" in msg` also
            # passes for the message with the two format arguments
            # transposed, which tells the operator that a pool of 2 was
            # asked to be 3 -- the exact opposite of the truth, and #500's
            # own theme in this branch. Matched as a rendered substring
            # rather than by regex so the assertion reads as the line an
            # operator sees.
            assert ("asks for 2 worker(s) and this session's shared "
                    "process pool has 3" in skipped[0]), (
                f"the warning does not name the requested width and the "
                f"pool's, in that order: {skipped[0]}")
            assert "ran at 3" in skipped[0] and "built at 2" in skipped[0], (
                f"the warning does not say which width this ingest() ran "
                f"at and which the next one gets: {skipped[0]}")
        finally:
            released.set()
            first.join(timeout=60.0)
        assert not first.is_alive(), "the first ingest never finished"
        # What this test is actually about: the peer's files. `join()`
        # returning and `is_alive()` being False are both true of a
        # thread whose target *raised*, so neither says the two files
        # arrived -- an ingest that lost its pool would satisfy both and
        # index nothing. The count is the direct measurement.
        assert _instance_count(session) == 2, (
            f"the ingest that was running when the resize was skipped "
            f"did not index its two files: "
            f"{_instance_count(session)} instance(s) in the graph")

        # The promise the warning makes: the next ingest() that starts
        # alone is built at the new width.
        session.ingest(_folder(tmp_path, "alone", files=1))
        assert [kwargs.get("max_workers") for kwargs in calls] == [3, 2], (
            f"the ingest() that started with no peer did not pick up the "
            f"new width: {[k.get('max_workers') for k in calls]}")
        assert _instance_count(session) == 3, (
            f"the ingest() on the rebuilt pool did not index its file: "
            f"{_instance_count(session)} instance(s) in the graph")


def test_the_shared_executor_gets_no_initializer_without_a_lever(
        tmp_path, monkeypatch):
    """`Session.__init__` calls `resolve_worker_initializer()` bare (#365).

    So the resolver's own `disable_gc=False` default is what decides
    whether every shared-executor worker runs with its collector off,
    and no test looked at that executor's initializer: the default could
    be flipped to `True` and ingest would run GC-less on every session.
    Reads a private attribute of the executor, as the spawn test above
    reads `_mp_context`. Killing mutation: `resolve_worker_initializer`'s
    default `False` -> `True`.
    """
    from isocenter.session import DicomSession

    monkeypatch.delenv("ISOCENTER_DISABLE_GC", raising=False)
    monkeypatch.delenv("ISOCENTER_WORKER_FAULTHANDLER", raising=False)

    with DicomSession(str(tmp_path / "s.db")) as session:
        assert session._executor._initializer is None, (
            "with neither lever set the session's executor was handed an "
            f"initializer: {session._executor._initializer!r}")


def test_the_initializer_actually_disables_the_collector_in_a_worker(
        monkeypatch):
    """`gc.disable()` runs, in a worker (#365).

    `test_parallel_config.py::_assert_disables_gc` asserts the partial's
    *shape* on purpose -- calling it would disable the test process's
    collector -- so the one line that does the work, `gc.disable()`,
    could be deleted and every test stayed green. A spawned worker
    reports its own `gc.isenabled()`. Processes structurally: threads
    get no initializer by rule (`_Strategy.worker_initializer`), so on
    3.14t the default path would make this vacuous. One spawn, about a
    second. Killing mutation: `gc.disable()` deleted from `_worker_init`.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

    assert parallel.run_parallel(_collector_enabled, [0], disable_gc=True,
                                 max_workers=1, show_progress=False) == [False]
    assert parallel.run_parallel(_collector_enabled, [0],
                                 max_workers=1, show_progress=False) == [True]


# --- #232: exceptions as values ---------------------------------------------
#
# Two consumers -- `Session._apply_redaction_outcomes` and the export
# reporters in `io_handlers` -- branch on a result *being* an Exception,
# under comments describing "`run_parallel` handing back a worker that
# died". No strategy ever did that: all three are a plain `yield from`
# over a mapper, and a mapper re-raises a worker's exception at the point
# of iteration, so the arms were unreachable and the raise discarded
# every result still queued behind it (#232). `yield_exceptions=True` is
# the mode that makes the comments true; the default stays a raise,
# because a caller with no Exception arm must not receive one as data.


def test_without_the_flag_a_worker_exception_still_propagates(monkeypatch):
    """The default contract is unchanged: a raise is a raise.

    Scan, verify and ingest have no `isinstance(result, Exception)` arm;
    handing them an exception as a value would let it flow into the graph
    as if it were a result.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    with pytest.raises(ValueError):
        parallel.run_parallel(double_or_raise, [1, -2, 3],
                              show_progress=False)


@pytest.mark.parametrize("lever", ["ISOCENTER_FORCE_THREADS",
                                   "ISOCENTER_FORCE_PROCESSES"])
def test_a_raising_task_is_yielded_as_a_value_when_asked(monkeypatch, lever):
    """One bad item costs that item, not the pass.

    Both per-call executors, because they are the two paths the redaction
    and export consumers actually take (3.14t defaults to threads, 3.12
    to processes) and the two `finally`-shaped halves of this module have
    diverged before (#213).
    """
    monkeypatch.setenv(lever, "1")

    results = parallel.run_parallel(
        double_or_raise, [1, -2, 3], show_progress=False, max_workers=2,
        yield_exceptions=True)

    assert results[0] == 2
    assert results[2] == 6, (
        "the item queued behind the failure was discarded with it")
    assert isinstance(results[1], ValueError), (
        f"the worker's exception was not handed back as a value: "
        f"{results[1]!r}")
    assert "-2" in str(results[1])


def test_a_raising_task_is_yielded_as_a_value_on_the_recycling_pool():
    """`maxtasksperchild` selects `multiprocessing.Pool`, the third path.

    `imap_unordered` by default, so the contract here is membership, not
    order (`ordered=True` keeps submission order; #450).
    """
    results = parallel.run_parallel(
        double_or_raise, [1, -2, 3], show_progress=False, max_workers=2,
        maxtasksperchild=1, yield_exceptions=True)

    exceptions = [r for r in results if isinstance(r, Exception)]
    assert sorted(r for r in results if not isinstance(r, Exception)) == [2, 6]
    assert len(exceptions) == 1 and isinstance(exceptions[0], ValueError)


def _head_waits_for_the_rest(item):
    """Module scope: it pickles into a recycling-pool worker.

    Item 0 does not return until items 1..3 have each left a marker file,
    so it *finishes* last whatever the scheduler does -- a sleep would
    only make that likely, and a respawn under `maxtasksperchild=1` can
    take longer than any sleep short enough to keep in the suite. The
    60-second bound turns a broken barrier into a failure, not a hang.

    **Finishing last is not arriving last (#505).** Items 1..3 write
    their marker *before* they return, so item 0 leaves the barrier
    while item 3 is still inside its worker, and item 0's result can
    reach the parent while item 3's is still being pickled back through
    the pool's result queue. Only item 3 can lose that race: under
    `max_workers=2, maxtasksperchild=1` item 0 holds one slot and items
    1..3 run one after another on the other, so 1 and 2 have delivered
    long before 3 starts. Anything asserted here about *arrival* order
    must therefore be about item 0 not being first, never about it being
    last.
    """
    import time  # pylint: disable=import-outside-toplevel
    index, folder = item
    if index == 0:
        deadline = time.monotonic() + 60
        while not all(os.path.exists(os.path.join(folder, str(k)))
                      for k in (1, 2, 3)):
            if time.monotonic() > deadline:
                raise TimeoutError("items 1..3 never finished")
            time.sleep(0.01)
    else:
        with open(os.path.join(folder, str(index)), "w",
                  encoding="utf-8"):
            pass
    return index


def test_an_ordered_recycling_run_yields_in_submission_order(tmp_path):
    """`ordered=True` makes the recycling pool yield in submission order (#450).

    The other two paths are ordered already (`executor.map`, or a
    shared Pool's `imap`); the recycling pool streams by arrival unless
    asked. `import_files` asks, so a direct call under
    `ISOCENTER_MAX_TASKS_PER_CHILD` links its files in path order and
    keeps the same duplicate every time.

    The unordered run is here as the precondition: item 0 is made to
    finish after items 1 and 2, and if the default did *not* then yield
    it after them the fixture would not be able to tell `imap` from
    `imap_unordered`, and the ordered assertion would pass for the wrong
    reason.

    **The precondition is item 0 not arriving first, not item 0 arriving
    last (#505).** Those establish the same thing -- under `imap` item 0
    is always first -- and only the first of them is race-free. Item 0
    cannot overtake items 1 and 2, which have delivered before item 3
    even starts; it can and does overtake item 3, whose marker it is
    waiting on. `unordered[-1] == 0` therefore lost the race once in a
    2313-test run on 3.14t, failing `got [1, 2, 0, 3]` (#505). See
    `_head_waits_for_the_rest` for why arrival order is not completion
    order.

    Both runs need the strategy's chunksize to be 1, `run_parallel`'s
    default, and the autouse `clean_env` fixture is what guarantees it:
    under a chunksize of 2 items 0 and 1 share a chunk, so item 0 waits
    60 s on a marker its own chunk-mate cannot write until item 0
    returns. That fixture's reach over `ISOCENTER_CHUNKSIZE` is
    load-bearing here, and the failure if it ever stops reaching is a
    loud `TimeoutError`, not a quiet pass.
    """
    def items(tag):
        folder = tmp_path / tag
        folder.mkdir()
        return [(k, str(folder)) for k in range(4)]

    unordered = parallel.run_parallel(
        _head_waits_for_the_rest, items("unordered"), show_progress=False,
        max_workers=2, maxtasksperchild=1)
    assert sorted(unordered) == [0, 1, 2, 3]
    assert unordered[0] != 0, (
        f"precondition: item 0 must not arrive first by default, or the "
        f"ordered run below cannot tell imap from imap_unordered; got "
        f"{unordered}")

    ordered = parallel.run_parallel(
        _head_waits_for_the_rest, items("ordered"), show_progress=False,
        max_workers=2, maxtasksperchild=1, ordered=True)
    assert ordered == [0, 1, 2, 3], ordered


def test_a_raising_task_is_yielded_as_a_value_on_a_shared_executor():
    """The fourth dispatch: an executor the caller owns.

    This is `write_tree` under `Session.export()`, which passes the
    session's own pool.
    """
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = parallel.run_parallel(
            double_or_raise, [1, -2, 3], show_progress=False,
            executor=executor, yield_exceptions=True)

    assert results[0] == 2 and results[2] == 6
    assert isinstance(results[1], ValueError)


def test_a_dead_worker_surfaces_as_a_trailing_value_not_a_raise(monkeypatch):
    """The loss the consumers' arms were written for, reproduced.

    A killed worker cannot be caught per-task -- there is no interpreter
    left in it to catch anything -- so the pool's own `BrokenProcessPool`
    is caught at the iteration and yielded as the final value: the
    results already produced survive, and the caller's Exception arm gets
    the one fact that remains. Without the fix this call raises
    `BrokenProcessPool` and the completed result is discarded with it,
    which is exactly the unreported loss of #232.

    Processes only, deliberately: a thread cannot die this way without
    taking the interpreter with it, and `multiprocessing.Pool` answers a
    dead worker by hanging on the lost task rather than raising, so the
    recycling path cannot make this promise (noted in `run_parallel`'s
    docstring).
    """
    from concurrent.futures.process import BrokenProcessPool

    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

    results = parallel.run_parallel(
        double_or_die, [1, -2, 3], show_progress=False, max_workers=1,
        yield_exceptions=True)

    assert results[0] == 2, (
        "the result completed before the death did not survive it")
    assert isinstance(results[-1], BrokenProcessPool), results
    assert not any(r == 6 for r in results), (
        "the item queued behind the dead worker cannot have run; if it "
        "did, this fixture is no longer killing anything")


# --- #796: a worker that outlives its broken pool's SIGTERM ------------------
#
# When a worker of a `ProcessPoolExecutor` ends outright, CPython's manager
# thread sends every other worker SIGTERM, twice, and then joins each one
# with no timeout while holding the pool's shutdown lock, which `submit()`
# and `shutdown()` (whatever `wait` says) take first. A worker that handles
# SIGTERM hung whichever of them came next, for good.
# `parallel._end_broken_pool_stragglers` gives the workers
# `_BROKEN_POOL_GRACE_S` to end and SIGKILLs the rest.
#
# The workers here install their handler in a pool initializer and only
# then write `ready-<pid>`. A worker still starting has SIGTERM's default
# disposition and simply dies of the pool's SIGTERM, leaving nothing to
# end, so every death waits for the marker of the worker it leaves behind.
# Every call that can hang runs under `_bounded`. The helpers are imported
# by `test_a_dead_ingest_worker_costs_the_file_it_was_reading.py` too.


def _ignore_signal(signum, frame):  # pylint: disable=unused-argument
    """A SIGTERM handler that does nothing, as a graceful-shutdown handler
    does while it lets the running task finish. Module scope: a pool
    initializer installs it inside the worker."""


def _end_half_a_second_later(signum, frame):  # pylint: disable=unused-argument
    """A SIGTERM handler that ends its process half a second later, as one
    that saves its state first does."""
    time.sleep(0.5)
    os._exit(0)  # pylint: disable=protected-access


def _mark_ready(marker_dir):
    with open(os.path.join(marker_dir, f"ready-{os.getpid()}"), "w",
              encoding="utf-8"):
        pass


def _outlive_sigterm(marker_dir):
    """Pool initializer: this worker outlives SIGTERM, and says so."""
    signal.signal(signal.SIGTERM, _ignore_signal)
    _mark_ready(marker_dir)


def _end_slowly_on_sigterm(marker_dir):
    """Pool initializer: SIGTERM ends this worker half a second later."""
    signal.signal(signal.SIGTERM, _end_half_a_second_later)
    _mark_ready(marker_dir)


def _wait_for_a_ready_sibling(marker_dir):
    """Block, for at most 30 s, until another worker has marked itself."""
    mine = f"ready-{os.getpid()}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(name.startswith("ready-") and name != mine
               for name in os.listdir(marker_dir)):
            return
        time.sleep(0.01)


def _double_or_die_after_a_ready_sibling(args):
    """`double_or_die`, once another worker has installed its handler."""
    value, marker_dir = args
    if value < 0:
        _wait_for_a_ready_sibling(marker_dir)
        os._exit(13)  # pylint: disable=protected-access
    return value * 2


def _double_or_die_when_told(args):
    """`double_or_die`, once another worker is ready and `go` exists.

    The test reads a result first and only then writes `go`, so the worker
    ends while the stream is open and nothing is reading it.

    Without `go` in 30 s it returns instead, and never ends its worker. A
    test that fails before it writes `go` leaves its stream open, and the
    pool is shut down whenever the stream is collected, as late as pytest's
    exit. A worker that ended then would break the pool while that
    `shutdown(wait=True)` waits, which nothing reaps, behind a sibling that
    outlives SIGTERM: the run would never end (review of #861).
    """
    value, marker_dir = args
    if value < 0:
        _wait_for_a_ready_sibling(marker_dir)
        go = os.path.join(marker_dir, "go")
        deadline = time.monotonic() + 30
        while not os.path.exists(go):
            if time.monotonic() >= deadline:
                return value * 2
            time.sleep(0.01)
        os._exit(13)  # pylint: disable=protected-access
    return value * 2


def _wait_until_broken(pool):
    """Block, for at most 60 s, until `pool` has seen a worker of it die."""
    deadline = time.monotonic() + 60
    while not pool._broken:
        assert time.monotonic() < deadline, "the pool never saw its worker die"
        time.sleep(0.01)


def _bounded(call, seconds):
    """Run `call()` on a thread, and say whether it hung.

    A call still running after `seconds` is freed by SIGKILLing every child
    process of this one, which is what ends a hang of #796's shape, and is
    given a minute more to finish. A red test then reports the hang instead
    of hanging the run.

    Returns:
        tuple: `(hung, value)`. What `call()` raised is raised here, unless
        it hung.
    """
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    hung = worker.is_alive()
    if hung:
        for child in multiprocessing.active_children():
            child.kill()
        worker.join(60)
    elif "error" in outcome:
        raise outcome["error"]
    return hung, outcome.get("value")


def _start_two_workers(pool):
    """Spawn both workers of a two-worker `pool`.

    Each `submit()` spawns a worker while none is idle, and the second comes
    long before the first worker has started, so two submits start two.
    """
    for future in [pool.submit(os.getpid) for _ in range(2)]:
        future.result(timeout=60)


def _break_while_idle(pool, marker_dir):
    """SIGKILL one idle worker of a two-worker `pool`, as the out-of-memory
    killer would between two tasks, and wait until the pool has seen it.

    Waits first for both workers' markers, so the other one outlives the
    pool's SIGTERM however its handler treats it.

    Returns:
        int: The pid of the worker left running.
    """
    deadline = time.monotonic() + 60
    pids = sorted(pool._processes)
    assert len(pids) == 2, (
        f"the pool has {len(pids)} worker(s); this needs one to kill and "
        "one to outlive the pool's SIGTERM")
    while not all(os.path.exists(os.path.join(marker_dir, f"ready-{pid}"))
                  for pid in pids):
        assert time.monotonic() < deadline, (
            "a worker never installed its SIGTERM handler")
        time.sleep(0.01)
    victim, survivor = pids
    os.kill(victim, signal.SIGKILL)
    while not pool._broken:
        assert time.monotonic() < deadline, (
            "the pool never saw its worker die")
        time.sleep(0.01)
    return survivor


@contextlib.contextmanager
def _two_worker_pool(initializer):
    """A spawned two-worker pool whose workers are gone when the block exits.

    However it exits: a red test must not leave a worker that ignores
    SIGTERM behind, or its `shutdown()` hangs the run.
    """
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn"),
        initializer=initializer)
    try:
        yield pool
    finally:
        for process in list((pool._processes or {}).values()):
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        pool.shutdown(wait=True)


def _sigkill_records(caplog):
    return [r for r in caplog.records if "SIGKILL" in r.getMessage()]


def test_run_parallels_own_pool_ends_a_worker_that_outlives_its_break(
        tmp_path, monkeypatch, caplog):
    """`run_parallel`'s own process pool returns though a sibling of the
    dead worker handles SIGTERM.

    Its `with` exit is `shutdown(wait=True)`, which waited on the manager
    thread's lock for good, so the trailing `BrokenProcessPool` that
    `yield_exceptions=True` promises never arrived: `audit()` and
    `redact()` dispatch through this pool.

    Killing mutation: the `_end_broken_pool_stragglers` call in
    `_run_on_new_executor` deleted.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    # raising=False: on a tree without the constant this goes red for the
    # hang it exists for, not on an AttributeError here.
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0, raising=False)
    initializer = functools.partial(_outlive_sigterm, str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)
    items = [(value, str(markers)) for value in (1, -2, 3, 4)]

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        hung, results = _bounded(lambda: parallel.run_parallel(
            _double_or_die_after_a_ready_sibling, items, max_workers=2,
            show_progress=False, yield_exceptions=True), 60)

    assert not hung, (
        "run_parallel() did not return: its own pool's `with` exit waited "
        "on a worker that outlives SIGTERM (#796)")
    assert isinstance(results[-1], BrokenProcessPool), results
    assert multiprocessing.active_children() == []
    assert len(_sigkill_records(caplog)) == 1, [
        r.getMessage() for r in caplog.records]


def test_run_parallels_own_pool_is_reaped_when_its_reader_stops(
        tmp_path, monkeypatch, caplog):
    """A stream closed after its pool broke returns, on `run_parallel`'s
    own process pool.

    The pool's own `BrokenProcessPool` is one way out of a broken pool. A
    reader that stops is another (`GeneratorExit`; `redact()` streams),
    and so are Ctrl-C and a task's own exception. Each reached the `with`
    exit's `shutdown(wait=True)` unreaped, which waited for good behind a
    worker that outlived the pool's SIGTERM (review of #861).

    Killing mutation: the reap's `finally` narrowed back to
    `except BrokenProcessPool`.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    initializer = functools.partial(_outlive_sigterm, str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)
    pools = []

    class _Recorded(concurrent.futures.ProcessPoolExecutor):
        """The pool `run_parallel` builds, kept so the test can wait on it."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pools.append(self)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _Recorded)
    items = [(1, str(markers)), (-1, str(markers))]
    stream = parallel.run_parallel(
        _double_or_die_when_told, items, max_workers=2,
        show_progress=False, return_generator=True)
    assert next(stream) == 2
    assert len(pools) == 1
    (markers / "go").touch()
    _wait_until_broken(pools[0])

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        hung, _ = _bounded(stream.close, 60)

    assert not hung, (
        "closing run_parallel()'s stream did not return: its pool had "
        "broken, and the `with` exit waited on a worker that outlives "
        "SIGTERM (#796)")
    assert multiprocessing.active_children() == []
    assert len(_sigkill_records(caplog)) == 1, [
        r.getMessage() for r in caplog.records]


def test_a_callers_pool_is_reaped_before_its_owner_shuts_it_down(
        tmp_path, monkeypatch, caplog):
    """A stream closed after a caller's pool broke leaves that pool fit to
    shut down.

    `_run_on_shared_executor` never shuts a pool down, so closing its
    stream returned at once; the owner's `shutdown()` then waited for good
    on the lock the manager thread holds while it joins a worker that
    outlived SIGTERM. `ingest()` builds its retry pools itself and shuts
    each one down in a `finally` (`io_handlers._ingest_results`), so a
    reader that stopped after one broke hung there (review of #861).

    Killing mutation: the reap's `finally` narrowed back to
    `except BrokenProcessPool`.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    items = [(1, str(markers)), (-1, str(markers))]
    with _two_worker_pool(functools.partial(
            _outlive_sigterm, str(markers))) as pool:
        stream = parallel.run_parallel(
            _double_or_die_when_told, items, executor=pool,
            show_progress=False, return_generator=True)
        assert next(stream) == 2
        (markers / "go").touch()
        _wait_until_broken(pool)
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            close_hung, _ = _bounded(stream.close, 60)
            # As `_ingest_results` shuts its own retry pools down.
            hung, _ = _bounded(
                lambda: pool.shutdown(wait=True, cancel_futures=True), 60)

    assert not close_hung
    assert not hung, (
        "the owner's shutdown() of a pool whose stream was closed after it "
        "broke waited on a worker that outlives SIGTERM (#796)")
    assert len(_sigkill_records(caplog)) == 1, [
        r.getMessage() for r in caplog.records]


def test_the_straggler_helper_leaves_a_healthy_pool_alone(monkeypatch):
    """A pool that is not broken is none of its business.

    It is called before every dispatch on a shared pool, so on a healthy
    one it must return at once and touch nothing.

    Killing mutation: the `_broken` guard deleted or inverted; the helper
    then waits out the grace on a live worker and kills it.
    """
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0, raising=False)
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    try:
        pid = pool.submit(os.getpid).result(timeout=60)
        assert parallel._end_broken_pool_stragglers(pool) == []
        assert pool.submit(os.getpid).result(timeout=60) == pid, (
            "the helper ended a worker of a pool that was not broken")
    finally:
        pool.shutdown(wait=True)


def test_the_straggler_helper_lets_every_other_kind_of_pool_through():
    """A `multiprocessing.Pool` or a thread pool handed in as `executor=`.

    `_run_on_shared_executor` accepts both, and calls the helper before
    every dispatch, so neither may trip it. A `Pool` has no `_broken`, but
    it does have `_processes`: the worker count, an int. It is the `False`
    default of the `_broken` read that lets it through.

    Killing mutation: that default made `True`, which fails every dispatch
    on a `Pool` with `AttributeError: 'int' object has no attribute
    'values'`.
    """
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        assert parallel._end_broken_pool_stragglers(pool) == []
        assert parallel.run_parallel(
            double_or_raise, [1, 2], executor=pool,
            show_progress=False) == [2, 4]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as threads:
        assert parallel._end_broken_pool_stragglers(threads) == []


def test_the_straggler_helper_has_nothing_to_end_on_a_pool_already_shut_down(
        monkeypatch, caplog):
    """A broken pool that has been shut down has no workers left to end.

    `shutdown()` sets `_processes` to None and leaves `_broken` set, so
    such a pool gets past the helper's first guard and stops at the second.
    `close()` hands it one on a second call, which its docstring promises
    is safe, when the session's pool broke; a caller's own pool, broken,
    shut down and then handed in as `executor=`, is another. The helper
    must return `[]` and kill nothing.

    Killing mutation: the `_processes` guard's `return []` made
    `return None`.
    """
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    try:
        pid = pool.submit(os.getpid).result(timeout=60)
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while not pool._broken:
            assert time.monotonic() < deadline, (
                "the pool never saw its worker die")
            time.sleep(0.01)
    finally:
        pool.shutdown(wait=True)
    # The case this is for: past the `_broken` guard, stopped at the other.
    assert pool._broken and pool._processes is None
    kills = []
    real_kill = multiprocessing.process.BaseProcess.kill

    def recording_kill(process):
        kills.append(process.pid)
        real_kill(process)

    monkeypatch.setattr(multiprocessing.process.BaseProcess, "kill",
                        recording_kill)
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        assert parallel._end_broken_pool_stragglers(pool) == []
    assert kills == [], (
        "the helper killed a process of a pool already shut down")
    assert _sigkill_records(caplog) == []


def test_a_worker_that_ends_within_the_grace_is_waited_for_not_killed(
        tmp_path, monkeypatch, caplog):
    """A worker that ends of the pool's own SIGTERM is not SIGKILLed.

    Its handler takes half a second, as one that saves its state does:
    coverage's `sigterm = True` handler was measured at up to 0.8 s under
    load, and a worker killed before it finishes loses its data. The dead
    worker's sentinel is ready at once, so the wait must go on until the
    deadline or the last sentinel, not stop at the first.

    Killing mutations: the wait skipped, or stopped at the first ready
    sentinel.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0, raising=False)
    with _two_worker_pool(functools.partial(
            _end_slowly_on_sigterm, str(markers))) as pool:
        _start_two_workers(pool)
        processes = dict(pool._processes)
        survivor = _break_while_idle(pool, str(markers))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            killed = parallel._end_broken_pool_stragglers(pool)
        # After the manager thread has joined every worker, so the exit
        # code below is the one it recorded.
        hung, _ = _bounded(lambda: pool.shutdown(wait=True), 60)
        assert not hung
    assert killed == []
    assert processes[survivor].exitcode == 0, (
        "the helper killed a worker that was ending of the pool's SIGTERM")
    assert _sigkill_records(caplog) == []


def test_a_worker_that_outlives_the_grace_is_killed_and_named(
        tmp_path, monkeypatch, caplog):
    """A worker still running at the deadline is SIGKILLed, and named once.

    Then the pool's shutdown lock is free: `shutdown(wait=False)`, which
    waited behind it for good, returns at once.

    Killing mutations: the `kill()` deleted, or turned into `terminate()`,
    a third SIGTERM the handler ignores too; the WARNING deleted.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0, raising=False)
    with _two_worker_pool(functools.partial(
            _outlive_sigterm, str(markers))) as pool:
        _start_two_workers(pool)
        processes = dict(pool._processes)
        survivor = _break_while_idle(pool, str(markers))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            killed = parallel._end_broken_pool_stragglers(pool)
        hung, _ = _bounded(lambda: pool.shutdown(wait=False), 30)
        assert not hung, (
            "shutdown(wait=False) still waited on the pool's lock: the "
            "worker that ignored SIGTERM was not ended")
    assert killed == [survivor]
    assert multiprocessing.connection.wait(
        [processes[survivor].sentinel], timeout=30)
    records = _sigkill_records(caplog)
    assert len(records) == 1 and records[0].levelno == logging.WARNING, [
        r.getMessage() for r in caplog.records]
    message = records[0].getMessage()
    assert str(survivor) in message
    # The time measured, and a full grace never logs less than the grace:
    # the wait leaves only once it has run out (#862; mutant M31, a line
    # that always says 0.0 s, survived without this).
    running = re.search(r"still running ([0-9.]+) s after", message)
    assert running, message
    assert float(running.group(1)) >= parallel._BROKEN_POOL_GRACE_S, message
    # Other tests count the dead-worker retry's lines and the recycling
    # override's by these phrases; this line must not be counted as either.
    assert "worker process ended" not in message
    assert "was set, but worker recycling" not in message


def test_a_ctrl_c_during_the_grace_kills_the_workers_it_waited_on(
        tmp_path, monkeypatch, caplog):
    """An interrupted wait kills the workers it was waiting on, and the
    interrupt goes on.

    The grace is seconds in which the program looks stalled, which is when
    a user presses Ctrl-C, and its `KeyboardInterrupt` is raised inside the
    wait. It left the helper with the survivor still running. At a `with`
    exit it reached `shutdown(wait=True)`, which waited on the lock the
    manager thread holds while it joins the survivor, so the Ctrl-C reached
    the user only when the survivor died some other way. From `close()`
    and a resize it reached the caller at once, and the survivor then held
    up the interpreter's exit (review of #861). Every caller goes through
    the helper.

    The interrupt comes in the helper's second wait, which is where a real
    Ctrl-C lands: the first returns the dead worker's sentinel at once, as
    the pool counted itself broken only on seeing it. Raised in the first,
    it would leave the dead worker in the wait set, to be named in the
    WARNING as still running (review of #861, round 3).

    Killing mutations: the kill moved out of the `finally`, so it runs only
    after a wait that returns; a `return` in the `finally`, which swallows
    the interrupt; the grace logged in place of the time measured.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    # Far beyond this test's run, so a kill seen here is the interrupt's
    # and not the deadline's.
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    real_wait = multiprocessing.connection.wait
    here = threading.get_ident()
    waits_here = []

    def interrupted_here(object_list, timeout=None):
        """Ctrl-C, in the second wait on this thread, and that one only.
        `Connection.poll()` reads the module's `wait` too, so any other
        thread's wait goes on as it was; and the helper is called on this
        thread, never under `_bounded`. The third call is the `finally`'s
        readiness check (#862), which must return: a wait that raised there
        too would skip the kill, which no real Ctrl-C does twice."""
        if threading.get_ident() != here:
            return real_wait(object_list, timeout)
        waits_here.append(len(object_list))
        if len(waits_here) != 2:
            return real_wait(object_list, timeout)
        raise KeyboardInterrupt

    with _two_worker_pool(functools.partial(
            _outlive_sigterm, str(markers))) as pool:
        _start_two_workers(pool)
        processes = dict(pool._processes)
        survivor = _break_while_idle(pool, str(markers))
        with monkeypatch.context() as interrupt, caplog.at_level(
                logging.WARNING, logger="isocenter"):
            interrupt.setattr(multiprocessing.connection, "wait",
                              interrupted_here)
            with pytest.raises(KeyboardInterrupt):
                parallel._end_broken_pool_stragglers(pool)
        assert multiprocessing.connection.wait(
            [processes[survivor].sentinel], timeout=30), (
            "the worker the interrupted wait was waiting on is still running")
        hung, _ = _bounded(lambda: pool.shutdown(wait=True), 30)
        assert not hung, (
            "shutdown(wait=True) after a Ctrl-C during the grace waited on a "
            "worker that outlives SIGTERM (#796)")
    # The premise: the first wait held both workers and returned the dead
    # one's sentinel, the interrupt came in the next, on the survivor's, and
    # the `finally` asked once more which of the survivors still runs.
    assert waits_here == [2, 1, 1], waits_here
    records = _sigkill_records(caplog)
    assert len(records) == 1 and records[0].levelno == logging.WARNING, [
        r.getMessage() for r in caplog.records]
    message = records[0].getMessage()
    assert message.startswith("1 worker process(es) "), message
    assert f"(pid {survivor})" in message, message
    # The time measured since the pool was found broken, not the grace: an
    # interrupted wait ends early, and the grace would be false.
    running = re.search(r"still running ([0-9.]+) s after", message)
    assert running, message
    assert float(running.group(1)) < parallel._BROKEN_POOL_GRACE_S, message


def test_an_interrupt_before_the_dead_worker_is_seen_names_only_the_survivor(
        tmp_path, monkeypatch, caplog):
    """A Ctrl-C that lands before the wait has returned the dead worker's
    sentinel kills, and names, only the worker still running (#862).

    The `finally` killed and named every process left in the wait set. An
    interrupt in the first wait leaves the worker that had already ended
    there, so the WARNING named it as still running and the helper
    returned its pid. The kill was harmless (`kill()` skips a reaped
    worker, and a zombie takes the signal); the record was wrong.

    Killing mutation: the readiness check before the kill removed (N15).
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    real_wait = multiprocessing.connection.wait
    here = threading.get_ident()
    waits_here = []

    def interrupted_first(object_list, timeout=None):
        """Ctrl-C in the first wait on this thread only; every later call,
        and every other thread's, goes through."""
        if threading.get_ident() != here:
            return real_wait(object_list, timeout)
        waits_here.append(len(object_list))
        if len(waits_here) == 1:
            raise KeyboardInterrupt
        return real_wait(object_list, timeout)

    with _two_worker_pool(functools.partial(
            _outlive_sigterm, str(markers))) as pool:
        _start_two_workers(pool)
        processes = dict(pool._processes)
        survivor = _break_while_idle(pool, str(markers))
        (victim,) = set(processes) - {survivor}
        # The premise: the dead worker has ended before the helper asks.
        assert multiprocessing.connection.wait(
            [processes[victim].sentinel], timeout=30)
        with monkeypatch.context() as interrupt, caplog.at_level(
                logging.WARNING, logger="isocenter"):
            interrupt.setattr(multiprocessing.connection, "wait",
                              interrupted_first)
            with pytest.raises(KeyboardInterrupt):
                parallel._end_broken_pool_stragglers(pool)
        assert multiprocessing.connection.wait(
            [processes[survivor].sentinel], timeout=30), (
            "the worker still running was not killed")
        hung, _ = _bounded(lambda: pool.shutdown(wait=True), 30)
        assert not hung
    assert waits_here and waits_here[0] == 2, waits_here
    records = _sigkill_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    message = records[0].getMessage()
    assert message.startswith("1 worker process(es) "), message
    assert f"(pid {survivor})" in message, message
    assert not re.search(rf"\b{victim}\b", message), (
        f"the worker that had already ended (pid {victim}) was named as "
        f"still running: {message}")


def test_the_pool_internals_the_straggler_helper_reads_are_there():
    """`_processes` and `_broken`, pinned on each interpreter the gate runs.

    Both are private to `concurrent.futures.process`, and 3.12 offers no
    public way to reach a pool's workers (3.14's `kill_workers()` takes the
    very lock this is about). The helper reads them with `getattr` defaults
    so that another kind of pool passes through, so a CPython that renamed
    either would make it a silent no-op and bring the hang back. This is
    what goes red instead.
    """
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    try:
        pid = pool.submit(os.getpid).result(timeout=60)
        assert pool._broken is False
        assert isinstance(pool._processes, dict)
        assert list(pool._processes) == [pid]
        process = pool._processes[pid]
        assert isinstance(process, multiprocessing.process.BaseProcess)
        assert isinstance(process.sentinel, int)
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while not pool._broken:
            assert time.monotonic() < deadline, (
                "the pool never saw its worker die")
            time.sleep(0.01)
        # Still listed once the pool is broken, which is when it is read.
        assert pool._processes[pid] is process
        assert multiprocessing.connection.wait([process.sentinel], timeout=30)
    finally:
        pool.shutdown(wait=True)


# --- #860: the recycling pool's exit -----------------------------------------
#
# `_run_on_recycling_pool` exited through `with ctx.Pool(...)`, which is
# `terminate()` on every way out, success included. `terminate()` SIGTERMs
# each worker and then joins it with no timeout (`Pool._terminate_pool`), and
# before that takes the inqueue's read lock for good, so a worker not yet
# waiting for work never reads its sentinel. A worker that also outlived
# SIGTERM hung the caller for good: `export()` always, and `audit()`,
# `redact()` and the rest under ISOCENTER_MAX_TASKS_PER_CHILD.
#
# `_cannot_leave` is the deterministic stand-in (the spec's repro): a worker
# that ignores SIGTERM at the C level and holds a non-daemon thread open, so
# it can leave neither by its sentinel (`threading._shutdown` joins the
# thread) nor by SIGTERM. Every call that can hang runs bounded, and every
# test SIGKILLs the workers of its own pools, by the pids it recorded, on
# the way out. Assertions are what the ordering guarantees (#851, #857): no
# upper bound on a time but the hang bound, no count of killed workers where
# scheduling decides it.

_HELD_OPEN = []


def _cannot_leave(marker_dir):
    """Pool initializer: this worker can leave neither by its sentinel nor by
    SIGTERM, and says so. Module scope: it pickles into the worker."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    never = threading.Event()
    held = threading.Thread(target=never.wait, daemon=False)
    held.start()
    _HELD_OPEN.append(held)
    _mark_ready(marker_dir)


def _one_cannot_leave(marker_dir):
    """Pool initializer: the first worker to get here cannot leave, and
    writes `blocked-<pid>`; every other leaves as usual. All mark ready."""
    try:
        fd = os.open(os.path.join(marker_dir, "claimed"),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        _mark_ready(marker_dir)
        return
    os.close(fd)
    with open(os.path.join(marker_dir, f"blocked-{os.getpid()}"), "w",
              encoding="utf-8"):
        pass
    _cannot_leave(marker_dir)


def _double_once_two_are_ready(args):
    """Double, once two workers have marked themselves ready (at most 30 s),
    so neither is still starting when the pool is told to stop."""
    value, marker_dir = args
    deadline = time.monotonic() + 30
    while (len([n for n in os.listdir(marker_dir) if n.startswith("ready-")])
           < 2 and time.monotonic() < deadline):
        time.sleep(0.01)
    return value * 2


def _slow_atexit_marker(marker_dir):
    """Pool initializer: at its exit this worker sleeps a second and then
    writes `exit-<pid>`. A SIGTERM that lands in the sleep loses the marker,
    which is how a worker's own exit work (coverage's data file among it)
    was lost."""
    import atexit  # pylint: disable=import-outside-toplevel

    def mark():
        time.sleep(1.0)
        with open(os.path.join(marker_dir, f"exit-{os.getpid()}"), "w",
                  encoding="utf-8"):
            pass

    atexit.register(mark)
    _mark_ready(marker_dir)


def _pid_and_double(value):
    """The worker's pid with the result, so a test knows who answered."""
    return os.getpid(), value * 2


def _unpicklable_result(value):
    """A result the worker cannot send: a pool failure, not a task's."""
    return lambda: value


def _marked_pids(marker_dir, prefix):
    return {int(name[len(prefix):]) for name in os.listdir(marker_dir)
            if name.startswith(prefix)}


class _RecordedPool(parallel._RecyclingPool):
    """The recycling pool, with every worker it ever starts recorded, so a
    test can name them and SIGKILL them on its way out."""

    started = []

    def Process(self, ctx, *args, **kwds):  # pylint: disable=invalid-name
        process = super().Process(ctx, *args, **kwds)
        _RecordedPool.started.append(process)
        return process


@pytest.fixture
def recorded_pools(monkeypatch):
    """Every worker a recycling pool starts in this test, SIGKILLed after it.

    `_run_on_recycling_pool` builds `parallel._RecyclingPool` by its module
    name when it is called, so the patched name is the one it builds; a
    patch of `multiprocessing.pool.Pool` would not reach it, since the class
    bound its base at import (#887). The kill is what keeps a red test from
    leaving a worker that cannot leave behind, for the interpreter's exit to
    join for good.
    """
    started = []
    monkeypatch.setattr(_RecordedPool, "started", started)
    monkeypatch.setattr(parallel, "_RecyclingPool", _RecordedPool)
    try:
        yield started
    finally:
        for process in list(started):
            try:
                process.kill()
            except (OSError, ValueError, AttributeError):
                pass
        sentinels = []
        for process in list(started):
            try:
                sentinels.append(process.sentinel)
            except (ValueError, AttributeError):
                pass
        if sentinels:
            multiprocessing.connection.wait(sentinels, timeout=30)
        deadline = time.monotonic() + 30
        while (any(t.name == "isocenter-pool-exit" and t.is_alive()
                   for t in threading.enumerate())
               and time.monotonic() < deadline):
            time.sleep(0.05)


def _blocking_initializer(monkeypatch, marker_dir):
    initializer = functools.partial(_cannot_leave, str(marker_dir))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)


def _run_bounded(call, seconds):
    """`_bounded`, keeping what the call raised beside whether it hung.

    Returns:
        dict: `hung`, and `value` or `error`.
    """
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    outcome["hung"] = worker.is_alive()
    if outcome["hung"]:
        for child in multiprocessing.active_children():
            child.kill()
        worker.join(60)
    return outcome


def _gone(processes, seconds=30):
    """Whether every one of `processes` has ended within `seconds`."""
    sentinels = [p.sentinel for p in processes]
    deadline = time.monotonic() + seconds
    while sentinels:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        for ready in multiprocessing.connection.wait(sentinels, timeout=left):
            sentinels.remove(ready)
    return True


def _no_child_left(seconds=30):
    """Every child of this process reaped, waiting for the exit's helper
    thread to finish its `join()`s first."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not multiprocessing.active_children():
            return True
        time.sleep(0.05)
    return not multiprocessing.active_children()


def _exit_records(caplog):
    return [r for r in caplog.records
            if "recycling pool" in r.getMessage()]


def _killed_pids(record):
    match = re.search(r"\(pid ([0-9, ]+)\)", record.getMessage())
    assert match, record.getMessage()
    return {int(pid) for pid in match.group(1).split(", ")}


def _logged_seconds(record):
    match = re.search(r"still running ([0-9.]+) s after", record.getMessage())
    assert match, record.getMessage()
    return float(match.group(1))


def test_a_recycling_pool_whose_worker_cannot_leave_returns_every_result(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """The success path: every result, then the workers that cannot leave
    are killed after the grace, and one WARNING names them.

    `with ctx.Pool()` ended in `terminate()`, which joined a worker that
    ignores SIGTERM with no timeout, after every result was in: `export()`
    hung with its files written. 3 of 3 runs hung on 3.12 and 3.14t.

    Killing mutations: the kill loop deleted; the kill issued while the pool
    is still RUN (the worker handler replaces the dead, and the replacements
    cannot leave either); the logged time replaced by the grace is caught
    with the Ctrl-C test, which logs less than the grace.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    monkeypatch.setattr(parallel, "_POOL_EXIT_AFTER_KILL_S", 30.0,
                        raising=False)
    _blocking_initializer(monkeypatch, markers)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _pid_and_double, list(range(8)), max_workers=2,
            maxtasksperchild=25, show_progress=False), 60)

    assert not outcome["hung"], (
        "run_parallel(maxtasksperchild=) did not return: the recycling "
        "pool's exit joined a worker that cannot leave (#860)")
    assert "error" not in outcome, outcome
    assert sorted(v for _, v in outcome["value"]) == [2 * i for i in range(8)]
    records = _exit_records(caplog)
    assert len(records) == 1 and records[0].levelno == logging.WARNING, [
        r.getMessage() for r in caplog.records]
    killed = _killed_pids(records[0])
    assert killed, records[0].getMessage()
    assert killed <= {p.pid for p in recorded_pools}, (killed, recorded_pools)
    # The time measured, and on a full grace never less than the grace.
    assert _logged_seconds(records[0]) >= 1.0, records[0].getMessage()
    # The cause by path: a finished pool is closed, and no SIGTERM is sent.
    assert "by its sentinel" in records[0].getMessage(), records[0].getMessage()
    assert "SIGTERM" not in records[0].getMessage(), records[0].getMessage()
    assert _gone(recorded_pools), "a worker of the pool is still running"
    assert _no_child_left(), multiprocessing.active_children()


def test_a_worker_that_left_is_neither_killed_nor_named(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """Only a worker still running is killed and named: one that left by
    its sentinel within the grace is not.

    One worker of two cannot leave; the other, which the tasks wait for
    until it has started, leaves at once on `close()`. The WARNING names
    the one, as the sentinels say.

    Killing mutation: every process in `pool._pool` killed, with no
    readiness check (the spec's M8), which names the worker that left.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    # The free worker leaves within milliseconds of `close()`, both having
    # started before any task returned; ten seconds is the margin a loaded
    # machine needs, not a measurement (#851).
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 10.0)
    monkeypatch.setattr(parallel, "_POOL_EXIT_AFTER_KILL_S", 30.0,
                        raising=False)
    initializer = functools.partial(_one_cannot_leave, str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _double_once_two_are_ready,
            [(value, str(markers)) for value in range(4)], max_workers=2,
            maxtasksperchild=25, show_progress=False), 60)

    assert not outcome["hung"], outcome
    assert sorted(outcome["value"]) == [0, 2, 4, 6]
    blocked = _marked_pids(markers, "blocked-")
    assert len(blocked) == 1 and len(_marked_pids(markers, "ready-")) == 2
    records = _exit_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert _killed_pids(records[0]) == blocked, records[0].getMessage()
    assert records[0].getMessage().startswith("1 worker process(es) ")
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_recycling_pool_closed_by_its_reader_ends_workers_that_cannot_leave(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """The failure path: a reader that stops (`redact()` streams) reaches
    `terminate()` with the workers busy or idle, and returns after the kill.

    It hung on every run with a surviving worker, 3 of 3 on both
    interpreters.

    Killing mutations: the kill loop deleted; the #861 shape (wait on the
    sentinels, kill, then `terminate()` on the caller's thread), which a
    killed idle worker holding the inqueue's read lock hangs.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    monkeypatch.setattr(parallel, "_POOL_EXIT_AFTER_KILL_S", 30.0,
                        raising=False)
    _blocking_initializer(monkeypatch, markers)
    stream = parallel.run_parallel(
        _pid_and_double, list(range(8)), max_workers=2, maxtasksperchild=25,
        show_progress=False, return_generator=True)
    assert next(stream)[1] in [2 * i for i in range(8)]

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(stream.close, 60)

    assert not outcome["hung"], (
        "closing a recycling pool's stream did not return: terminate() "
        "joined a worker that cannot leave (#860)")
    assert "error" not in outcome, outcome
    records = _exit_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert _killed_pids(records[0]) <= {p.pid for p in recorded_pools}
    # The cause by path: this way out goes through terminate()'s SIGTERM.
    assert "outlives SIGTERM" in records[0].getMessage(), (
        records[0].getMessage())
    assert _gone(recorded_pools), "a worker of the pool is still running"
    assert _no_child_left(), multiprocessing.active_children()


def test_a_raising_task_reaches_the_caller_after_the_recycling_pools_exit(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """A task's exception, without `yield_exceptions`, is the third way out,
    and reaches the caller once the workers that cannot leave are killed."""
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    monkeypatch.setattr(parallel, "_POOL_EXIT_AFTER_KILL_S", 30.0,
                        raising=False)
    _blocking_initializer(monkeypatch, markers)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            double_or_raise, [1, -2, 3, 4], max_workers=2,
            maxtasksperchild=25, show_progress=False), 60)

    assert not outcome["hung"], (
        "a raising task on the recycling pool did not reach the caller: "
        "terminate() joined a worker that cannot leave (#860)")
    assert isinstance(outcome.get("error"), ValueError), outcome
    assert len(_exit_records(caplog)) == 1, [
        r.getMessage() for r in caplog.records]
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_pool_failure_is_still_yielded_last_after_the_recycling_pools_exit(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """`yield_exceptions=True`'s trailing value for a failure of the pool
    itself (a result that will not pickle) still comes, after the kill."""
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    monkeypatch.setattr(parallel, "_POOL_EXIT_AFTER_KILL_S", 30.0,
                        raising=False)
    _blocking_initializer(monkeypatch, markers)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _unpicklable_result, [1, 2], max_workers=2, maxtasksperchild=25,
            show_progress=False, yield_exceptions=True), 60)

    assert not outcome["hung"], (
        "a pool failure on the recycling pool was never yielded: "
        "terminate() joined a worker that cannot leave (#860)")
    assert "error" not in outcome, outcome
    assert isinstance(outcome["value"][-1], Exception), outcome["value"]
    assert len(_exit_records(caplog)) == 1, [
        r.getMessage() for r in caplog.records]
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_ctrl_c_during_the_recycling_pools_exit_kills_at_once(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """Ctrl-C during the grace kills the workers that cannot leave at once,
    and the `KeyboardInterrupt` reaches the caller.

    A real SIGINT, because the 3.12 trap is inside `Thread.join`: an
    interrupt there runs bpo-45274's handler, which marks a thread that is
    still running as stopped, so an exit that waited with `join()` and
    asked `is_alive()` killed nothing and left the worker for the
    interpreter's exit to join for good (measured: no kill, a child left,
    the process never exited). The signal is sent only once the exit's
    helper thread is running and the main thread is seen, twice, blocked in
    a call the exit made after starting it (read from its frames), so it
    lands in the wait and cannot land outside this call. The call runs on the main thread,
    where Python delivers SIGINT, bounded by a timer that SIGKILLs this
    test's own workers.

    Killing mutations: the `Event` replaced by `helper.join()` and
    `is_alive()` (3.12 only: 3.14 has no such handler); the `finally`
    around the wait narrowed to `except Exception`; the kill moved after
    the wait so it runs only when the wait returns.
    """
    assert threading.current_thread() is threading.main_thread()
    markers = tmp_path / "markers"
    markers.mkdir()
    # Far beyond this test's run, so a kill seen here is the interrupt's.
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    monkeypatch.setattr(parallel, "_POOL_EXIT_AFTER_KILL_S", 30.0,
                        raising=False)
    _blocking_initializer(monkeypatch, markers)
    sent = threading.Event()
    done = threading.Event()

    main = threading.main_thread().ident

    def waiting_in_the_exit():
        """The main thread is blocked in a call `_end_recycling_pool` made
        once its helper was running: its wait for the helper, whichever
        way that wait is written, and not the helper's `start()`."""
        if not any(t.name == "isocenter-pool-exit" and t.is_alive()
                   for t in threading.enumerate()):
            return False
        frame = sys._current_frames().get(main)
        names = []
        while frame is not None:
            names.append(frame.f_code.co_name)
            frame = frame.f_back
        if "_end_recycling_pool" not in names:
            return False
        inner = names[:names.index("_end_recycling_pool")]
        return bool(inner) and "start" not in inner

    def interrupt_once_the_exit_waits():
        deadline = time.monotonic() + 40
        while not done.is_set() and time.monotonic() < deadline:
            # Twice, 20 ms apart: blocked, not passing through.
            if waiting_in_the_exit():
                time.sleep(0.02)
                if waiting_in_the_exit():
                    sent.set()
                    os.kill(os.getpid(), signal.SIGINT)
                    return
            time.sleep(0.01)

    def free_a_hang():
        for process in list(recorded_pools):
            try:
                process.kill()
            except (OSError, ValueError):
                pass

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    watcher = threading.Thread(target=interrupt_once_the_exit_waits,
                               daemon=True)
    watchdog = threading.Timer(50, free_a_hang)
    started = time.monotonic()
    interrupted = False
    try:
        watcher.start()
        watchdog.start()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            try:
                parallel.run_parallel(
                    _pid_and_double, list(range(4)), max_workers=2,
                    maxtasksperchild=25, show_progress=False)
            except KeyboardInterrupt:
                interrupted = True
        elapsed = time.monotonic() - started
    finally:
        done.set()
        watcher.join(60)
        watchdog.cancel()
        signal.signal(signal.SIGINT, previous)

    assert sent.is_set(), (
        "the recycling pool's exit never started its helper thread; it "
        "waited on the caller's thread (#860)")
    assert interrupted, "the KeyboardInterrupt did not reach the caller"
    assert elapsed < 50, (
        f"the interrupted exit took {elapsed:.1f} s; the watchdog freed it")
    assert _gone(recorded_pools, 10), (
        "a worker the interrupted exit was waiting on is still running: the "
        "interrupt was let out before the kill (#860)")
    assert _no_child_left(), multiprocessing.active_children()
    records = _exit_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert _logged_seconds(records[0]) < 30.0, records[0].getMessage()


def test_a_recycling_pools_workers_leave_by_their_sentinel_and_run_atexit(
        tmp_path, monkeypatch, recorded_pools):
    """On success every worker leaves by its sentinel, so its own exit work
    runs: here, an `atexit` handler that takes a second.

    `terminate()` SIGTERMed the idle workers, so a worker's `atexit` ran
    only when the signal happened to miss it (measured: 4 markers of 8, on
    both interpreters). Coverage's data file is such exit work.

    Killing mutation: the success flag never set, so success exits through
    `terminate()` again.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    initializer = functools.partial(_slow_atexit_marker, str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)

    outcome = _run_bounded(lambda: parallel.run_parallel(
        _pid_and_double, list(range(16)), max_workers=4, maxtasksperchild=4,
        show_progress=False), 90)

    assert not outcome["hung"], outcome
    assert sorted(v for _, v in outcome["value"]) == [2 * i for i in range(16)]
    ready = _marked_pids(markers, "ready-")
    assert ready, "no worker ran its initializer"
    assert _marked_pids(markers, "exit-") == ready, (
        "a worker of the recycling pool was ended before its atexit "
        "handler ran: the success path exited through terminate() (#860)")


def test_a_healthy_recycling_pool_exits_without_a_kill(monkeypatch, caplog):
    """Workers with SIGTERM's default disposition leave by their sentinel,
    and nothing is killed or logged."""
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            identity, list(range(20)), max_workers=2, maxtasksperchild=3,
            show_progress=False), 60)
    assert not outcome["hung"], outcome
    assert sorted(outcome["value"]) == list(range(20))
    assert _exit_records(caplog) == [], [
        r.getMessage() for r in caplog.records]
    assert _no_child_left(), multiprocessing.active_children()


def test_a_recycling_pool_exit_stuck_behind_a_leaked_lock_lets_the_caller_go(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """When `terminate()` itself is stuck, the caller is held no longer than
    the grace plus `_POOL_EXIT_AFTER_KILL_S`, and a second WARNING says the
    exit was left behind.

    `_help_stuff_finish` acquires the inqueue's read lock, which a killed
    worker can hold for good: the kernel does not release a POSIX semaphore
    (measured in the spec's `rlock.py`). It is made to block here.

    Killing mutation: the #861 shape, `terminate()` on the caller's thread.
    """
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    monkeypatch.setattr(parallel, "_POOL_EXIT_AFTER_KILL_S", 1.0,
                        raising=False)
    release = threading.Event()
    blocked_on = []

    def leaked_lock(inqueue, task_handler, size):  # pylint: disable=unused-argument
        blocked_on.append(threading.current_thread().name)
        release.wait(120)

    monkeypatch.setattr(multiprocessing.pool.Pool, "_help_stuff_finish",
                        staticmethod(leaked_lock))
    try:
        stream = parallel.run_parallel(
            identity, list(range(8)), max_workers=2, maxtasksperchild=25,
            show_progress=False, return_generator=True)
        assert next(stream) in range(8)
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            outcome = _run_bounded(stream.close, 60)
        assert not outcome["hung"], (
            "the caller waited on a recycling pool's exit stuck behind a "
            "leaked lock (#860)")
        assert "error" not in outcome, outcome
        # Whether a worker is left to kill is scheduling's: `terminate()`
        # sends each idle worker its sentinel before `_help_stuff_finish`,
        # and they usually leave within the grace. The second line is not.
        records = _exit_records(caplog)
        left = [r for r in records
                if "left to a daemon thread" in r.getMessage()]
        assert len(left) == 1 and left[0] is records[-1], [
            r.getMessage() for r in caplog.records]
        assert len(records) <= 2, [r.getMessage() for r in records]
        assert blocked_on == ["isocenter-pool-exit"], blocked_on
    finally:
        release.set()


def test_a_recycling_pool_is_finalized_on_the_exits_thread_not_the_callers(
        monkeypatch, recorded_pools):
    """The exit's helper thread runs the pool's `terminate()` itself, so the
    pool's finalizer, which runs `_help_stuff_finish`, has nothing left to do
    when the pool is collected on the caller's thread.

    A pool closed and joined but never terminated still holds its
    `util.Finalize`, and its collection runs `_terminate_pool` on whichever
    thread drops the last reference, where a leaked lock blocks for good.

    Killing mutation: the helper's trailing `terminate()` removed.
    """
    import gc  # pylint: disable=import-outside-toplevel
    called_on = []
    real = multiprocessing.pool.Pool._help_stuff_finish

    def recorded(inqueue, task_handler, size):
        called_on.append(threading.current_thread().name)
        return real(inqueue, task_handler, size)

    monkeypatch.setattr(multiprocessing.pool.Pool, "_help_stuff_finish",
                        staticmethod(recorded))
    outcome = _run_bounded(lambda: parallel.run_parallel(
        identity, list(range(6)), max_workers=2, maxtasksperchild=25,
        show_progress=False), 60)
    assert not outcome["hung"], outcome
    gc.collect()
    assert called_on == ["isocenter-pool-exit"], called_on


def test_the_pool_internals_the_recycling_exit_reads_are_there():
    """`Pool._pool`, a worker's `sentinel`, and a `terminate()` that runs its
    finalizer once, pinned on each interpreter the gate runs.

    The exit kills the processes in `_pool` whose sentinel is not ready, and
    relies on its helper's `terminate()` consuming the pool's finalizer so
    the collector cannot run it again on the caller's thread. A CPython that
    renamed `_pool` would make the kill a silent no-op, and the hang return.
    """
    pool = multiprocessing.get_context("spawn").Pool(2, maxtasksperchild=25)
    try:
        assert sorted(pool.imap_unordered(identity, range(4))) == [0, 1, 2, 3]
        assert isinstance(pool._pool, list) and pool._pool
        for process in pool._pool:
            assert isinstance(process, multiprocessing.process.BaseProcess)
            assert isinstance(process.sentinel, int)
        assert callable(multiprocessing.pool.Pool._help_stuff_finish)
        pool.close()
        pool.join()
        pool.terminate()
        assert not pool._terminate.still_active()
        started = time.monotonic()
        pool.terminate()
        assert time.monotonic() - started < 5
    finally:
        pool.terminate()


# --- #887, #888: a worker that ends, or cannot end, while the pool runs ------
#
# #887: a recycling-pool worker that died while the pool ran (the
# out-of-memory killer, `os._exit` in a task) was replaced by the worker
# handler, and the stream waited for the task it held for good, before the
# pool's exit, so #860's bound never ran. The pool now records every worker
# it starts and the stream checks them, every `_POOL_WATCH_S`, while it
# waits: any abnormal exit fails it with `BrokenProcessPool`.
#
# #888: a worker that had run its `maxtasksperchild` tasks and could not
# finish exiting (a non-daemon thread, an `atexit` that never returns) was
# never replaced, since the pool replaces a worker only once it has ended.
# Each worker now bounds its own exit with SIGALRM once it leaves its task
# loop, and the parent names it in one WARNING line.
#
# Every call that can hang runs under `_run_bounded`, and the
# `recorded_pools` fixture SIGKILLs this test's own workers on the way out.
# Assertions are what is guaranteed: no count of results that scheduling
# decides, and every pid named is one of this pool's.


def double_or_sigkill(value):
    """A negative SIGKILLs its own worker, as the out-of-memory killer does."""
    if value < 0:
        os.kill(os.getpid(), signal.SIGKILL)
    return value * 2


def _initializer_that_raises():
    """Pool initializer that fails: the worker exits with code 1 before it
    takes a task, and the pool starts another just like it."""
    raise RuntimeError("this worker cannot start")


def _cannot_leave_and_ignores_sigalrm(marker_dir):
    """`_cannot_leave`, and SIGALRM ignored too, as a worker whose own code
    took the signal would. The exit bound must reset it to its default."""
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    _cannot_leave(marker_dir)


def _double_and_mark(args):
    """Double, and leave `done-<value>` behind, so the test knows the task
    ran. A negative waits (at most 30 s) for `go`, then ends its worker."""
    value, marker_dir = args
    if value < 0:
        go = os.path.join(marker_dir, "go")
        deadline = time.monotonic() + 30
        while not os.path.exists(go):
            if time.monotonic() >= deadline:
                return value * 2
            time.sleep(0.01)
        os._exit(13)  # pylint: disable=protected-access
    with open(os.path.join(marker_dir, f"done-{value}"), "w",
              encoding="utf-8"):
        pass
    return value * 2


def _broken_message(value):
    """The `BrokenProcessPool`'s message, with its pid and exit code."""
    assert isinstance(value, BrokenProcessPool), value
    message = str(value)
    match = re.search(r"\(pid (\d+), exit code (-?\d+)\)", message)
    assert match, message
    return message, int(match.group(1)), int(match.group(2))


def _late_leaver_records(caplog):
    return [r for r in caplog.records
            if "ended by their own exit bound" in r.getMessage()]


def _late_leaver_pids(record):
    match = re.search(r"\(SIGALRM\) \(pid ([0-9, ]+)\)", record.getMessage())
    assert match, record.getMessage()
    return {int(pid) for pid in match.group(1).split(", ")}


_FORBIDDEN_PHRASES = ("recycling pool", "SIGKILL", "worker process ended",
                      "was set, but worker recycling")


def test_a_worker_killed_while_the_recycling_pool_runs_fails_the_stream(
        monkeypatch, caplog, recorded_pools):
    """K1: a task's `os._exit(13)` fails the stream with `BrokenProcessPool`,
    yielded last, naming the worker and its exit code (#887).

    `multiprocessing.Pool` replaced the dead worker and waited for the task
    it held for good: 3 of 3 runs hung at 30 s on 3.12 and 3.14t.

    Killing mutations: the watch removed (N1); a process dropped before its
    exit code is read (N7).
    """
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2, raising=False)
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            double_or_die, [1, -2, 3], max_workers=2, maxtasksperchild=25,
            show_progress=False, yield_exceptions=True), 30)

    assert not outcome["hung"], (
        "run_parallel(maxtasksperchild=) did not return: the recycling pool "
        "waited for good on the task a dead worker held (#887)")
    assert "error" not in outcome, outcome
    values = outcome["value"]
    message, pid, code = _broken_message(values[-1])
    assert code == 13 and re.search(r"exit code 13\b", message), message
    assert pid in {p.pid for p in recorded_pools}, (pid, recorded_pools)
    assert set(values[:-1]) <= {2, 6}, values
    # Other tests count lines by these phrases; this message is none of them.
    for phrase in _FORBIDDEN_PHRASES:
        assert phrase not in message, message
    assert _gone(recorded_pools), "a worker of the pool is still running"
    assert _no_child_left(), multiprocessing.active_children()


def test_a_worker_killed_while_the_recycling_pool_runs_raises_to_the_caller(
        monkeypatch, recorded_pools):
    """K2: the same death without `yield_exceptions`: `BrokenProcessPool`
    reaches the caller (#887).

    Killing mutation: the watch removed (N1).
    """
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2, raising=False)
    outcome = _run_bounded(lambda: parallel.run_parallel(
        double_or_die, [1, -2, 3], max_workers=2, maxtasksperchild=25,
        show_progress=False), 30)

    assert not outcome["hung"], (
        "the recycling pool waited for good on a dead worker's task (#887)")
    _, pid, code = _broken_message(outcome.get("error"))
    assert code == 13
    assert pid in {p.pid for p in recorded_pools}
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_worker_the_out_of_memory_killer_ends_fails_the_stream(
        monkeypatch, recorded_pools):
    """K3: a worker SIGKILLed mid-task, the out-of-memory killer's shape:
    the message reads `exit code -9` (#887).

    Killing mutations: the watch removed (N1); a process dropped before its
    exit code is read (N7).
    """
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2, raising=False)
    outcome = _run_bounded(lambda: parallel.run_parallel(
        double_or_sigkill, [1, -2, 3], max_workers=2, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True), 30)

    assert not outcome["hung"], (
        "the recycling pool waited for good on a killed worker's task (#887)")
    message, pid, code = _broken_message(outcome["value"][-1])
    assert code == -signal.SIGKILL and re.search(r"exit code -9\b", message), (
        message)
    assert pid in {p.pid for p in recorded_pools}
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_dead_worker_replaced_before_the_first_check_is_still_seen(
        monkeypatch, recorded_pools):
    """K4: the watch reads every worker the pool ever started, not the pool's
    list at the time of a check (#887).

    The first check is at 5 s; the dead worker is reaped and replaced by
    the worker handler within a tenth of a second, so a watch that read
    `pool._pool` at each check would never see it, and would wait for good.

    Killing mutation: the watch reads `pool._pool` (N2).
    """
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 5.0, raising=False)
    outcome = _run_bounded(lambda: parallel.run_parallel(
        double_or_die, [-1, 2, 3], max_workers=1, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True), 40)

    assert not outcome["hung"], (
        "the recycling pool waited for good on a dead worker that had "
        "already been replaced when the watch first looked (#887)")
    _, pid, code = _broken_message(outcome["value"][-1])
    assert code == 13
    assert pid in {p.pid for p in recorded_pools}
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_workers_that_leave_at_their_quota_are_not_read_as_dead(
        monkeypatch, caplog):
    """K5: a healthy run whose workers each leave after two tasks, every one
    with exit code 0, returns every result, with nothing raised or logged
    (#887's guard against reading a quota exit as a death).

    Killing mutation: exit code 0 read as abnormal (N3).
    """
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.05, raising=False)
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _slow_identity, list(range(12)), max_workers=2,
            maxtasksperchild=2, show_progress=False), 60)

    assert not outcome["hung"], outcome
    assert "error" not in outcome, outcome
    assert sorted(outcome["value"]) == list(range(12))
    assert [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING] == []
    assert _no_child_left(), multiprocessing.active_children()


def test_results_delivered_before_the_death_is_seen_come_first(
        tmp_path, monkeypatch, recorded_pools):
    """K6: every result the pool had delivered when the death was seen is
    yielded, and the `BrokenProcessPool` after them (#887).

    The test reads one result, waits until every other healthy task has
    run, and only then lets the dying task end its worker. It then stops
    reading for a second, so the results pile up in the pool's iterator,
    and the check that comes with the first of them finds the death with
    the rest still waiting to be read.

    Killing mutation: the drain removed (N4), which loses all but one.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2, raising=False)
    healthy = [1, 3, 4, 5, 6]
    items = [(value, str(markers)) for value in [1, -1, 3, 4, 5, 6]]
    stream = parallel.run_parallel(
        _double_and_mark, items, max_workers=2, maxtasksperchild=25,
        show_progress=False, return_generator=True, yield_exceptions=True)
    first = next(stream)
    deadline = time.monotonic() + 30
    while not all((markers / f"done-{v}").exists() for v in healthy):
        assert time.monotonic() < deadline, os.listdir(markers)
        time.sleep(0.01)
    (markers / "go").touch()
    time.sleep(1.0)

    outcome = _run_bounded(lambda: list(stream), 30)

    assert not outcome["hung"], (
        "the recycling pool waited for good on a dead worker's task (#887)")
    values = [first] + outcome["value"]
    _broken_message(values[-1])
    assert sorted(values[:-1]) == [2 * v for v in healthy], values
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_pool_initializer_that_raises_fails_the_recycling_pool(
        monkeypatch, recorded_pools):
    """K7: a worker whose pool initializer raises exits with code 1, and the
    stream fails rather than respawning it for good (#887).

    `multiprocessing.pool.worker` lets the initializer's exception end the
    process, and the worker handler starts another that does the same; no
    task ever ran, and the stream never ended.

    Killing mutation: the watch removed (N1).
    """
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2, raising=False)
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: _initializer_that_raises)
    outcome = _run_bounded(lambda: parallel.run_parallel(
        identity, [1, 2], max_workers=1, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True), 30)

    assert not outcome["hung"], (
        "a recycling pool whose initializer raises respawned it for good")
    message, pid, code = _broken_message(outcome["value"][-1])
    assert code == 1 and re.search(r"exit code 1\b", message), message
    assert pid in {p.pid for p in recorded_pools}


def test_the_pool_internals_the_recycling_watch_reads_are_there():
    """U8b: the stdlib shapes the watch and the exit bound rely on, pinned on
    each interpreter the gate runs (#887, #888).

    Every worker, first and replacement, goes through the pool's `Process`,
    with `multiprocessing.pool.worker` as its target, which the recycling
    pool swaps for `_recycling_worker`: a stdlib that started its workers
    another way would leave them unwatched and unbounded. `worker`'s
    parameters are the ones `_recycling_worker` passes on, and
    `IMapIterator.next(timeout=)` raises `multiprocessing.TimeoutError`.
    """
    import inspect  # pylint: disable=import-outside-toplevel

    assert list(inspect.signature(multiprocessing.pool.worker).parameters) == [
        "inqueue", "outqueue", "initializer", "initargs", "maxtasks",
        "wrap_exception"]
    handed = []

    class _Context:
        """The spawn context, recording what each worker is started with:
        `start()` deletes a process's `_target` and `_args`."""

        def __init__(self, ctx):
            self._ctx = ctx

        def Process(self, *args, **kwds):  # pylint: disable=invalid-name
            handed.append((kwds.get("target"), tuple(kwds.get("args", ()))))
            return self._ctx.Process(*args, **kwds)

    class _Seen(parallel._RecyclingPool):
        def Process(self, ctx, *args, **kwds):  # pylint: disable=invalid-name
            return super().Process(_Context(ctx), *args, **kwds)

    pool = _Seen(processes=1, maxtasksperchild=1,
                 context=multiprocessing.get_context("spawn"),
                 exit_grace=parallel._WORKER_EXIT_GRACE_S)
    started = []
    try:
        assert sorted(pool.imap_unordered(identity, range(4))) == [0, 1, 2, 3]
        started = list(pool._started)
        assert len(started) >= 4, started
        assert len(handed) >= 4, handed
        for target, args in handed:
            assert target is parallel._recycling_worker, target
            assert args[-1] == parallel._WORKER_EXIT_GRACE_S, args
        pending = pool.imap_unordered(time.sleep, [5])
        with pytest.raises(multiprocessing.TimeoutError):
            pending.next(timeout=0)
    finally:
        for process in started + list(pool._pool):
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        pool.terminate()


def test_an_exit_code_read_after_another_caller_reaped_is_none():
    """U8c: `exitcode` on a process another caller has already reaped is
    None, not a raise (#887).

    The watch reads `exitcode` once a sentinel is ready, while the pool's
    worker handler may reap the same process (`_join_exited_workers`). That
    race costs one re-read at the next check, never a wrong answer or a
    block, only because `Popen.poll` swallows ECHILD; this pins it.
    """
    process = multiprocessing.get_context("spawn").Process(
        target=time.sleep, args=(0,))
    process.start()
    assert multiprocessing.connection.wait([process.sentinel], timeout=30)
    os.waitpid(process.pid, 0)
    try:
        assert process.exitcode is None
    finally:
        # Reaped behind its back, it would stay in `active_children()` for
        # good (its `poll` keeps answering None), and every later test
        # asserting no child is left would fail on it.
        multiprocessing.process._children.discard(process)


class _EndedButUnreadProcess:
    """A process whose sentinel is ready and whose exit code reads None and
    then its real code: the watch losing the reap race to the pool's worker
    handler, as U8c shows the stdlib answers it."""

    def __init__(self, codes):
        self._read, self._write = os.pipe()
        os.close(self._write)  # the read end is ready from now on
        self._codes = list(codes)
        self.pid = 4242

    @property
    def sentinel(self):
        return self._read

    @property
    def exitcode(self):
        return self._codes.pop(0) if len(self._codes) > 1 else self._codes[0]

    def close(self):
        os.close(self._read)


def test_an_ended_worker_whose_exit_code_reads_none_is_read_again():
    """U8d: a ready sentinel with an exit code of None is kept and read at
    the next check, never taken as a death (#887, review of #913).

    The pool's worker handler can reap a worker before the watch reads it,
    and then `exitcode` is None (U8c). Read as a death, that race fails a
    healthy export with `BrokenProcessPool`. Killing mutation: None read as
    abnormal (N5).
    """
    process = _EndedButUnreadProcess([None, 0])

    class _Pool:
        _started = collections.deque([process])
        _exit_grace = 15.0

    try:
        watch = parallel._RecyclingWatch(_Pool())
        watch.check()
        assert watch._live == [process]
        watch.check()
        assert watch._live == []
    finally:
        process.close()


def test_a_ctrl_c_while_the_sentinels_are_asked_reaches_the_caller(
        monkeypatch):
    """U8e: `_still_running_or_all` lets a `KeyboardInterrupt` through; only
    a handle that cannot be asked falls back to every process (#862).

    Killing mutation: the readiness check catching `BaseException` (N16),
    which would swallow a second Ctrl-C.
    """
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(multiprocessing.connection, "wait", interrupted)
    process = _EndedButUnreadProcess([0])
    try:
        with pytest.raises(KeyboardInterrupt):
            parallel._still_running_or_all([process])
    finally:
        process.close()


class _KillRecorded:
    """A process that records `kill()`; its sentinel is never asked."""

    def __init__(self, pid):
        self.pid = pid
        self.sentinel = pid
        self.killed = False

    def kill(self):
        self.killed = True


def test_a_ctrl_c_while_the_sentinels_are_asked_still_kills_them_all(
        monkeypatch):
    """U8f: an interrupt in the readiness check kills every process, then
    is raised (review of #913).

    Raised out of the kill's `finally` before any `kill()`, it let the
    caller's next step wait on a running worker for good, against the
    comment's own rule: never skip the kill.
    """
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(multiprocessing.connection, "wait", interrupted)
    processes = [_KillRecorded(1), _KillRecorded(2)]
    with pytest.raises(KeyboardInterrupt):
        parallel._kill_still_running(processes)
    assert [p.killed for p in processes] == [True, True]


# U8g and U8h drive each caller of `_kill_still_running` rather than pin the
# call by AST: an AST pin would hold for any spelling that calls the helper
# and would go red on an equivalent rewrite, while these go red on exactly
# the regression that matters -- an interrupt in the readiness check that
# leaves a worker unkilled. Every `connection.wait` raises, so the first
# interrupt lands in the grace wait and the second in the readiness check
# inside the `finally`, which is the path a reverted site gets wrong.


def _every_wait_interrupted(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(multiprocessing.connection, "wait", interrupted)


def test_a_broken_pools_kill_survives_a_ctrl_c_in_its_readiness_check(
        monkeypatch):
    """U8g: `_end_broken_pool_stragglers` kills every worker when a Ctrl-C
    lands in the readiness check, then raises it (review of #913).

    Killing mutation: the site reverted to asking `_still_running_or_all`
    and killing inline, which raises before any `kill()`.
    """
    processes = [_KillRecorded(1), _KillRecorded(2)]

    class _BrokenExecutor:
        _broken = True
        _processes = {p.pid: p for p in processes}

    _every_wait_interrupted(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        parallel._end_broken_pool_stragglers(_BrokenExecutor())
    assert [p.killed for p in processes] == [True, True]


def test_a_recycling_pools_exit_kill_survives_a_ctrl_c_in_its_readiness_check(
        monkeypatch):
    """U8h: `_end_recycling_pool` kills every worker when a Ctrl-C lands in
    the readiness check after the grace, then raises it (review of #913).

    The pool's stdlib exit is held on its helper thread so the grace runs
    out. Killing mutation: the site reverted to asking
    `_still_running_or_all` and killing inline.
    """
    processes = [_KillRecorded(1), _KillRecorded(2)]
    release = threading.Event()

    class _StuckPool:
        _pool = processes

        def terminate(self):
            release.wait(30)

    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 0.05)
    _every_wait_interrupted(monkeypatch)
    try:
        with pytest.raises(KeyboardInterrupt):
            parallel._end_recycling_pool(_StuckPool(), finished=False)
    finally:
        release.set()
    assert [p.killed for p in processes] == [True, True]


def test_a_worker_that_cannot_leave_at_its_quota_ends_itself(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """R1: workers that cannot finish exiting after their quota are ended by
    their own exit bound, replaced, and named; every result comes back
    (#888).

    A worker that has run its `maxtasksperchild` tasks leaves its loop and
    exits, and the pool replaces it only once it has ended. With a
    non-daemon thread still running, none ever did: 3 of 3 runs hung at
    30 s on 3.12 and 3.14t.

    Killing mutations: the exit bound never armed (N9); SIGALRM read as a
    crash (N8).
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_WORKER_EXIT_GRACE_S", 0.5, raising=False)
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2, raising=False)
    _blocking_initializer(monkeypatch, markers)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _pid_and_double, list(range(8)), max_workers=2,
            maxtasksperchild=2, show_progress=False), 60)

    assert not outcome["hung"], (
        "a recycling pool whose workers cannot leave at their quota stalled "
        "(#888)")
    assert "error" not in outcome, outcome
    assert sorted(v for _, v in outcome["value"]) == [2 * i for i in range(8)]
    records = _late_leaver_records(caplog)
    assert records, [r.getMessage() for r in caplog.records]
    pool_pids = {p.pid for p in recorded_pools}
    for record in records:
        assert record.levelno == logging.WARNING
        assert _late_leaver_pids(record) <= pool_pids, record.getMessage()
        for phrase in _FORBIDDEN_PHRASES:
            assert phrase not in record.getMessage(), record.getMessage()
    assert _sigkill_records(caplog) == [], [
        r.getMessage() for r in caplog.records]
    assert _gone(recorded_pools), "a worker of the pool is still running"
    assert _no_child_left(), multiprocessing.active_children()


def test_the_exit_bound_resets_a_sigalrm_the_worker_ignores(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """R1b: a worker that had set SIGALRM to be ignored is still ended by its
    exit bound, which restores the signal's default action first (#888).

    Killing mutation: the `SIG_DFL` reset removed (N14).
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_WORKER_EXIT_GRACE_S", 0.5, raising=False)
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2, raising=False)
    initializer = functools.partial(_cannot_leave_and_ignores_sigalrm,
                                    str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _pid_and_double, list(range(4)), max_workers=2,
            maxtasksperchild=1, show_progress=False), 25)

    assert not outcome["hung"], (
        "a worker that ignores SIGALRM outlived its exit bound (#888)")
    assert sorted(v for _, v in outcome["value"]) == [0, 2, 4, 6]
    assert _late_leaver_records(caplog), [
        r.getMessage() for r in caplog.records]
    assert _sigkill_records(caplog) == [], [
        r.getMessage() for r in caplog.records]
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_worker_ended_by_its_bound_after_the_last_check_is_still_named(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """R2: the sweep after the pool's exit names a worker whose bound fired
    inside that exit, where no check runs (#888).

    One task per worker and two of each: both reach their quota as the
    stream ends, so both bounds fire while the exit waits for the workers.

    Killing mutation: the sweep deleted (N10).
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_WORKER_EXIT_GRACE_S", 0.5, raising=False)
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    _blocking_initializer(monkeypatch, markers)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _pid_and_double, [1, 2], max_workers=2, maxtasksperchild=1,
            show_progress=False), 60)

    assert not outcome["hung"], outcome
    assert sorted(v for _, v in outcome["value"]) == [2, 4]
    records = _late_leaver_records(caplog)
    assert records, [r.getMessage() for r in caplog.records]
    named = set().union(*(_late_leaver_pids(r) for r in records))
    assert named <= {p.pid for p in recorded_pools}, named
    assert _sigkill_records(caplog) == [], [
        r.getMessage() for r in caplog.records]
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_the_exit_bound_crosses_the_spawn_as_an_argument(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """R3: `_WORKER_EXIT_GRACE_S` patched in the parent is the bound the
    worker arms (#888).

    A worker that read its own module's constant would wait 15 s; the call
    is bounded at 10 s, and the parent's own kill is held off at 30 s.

    Killing mutation: the grace read in the worker from its own module (N11).
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_WORKER_EXIT_GRACE_S", 0.5, raising=False)
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 30.0)
    _blocking_initializer(monkeypatch, markers)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _pid_and_double, [1, 2], max_workers=2, maxtasksperchild=1,
            show_progress=False), 10)

    assert not outcome["hung"], (
        "the worker's exit bound was not the parent's (#888)")
    assert _late_leaver_records(caplog), [
        r.getMessage() for r in caplog.records]
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_the_exit_bound_is_never_armed_in_the_parent():
    """R5: `_arm_exit_bound` in a process that is not a pool's child arms no
    timer and leaves SIGALRM's handler alone (#888).

    A SIGALRM in the caller's own process would end their program.

    Killing mutation: the parent-process guard deleted (N12).
    """
    before = signal.getsignal(signal.SIGALRM)
    try:
        parallel._arm_exit_bound(30)
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        assert signal.getsignal(signal.SIGALRM) is before
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)


def _block_forever():
    time.sleep(3600)


def _block_at_exit_then(inner):
    """Pool initializer: register an `atexit` that never returns, then run
    `inner`. Registered first, it runs after anything `inner` registers."""
    import atexit  # pylint: disable=import-outside-toplevel
    atexit.register(_block_forever)
    inner()


def _cancel_blocks_then(inner):
    """Pool initializer: run `inner`, then make the faulthandler's cancel
    block for good, as one waiting on a dump that spins does (#844)."""
    import faulthandler  # pylint: disable=import-outside-toplevel
    inner()
    faulthandler.cancel_dump_traceback_later = _block_forever


def _watchdog_initializer(monkeypatch, wrap):
    """`ISOCENTER_WORKER_FAULTHANDLER=1`'s initializer, resolved here in the
    parent, wrapped by `wrap` and handed to every worker."""
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    monkeypatch.setenv("ISOCENTER_WORKER_FAULTHANDLER", "1")
    inner = parallel.resolve_worker_initializer()
    assert inner is not None
    initializer = functools.partial(wrap, inner)
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)


def _pool_children_killed():
    for child in multiprocessing.active_children():
        try:
            child.kill()
        except (OSError, ValueError):
            pass


def test_under_the_watchdog_a_worker_whose_exit_never_finishes_is_ended(
        monkeypatch):
    """R7: with `ISOCENTER_WORKER_FAULTHANDLER=1`, a worker whose exit waits
    for good is ended by its exit bound, and the call returns (#844).

    The 1.0.0rc4 worker's exit waited in `_PyFaulthandler_Fini` on a dump
    that spun; here an `atexit` that never returns stands in for it.
    `run_parallel`'s own process pool joined the worker at its `with` exit.

    Killing mutations: the `atexit` registration deleted (N18); the bound
    read in the worker from its own module (N19).
    """
    monkeypatch.setattr(parallel, "_WORKER_EXIT_GRACE_S", 0.5, raising=False)
    _watchdog_initializer(monkeypatch, _block_at_exit_then)
    try:
        outcome = _run_bounded(lambda: parallel.run_parallel(
            identity, [1], max_workers=1, show_progress=False), 10)
    finally:
        _pool_children_killed()

    assert not outcome["hung"], (
        "a worker whose exit never finishes held run_parallel() under "
        "ISOCENTER_WORKER_FAULTHANDLER=1 (#844)")
    assert outcome.get("value") == [1], outcome


def test_under_the_watchdog_the_bound_is_armed_before_the_dump_is_cancelled(
        monkeypatch):
    """R7b: the exit bound is armed before the pending dump is cancelled, so
    a cancel that waits for good on a spinning dump is still ended (#844).

    On `main` nothing called the Python-level cancel, so this passes there;
    it exists for the order.

    Killing mutation: the bound armed after the cancel (N17).
    """
    monkeypatch.setattr(parallel, "_WORKER_EXIT_GRACE_S", 0.5, raising=False)
    _watchdog_initializer(monkeypatch, _cancel_blocks_then)
    try:
        outcome = _run_bounded(lambda: parallel.run_parallel(
            identity, [1], max_workers=1, show_progress=False), 10)
    finally:
        _pool_children_killed()

    assert not outcome["hung"], (
        "a cancel that waits for good held the worker's exit: the bound was "
        "armed after it (#844)")
    assert outcome.get("value") == [1], outcome


# --- #250: the child-side watchdog ------------------------------------------
#
# pytest's faulthandler_timeout dumps the *parent's* threads; #250's
# occurrence-four dump showed all of them idle, because the 900-second
# lock holder was a pool child no parent-side instrumentation can see
# into. `ISOCENTER_WORKER_FAULTHANDLER=1` (set only in tests.yml) arms
# `faulthandler.dump_traceback_later(..., exit=False)` inside each
# worker process, so the next child-side stall of any mechanism delivers
# the child's own stack.


def test_the_watchdog_env_var_arms_a_picklable_initializer(monkeypatch):
    """The strategy resolves an initializer, and it must cross a spawn.

    Picklability is the load-bearing half: an initializer that cannot
    pickle kills every worker at startup, which is why the worker
    functions all live at module scope. Resolved in the parent so the
    settings travel as arguments rather than relying on the child
    re-reading anything.
    """
    import pickle

    from isocenter.parallel import _resolve_strategy

    monkeypatch.setenv("ISOCENTER_WORKER_FAULTHANDLER", "1")
    # Processes selected structurally: a free-threaded build defaults to
    # threads, whose (correct) no-initializer rule would otherwise decide
    # this test before the resolver is ever consulted.
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

    strategy = _resolve_strategy(2, 1, None, False, False, False, "t", None)
    initializer = strategy.worker_initializer
    assert initializer is not None, (
        "ISOCENTER_WORKER_FAULTHANDLER=1 resolved no worker initializer; "
        "no child ever arms its watchdog (#250)")
    pickle.dumps(initializer)
    # The worker's exit is bounded whenever the dump is armed, by the bound
    # the parent resolved (#844).
    assert initializer.keywords["exit_bound"] == parallel._WORKER_EXIT_GRACE_S

    # Threads share the parent's interpreter: arming a process-lifetime
    # watchdog there would dump the whole program's threads mid-run.
    threaded = _resolve_strategy(2, 1, None, False, True, False, "t", None)
    assert threaded.worker_initializer is None


def test_without_the_env_var_no_initializer_is_forced_on_workers(monkeypatch):
    """Production never pays for CI's instrumentation."""
    monkeypatch.delenv("ISOCENTER_WORKER_FAULTHANDLER", raising=False)
    # Processes, structurally, for the same reason as the arming test:
    # on threads the answer is None regardless of the resolver.
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

    from isocenter.parallel import _resolve_strategy

    strategy = _resolve_strategy(2, 1, None, False, False, False, "t", None)
    assert strategy.worker_initializer is None
    # Without the dump there is no exit bound to arm (#844).
    monkeypatch.setenv("ISOCENTER_DISABLE_GC", "1")
    gc_only = _resolve_strategy(2, 1, None, False, False, False, "t", None)
    assert gc_only.worker_initializer.keywords.get("exit_bound") is None


def test_a_stalled_worker_dumps_its_own_stack_and_still_finishes(
        monkeypatch, capfd):
    """The integration: a spawned child actually arms the watchdog.

    The threshold is monkeypatched in the *parent* and must reach the
    child through the initializer's own arguments -- a spawned child
    re-imports the module fresh, so a patched constant proves the
    resolution happens parent-side. Two assertions, both load-bearing:
    the dump text arrives (the watchdog fired), and the results are
    still correct (`exit=False` -- the watchdog is diagnosis, and a
    slow-but-healthy worker must finish its task, not be killed by its
    own instrumentation; "simplifying" to exit=True turns every slow
    worker into a lost task). The sleep budget is generous on purpose:
    this test must not be able to become a #250 itself.

    The task is `time.sleep` itself, and must stay a callable every child
    already has (#844). It was a function in this module, so the child
    imported the whole test module -- pytest and its dependencies, some
    hundred modules -- *after* the initializer armed the timer (isocenter
    itself was loaded before, to unpickle the initializer), and a 0.2 s
    timer could fire
    in the middle of that import. In the 1.0.0rc4 integration run the
    dump never returned: it spun in CPython's `dump_traceback`, the
    worker's exit waited for it in `_PyFaulthandler_Fini`, and the
    parent's pool shutdown waited for the worker. `time.sleep` pickles by
    reference to a module the child has loaded, so the child goes from
    arming to blocking in C after a dozen Python calls and no imports,
    and its frames hold still there, and
    the 0.5 s timer fires inside the first 1.5 s sleep. That removes the
    window this test opened; why the dump spun is not established.
    """
    import time

    from isocenter import parallel

    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    monkeypatch.setenv("ISOCENTER_WORKER_FAULTHANDLER", "1")
    monkeypatch.setattr(parallel, "_WORKER_FAULTHANDLER_TIMEOUT_S", 0.5)

    results = parallel.run_parallel(
        time.sleep, [1.5, 1.5], show_progress=False, max_workers=1)

    assert results == [None, None], (
        "the watchdog changed the run's outcome; it must only ever dump "
        "(exit=False), never kill the worker (#250)")

    stderr = capfd.readouterr().err
    assert "Timeout" in stderr and "Thread" in stderr, (
        "no traceback dump reached stderr: the spawned child never armed "
        "its watchdog, so the next CI stall is again missing the child's "
        f"half of the picture (#250). stderr was: {stderr!r}")


def test_a_lever_set_both_ways_runs_in_threads(monkeypatch):
    """The order the three levers resolve in, pinned (#331).

    `docs/environment.md` now states an order, and prose stating a
    precedence with nothing behind it is the shape this milestone is
    about -- there was no precedence test at all before this one.

    `ISOCENTER_FORCE_THREADS` wins over `ISOCENTER_FORCE_PROCESSES`
    because it is asked first; that is a real decision and not an
    accident of ordering, since the threads lever is the debugging
    escape hatch for an environment where processes do not work, and an
    escape hatch that a second variable can veto is not one.

    Worker recycling beats both, whichever way they are set: on 3.12,
    the floor, only `multiprocessing.Pool` recycles workers
    (`ProcessPoolExecutor(max_tasks_per_child=)` deadlocks `map` there at
    the first replacement). That is why
    `session.export()`, which passes `maxtasksperchild=25`, ignores both
    force variables entirely (#185).

    The second assertion below now also **emits a warning**, since #185:
    `ISOCENTER_FORCE_THREADS` is set and recycling was asked for, which
    is exactly the contradiction that arm announces. Nothing here reads
    caplog, so no assertion changes -- but the two tests interact
    through the environment, and the caplog tests for that warning set
    their own rather than inheriting this one's.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

    assert _threads_chosen(False, None) is True, (
        "ISOCENTER_FORCE_PROCESSES overrode ISOCENTER_FORCE_THREADS; "
        "docs/environment.md says the reverse")
    assert _threads_chosen(False, 25, "the maxtasksperchild argument") is False, (
        "worker recycling was asked for and threads were chosen anyway; "
        "on 3.12 only multiprocessing.Pool recycles workers")


def test_only_the_literal_one_switches_a_flag_on(monkeypatch):
    """`true`, `yes` and `on` do nothing (#331).

    `_env_is` lowercases and compares against the literal `"1"`. That is
    documented now, and it is the kind of claim that is cheap to state
    and expensive to discover wrong: a user who writes `=true` gets the
    default back with no warning anywhere, unlike a malformed integer,
    which `_env_int` reports.

    Asserted through the processes lever rather than the threads one, so
    the expected answer does not depend on whether the interpreter
    running the test is free-threaded.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "true")
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

    assert parallel._env_is("ISOCENTER_FORCE_THREADS", ("1",)) is False, (
        "'true' switched ISOCENTER_FORCE_THREADS on; the table in "
        "docs/environment.md says only the literal 1 counts")
    assert _threads_chosen(False, None) is False, (
        "and so the processes lever, which is set to the literal 1, is "
        "the one that decides")


def test_the_default_worker_count_is_one_per_cpu(monkeypatch):
    """The number `docs/environment.md`'s default cell is written from (#333).

    **This test is green on the code it was written against, and that is
    the finding.** The table said the default was `CPU_COUNT * 1.5` for
    the life of that row. `_resolve_strategy` has never computed that,
    and the comment beside the expression records the 1.5x as an earlier
    version's behaviour that was dropped on purpose -- predictable beats
    marginally faster when a run is hours long. So the defect was
    entirely in the prose and the fix is entirely in the prose. What was
    missing was anything holding the number still, which is why this is a
    characterization test rather than a red one: the convention the two
    tests above already use for a claim the docs make and the code was
    already keeping (both #331). Asserting on the wording instead would
    pin a spelling rather than a behaviour, and forbidding the string
    "1.5" would not fire on `1.5x` or `150%`.

    Asserted on `_resolve_strategy` rather than through `run_parallel`
    with a mocked executor, because the claim is about the *number*, not
    about which pool receives it -- the pool question has its own tests
    two rows up.

    The negative half is not decoration, and it is guarded rather than
    unconditional: `os.cpu_count()` and `int(cpu_count * 1.5)` are equal
    when `cpu_count()` is 0 or 1, so on a one-CPU runner an unguarded
    negative assertion would be red while the code was right, and a
    silently-satisfied one would say nothing about the reading it exists
    to rule out.
    """
    monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)

    strategy = parallel._resolve_strategy(
        None, 1, None, False, False, False, "", None)

    cpus = os.cpu_count() or 1
    assert strategy.max_workers == cpus, (
        "the default worker count is no longer one per CPU, so "
        "docs/environment.md's default cell has to be rewritten with it "
        f"-- it is written from this call (#333). Got {strategy.max_workers} "
        f"for {cpus} CPUs")
    if cpus > 1:
        assert strategy.max_workers != int(cpus * 1.5), (
            "the 1.5x an earlier version used is back; it was abandoned "
            "on purpose (the comment beside the expression says why) and "
            "the docs spent the life of that row claiming it was still "
            "in force (#333)")


def test_a_zero_worker_count_is_reported_like_a_malformed_one(
        monkeypatch, caplog):
    """`ISOCENTER_MAX_WORKERS=0` is a typo, and it was read as no value (#335).

    `_resolve_strategy` settled the count with
    `_env_int(...) or (os.cpu_count() or 1)`, and `or` cannot tell an
    unset variable from a set-but-falsey one. So `0` -- the one integer a
    worker pool can never honour -- was the one integer that vanished
    without a word, while `banana` two tests up was reported.

    **The count itself was already right, and that is the point.** The
    first assertion below passes on both sides of the fix; the second is
    the red one. An operator who set `0` got a run at CPU width with
    nothing anywhere saying their setting was discarded, which is exactly
    the symptom `_env_int`'s docstring says the malformed-value warning
    exists to prevent.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "0")

    with caplog.at_level(logging.WARNING):
        strategy = parallel._resolve_strategy(
            None, 1, None, False, False, False, "", None)

    assert strategy.max_workers == (os.cpu_count() or 1), (
        "a rejected worker count must fall back to the documented "
        "default, not to some third number")
    assert any("ISOCENTER_MAX_WORKERS" in record.message
               for record in caplog.records), (
        "0 was discarded without a word; a malformed value is reported "
        "and the one value a pool can never honour was not")
    assert any("0" in record.message for record in caplog.records), (
        "the warning does not name the value that was rejected, so it "
        "cannot be matched against what the operator typed")


def test_a_negative_worker_count_is_reported_rather_than_carried_to_the_pool(
        monkeypatch, caplog):
    """A negative count reached the pool constructor and raised there (#335).

    Worse than the zero case above: `_env_int` returns `-1`, `-1` is
    truthy, so `or` passed it straight through and the strategy carried
    it into `ThreadPoolExecutor`/`ProcessPoolExecutor` (`ValueError:
    max_workers must be greater than 0`) or, on the recycling path, into
    `multiprocessing.Pool` (`ValueError: Number of processes must be at
    least 1`). Neither message names an environment variable, so the
    traceback pointed at isocenter's own call and not at the setting that
    caused it.

    Both assertions are red before the fix: the value is wrong *and* the
    channel is silent.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "-1")

    with caplog.at_level(logging.WARNING):
        strategy = parallel._resolve_strategy(
            None, 1, None, False, False, False, "", None)

    assert strategy.max_workers == (os.cpu_count() or 1), (
        "a negative worker count is still being carried to the pool "
        "constructor, which raises a ValueError naming no environment "
        "variable")
    assert any("ISOCENTER_MAX_WORKERS" in record.message
               for record in caplog.records), (
        "the negative value was rejected in silence")
    assert any("-1" in record.message for record in caplog.records), (
        "the warning does not name the value that was rejected")


def test_an_explicit_zero_worker_count_is_not_an_environment_typo(monkeypatch):
    """The guard stays under `if max_workers is None` (#335).

    `run_parallel(..., max_workers=0)` is a programming error in the
    caller's own source, not a misconfigured deployment: the caller can
    see the literal, and silently rewriting it to the CPU count would
    hide a bug in a line they wrote. A warning on a log line nobody is
    reading is the right channel for an environment variable and the
    wrong one for an argument, so the explicit value keeps travelling to
    the pool and keeps raising there.
    """
    monkeypatch.delenv("ISOCENTER_MAX_WORKERS", raising=False)

    strategy = parallel._resolve_strategy(
        0, 1, None, False, False, False, "", None)

    assert strategy.max_workers == 0, (
        "an explicit max_workers=0 was rewritten to the default; the "
        "environment guard has escaped its `if max_workers is None` "
        "arm and is now swallowing a caller's own bug")


# --------------------------------------------------------------------
# Worker recycling versus a forced thread request (#185)
# --------------------------------------------------------------------
#
# `session.export()` passes `maxtasksperchild=25`, and worker recycling
# rules threads out however the rest of the environment is set -- on
# 3.12, the floor, only `multiprocessing.Pool` recycles workers without
# deadlocking. So the export path runs in
# processes on every interpreter, including a free-threaded build, and
# `ISOCENTER_FORCE_THREADS` cannot change that. That is a decision (the
# recycling reclaims memory leaked by the imaging C libraries, and a
# thread pool has no process to recycle), and until now it was a silent
# one: the request was dropped with nothing anywhere saying so.


def test_which_thread_lever_lost_to_worker_recycling_is_recorded(
        monkeypatch, caplog):
    """The attribution half of #185: *which* lever was overridden.

    Both levers reach the same arm and both are asserted: the
    environment variable, which is what `docs/environment.md` documents
    as the way to force threads, and the `force_threads=True` argument,
    which is reachable only from user code -- the one in-library
    `force_threads=True` (the discovery scan) passes no
    `maxtasksperchild` and so never contradicts anything.

    This used to assert the *message* too, because the resolver was
    where the message came from. Since #400 the resolver is silent and
    the warning is emitted at dispatch, so this test states the fact and
    `test_the_warning_at_dispatch_names_the_lever_and_quotes_the_value`
    states the speech. One assertion about one thing each, where there
    was one about two -- and the resolver's silence is its own test
    (`test_resolving_a_strategy_emits_nothing`), because a
    dispatch-level test cannot tell a warning that was never emitted
    from one emitted twice and deduplicated.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    with caplog.at_level(logging.WARNING):
        choice = parallel._resolve_execution_choice(
            False, 25, "the maxtasksperchild argument")

    assert choice.use_threads is False, (
        "recycling must still win; this test is about the report, "
        "not about the decision")
    assert choice.threads_request_overridden_by == "ISOCENTER_FORCE_THREADS", (
        "the request was overridden without recording the lever that was "
        "ignored, so nothing downstream can tell an operator why threads "
        f"did not happen; got {choice.threads_request_overridden_by!r}")

    monkeypatch.delenv("ISOCENTER_FORCE_THREADS")
    argument = parallel._resolve_execution_choice(
        True, 25, "the maxtasksperchild argument")
    assert argument.threads_request_overridden_by == "force_threads=True", (
        "an explicit force_threads=True argument was overridden with no "
        "record of it; the caller can see their own literal, but not that "
        f"it lost to a value passed somewhere else; got "
        f"{argument.threads_request_overridden_by!r}")


def test_the_warning_at_dispatch_names_the_lever_and_quotes_the_value(
        monkeypatch, caplog):
    """The speech half of #185, at the point of use (#400).

    The message has to name **both** levers and quote the recycling
    value: a warning that says only "there is a conflict" cannot be
    matched against what the operator typed, which is the same argument
    `_env_int`'s and `ISOCENTER_MAX_WORKERS`' warnings already make.

    A single record is selected before anything is asserted about its
    text, and the count is asserted first. One real line of this
    library's output names `threads`, `processes` and two of the three
    variables at once, so an `any(... in caplog.text)` over the whole log
    is satisfied by prose from somewhere else.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    with caplog.at_level(logging.WARNING):
        parallel.run_parallel(identity, [1], max_workers=1,
                              maxtasksperchild=25, show_progress=False)

    selected = [record.getMessage() for record in caplog.records
                if "maxtasksperchild" in record.getMessage()]
    assert len(selected) == 1, (
        f"expected exactly one override warning per dispatch, got "
        f"{len(selected)}: {selected}")
    assert "ISOCENTER_FORCE_THREADS" in selected[0], (
        "the warning does not name the lever that was ignored")
    assert "maxtasksperchild=25" in selected[0], (
        "the warning does not name what overrode the request, with the "
        "value that won")


def test_no_warning_when_recycling_was_not_contradicted(caplog, monkeypatch):
    """The silence half, and it is the ordinary export path.

    `session.export()` dispatches exactly this -- `force_threads=False`,
    no environment variable, `maxtasksperchild=25` -- on every export.
    Nobody asked for threads, so nothing was overridden, and a warning
    on every export would be noise that teaches readers to filter this
    logger.

    Asserted through `run_parallel` rather than through the resolver
    since #400 moved the emission to dispatch. Through the resolver this
    would now be silent for the wrong reason -- the resolver is silent on
    *every* row -- which is the `0 == 0` shape: a test that grades
    nothing because its subject was removed from the code it calls.

    The `delenv` is not decoration. The ranking reads
    `ISOCENTER_FORCE_THREADS` from the ambient environment, so without
    it this test asserts silence about whatever the operator happens to
    have exported -- and would go green inside a full pytest run purely
    because the sibling test's `setenv` is undone after it. A test whose
    subject is "no warning" must own every input that could produce one.
    """
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)

    with caplog.at_level(logging.WARNING):
        parallel.run_parallel(identity, [1], max_workers=1,
                              maxtasksperchild=25, show_progress=False)

    assert not [record for record in caplog.records
                if "maxtasksperchild" in record.getMessage()], (
        "the ordinary export path warns; nothing was contradicted")


def test_export_runs_in_processes_by_decision(monkeypatch, caplog):
    """A characterization test for a prose change (#185).

    `docs/environment.md` and `_run_export_batch`'s docstring now say
    that `session.export()` runs in processes on every interpreter,
    including free-threaded builds, and why: workers are recycled every
    25 tasks so memory leaked by the imaging C libraries is reclaimed,
    and a thread pool has no process to recycle. A prose change admits
    no red, so this pins the code the prose describes rather than the
    prose itself -- grepping the document for the sentence would pin the
    wording and pass just as happily if the wording were wrong.

    What it holds still: the `25`. If it ever becomes `None`, the export
    path takes threads on a free-threaded build, and eight test files
    assume a subprocess boundary and must be revisited before that
    lands --
    `tests/test_private_tag_vr_roundtrip.py`,
    `tests/test_redaction_failure_is_reported.py`,
    `tests/test_float_pixel_data_export.py`,
    `tests/test_export_worker_graph_purity.py`,
    `tests/test_redaction_identity.py`,
    `tests/test_redaction_attestation.py`,
    `tests/test_redaction_multizone.py` and this file. All eight are
    collected by the full suite. (A ninth, `tests/profile_memory.py`, was listed here for
    its assumption rather than its protection until #347 deleted it: it
    was neither collected nor importable, and its own assertion had
    drifted to `10` against the shipped `25` with nothing noticing.)
    """
    from types import SimpleNamespace

    from isocenter import session as session_module

    captured = {}

    def fake_export_batch(tasks, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(written=0, failures=[])

    monkeypatch.setattr(session_module.DicomExporter, "export_batch",
                        staticmethod(fake_export_batch))

    session_module.DicomSession._run_export_batch([], show_progress=False)

    assert captured["maxtasksperchild"] == 25, (
        "the export path stopped asking for worker recycling; that is a "
        "decision with a memory argument behind it and eight test files "
        "resting on the process boundary it creates (#185)")

    # The override warning repeats this number as a literal sentence --
    # "session.export() always sets maxtasksperchild=25" -- and nothing
    # reads it from here, so it can drift exactly the way an uncollected
    # `tests/profile_memory.py`, deleted in #347, had drifted to `10`
    # from this same `25`. Tying the two together is the whole point of
    # capturing the kwarg: the shipped log line must quote what the
    # shipped call passes. Elicited through `run_parallel`, because
    # since #400 the warning is emitted at dispatch rather than while
    # the ranking is resolved.
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    with caplog.at_level(logging.WARNING):
        parallel.run_parallel(identity, [1], max_workers=1, force_threads=True,
                              maxtasksperchild=captured["maxtasksperchild"],
                              show_progress=False)

    sentence = "session.export() always sets maxtasksperchild=%s" % (
        captured["maxtasksperchild"],)
    assert any(sentence in record.getMessage() for record in caplog.records), (
        "the override warning tells operators session.export() sets a "
        "number that is not the one it sets; the message is prose in "
        "shipped output and this is the only thing reading it against "
        "the call (#347's defect class)")


def test_a_zero_tasks_per_child_is_reported_rather_than_raising_in_the_pool(
        monkeypatch, caplog):
    """`ISOCENTER_MAX_TASKS_PER_CHILD=0` is the mirror of #335's defect.

    `_env_int` returns `0` -- the `if not raw` guard sees the non-empty
    string `"0"` -- and `0 is not None`, so it turned threads off on
    every call site except export and then reached
    `multiprocessing.Pool`, which raises `ValueError: maxtasksperchild
    must be a positive int or None` naming no environment variable. The
    new override warning made it noisier rather than better: it warned
    about a conflict and then crashed.

    Both assertions matter. The value must fall back to the documented
    *Unlimited*, which also re-enables the free-threaded threads path
    that a `0` was silently switching off; and the rejection must name
    the variable and the value, because a message that quotes neither
    cannot be matched against what was typed.
    """
    monkeypatch.setenv("ISOCENTER_MAX_TASKS_PER_CHILD", "0")

    with caplog.at_level(logging.WARNING):
        strategy = parallel._resolve_strategy(
            1, 1, None, False, False, False, "", None)

    assert strategy.maxtasksperchild is None, (
        "0 was carried to multiprocessing.Pool, which raises ValueError: "
        "maxtasksperchild must be a positive int or None")
    assert any("ISOCENTER_MAX_TASKS_PER_CHILD" in record.message
               for record in caplog.records), (
        "the value was rejected without naming the variable")
    assert any("set to 0" in record.message for record in caplog.records), (
        "the warning does not quote the value that was rejected")


def test_a_negative_tasks_per_child_is_rejected_in_the_same_arm(
        monkeypatch, caplog):
    """`0` and every negative in one arm, as #335 argued for its own value.

    Neither is a recycling interval, `multiprocessing.Pool` raises on
    both with the same message, and an operator who typed either made
    the same mistake. Two behaviours for two wrong values would be two
    things to remember.
    """
    monkeypatch.setenv("ISOCENTER_MAX_TASKS_PER_CHILD", "-5")

    with caplog.at_level(logging.WARNING):
        strategy = parallel._resolve_strategy(
            1, 1, None, False, False, False, "", None)

    assert strategy.maxtasksperchild is None
    assert any("-5" in record.message for record in caplog.records)


def test_an_explicit_zero_tasks_per_child_argument_still_reaches_the_pool(
        monkeypatch):
    """The guard stays under `if maxtasksperchild is None`, as #335's does.

    `run_parallel(..., maxtasksperchild=0)` is a programming error in a
    line the caller can see, not a misconfigured deployment. Rewriting
    it to *Unlimited* would hide their bug on a log line nobody is
    reading, so the argument still travels to the pool and still raises
    there.
    """
    strategy = parallel._resolve_strategy(
        1, 1, 0, False, False, False, "", None)

    assert strategy.maxtasksperchild == 0


# --------------------------------------------------------------------
# One floor for every integer tuning variable (#341)
# --------------------------------------------------------------------
#
# #335 rejected `ISOCENTER_MAX_WORKERS` below 1 at its call site; #185
# copied the arm for `ISOCENTER_MAX_TASKS_PER_CHILD`; `ISOCENTER_CHUNKSIZE`
# kept the `or` both of them had removed, so `0` was discarded in silence
# and `-1` travelled to the map call. Three variables, one helper, three
# answers to "what does 0 mean". The floor now lives in `_env_int` itself,
# as a keyword-only `minimum` every read site has to state, and the tests
# below hold all three variables to one answer at once.

_TUNING_INTEGERS = {
    # variable: (the `_Strategy` field it settles, its documented default)
    "ISOCENTER_MAX_WORKERS": ("max_workers", lambda: os.cpu_count() or 1),
    "ISOCENTER_CHUNKSIZE": ("chunksize", lambda: 1),
    "ISOCENTER_MAX_TASKS_PER_CHILD": ("maxtasksperchild", lambda: None),
}


@pytest.mark.parametrize(
    "value", [None, "1", "2", "0", "-3", "banana"],
    ids=lambda value: "unset" if value is None else value)
@pytest.mark.parametrize(
    "name", sorted(_TUNING_INTEGERS),
    ids=lambda name: name.removeprefix("ISOCENTER_"))
def test_a_tuning_integer_below_its_floor_is_reported_and_replaced_by_the_default(
        name, value, monkeypatch, caplog):
    """`0` and every negative are reported and replaced, for all three (#341).

    **Red on exactly two of the eighteen cells when written:
    `CHUNKSIZE-0` (the value is right by accident and nothing is said)
    and `CHUNKSIZE--3` (`-3` is carried into the strategy, and nothing
    is said).** The other sixteen are green on the tree they were
    written against, and they are here on purpose: `ISOCENTER_MAX_WORKERS`
    and `ISOCENTER_MAX_TASKS_PER_CHILD` were already fixed, one call site
    each (#335, #185), and moving the floor into the shared helper is a
    change to both of them as well. A matrix that holds all three to the
    same answer is what stops the helper from regressing one variable
    while it fixes the third.

    Three cells carry weight that is easy to miss. The `1` cells are the
    boundary -- the floor itself must be accepted without a word, which
    is the only thing that tells `value < minimum` from `value <=
    minimum`; for `ISOCENTER_CHUNKSIZE` the value `1` is also the default,
    so its `1` cell is decided entirely by the silence assertion. The
    "no warning" cells filter `caplog` to records that **name the
    variable** rather than asserting the log is empty: an unrelated
    warning would otherwise turn a healthy cell red and bury the one
    that matters. The below-floor cells assert `set to <value>` and not a
    bare `"0" in message`, which almost any message satisfies.
    """
    field, default = _TUNING_INTEGERS[name]
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)

    with caplog.at_level(logging.WARNING):
        strategy = parallel._resolve_strategy(
            None, 1, None, False, False, False, "", None)

    actual = getattr(strategy, field)
    naming = [record.message for record in caplog.records
              if name in record.message]

    if value is None:
        assert actual == default(), (
            f"with {name} unset, {field} should be the documented "
            f"default {default()!r}, got {actual!r}")
        assert not naming, (
            f"{name} is unset and something warned about it: {naming}")
    elif value in ("1", "2"):
        assert actual == int(value), (
            f"{name}={value} is a usable value and must be honoured; "
            f"got {actual!r}")
        assert not naming, (
            f"{name}={value} is on or above the floor and was reported "
            f"anyway -- the comparison has become `<=`: {naming}")
    elif value == "banana":
        assert actual == default(), (
            f"a malformed {name} must fall back to the documented "
            f"default {default()!r}, got {actual!r}")
        assert any("'banana'" in message for message in naming), (
            f"a malformed {name} was not reported quoting the value: "
            f"{naming}")
    else:
        assert actual == default(), (
            f"{name}={value} is below the floor and must fall back to "
            f"the documented default {default()!r}, not be honoured or "
            f"carried to the pool; got {actual!r}")
        assert any(f"set to {value}" in message for message in naming), (
            f"{name}={value} was rejected without a warning naming the "
            f"variable and quoting the value (#341): {naming}")


def test_env_int_returns_the_floor_itself_and_rejects_one_below_it(
        monkeypatch, caplog):
    """The floor is inclusive, and a rejection returns `None`, not the floor.

    Red first on `TypeError`: `_env_int` had no `minimum` parameter.

    The floor is 5 and not 1 on purpose. Every documented default in the
    matrix above collapses to `1` on a one-CPU runner, so on such a box
    a mutant that returns `minimum` instead of `None` on rejection is
    indistinguishable from the correct helper everywhere but here.
    """
    monkeypatch.setenv("ISOCENTER_ZZ_TEST", "5")
    with caplog.at_level(logging.WARNING):
        assert parallel._env_int("ISOCENTER_ZZ_TEST", minimum=5) == 5, (
            "the floor itself is a usable value and must be returned")
    assert not [record for record in caplog.records
                if "ISOCENTER_ZZ_TEST" in record.message], (
        "a value on the floor was reported; the comparison is `<=`")

    caplog.clear()
    monkeypatch.setenv("ISOCENTER_ZZ_TEST", "4")
    with caplog.at_level(logging.WARNING):
        assert parallel._env_int("ISOCENTER_ZZ_TEST", minimum=5) is None, (
            "a value below the floor must come back as None -- the "
            "caller's `is not None` is the only test it should need -- "
            "not as the floor and not as the value")
    messages = [record.message for record in caplog.records
                if "ISOCENTER_ZZ_TEST" in record.message]
    assert any("set to 4" in message and "minimum of 5" in message
               for message in messages), (
        "the rejection must name the variable, the value and the floor, "
        f"so it can be matched against what was typed: {messages}")


def test_env_int_without_a_floor_returns_zero(monkeypatch, caplog):
    """`minimum=None` means no floor, so `0` is a value like any other.

    Red first on `TypeError`. This pins that the floor is stated per
    read site and is not a `< 1` hard-coded inside the helper: a future
    integer variable for which `0` is legitimate must be able to say so,
    and the keyword is how it says it.
    """
    monkeypatch.setenv("ISOCENTER_ZZ_TEST", "0")
    with caplog.at_level(logging.WARNING):
        assert parallel._env_int("ISOCENTER_ZZ_TEST", minimum=None) == 0
    assert not [record for record in caplog.records
                if "ISOCENTER_ZZ_TEST" in record.message]


# --- The threads-or-processes decision, and who asked for what (#384, #400) --
#
# `_resolve_execution_choice` returns three facts where `_use_threads`
# returned one. The two extra fields exist because `session.py` cannot
# re-derive them: an attribution computed from the resolved
# `use_threads` is impossible on `redact()`'s `:memory:` path, where
# `use_threads` is always True and the operator's request has already
# been silently dropped.


def _pid_of_worker(_):
    """Module scope so the processes path can pickle it, if it takes one."""
    return os.getpid()


def test_the_choice_records_which_lever_asked_for_processes(monkeypatch):
    """The whole four-field `_Choice`, per row of the lever matrix (#400).

    The single mutation the refusal and the warning both rest on is an
    attribution computed *after* the `force_threads` short-circuit: the
    row "processes lever set, threads argument passed" is the one that
    carries `"ISOCENTER_FORCE_PROCESSES"` while `use_threads` is True,
    and it is exactly the row a short-circuited attribution turns into
    `None`. That row is `redact()` on a `:memory:` store, so an
    attribution that loses it makes the warning unreachable and every
    session-level test green for the wrong reason.

    The whole tuple is asserted per row rather than one field at a time:
    `threads_request_overridden_by` must be `None` on every row except
    the two where recycling beat a threads request, and a per-field
    assertion cannot say "and nothing else changed".

    `sys._is_gil_enabled` is patched to True for the rows that reach the
    free-threaded default, so the table reads the same on 3.12.14 and
    3.14.7t. Without that patch the first row is `(True, None, None)` on
    the gate's free-threaded leg and this test is red there for a reason
    that has nothing to do with attribution.
    """
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
    force_processes = "ISOCENTER_FORCE_PROCESSES"
    max_tasks = "ISOCENTER_MAX_TASKS_PER_CHILD"

    # (env, force_threads, maxtasksperchild, recycling_lever) ->
    # (use_threads, processes_requested_by, threads_request_overridden_by,
    #  threads_requested_by -- #393's field, which has its own table in
    #  `test_threads_requested_by_names_only_a_lever_that_asked_and_won`)
    rows = [
        ({}, False, None, None,
         (False, None, None, None),
         "nothing set: rank 4 is a default, and a default is not a request"),
        ({}, True, None, None,
         (True, None, None, "force_threads=True"),
         "the force_threads argument alone denies nobody"),
        ({force_processes: "1"}, False, None, None,
         (False, force_processes, None, None),
         "the processes lever was set and got what it asked for"),
        ({force_processes: "1"}, True, None, None,
         (True, force_processes, None, "force_threads=True"),
         "the processes lever was set and the argument beat it -- the "
         "request must survive the short-circuit, or redact() on a "
         ":memory: store can never report it"),
        ({"ISOCENTER_FORCE_THREADS": "1", force_processes: "1"}, False, None,
         None,
         (True, None, None, "ISOCENTER_FORCE_THREADS"),
         "the operator's own threads lever supersedes their processes "
         "lever by the documented order, so nothing was denied"),
        ({max_tasks: "2"}, False, 2, max_tasks,
         (False, max_tasks, None, None),
         "recycling asked for processes and nobody asked for threads"),
        ({max_tasks: "2"}, True, 2, max_tasks,
         (False, max_tasks, "force_threads=True", None),
         "recycling beat the force_threads argument"),
        ({"ISOCENTER_FORCE_THREADS": "1", max_tasks: "2"}, False, 2, max_tasks,
         (False, max_tasks, "ISOCENTER_FORCE_THREADS", None),
         "recycling beat the threads variable, which is the lever the "
         "#185 warning must name"),
        ({}, False, 25, "the maxtasksperchild argument",
         (False, "the maxtasksperchild argument", None, None),
         "session.export()'s own call: the argument asked, not a variable"),
    ]

    for env, force_threads, maxtasks, lever, expected, why in rows:
        for name in ("ISOCENTER_FORCE_THREADS", force_processes, max_tasks):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        choice = parallel._resolve_execution_choice(force_threads, maxtasks,
                                                    lever)
        assert tuple(choice) == expected, (
            f"{env}, force_threads={force_threads}, "
            f"maxtasksperchild={maxtasks}: {why}; got {tuple(choice)}")


def test_threads_requested_by_names_only_a_lever_that_asked_and_won(
        monkeypatch):
    """Who asked for threads *and got them* (#393).

    `ingest()` warns that `ISOCENTER_FORCE_THREADS` had no effect on it,
    so it needs to know that the variable asked -- not merely that the
    strategy resolved to threads. On a free-threaded build with nothing
    set, `use_threads` is True and nobody asked: a field derived from
    `use_threads` after the fact would make every free-threaded
    `ingest()` warn about a lever the operator never touched, which is
    the row that patches `_is_gil_enabled` to False below. And when
    recycling beat the request, the request lost,
    `threads_request_overridden_by` already says so, and #185's warning
    is the one line; naming the lever here too would make `ingest()`
    say it twice.
    """
    force_threads = "ISOCENTER_FORCE_THREADS"
    force_processes = "ISOCENTER_FORCE_PROCESSES"
    max_tasks = "ISOCENTER_MAX_TASKS_PER_CHILD"

    # (env, force_threads, maxtasksperchild, lever, gil) ->
    # (use_threads, threads_requested_by, threads_request_overridden_by)
    rows = [
        ({force_threads: "1"}, False, None, None, True,
         (True, force_threads, None),
         "the variable asked and threads won"),
        ({}, True, None, None, True,
         (True, "force_threads=True", None),
         "the argument asked and threads won"),
        ({force_threads: "1"}, True, None, None, True,
         (True, force_threads, None),
         "both asked: the variable wins the name, as it does for "
         "threads_request_overridden_by"),
        ({force_threads: "1", max_tasks: "2"}, False, 2, max_tasks, True,
         (False, None, force_threads),
         "recycling beat the request, so nothing was granted and #185 "
         "is the one line"),
        ({}, False, None, None, False,
         (True, None, None),
         "the free-threaded default is not a request"),
        ({force_processes: "1"}, False, None, None, False,
         (False, None, None),
         "the processes lever asked for processes and got them"),
    ]

    for env, force, maxtasks, lever, gil, expected, why in rows:
        for name in (force_threads, force_processes, max_tasks):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(sys, "_is_gil_enabled", lambda gil=gil: gil,
                            raising=False)
        choice = parallel._resolve_execution_choice(force, maxtasks, lever)
        got = (choice.use_threads, choice.threads_requested_by,
               choice.threads_request_overridden_by)
        assert got == expected, (
            f"{env}, force_threads={force}, maxtasksperchild={maxtasks}, "
            f"GIL={gil}: {why}; got {got}")

    # And `_resolve_strategy` carries it to the callers that read it.
    for name in (force_processes, max_tasks):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(force_threads, "1")
    strategy = parallel._resolve_strategy(
        None, 1, None, False, False, False, "x", None)
    assert strategy.threads_requested_by == force_threads


def test_resolving_a_strategy_emits_nothing(monkeypatch, caplog):
    """The resolver is a pure function; it does not speak (#400).

    This is the half of the warning's move to dispatch that a
    session-level test cannot see: a session-level test cannot tell a
    warning that was never emitted from one emitted and then filtered.
    Resolving has to be silent because `redact()` now resolves a
    strategy *before* deciding whether to run at all -- and the
    combination it refuses is exactly the one whose resolution used to
    log "so this run uses processes", one line above a refusal of a run
    that never starts.

    Killing edit: the `get_logger().warning(...)` left inside
    `_resolve_execution_choice`. Every row of the matrix is passed,
    including the recycling-beats-threads rows, which are the only ones
    that ever had anything to say.
    """
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
    rows = [
        ({}, False, None, None),
        ({"ISOCENTER_FORCE_PROCESSES": "1"}, True, None, None),
        ({"ISOCENTER_FORCE_THREADS": "1"}, False, 2,
         "ISOCENTER_MAX_TASKS_PER_CHILD"),
        ({}, True, 25, "the maxtasksperchild argument"),
    ]
    with caplog.at_level(logging.WARNING):
        for env, force_threads, maxtasks, lever in rows:
            for name in ("ISOCENTER_FORCE_THREADS",
                         "ISOCENTER_FORCE_PROCESSES",
                         "ISOCENTER_MAX_TASKS_PER_CHILD"):
                monkeypatch.delenv(name, raising=False)
            for name, value in env.items():
                monkeypatch.setenv(name, value)
            parallel._resolve_execution_choice(force_threads, maxtasks, lever)

    assert [record.getMessage() for record in caplog.records
            if record.levelname == "WARNING"] == [], (
        "resolving a strategy warned; the #185 warning belongs at "
        "dispatch, where the strategy is actually used, so a strategy "
        "that is resolved and then refused says nothing")


def test_a_pre_resolved_strategy_is_used_as_given(monkeypatch):
    """`strategy=` is obeyed, not re-resolved (#384).

    The point of `redact()` resolving once and handing the object over is
    that the line it printed and the pool it got are readings of one
    decision. A `run_parallel` that resolved a second time would reopen
    the gap: the environment says processes here and the pre-resolved
    strategy says threads, so a re-resolution runs in a subprocess and
    the child's pid differs from this one's.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    strategy = parallel._resolve_strategy(
        max_workers=2, chunksize=1, maxtasksperchild=None, disable_gc=False,
        force_threads=True, show_progress=False, desc="Pinned", total=None)
    assert strategy.use_threads is True, (
        "the force_threads argument must beat ISOCENTER_FORCE_PROCESSES; "
        "this test has no subject otherwise")

    results = parallel.run_parallel(_pid_of_worker, [None], strategy=strategy)

    assert results == [os.getpid()], (
        "run_parallel re-resolved the strategy from the environment and "
        "ran in a subprocess; a strategy handed in is the decision")


def test_a_handed_in_strategy_still_announces_its_override(monkeypatch,
                                                           caplog):
    """The second entry point warns too (#185, #400).

    `test_the_warning_at_dispatch_names_the_lever_and_quotes_the_value`
    covers a strategy `run_parallel` resolved for itself. This covers one
    handed in as `strategy=`, which is the path `redact()` takes: the
    warning is keyed on the strategy object, not on the environment, so
    both entry points reach it and neither reads a variable a second
    time.

    `ISOCENTER_FORCE_THREADS` is deliberately *not* set, so the lever
    the message must name is `force_threads=True` -- an assertion the
    ambient environment cannot satisfy by accident.
    """
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    strategy = parallel._resolve_strategy(
        max_workers=1, chunksize=1, maxtasksperchild=25, disable_gc=False,
        force_threads=True, show_progress=False, desc="Handed", total=None)

    with caplog.at_level(logging.WARNING):
        parallel.run_parallel(identity, [1], strategy=strategy)

    handed = [record.getMessage() for record in caplog.records
              if "maxtasksperchild" in record.getMessage()]
    assert len(handed) == 1, (
        f"a strategy handed in as strategy= must warn at dispatch too; "
        f"got {len(handed)}: {handed}")
    assert "force_threads=True" in handed[0], (
        "the warning must name the lever that actually lost -- here the "
        "argument, since ISOCENTER_FORCE_THREADS is not what was set")
    assert "ISOCENTER_FORCE_THREADS was set" not in handed[0], (
        "the warning named a variable the operator did not set")

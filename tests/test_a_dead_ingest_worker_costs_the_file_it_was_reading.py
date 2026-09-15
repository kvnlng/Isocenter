"""A worker process that dies during `ingest()` costs the file it was reading (#654).

When an ingest worker process ended outright -- the out-of-memory killer on
one large file, a decoder crash, `SIGKILL` -- `ingest()` raised
`BrokenProcessPool`. The files already linked were never saved and their
frames stayed in the sidecar with nothing referencing them; every file
behind the dead one was lost with no row; and the session's shared pool
stayed broken, so every later `ingest()` on that session raised at once
until `close()`.

Now the results already returned are kept, the files not yet returned are
read one at a time on a fresh spawned pool until the worker ends again, and
the file it ends on is rejected with an `ERROR` row. The rest are read at
full width again, `ingest()` saves, and the shared pool is replaced.

**The lever.** A death cannot be injected with a parent-side monkeypatch:
a spawned child imports `ingest_worker` afresh. `_die_reading` is a pool
initializer, at module scope so the child imports it by name, that
replaces `pydicom.dcmread` inside the child with one that ends the process
for a path containing `POISON`. `_poison_pools` hands it to the session's
pool and to the retry pools through `resolve_worker_initializer`, so every
pool this code builds is poisoned the same way.

**What no test here asserts: how many results arrive before the death.** At
more than one worker that is scheduling -- measured on the same shape, 4
files linked before the raise on 3.12 and 1 on 3.14t. Only the outcome after
the retry is deterministic, and that is what is asserted.
"""
import concurrent.futures
import contextlib
import functools
import logging
import multiprocessing
import os
import signal
import sqlite3
from concurrent.futures.process import BrokenProcessPool

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.uid import generate_uid

import isocenter.io_handlers as io_handlers
import isocenter.parallel as parallel
import isocenter.session as session_module
from isocenter.io_handlers import DicomImporter
from isocenter.session import DicomSession
from isocenter.store import DicomStore


def _die_reading(marker, how, once_marker=None):
    """Pool initializer: reading a path containing `marker` ends the worker.

    `once_marker` makes the death happen only the first time, across
    processes -- the file exists once a worker has died -- so that it does
    not recur when the file is read again.
    """
    real = pydicom.dcmread

    def dcmread(fp, *args, **kwargs):
        if marker in str(fp):
            if once_marker is None or not os.path.exists(once_marker):
                if once_marker is not None:
                    with open(once_marker, "w", encoding="utf-8"):
                        pass
                if how == "sigkill":
                    os.kill(os.getpid(), signal.SIGKILL)
                os._exit(13)  # pylint: disable=protected-access
        return real(fp, *args, **kwargs)
    pydicom.dcmread = dcmread


def _die_at_start():
    """Pool initializer: every worker ends before it runs a task."""
    os._exit(13)  # pylint: disable=protected-access


def _poison_pools(monkeypatch, how="exit", once_marker=None, initializer=None):
    """Every pool `ingest()` builds runs `initializer` in each worker.

    Both bindings are patched: the session's, for the shared pool, and
    `io_handlers`', for the pools the retry builds. `raising=False` on the
    second, so that on a tree without that binding the tests go red for the
    reason they exist -- the raise out of `ingest()` -- and not on an
    `AttributeError` in this fixture.
    """
    init = initializer or functools.partial(_die_reading, "POISON", how,
                                            once_marker)

    def resolve(disable_gc=False):  # pylint: disable=unused-argument
        return init

    monkeypatch.setattr(session_module, "resolve_worker_initializer", resolve)
    monkeypatch.setattr(io_handlers, "resolve_worker_initializer", resolve,
                        raising=False)


def _folder(tmp_path, n, poison_at=()):
    """`n` copies of CT_small, each with its own SOP Instance UID."""
    src = tmp_path / "src"
    src.mkdir()
    base = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    paths = []
    for i in range(n):
        ds = base.copy()
        ds.SOPInstanceUID = generate_uid()
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        tag = "POISON" if i in poison_at else "ok"
        path = str(src / f"{i:02d}_{tag}.dcm")
        ds.save_as(path, enforce_file_format=True)
        paths.append(path)
    return src, paths


def _stored(db, sidecar):
    """Rows, bytes referenced, sidecar size and ERROR rows, read from disk."""
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT count(*), coalesce(sum(pixel_length), 0) "
                            "FROM instances").fetchone()
        errors = conn.execute("SELECT entity_uid, details FROM audit_log "
                              "WHERE action_type='ERROR' "
                              "ORDER BY id").fetchall()
    return rows[0], rows[1], os.path.getsize(sidecar), errors


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


@pytest.fixture(autouse=True)
def _two_workers(monkeypatch):
    for name in ("ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
                 "ISOCENTER_MAX_TASKS_PER_CHILD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")


@pytest.mark.parametrize("how", ["exit", "sigkill"])
@pytest.mark.parametrize("at", [0, 3, 7])
def test_a_worker_killed_mid_ingest_costs_only_the_file_it_was_reading(
        tmp_path, monkeypatch, how, at):
    """Seven files reach the store, and the eighth is named.

    First, middle and last in path order, and by `os._exit` and `SIGKILL`.
    On main every shape raised `BrokenProcessPool` out of `ingest()`.

    `sum(pixel_length) == sidecar size` is the orphan check: no worker
    writes the sidecar, the parent appends each result's frame as it links
    it, so bytes nothing references mean linked instances that were never
    saved.

    **`active_children() == []` is on every case, and has to be.** It is
    what catches a retry pool that is never shut down, because its workers
    linger -- but only by timing: a pool dropped without `shutdown` is torn
    down eventually by its weakref callback. Measured with the shutdown
    removed, it failed 5 of these 6 cases on 3.12 and 4 of 6 on 3.14t, so
    any one case alone could pass by luck.
    """
    _poison_pools(monkeypatch, how)
    src, paths = _folder(tmp_path, 8, poison_at=(at,))
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        sidecar = session.store_backend.sidecar.filepath
        summary = session.ingest(str(src))
        assert len(_instances(session)) == 7
        assert all(not i.has_unsaved_changes for i in _instances(session))
        assert multiprocessing.active_children() == []
    assert summary.ingested == 7
    assert [p for p, _ in summary.failures] == [paths[at]]
    assert summary.failures[0][1].startswith(io_handlers._WORKER_ENDED_READING)
    assert "BrokenProcessPool" in summary.failures[0][1]
    count, referenced, size, errors = _stored(db, sidecar)
    assert count == 7
    assert referenced == size
    assert [uid for uid, _ in errors] == [paths[at]]
    assert errors[0][1].startswith(
        f"Ingest failed for {paths[at]}: {io_handlers._WORKER_ENDED_READING}")


def test_the_next_ingest_on_the_same_session_needs_no_retry(
        tmp_path, monkeypatch, caplog):
    """The broken shared pool is replaced, so the next call runs normally.

    Without the rebuild the second call is still *correct* -- it finds the
    pool broken at its first result and heals through a retry of its own --
    just slow, and the retry's `WARNING` is the only thing that shows it.
    """
    _poison_pools(monkeypatch)
    src, paths = _folder(tmp_path, 6, poison_at=(2,))
    with DicomSession(str(tmp_path / "s.db")) as session:
        first = session.ingest(str(src))
        assert [p for p, _ in first.failures] == [paths[2]]
        os.rename(paths[2], paths[2].replace("POISON", "healed"))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="isocenter"):
            second = session.ingest(str(src))
    assert second.ingested == 1 and second.failures == []
    assert not [r for r in caplog.records
                if "worker process ended" in r.getMessage()]


def test_a_death_that_does_not_recur_costs_no_file(tmp_path, monkeypatch,
                                                   caplog):
    """A worker that dies once and not again on the retry costs nothing.

    No row either (Q3): every file is in the store, and the `WARNING` log
    line is the record that the pool died.
    """
    _poison_pools(monkeypatch, once_marker=str(tmp_path / "died-once"))
    src, _paths = _folder(tmp_path, 8, poison_at=(3,))
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        sidecar = session.store_backend.sidecar.filepath
        caplog.clear()
        summary = session.ingest(str(src))
    assert os.path.exists(tmp_path / "died-once")
    assert summary.ingested == 8 and summary.failures == []
    count, referenced, size, errors = _stored(db, sidecar)
    assert (count, errors) == (8, []) and referenced == size
    assert [r for r in caplog.records if r.levelno == logging.WARNING
            and "An ingest worker process ended" in r.getMessage()]


def test_adjacent_fatal_files_are_each_named_and_the_rest_ingest(
        tmp_path, monkeypatch):
    """Four fatal files, three of them adjacent, are each named.

    Pins the absence of a cap on consecutive fatal files: a prototype that
    stopped after three in a row left good files behind them unread.
    """
    _poison_pools(monkeypatch)
    src, paths = _folder(tmp_path, 8, poison_at=(1, 2, 3, 5))
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(src))
    assert summary.ingested == 4
    assert [p for p, _ in summary.failures] == [paths[i] for i in (1, 2, 3, 5)]
    assert all(r.startswith(io_handlers._WORKER_ENDED_READING)
               for _, r in summary.failures)


def test_a_worker_that_cannot_start_is_not_blamed_on_a_file(tmp_path,
                                                           monkeypatch):
    """No worker can run anything: every file is "not read", none is blamed.

    The shape a script without the main guard produces when its re-imported
    copy raises. Each one-at-a-time pool runs a trivial task first; without
    that, a worker that cannot start would blame each file in turn.

    Seven files, more than the 2W+1 = 5 a one-at-a-time round reads: with
    five, a canary arm that fell through instead of stopping would find
    nothing left and pass; with seven it reads the last two again and
    rejects them twice.
    """
    _poison_pools(monkeypatch, initializer=_die_at_start)
    src, paths = _folder(tmp_path, 7)
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        sidecar = session.store_backend.sidecar.filepath
        summary = session.ingest(str(src))
    assert summary.ingested == 0
    assert summary.failures == [(p, io_handlers._NO_INGEST_WORKER_STARTS)
                                for p in paths]
    assert [uid for uid, _ in _stored(db, sidecar)[3]] == paths


def _strategy(max_workers=4):
    return parallel._resolve_strategy(max_workers, 1, None, False,
                                      False, False, "Ingesting", None)


def test_a_pool_failure_that_is_not_a_dead_worker_still_raises(monkeypatch):
    """Only a dead worker is retried; anything else from the pool raises.

    It is not about these files. Turning it into rows instead let the
    re-imported copies of an unguarded script write rows into the parent's
    store (measured while designing #654).
    """
    calls = []

    def fake(func, items, **kwargs):  # pylint: disable=unused-argument
        calls.append(list(items))
        yield ({'path': items[0]}, None, None, None, None, None, None, "x")
        yield RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(io_handlers, "run_parallel", fake)
    results = io_handlers._ingest_results(["a", "b", "c"], object(),
                                          _strategy())
    assert next(results)[0]['path'] == "a"
    with pytest.raises(RuntimeError, match="after shutdown"):
        next(results)
    assert len(calls) == 1


def test_the_retry_reads_alone_on_a_fresh_spawned_pool(monkeypatch):
    """The retry's shape, with the pool stubbed out.

    Round 0 returns one file and dies: the shared pool is reported broken,
    and the next 2W+1 files from the first not returned are read on a
    one-worker pool. That round dies at once, so its first file is blamed,
    and the rest are read at full width. Every pool is a spawned
    `ProcessPoolExecutor` -- never threads, which a file that kills its
    process would take the parent down with on 3.14t.
    """
    calls, pools = [], []
    shared = object()
    real = concurrent.futures.ProcessPoolExecutor

    class Recording(real):
        def __init__(self, *args, **kwargs):
            pools.append(kwargs)
            super().__init__(*args, **kwargs)

    def fake(func, items, executor=None, **kwargs):  # pylint: disable=unused-argument
        calls.append((list(items), executor))
        if len(calls) == 1:
            yield ({'path': items[0]}, "inst", None, None, None, None, None,
                   None)
            yield BrokenProcessPool("died")
            return
        if len(calls) == 2:
            yield BrokenProcessPool("died again")
            return
        for it in items:
            yield ({'path': it}, "inst", None, None, None, None, None, None)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Recording)
    monkeypatch.setattr(io_handlers, "run_parallel", fake)
    broken = []
    files = [f"f{i}" for i in range(20)]
    out = list(io_handlers._ingest_results(files, shared, _strategy(4),
                                           broken.append))
    assert broken == [shared]
    assert calls[0][1] is shared
    assert calls[1][0] == files[1:1 + 9]          # 2 * 4 + 1
    assert calls[2][0] == files[2:]
    assert [p["max_workers"] for p in pools] == [1, 4]
    assert all(p["mp_context"].get_start_method() == "spawn" for p in pools)
    assert [r[0]['path'] for r in out] == files
    assert out[1][7].startswith(io_handlers._WORKER_ENDED_READING)
    assert all(r[7] is None for i, r in enumerate(out) if i != 1)


def test_a_rebuild_replaces_only_the_pool_that_broke(tmp_path):
    """A compare-and-swap: a second caller holding a stale pool changes nothing."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        live = session._executor
        session._restart_executor(broken=object())
        assert session._executor is live
        session._restart_executor(broken=live)
        assert session._executor is not live


def test_a_direct_import_survives_a_dead_worker_too(tmp_path, monkeypatch):
    """`import_files` with a caller's own pool: the same retry, and the report.

    `on_executor_broken` is how the caller learns its pool is unusable.
    """
    _poison_pools(monkeypatch)
    src, paths = _folder(tmp_path, 6, poison_at=(4,))
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn"),
        initializer=functools.partial(_die_reading, "POISON", "exit"))
    seen = []
    try:
        summary = DicomImporter.import_files([str(src)], DicomStore(),
                                             executor=executor,
                                             on_executor_broken=seen.append)
    finally:
        executor.shutdown(wait=True)
    assert summary.ingested == 5
    assert [p for p, _ in summary.failures] == [paths[4]]
    assert seen == [executor]


def test_the_pool_is_rebuilt_after_the_pass_lock_is_released(tmp_path,
                                                            monkeypatch):
    """The rebuild holds nothing: not the pass-lock, not the ingest counter.

    `_ingest_lock` is never held while another lock is taken, and the
    rebuild takes it, so it has to wait until the pass-lock is released.
    """
    _poison_pools(monkeypatch)
    src, _paths = _folder(tmp_path, 4, poison_at=(1,))
    seen = []
    with DicomSession(str(tmp_path / "s.db")) as session:
        held = {"pass": False}
        real_hold = session.store_backend._hold_pass_lock
        real_restart = session._restart_executor

        @contextlib.contextmanager
        def recording_hold(*a, **k):
            with real_hold(*a, **k):
                held["pass"] = True
                try:
                    yield
                finally:
                    held["pass"] = False

        def recording_restart(*a, **k):
            seen.append((held["pass"], session._ingests_in_flight,
                         session._ingest_lock.locked()))
            return real_restart(*a, **k)

        monkeypatch.setattr(session.store_backend, "_hold_pass_lock",
                            recording_hold)
        monkeypatch.setattr(session, "_restart_executor", recording_restart)
        session.ingest(str(src))
    assert seen == [(False, 0, False)]


def test_a_rebuild_that_fails_does_not_cost_the_ingest(tmp_path, monkeypatch,
                                                       caplog):
    """`OSError` from the replacement pool is a log line, after the save.

    EMFILE or ENOMEM from `ProcessPoolExecutor(...)` is the failure of a
    box short of resources. The ingest has completed and saved by then;
    the next `ingest()` finds the pool broken and heals through a retry.
    """
    _poison_pools(monkeypatch)
    src, paths = _folder(tmp_path, 4, poison_at=(1,))
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        sidecar = session.store_backend.sidecar.filepath

        def failing_restart(*a, **k):
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(session, "_restart_executor", failing_restart)
        caplog.clear()
        summary = session.ingest(str(src))
    assert summary.ingested == 3
    assert [p for p, _ in summary.failures] == [paths[1]]
    assert _stored(db, sidecar)[0] == 3
    assert [r for r in caplog.records if r.levelno == logging.WARNING
            and "Too many open files" in r.getMessage()]


def test_a_retry_draws_no_second_progress_bar(tmp_path, monkeypatch):
    """One `Ingesting` bar per call, with progress on (Q7).

    Each retry round is a `run_parallel` call, and `_tracked` draws a bar
    for each: a death would show as a bar stopping short, then a bar per
    round. The retry rounds run with progress off; the `WARNING` line
    announces the retry instead.
    """
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "1")
    _poison_pools(monkeypatch)
    src, paths = _folder(tmp_path, 6, poison_at=(2,))
    bars = []
    real_tqdm = parallel.tqdm

    def recording_tqdm(*args, **kwargs):
        bars.append(kwargs.get("desc"))
        return real_tqdm(*args, **kwargs)

    monkeypatch.setattr(parallel, "tqdm", recording_tqdm)
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(src))
    assert [p for p, _ in summary.failures] == [paths[2]]
    assert bars.count("Ingesting") == 1


def test_a_retry_repeats_no_recycling_override_warning(tmp_path, monkeypatch,
                                                       caplog):
    """#185's line is one per call, not one per retry round (Q7).

    With `ISOCENTER_FORCE_THREADS` and `ISOCENTER_MAX_TASKS_PER_CHILD` both
    set, `run_parallel` announces the override on every call it makes.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_TASKS_PER_CHILD", "5")
    _poison_pools(monkeypatch)
    src, paths = _folder(tmp_path, 6, poison_at=(2,))
    with DicomSession(str(tmp_path / "s.db")) as session:
        caplog.clear()
        summary = session.ingest(str(src))
    assert [p for p, _ in summary.failures] == [paths[2]]
    assert len([r for r in caplog.records
                if "was set, but worker recycling" in r.getMessage()]) == 1

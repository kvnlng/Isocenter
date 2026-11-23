"""A file that never finishes reading or writing costs that file, not the
call (#673).

`ingest()` and `export()` waited for good on a worker that neither returned
nor died: a decoder in an endless loop held the whole call, with nothing
saved, no row and nothing naming the file (measured at ea3fb12c on 3.12 and
3.14t: still running at 30 s). Each task now has a time bound,
`ISOCENTER_TASK_TIMEOUT` (600 s by default, `0` for none). A task past it is
stopped and read again alone on a fresh worker process, and only a second
run past the bound rejects it, with one `ERROR` row naming the file and the
bound (owner rulings Q4-Q9 on the X2 brief, 2026-11-21).

- **Export** runs on the slot pool (#885), where each worker holds its own
  chunks, so the bound is measured on the chunk a worker is running.
- **Ingest** runs on the session's shared pool, where no task's start time
  can be read. A watcher ends the pool's workers when the call has waited on
  one file for longer than the bound. That is only a suspicion: no row is
  written, and #654's one-at-a-time rounds then read each file alone, with
  the bound, where it is exact.

Every call that can hang runs under `_run_bounded` or a subprocess with a
timeout, so a regression fails here instead of hanging the suite. The bound
in these tests is several seconds because a fresh spawn imports numpy and
pydicom, which this machine has been measured to take seconds under load;
the assertions hold even when a healthy task is stopped once by mistake,
because a mistaken first stop costs nothing by design.
"""
import concurrent.futures
import functools
import logging
import multiprocessing
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures.process import BrokenProcessPool

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.uid import generate_uid

import isocenter.io_handlers as io_handlers
from isocenter import parallel
from isocenter.session import DicomSession
from isocenter.store import DicomStore

from support.ct_small_files import write_ct
from support.project_secret import FIXED_A, load_fixed_secret
# pylint: disable=wrong-import-order
from test_parallel_contract import (_gone, _no_child_left, _run_bounded,
                                    recorded_pools)  # pylint: disable=unused-import

#: The bound every test here runs under, in seconds.
BOUND = 6


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No test inherits another's tuning variables."""
    for name in ("ISOCENTER_MAX_WORKERS", "ISOCENTER_CHUNKSIZE",
                 "ISOCENTER_MAX_TASKS_PER_CHILD", "ISOCENTER_DISABLE_GC",
                 "ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
                 "ISOCENTER_WORKER_FAULTHANDLER", "ISOCENTER_TASK_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")


# --- worker functions (module scope: they pickle into spawned workers) ------

def _wedge_at(args):
    """Return the value, except that `wedge` never returns."""
    value, wedge = args
    if value == wedge:
        while True:
            time.sleep(3600)
    return value


def _wedge_once_at(args):
    """`_wedge_at`, but only the first time: the marker file is written as
    the task wedges, and a later run of the same item returns."""
    value, wedge, marker = args
    if value == wedge and not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8"):
            pass
        while True:
            time.sleep(3600)
    return value


def _wedge_at_and_once_at(args):
    """`wedge` never returns. Each value in `once` never returns the first
    time it runs, across processes (a marker file under `markers` is written
    as it wedges), and returns on any later run."""
    value, wedge, once, markers = args
    if value == wedge:
        while True:
            time.sleep(3600)
    if value in once:
        marker = os.path.join(markers, str(value))
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8"):
                pass
            while True:
                time.sleep(3600)
    return value


def _sleep_then_return(args):
    value, seconds = args
    time.sleep(seconds)
    return value


def _wedge_once_then_die_at(args):
    """`_wedge_once_at`, except that the later run of the wedged item ends
    its worker instead of returning."""
    value, wedge, marker = args
    if value == wedge:
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8"):
                pass
            while True:
                time.sleep(3600)
        os._exit(7)  # pylint: disable=protected-access
    return value


def _wedge_reading(marker, once_marker=None, flag=None):
    """Pool initializer: reading a path containing `marker` never returns.

    With `once_marker`, only the first time across processes. With `flag`,
    a file is written before the wedge, so a test can wait for it.
    SIGINT is ignored while wedged, as a C loop that never checks for
    signals would ignore it.
    """
    real = pydicom.dcmread

    def dcmread(fp, *args, **kwargs):
        if marker in str(fp):
            if once_marker is None or not os.path.exists(once_marker):
                if once_marker is not None:
                    with open(once_marker, "w", encoding="utf-8"):
                        pass
                if flag is not None:
                    with open(flag, "w", encoding="utf-8"):
                        pass
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                while True:
                    time.sleep(3600)
        return real(fp, *args, **kwargs)
    pydicom.dcmread = dcmread


def _die_or_wedge_reading(flag):
    """Pool initializer: a path containing `POISON` ends the worker, and one
    containing `WEDGE` never returns, after `flag` is written."""
    # pylint: disable=import-outside-toplevel
    from test_a_dead_ingest_worker_costs_the_file_it_was_reading import (
        _die_reading)
    _die_reading("POISON", "exit")
    _wedge_reading("WEDGE", flag=flag)


def _wedge_twice_then_poison(strikes):
    """Pool initializer: a path containing `WEDGE` never returns on its first
    two reads across processes (each writes a file under `strikes` as it
    wedges) and returns on the third; one containing `POISON` ends the
    worker, but only once the first strike is written, so the first round
    has begun reading the wedged file before the pool breaks."""
    real = pydicom.dcmread

    def dcmread(fp, *args, **kwargs):
        if "WEDGE" in str(fp):
            for strike in ("1", "2"):
                try:
                    fd = os.open(os.path.join(strikes, strike),
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    continue
                os.close(fd)
                while True:
                    time.sleep(3600)
        if "POISON" in str(fp):
            deadline = time.monotonic() + 60
            while (not os.path.exists(os.path.join(strikes, "1"))
                   and time.monotonic() < deadline):
                time.sleep(0.05)
            os._exit(13)  # pylint: disable=protected-access
        return real(fp, *args, **kwargs)
    pydicom.dcmread = dcmread


def _wedge_writing(uid):
    """Pool initializer: the export worker handed instance `uid` never
    returns."""
    # pylint: disable=import-outside-toplevel
    from isocenter import io_handlers as handlers
    real = handlers._export_instance_worker

    def wedged_on_uid(ctx):
        if getattr(ctx.instance, "sop_instance_uid", None) == uid:
            while True:
                time.sleep(3600)
        return real(ctx)

    handlers._export_instance_worker = wedged_on_uid


def _bound_records(caplog):
    return [r for r in caplog.records
            if "ISOCENTER_TASK_TIMEOUT" in r.getMessage()]


# --- the variable --------------------------------------------------------------

def test_the_bound_is_600_s_unless_the_variable_says_otherwise(monkeypatch,
                                                                caplog):
    """Unset: 600. A whole number: that. `0`: no bound. Negative or not a
    number: a WARNING and 600.

    Killing mutation: `0` read as a bound of 0 s, which would stop every
    task at once.
    """
    assert parallel.resolve_task_timeout() == 600
    monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", "45")
    assert parallel.resolve_task_timeout() == 45
    monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", "0")
    assert parallel.resolve_task_timeout() is None
    for bad in ("-1", "ten"):
        monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", bad)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            assert parallel.resolve_task_timeout() == 600
        assert [r for r in caplog.records
                if "ISOCENTER_TASK_TIMEOUT" in r.getMessage()], bad


# --- the slot pool (export's) --------------------------------------------------

def test_a_chunk_that_never_finishes_is_run_again_alone_and_then_reported(
        caplog, recorded_pools):
    """Item 3 never returns. The other items come back, item 3 comes back as
    `_TaskDidNotFinish` naming the item and the bound, after two runs past
    the bound, and no worker is left.

    Killing mutation: no deadline in the slot pool (the call hangs); or the
    first run past the bound reported at once (two WARNING lines, not one
    plus the report, and under 2x the bound).
    """
    items = [(value, 3) for value in range(8)]
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _wedge_at, items, max_workers=2, maxtasksperchild=25,
            show_progress=False, yield_exceptions=True,
            task_timeout=BOUND), 120)
    elapsed = time.monotonic() - started

    assert not outcome["hung"], "the slot pool waited for good (#673)"
    assert "error" not in outcome, outcome
    values = outcome["value"]
    stopped = [v for v in values if isinstance(v, BaseException)]
    assert sorted(v for v in values if not isinstance(v, BaseException)) == [
        0, 1, 2, 4, 5, 6, 7]
    assert len(stopped) == 1, values
    assert isinstance(stopped[0], parallel._TaskDidNotFinish), stopped
    assert stopped[0].item == (3, 3)
    assert stopped[0].bound == BOUND
    assert elapsed >= 2 * BOUND, elapsed
    lines = _bound_records(caplog)
    assert len(lines) == 2, [r.getMessage() for r in lines]
    assert "once more, alone" in lines[0].getMessage()
    assert "second time" in lines[1].getMessage()
    assert _gone(recorded_pools), "a stopped worker is still running"
    assert _no_child_left(), multiprocessing.active_children()


def test_a_chunk_past_the_bound_only_once_is_delivered(tmp_path,
                                                       recorded_pools):
    """Item 2 wedges the first time only. Run again alone, it returns, and
    nothing is reported (owner ruling Q8: confirm before rejecting).

    Killing mutation: the first run past the bound reported at once.
    """
    marker = str(tmp_path / "wedged-once")
    items = [(value, 2, marker) for value in range(6)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _wedge_once_at, items, max_workers=2, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True,
        task_timeout=BOUND), 120)

    assert not outcome["hung"]
    assert "error" not in outcome, outcome
    assert sorted(outcome["value"]) == list(range(6))
    assert os.path.exists(marker)
    assert _gone(recorded_pools)


def test_the_other_chunk_a_stopped_worker_held_is_run_again(recorded_pools):
    """One worker, so it holds item 0, which never returns, and item 1
    behind it, which never started. Item 1 is innocent: it is run again,
    delivered once, and never reported.

    Killing mutation: every chunk the stopped worker held treated as past
    the bound, or dropped.
    """
    items = [(value, 0) for value in range(4)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _wedge_at, items, max_workers=1, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True,
        task_timeout=BOUND), 120)

    assert not outcome["hung"]
    values = outcome["value"]
    good = [v for v in values if not isinstance(v, BaseException)]
    assert sorted(good) == [1, 2, 3], values
    stopped = [v for v in values if isinstance(v, BaseException)]
    assert [s.item for s in stopped] == [(0, 0)], values
    assert _gone(recorded_pools)


def test_an_ordered_run_keeps_the_order_with_the_report_in_its_place(
        recorded_pools):
    """`ordered=True`: the report for item 2 comes back between 1 and 3.

    Killing mutation: the report appended at the end, out of its sequence.
    """
    items = [(value, 2) for value in range(5)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _wedge_at, items, max_workers=2, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True, ordered=True,
        task_timeout=BOUND), 120)

    assert not outcome["hung"]
    values = outcome["value"]
    assert values[:2] == [0, 1] and values[3:] == [3, 4], values
    assert isinstance(values[2], parallel._TaskDidNotFinish), values
    assert _gone(recorded_pools)


@pytest.mark.parametrize("ordered", [False, True])
def test_a_chunk_of_several_items_is_confirmed_one_item_at_a_time(
        recorded_pools, ordered):
    """Chunks of 3 items: the chunk holding the item that never returns is
    run past the bound, then each of its items alone. Only that item is
    reported; the two beside it come back. Ordered, the three come back
    as their chunk, in its place, with the report between its neighbours.

    Killing mutations: the whole chunk confirmed as one task, which reports
    its two good items too; each item run alone handed over as the whole
    chunk, which in order keeps only the last one in.
    """
    items = [(value, 4) for value in range(9)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _wedge_at, items, max_workers=2, maxtasksperchild=25, chunksize=3,
        show_progress=False, yield_exceptions=True, ordered=ordered,
        task_timeout=BOUND), 150)

    assert not outcome["hung"]
    values = outcome["value"]
    good = sorted(v for v in values if not isinstance(v, BaseException))
    assert good == [0, 1, 2, 3, 5, 6, 7, 8], values
    assert [v.item for v in values if isinstance(v, BaseException)] == [(4, 4)]
    if ordered:
        assert values[:4] == [0, 1, 2, 3] and values[5:] == [5, 6, 7, 8], \
            values
        assert isinstance(values[4], parallel._TaskDidNotFinish), values
    assert _gone(recorded_pools)


def test_a_chunk_mate_that_never_started_is_not_rejected_at_its_first_stop(
        tmp_path, recorded_pools):
    """Chunks of 3: item 4 never returns, and item 5, behind it in its
    chunk, never started there. Run alone, item 5 is slow on that run only.
    That run alone is item 5's first run past the bound, so it is run once
    more, returns, and is not reported (owner ruling Q8). Only item 4, which
    never returns, is reported. Which item of a chunk was running when the
    chunk was stopped cannot be read in the parent, so the chunk's stop is
    charged to none of its items.

    Killing mutation: an item of a stopped chunk of several reported at its
    first stop alone (found in the review of #673).
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    items = [(value, 4, (5,), str(markers)) for value in range(9)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _wedge_at_and_once_at, items, max_workers=2, maxtasksperchild=25,
        chunksize=3, show_progress=False, yield_exceptions=True,
        task_timeout=BOUND), 180)

    assert not outcome["hung"]
    assert "error" not in outcome, outcome
    values = outcome["value"]
    assert os.listdir(markers) == ["5"], "item 5 never ran alone"
    assert [v.item[0] for v in values
            if isinstance(v, BaseException)] == [4], values
    assert sorted(v for v in values if not isinstance(v, BaseException)) == [
        0, 1, 2, 3, 5, 6, 7, 8]
    assert _gone(recorded_pools)


def test_the_items_run_alone_are_bounded_by_the_width(tmp_path,
                                                      recorded_pools,
                                                      monkeypatch):
    """Two workers, chunks of 4, and the first item of each chunk is slow
    on its first run only: both chunks are stopped, and their 8 items are
    run alone. At most `width` of those run at once, so the pool never holds
    more than twice the width of live workers (a storage stall under
    `ISOCENTER_CHUNKSIZE` must not spawn one interpreter per item). Every
    item comes back.

    Killing mutation: each item run alone opened on a worker of its own at
    once, whatever the width (16 items in chunks of 8 at width 2 spawned 17
    processes, found in the review of #673).
    """
    peak = {"alone": 0, "live": 0}
    real_open = parallel._SlotPool._open

    def counting_open(self, alone=False):
        slot = real_open(self, alone=alone)
        peak["alone"] = max(peak["alone"],
                            sum(1 for s in self._live if s.alone))
        peak["live"] = max(peak["live"], len(self._live))
        return slot

    monkeypatch.setattr(parallel._SlotPool, "_open", counting_open)
    markers = tmp_path / "markers"
    markers.mkdir()
    items = [(value, None, (0, 4), str(markers)) for value in range(8)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _wedge_at_and_once_at, items, max_workers=2, maxtasksperchild=25,
        chunksize=4, show_progress=False, yield_exceptions=True,
        task_timeout=BOUND), 180)

    assert not outcome["hung"]
    assert "error" not in outcome, outcome
    assert sorted(outcome["value"]) == list(range(8)), outcome["value"]
    assert sorted(os.listdir(markers)) == ["0", "4"]
    assert peak["alone"] <= 2, peak
    assert peak["live"] <= 4, peak
    assert _gone(recorded_pools)


def test_without_yield_exceptions_a_task_past_the_bound_raises(
        recorded_pools):
    """With no `yield_exceptions`, a task twice past the bound fails the
    stream as a task's own exception would, and every worker is ended.

    Killing mutation: the report delivered as a value with no
    `yield_exceptions`.
    """
    items = [(value, 1) for value in range(3)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _wedge_at, items, max_workers=2, maxtasksperchild=25,
        show_progress=False, task_timeout=BOUND), 120)

    assert not outcome["hung"]
    assert isinstance(outcome.get("error"), parallel._TaskDidNotFinish), \
        outcome
    assert _gone(recorded_pools)
    assert _no_child_left(), multiprocessing.active_children()


@pytest.mark.parametrize("ordered", [False, True])
def test_a_worker_that_dies_on_a_task_run_alone_fails_the_stream(
        tmp_path, recorded_pools, ordered):
    """Item 2 is stopped past the bound, and its run alone ends its worker.
    That is a death like any other (#887): the stream fails with
    `BrokenProcessPool`, after the results already delivered, and its
    `lost` names the item run alone by its own index.

    Killing mutation: a run alone keyed by a tuple sequence that the death
    path or the ordered buffer cannot read (a `TypeError`, or a hang).
    """
    marker = str(tmp_path / "wedged-once")
    items = [(value, 2, marker) for value in range(5)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _wedge_once_then_die_at, items, max_workers=2, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True, ordered=ordered,
        task_timeout=BOUND), 120)

    assert not outcome["hung"]
    assert "error" not in outcome, outcome
    values = outcome["value"]
    broken = values[-1]
    assert type(broken) is BrokenProcessPool, values
    assert not [v for v in values[:-1] if isinstance(v, BaseException)], values
    assert (2, [items[2]]) in broken.lost, broken.lost
    if ordered:
        assert values[:-1] == [0, 1], values
    else:
        assert 2 not in values[:-1], values
    assert _gone(recorded_pools)
    assert _no_child_left(), multiprocessing.active_children()


def test_a_slow_task_inside_the_bound_is_not_stopped(caplog, recorded_pools):
    """Two tasks of 5 s each on one worker at depth 2 under a bound of 8 s:
    the second is timed from when the first returned, not from when it was
    handed over, so neither is stopped. The first one's clock includes the
    worker's spawn, so the margin is 3 s.

    Killing mutation: a chunk's clock started at its submit, which reads the
    second chunk as running for 10 s.
    """
    items = [(value, 5) for value in range(2)]
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _sleep_then_return, items, max_workers=1, maxtasksperchild=25,
            show_progress=False, yield_exceptions=True, task_timeout=8), 120)
    assert not outcome["hung"]
    assert sorted(outcome["value"]) == [0, 1], outcome
    assert not _bound_records(caplog), [r.getMessage()
                                        for r in _bound_records(caplog)]


def test_a_thread_is_not_bounded(caplog):
    """Threads cannot be ended, so the bound is not applied to a run in
    threads; the task finishes and is delivered. `ingest()` and `export()`
    never run in threads, on 3.14t either.

    Killing mutation: a bound applied to threads, which could only report
    a task it cannot stop.
    """
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _sleep_then_return, [(0, 2), (1, 0)], max_workers=2,
            force_threads=True, show_progress=False, yield_exceptions=True,
            task_timeout=1), 60)
    assert not outcome["hung"]
    assert sorted(outcome["value"]) == [0, 1], outcome
    assert not _bound_records(caplog)


def test_a_shared_thread_pool_is_not_bounded(caplog):
    """The same on an executor the caller shares: a `ThreadPoolExecutor`
    has no worker process to end, so no watch runs on it.

    Killing mutation: the watch started on any shared executor, which logs
    a stop it cannot make.
    """
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            outcome = _run_bounded(lambda: parallel.run_parallel(
                _sleep_then_return, [(0, 2), (1, 0)], executor=executor,
                show_progress=False, yield_exceptions=True,
                task_timeout=1), 60)
    assert not outcome["hung"]
    assert outcome["value"] == [0, 1], outcome
    assert not _bound_records(caplog), [r.getMessage()
                                        for r in _bound_records(caplog)]


# --- ingest (the shared pool and #654's rounds) ---------------------------------

def _folder(tmp_path, n, tags):
    """`n` copies of CT_small, each with its own SOP Instance UID; a file's
    name carries `tags[i]`, else `ok`."""
    src = tmp_path / "src"
    src.mkdir()
    base = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    paths = []
    for i in range(n):
        ds = base.copy()
        ds.SOPInstanceUID = generate_uid()
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        path = str(src / f"{i:02d}_{tags.get(i, 'ok')}.dcm")
        ds.save_as(path, enforce_file_format=True)
        paths.append(path)
    return src, paths


def _wedge_pools(monkeypatch, initializer):
    """Every pool `ingest()` builds runs `initializer` in each worker
    (`parallel.new_spawn_process_pool` resolves it through this binding,
    #667)."""
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)


def _error_rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT entity_uid, details FROM audit_log "
                            "WHERE action_type='ERROR' ORDER BY id").fetchall()


def test_ingest_rejects_a_file_that_never_finishes_reading_with_one_row(
        tmp_path, monkeypatch):
    """Six files, one of which never finishes reading: `ingest()` returns,
    five files reach the store, and the sixth is named in one `ERROR` row
    that names the path and the bound. The next `ingest()` on the session
    runs on a working pool.

    Killing mutation: no watcher on the shared pool (`ingest()` hangs); a
    timeout read as a death, which names `_WORKER_ENDED_READING` and not
    the bound.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", str(BOUND))
    _wedge_pools(monkeypatch, functools.partial(_wedge_reading, "WEDGE"))
    src, paths = _folder(tmp_path, 6, {3: "WEDGE"})
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        outcome = _run_bounded(lambda: session.ingest(str(src)), 180)
        assert not outcome["hung"], "ingest() waited for good (#673)"
        summary = outcome["value"]
        os.rename(paths[3], paths[3].replace("WEDGE", "healed"))
        after = _run_bounded(lambda: session.ingest(str(src)), 120)
        assert not after["hung"]
    assert summary.ingested == 5
    assert [p for p, _ in summary.failures] == [paths[3]]
    reason = summary.failures[0][1]
    assert reason.startswith(io_handlers._RAN_PAST_THE_BOUND.format(
        bound=BOUND)), reason
    assert f"{BOUND} s" in reason and "ISOCENTER_TASK_TIMEOUT" in reason
    errors = _error_rows(db)
    assert [uid for uid, _ in errors] == [paths[3]]
    assert paths[3] in errors[0][1] and f"{BOUND} s" in errors[0][1]
    assert after["value"].ingested == 1 and not after["value"].failures
    assert _no_child_left(), multiprocessing.active_children()


def test_a_file_slow_only_on_the_shared_pool_costs_no_file(tmp_path,
                                                           monkeypatch):
    """The file wedges only its first read. The shared pool is stopped for
    it, which is a suspicion only: read again alone, it returns, every file
    reaches the store, and no row is written.

    Killing mutation: a row written for the shared pool's stop, or the
    file rejected at the first run past the bound.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", str(BOUND))
    once = str(tmp_path / "once")
    _wedge_pools(monkeypatch,
                 functools.partial(_wedge_reading, "WEDGE", once))
    src, _ = _folder(tmp_path, 5, {2: "WEDGE"})
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        outcome = _run_bounded(lambda: session.ingest(str(src)), 180)
    assert not outcome["hung"]
    assert os.path.exists(once)
    assert outcome["value"].ingested == 5, outcome["value"]
    assert not outcome["value"].failures
    assert _error_rows(db) == []


def test_a_file_past_the_bound_once_after_a_death_is_read_again(tmp_path,
                                                                monkeypatch):
    """The first round ends on a death, not on the bound: file 1 ends its
    worker while file 0 is wedged. The one-at-a-time round then reads file 0
    first, and it runs past the bound; that is its first stop, so it is
    read alone once more, returns, and reaches the store (owner ruling Q8).
    Only file 1 is rejected, for ending its worker.

    Killing mutation: a file rejected at its first stop in a one-at-a-time
    round, whatever sent the call there.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", str(BOUND))
    strikes = tmp_path / "strikes"
    strikes.mkdir()
    _wedge_pools(monkeypatch,
                 functools.partial(_wedge_twice_then_poison, str(strikes)))
    src, paths = _folder(tmp_path, 4, {0: "WEDGE", 1: "POISON"})
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        outcome = _run_bounded(lambda: session.ingest(str(src)), 240)
    assert not outcome["hung"]
    assert sorted(os.listdir(strikes)) == ["1", "2"]
    summary = outcome["value"]
    assert [p for p, _ in summary.failures] == [paths[1]], summary.failures
    assert summary.failures[0][1].startswith(
        io_handlers._WORKER_ENDED_READING), summary.failures
    assert summary.ingested == 3
    assert [uid for uid, _ in _error_rows(db)] == [paths[1]]


def test_a_direct_import_on_the_recycling_pool_rejects_the_file_with_its_row(
        tmp_path, monkeypatch):
    """`DicomImporter.import_files(executor=None)` under
    `ISOCENTER_MAX_TASKS_PER_CHILD` reads on the slot pool, which confirms a
    stopped file alone itself and hands back `_TaskDidNotFinish` in its
    place. That is the file's rejection, with the bound's reason, and the
    other files are read; the import does not raise.

    Killing mutation: the marker read as the pool's own failure, which
    raises `_TaskDidNotFinish` out of the import and loses every file.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_MAX_TASKS_PER_CHILD", "25")
    monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", str(BOUND))
    _wedge_pools(monkeypatch, functools.partial(_wedge_reading, "WEDGE"))
    src, paths = _folder(tmp_path, 4, {2: "WEDGE"})
    outcome = _run_bounded(lambda: io_handlers.DicomImporter.import_files(
        [str(src)], DicomStore()), 180)
    assert not outcome["hung"]
    assert "error" not in outcome, outcome.get("error")
    summary = outcome["value"]
    assert summary.ingested == 3
    assert [p for p, _ in summary.failures] == [paths[2]], summary.failures
    assert summary.failures[0][1].startswith(
        io_handlers._RAN_PAST_THE_BOUND.format(bound=BOUND)), summary.failures
    assert _no_child_left(), multiprocessing.active_children()


def test_a_peer_ingest_on_the_stopped_pool_loses_no_file(tmp_path,
                                                         monkeypatch):
    """Two `ingest()` calls on one session share its pool. One holds a file
    that never finishes reading; the watcher stops the pool under both. The
    peer reads its own files again on its own rounds and loses none.

    Killing mutation: a peer's broken round read as the end of its files.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", str(BOUND))
    _wedge_pools(monkeypatch, functools.partial(_wedge_reading, "WEDGE"))
    wedged_dir = tmp_path / "a"
    healthy_dir = tmp_path / "b"
    wedged_dir.mkdir()
    healthy_dir.mkdir()
    src, paths = _folder(wedged_dir, 4, {1: "WEDGE"})
    peer_src, _ = _folder(healthy_dir, 12, {})
    with DicomSession(str(tmp_path / "s.db")) as session:
        results = {}

        def run(name, folder):
            results[name] = session.ingest(str(folder))

        threads = [threading.Thread(target=run, args=("wedged", src),
                                    daemon=True),
                   threading.Thread(target=run, args=("peer", peer_src),
                                    daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(240)
        assert not any(t.is_alive() for t in threads), "an ingest hung"
    assert [p for p, _ in results["wedged"].failures] == [paths[1]]
    assert results["peer"].ingested == 12
    assert not results["peer"].failures


_CTRL_C_SCRIPT = textwrap.dedent("""
    import functools, os, sys
    sys.path.insert(0, {tests!r})
    os.environ["ISOCENTER_MAX_WORKERS"] = "1"
    os.environ["ISOCENTER_SHOW_PROGRESS"] = "0"
    os.environ["ISOCENTER_TASK_TIMEOUT"] = "0"
    from isocenter import parallel
    import test_a_task_that_never_finishes_is_stopped as t

    if __name__ == "__main__":
        init = functools.partial(t._die_or_wedge_reading, {flag!r})
        parallel.resolve_worker_initializer = lambda disable_gc=False: init
        from isocenter.session import DicomSession
        with DicomSession({db!r}) as session:
            session.ingest({src!r})
""")


def test_a_ctrl_c_in_a_retry_round_with_a_wedged_worker_returns(tmp_path):
    """No bound (`0`), one worker, a death in the first round on the first
    file, and then a file that never returns in a one-at-a-time round (with
    one worker the first round never reaches it). Ctrl-C to the process group
    ends the call: the retry pool's exit no longer waits for good on its
    worker (`shutdown(wait=True)`), and no process of the group is left.

    Killing mutation: the retry pool shut down with `wait=True` on every
    way out.
    """
    src, _ = _folder(tmp_path, 4, {0: "POISON", 1: "WEDGE"})
    flag = str(tmp_path / "wedged")
    script = tmp_path / "ctrl_c.py"
    script.write_text(_CTRL_C_SCRIPT.format(
        tests=os.path.dirname(os.path.abspath(__file__)), flag=flag,
        db=str(tmp_path / "s.db"), src=str(src)), encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("COVERAGE_")}
    child = subprocess.Popen([sys.executable, str(script)], env=env,
                             start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        while not os.path.exists(flag) and time.monotonic() < deadline:
            if child.poll() is not None:
                break
            time.sleep(0.1)
        assert os.path.exists(flag), "the wedged file was never reached"
        time.sleep(0.5)
        os.killpg(child.pid, signal.SIGINT)
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail("ingest() did not return after Ctrl-C: the retry "
                        "pool's exit waited on its wedged worker (#673)")
        time.sleep(0.5)
        left = subprocess.run(["pgrep", "-g", str(child.pid)],
                              capture_output=True, text=True,
                              check=False).stdout.split()
        assert left == [], left
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# --- export (the slot pool) -----------------------------------------------------

def test_an_export_that_never_finishes_one_instance_returns_with_one_row(
        tmp_path, monkeypatch):
    """Three instances, two workers, and the worker handed one never
    returns: `export()` returns, the other two are written, and one
    `ERROR` row names that instance and the bound. No worker is left.

    Killing mutation: no deadline in the slot pool (the export hangs); the
    report written as `Export worker failed` keyed UNKNOWN.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_TASK_TIMEOUT", str(BOUND))
    started = []
    monkeypatch.setattr(parallel._SlotPool, "_started",
                        lambda self, process: started.append(process))
    for index in range(3):
        write_ct(tmp_path / "in" / f"{index}.dcm", f"P673{index}",
                 f"673{index}")
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"
    with DicomSession(db) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        assert not session.ingest(str(tmp_path / "in")).failures
        session.save(sync=True)
        session.store_backend.flush_audit_queue()
        uids = sorted(instance.sop_instance_uid
                      for patient in session.store.patients
                      for study in patient.studies
                      for series in study.series
                      for instance in series.instances)
        chosen = uids[1]
        before = len(_error_rows(db))
        with monkeypatch.context() as wedging:
            wedging.setattr(parallel, "resolve_worker_initializer",
                            lambda disable_gc=False: functools.partial(
                                _wedge_writing, chosen))
            try:
                outcome = _run_bounded(lambda: session.export(
                    str(out), use_compression=False), 180)
            finally:
                for process in list(started):
                    try:
                        process.kill()
                    except (OSError, ValueError):
                        pass
        assert not outcome["hung"], "export() waited for good (#673)"
        assert "error" not in outcome, outcome
        summary = outcome["value"]
        session.store_backend.flush_audit_queue()
        added = _error_rows(db)[before:]
    assert summary.written == 2, summary
    assert [uid for uid, _ in summary.failures] == [chosen]
    assert [uid for uid, _ in added] == [chosen], added
    detail = added[0][1]
    assert detail.startswith(f"Export failed for instance {chosen}: "), detail
    assert f"{BOUND} s" in detail and "ISOCENTER_TASK_TIMEOUT" in detail
    written = [p.name for p in out.rglob("*.dcm")]
    assert len(written) == 2 and not [n for n in written if chosen in n]
    assert _gone(started), "a worker of the export's pool is still running"

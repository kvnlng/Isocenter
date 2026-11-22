"""The recycling pool is one-worker spawned executors (#885), and every
spawned process pool is built in one place (#667).

`run_parallel(maxtasksperchild=N)` (every `export()`, and every pass under
`ISOCENTER_MAX_TASKS_PER_CHILD`) used to run on `multiprocessing.Pool`. A
worker of it killed while it waited for work left the inqueue's read lock
held for good, and the pool's exit then waited 19 s on it and left a thread
behind. It now runs on `parallel._SlotPool`: W one-worker executors, each
replaced after N chunks, each holding at most `_SLOT_DEPTH` chunks.

Every call that can hang runs under `_run_bounded`, and the
`recorded_pools` fixture SIGKILLs every worker the test's pools spawned on
the way out. Each test names the mutation that makes it red.
"""
import ast
import concurrent.futures
import functools
import logging
import multiprocessing
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures.process import BrokenProcessPool

import pytest

from isocenter import parallel

# pylint: disable=wrong-import-order
from test_parallel_contract import (_broken_message, _exit_records, _gone,
                                    _killed_pids, _late_leaver_records,
                                    _no_child_left,
                                    _outlive_sigterm, _cannot_leave,
                                    _double_and_mark,
                                    _double_or_die_after_a_ready_sibling,
                                    _run_bounded, double_or_die, identity,
                                    recorded_pools)  # pylint: disable=unused-import


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No test should inherit another's tuning variables."""
    for name in ("ISOCENTER_MAX_WORKERS", "ISOCENTER_CHUNKSIZE",
                 "ISOCENTER_MAX_TASKS_PER_CHILD", "ISOCENTER_DISABLE_GC",
                 "ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
                 "ISOCENTER_SHOW_PROGRESS", "ISOCENTER_WORKER_FAULTHANDLER"):
        monkeypatch.delenv(name, raising=False)


def _pid_of(value):
    """The worker's pid, so a test can count tasks per worker."""
    return os.getpid(), value


def _quick_then_slow(args):
    """Item 0 returns at once. Item 1 writes `busy-<pid>` and then sleeps
    30 s, so the test knows which worker holds a task."""
    value, marker_dir = args
    if value == 1:
        with open(os.path.join(marker_dir, f"busy-{os.getpid()}"), "w",
                  encoding="utf-8"):
            pass
        time.sleep(30)
    return value


def _sleep_then_die_at(args):
    """Sleep `delay`, then return the index; the index `die` ends its
    worker instead, after its delay."""
    index, delay, die = args
    time.sleep(delay)
    if index == die:
        os._exit(13)  # pylint: disable=protected-access
    return index


def _sleep_long(args):
    """Sleep 30 s: a task that is still running when the test interrupts."""
    _, marker_dir = args
    with open(os.path.join(marker_dir, f"task-{os.getpid()}"), "w",
              encoding="utf-8"):
        pass
    time.sleep(30)
    return 0


def _initializer_that_raises():
    """Pool initializer that fails. Under `concurrent.futures` the worker
    logs it and exits with code 0, and its future fails."""
    raise RuntimeError("this worker cannot start")


def test_no_worker_runs_more_than_its_quota():
    """T1: N = 3, so no worker runs more than three chunks, and more than
    two workers take part.

    Killing mutation: the slot's `given < recycle_after` check removed.
    """
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _pid_of, list(range(20)), max_workers=2, maxtasksperchild=3,
        show_progress=False), 120)

    assert not outcome["hung"], outcome
    assert "error" not in outcome, outcome
    pids = [pid for pid, _ in outcome["value"]]
    assert sorted(v for _, v in outcome["value"]) == list(range(20))
    counts = {pid: pids.count(pid) for pid in set(pids)}
    assert max(counts.values()) <= 3, counts
    assert len(counts) > 2, counts
    assert _no_child_left(), multiprocessing.active_children()


def _package_sources():
    root = pathlib.Path(parallel.__file__).parent
    for path in sorted(root.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _enclosing_function(tree, node):
    found = None
    for candidate in ast.walk(tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(child is node for child in ast.walk(candidate)):
                found = candidate.name
    return found


def test_every_spawned_process_pool_is_built_in_one_place():
    """T2: the package holds exactly one `ProcessPoolExecutor(` call and one
    `get_context("spawn")`, both in `parallel.new_spawn_process_pool`, and
    no `multiprocessing.Pool` (#667).

    Killing mutation: any hand-built pool, the regression #667 is about.
    """
    executors, spawns, pools = [], [], []
    for path, tree in _package_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else getattr(func, "id", None))
                if name == "ProcessPoolExecutor":
                    executors.append((path.name,
                                      _enclosing_function(tree, node)))
                if (name == "get_context" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "spawn"):
                    spawns.append((path.name, _enclosing_function(tree, node)))
            if (isinstance(node, ast.Attribute) and node.attr == "Pool"
                    and not isinstance(getattr(node, "ctx", None), ast.Store)):
                pools.append((path.name, node.lineno))
    here = [("parallel.py", "new_spawn_process_pool")]
    assert executors == here, executors
    assert spawns == here, spawns
    assert pools == [], pools


def test_a_retired_worker_that_cannot_leave_is_ended_and_named(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """T3: workers that cannot leave after their quota are SIGKILLed by the
    parent `_BROKEN_POOL_GRACE_S` after they were told to leave, and named;
    every result comes back, and no child is left (Q2 on #885).

    Replaces #888's in-worker SIGALRM. Killing mutation: the retiring check
    removed, so the stream ends with workers that never leave (the
    interpreter's exit then joins them for good).
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2)
    initializer = functools.partial(_cannot_leave, str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _pid_of, list(range(8)), max_workers=2, maxtasksperchild=2,
            show_progress=False), 90)

    assert not outcome["hung"], outcome
    assert "error" not in outcome, outcome
    assert sorted(v for _, v in outcome["value"]) == list(range(8))
    records = _late_leaver_records(caplog)
    assert records, [r.getMessage() for r in caplog.records]
    named = set()
    for record in records:
        assert record.levelno == logging.WARNING
        assert "SIGKILL" in record.getMessage()
        named |= _killed_pids(record)
    worked = {pid for pid, _ in outcome["value"]}
    assert named == worked, (named, worked)
    assert named <= {p.pid for p in recorded_pools}
    assert _exit_records(caplog) == [], [r.getMessage() for r in caplog.records]
    assert _gone(recorded_pools), "a worker of the pool is still running"
    assert _no_child_left(), multiprocessing.active_children()


def test_an_idle_worker_killed_from_outside_fails_the_stream_with_no_thread_left(
        tmp_path, monkeypatch, recorded_pools):
    """T4: the #885 shape, real. Two workers; one holds a 30 s task, the
    other has returned its task and holds nothing. SIGKILL the idle one:
    the stream ends with `BrokenProcessPool` within 5 s, no thread named
    `isocenter-pool-exit` exists, and no child is left.

    Against `multiprocessing.Pool` this took 19 s and left that thread
    running. The victim is the worker with no task, chosen by the busy
    worker's marker, never by pid order. Killing mutation: the idle-slot
    sentinel read removed (the stream waits 30 s on the busy task).
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2)
    stream = parallel.run_parallel(
        _quick_then_slow, [(0, str(markers)), (1, str(markers))],
        max_workers=2, maxtasksperchild=25, show_progress=False,
        yield_exceptions=True, return_generator=True)
    assert next(stream) == 0
    deadline = time.monotonic() + 60
    while not [n for n in os.listdir(markers) if n.startswith("busy-")]:
        assert time.monotonic() < deadline, "the slow task never started"
        time.sleep(0.01)
    busy = {int(n[len("busy-"):]) for n in os.listdir(markers)}
    idle = [p for p in recorded_pools if p.pid not in busy]
    assert len(idle) == 1, (busy, recorded_pools)
    os.kill(idle[0].pid, signal.SIGKILL)
    killed_at = time.monotonic()

    outcome = _run_bounded(lambda: list(stream), 60)

    assert not outcome["hung"], outcome
    assert time.monotonic() - killed_at < 5, (
        "the stream took more than 5 s to see an idle worker die")
    assert isinstance(outcome["value"][-1], BrokenProcessPool), outcome
    assert not any(t.name == "isocenter-pool-exit"
                   for t in threading.enumerate())
    assert _gone(recorded_pools), "a worker of the pool is still running"
    assert _no_child_left(), multiprocessing.active_children()


def test_a_death_names_the_chunks_it_lost(tmp_path, monkeypatch,
                                          recorded_pools):
    """T5: the trailing `BrokenProcessPool` carries `lost`: exactly the
    `(first index, chunk)` pairs the dead worker held, and nothing another
    worker held. Its message names the pid and `exit code 13`.

    Two workers, chunksize 2, depth 2: worker A holds chunks 0 and 2, B
    holds 1 and 3, and the item -4 in chunk 1 ends B once chunks 0 and 2
    have run. Killing mutation: `lost` left empty, or taken from a chunk
    the worker did not hold. Neither `lost` taken from every live slot
    (A's last chunk has returned by then; see
    `test_a_death_does_not_name_a_chunk_a_healthy_worker_still_holds`) nor
    the exit code read before the sentinel is ready (the executor fails the
    future only once it has seen the worker end) is killed here.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    values = [1, 2, 3, -4, 5, 6, 7, 8, 9, 10]
    items = [(v, str(markers)) for v in values]
    stream = parallel.run_parallel(
        _double_and_mark, items, max_workers=2, maxtasksperchild=25,
        chunksize=2, show_progress=False, yield_exceptions=True,
        return_generator=True)
    first = next(stream)
    deadline = time.monotonic() + 60
    while not all((markers / f"done-{v}").exists() for v in (1, 2, 5, 6)):
        assert time.monotonic() < deadline, os.listdir(markers)
        time.sleep(0.01)
    (markers / "go").touch()

    outcome = _run_bounded(lambda: list(stream), 60)

    assert not outcome["hung"], outcome
    results = [first] + outcome["value"]
    exc = results[-1]
    message, pid, code = _broken_message(exc)
    assert code == 13 and "exit code 13" in message, message
    assert pid in {p.pid for p in recorded_pools}
    assert exc.lost == ((2, items[2:4]), (6, items[6:8])), exc.lost
    lost_values = {v for _, chunk in exc.lost for v, _ in chunk}
    assert not lost_values & {r // 2 for r in results[:-1]}, results
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_an_ordered_slot_run_yields_in_submission_order_and_a_prefix_before_a_death(
        recorded_pools):
    """T6: ordered, with later items finishing first; the run yields in
    submission order. With a death at index 4, only indices before 4 come
    before the exception, and they are a prefix.

    Killing mutation: yielding as completed under `ordered=True`.
    """
    clean = [(i, (5 - i) * 0.05, None) for i in range(6)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _sleep_then_die_at, clean, max_workers=3, maxtasksperchild=25,
        show_progress=False, ordered=True), 120)
    assert not outcome["hung"], outcome
    assert outcome["value"] == list(range(6)), outcome

    dying = [(i, (7 - i) * 0.05, 4) for i in range(8)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _sleep_then_die_at, dying, max_workers=3, maxtasksperchild=25,
        show_progress=False, ordered=True, yield_exceptions=True), 120)
    assert not outcome["hung"], outcome
    values = outcome["value"]
    assert isinstance(values[-1], BrokenProcessPool), values
    assert values[:-1] == list(range(len(values) - 1)), values
    assert len(values) - 1 <= 4, values
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_items_are_read_lazily(recorded_pools):
    """T7: after the first result, at most 2 * W * depth items have been
    read: `export_batch` hands an iterator.

    Killing mutation: `list(items)`.
    """
    read = []

    def items():
        for value in range(200):
            read.append(value)
            yield value

    stream = parallel.run_parallel(
        identity, items(), max_workers=2, maxtasksperchild=25,
        show_progress=False, return_generator=True)
    try:
        first = _run_bounded(lambda: next(stream), 60)
        assert not first["hung"], first
        # Not W * depth + 1: one wake of the wait can return several
        # finished tasks, and each is replaced. Still far below the 200.
        assert len(read) <= 2 * 2 * parallel._SLOT_DEPTH, len(read)
    finally:
        closed = _run_bounded(stream.close, 60)
    assert not closed["hung"], closed
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_ctrl_c_mid_stream_ends_every_slot_at_once(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """T8: a Ctrl-C that lands in the stream's wait reaches the caller, and
    every worker, which here outlives SIGTERM, is gone within a second of
    the grace (patched to 0.5 s); one WARNING names them.

    The signal is sent once both workers are running their 30 s tasks and
    the main thread is seen, twice, inside `concurrent.futures.wait` under
    the pool's stream. Killing mutations: the kill in an `except` that
    re-raises before it kills; a `return` in the exit's `finally`.
    """
    assert threading.current_thread() is threading.main_thread()
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 0.5)
    initializer = functools.partial(_outlive_sigterm, str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)
    main = threading.main_thread().ident
    sent = threading.Event()
    done = threading.Event()

    def in_the_wait():
        if len(os.listdir(markers)) < 4:  # 2 ready from init, 2 from tasks
            return False
        frame = sys._current_frames().get(main)  # pylint: disable=protected-access
        names = []
        while frame is not None:
            names.append(frame.f_code.co_name)
            frame = frame.f_back
        return "_stream" in names and "wait" in names

    def interrupt():
        deadline = time.monotonic() + 60
        while not done.is_set() and time.monotonic() < deadline:
            if in_the_wait():
                time.sleep(0.02)
                if in_the_wait():
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
    watcher = threading.Thread(target=interrupt, daemon=True)
    watchdog = threading.Timer(90, free_a_hang)
    interrupted = False
    try:
        watcher.start()
        watchdog.start()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            try:
                parallel.run_parallel(
                    _sleep_long, [(i, str(markers)) for i in range(2)],
                    max_workers=2, maxtasksperchild=25, show_progress=False)
            except KeyboardInterrupt:
                interrupted = True
        ended = time.monotonic()
    finally:
        done.set()
        watcher.join(60)
        watchdog.cancel()
        signal.signal(signal.SIGINT, previous)

    assert sent.is_set(), "the interrupt was never sent"
    assert interrupted, "the KeyboardInterrupt did not reach the caller"
    assert _gone(recorded_pools, 1.0), (
        "a worker was still running when the interrupted stream returned")
    assert time.monotonic() - ended < 5
    records = _exit_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert _killed_pids(records[0]) == {p.pid for p in recorded_pools}
    assert _no_child_left(), multiprocessing.active_children()


def test_a_slot_that_breaks_does_not_hang_the_next_submit(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """T10: two workers that outlive SIGTERM; one dies. The stream raises
    within the grace and returns, the survivor is killed and named, and no
    child is left.

    This is the test that keeps the slot from being "simplified" into one
    W-wide executor, whose manager thread SIGTERMs and joins the survivor
    while it holds the lock the next `submit()` takes (#796, #861).
    Killing mutation: a 2-wide executor in place of the slots.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    initializer = functools.partial(_outlive_sigterm, str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)
    items = [(value, str(markers)) for value in (1, -2, 3, 4, 5, 6)]

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _double_or_die_after_a_ready_sibling, items, max_workers=2,
            maxtasksperchild=25, show_progress=False,
            yield_exceptions=True), 60)

    assert not outcome["hung"], outcome
    _broken_message(outcome["value"][-1])
    assert len(_exit_records(caplog)) == 1, [
        r.getMessage() for r in caplog.records]
    assert _gone(recorded_pools), "a worker of the pool is still running"
    assert _no_child_left(), multiprocessing.active_children()


def test_a_pool_initializer_that_raises_fails_the_stream_without_blaming_exit_code_0(
        monkeypatch, recorded_pools):
    """K7, rewritten for executors: a worker whose initializer raises logs
    it and exits with code 0, and its future fails. The stream fails with
    `BrokenProcessPool`, whose message quotes no exit code as the cause.

    Killing mutation: a death read from a nonzero exit code (the stream
    then waits for good on the dead worker's future, or a 0 is quoted).
    """
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: _initializer_that_raises)
    outcome = _run_bounded(lambda: parallel.run_parallel(
        identity, [1, 2], max_workers=1, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True), 60)

    assert not outcome["hung"], outcome
    exc = outcome["value"][-1]
    assert isinstance(exc, BrokenProcessPool), outcome
    assert type(exc) is BrokenProcessPool
    assert "exit code" not in str(exc), str(exc)
    assert "initializer raises" in str(exc), str(exc)
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_new_spawn_process_pool_pins_spawn_and_the_initializer(monkeypatch):
    """T11: the helper builds with the spawn context and the resolved
    initializer, and `disable_gc` reaches it; the recycling pool and
    `run_parallel`'s own pool both build through it (#667, #220).

    Killing mutations: the `mp_context` argument dropped; `disable_gc` not
    passed on.
    """
    calls = []
    real = concurrent.futures.ProcessPoolExecutor

    class Recording(real):
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Recording)
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

    pool = parallel.new_spawn_process_pool(1, disable_gc=True)
    pool.shutdown(wait=True)
    assert calls[-1]["initializer"].keywords["disable_gc"] is True
    assert parallel.run_parallel(identity, [1, 2], max_workers=2,
                                 show_progress=False) == [1, 2]
    assert sorted(parallel.run_parallel(
        identity, [1, 2], max_workers=2, maxtasksperchild=5,
        show_progress=False)) == [1, 2]
    assert len(calls) >= 3, calls
    for kwargs in calls:
        assert kwargs["mp_context"].get_start_method() == "spawn", kwargs
        assert "max_tasks_per_child" not in kwargs, kwargs
    assert [k["max_workers"] for k in calls[2:]] == [1] * len(calls[2:]), (
        "the recycling pool's executors are one worker each")


def test_the_ingest_retry_pool_passes_disable_gc_to_the_helper():
    """T11, the retry pool's half: `_ingest_results` builds its retry pools
    through the helper with the strategy's `disable_gc`, read from its
    syntax tree (the retry rounds need a dying worker to reach).

    Killing mutation: the keyword dropped from that call.
    """
    # By path, beside `parallel.py`: an import of the module would put this
    # file in its `TARGETS` row for one syntax-tree read.
    source = pathlib.Path(parallel.__file__).with_name("io_handlers.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "id", getattr(node.func, "attr", None))
             == "new_spawn_process_pool"]
    assert len(calls) == 1, calls
    keywords = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert keywords == {"disable_gc": "strategy.disable_gc"}, keywords


def test_the_executor_internals_the_slot_pool_reads_are_there():
    """The private shapes the slot pool reads, pinned on each interpreter:
    `submit()` spawns the worker before it returns, so `_processes` holds
    it; `shutdown(wait=False)` drops `_processes`, which is why the handle
    is captured at spawn; a dead worker sets `_broken`.

    A CPython that renamed either would leave every worker unrecorded, and
    the exits unable to end it.
    """
    pool = parallel.new_spawn_process_pool(1)
    try:
        future = pool.submit(identity, 1)
        processes = list(pool._processes.values())
        assert len(processes) == 1
        process = processes[0]
        assert process.pid and isinstance(process.sentinel, int)
        assert future.result(timeout=60) == 1
        assert not pool._broken
        dying = pool.submit(double_or_die, -1)
        with pytest.raises(BrokenProcessPool):
            dying.result(timeout=60)
        assert pool._broken
        with pytest.raises(BrokenProcessPool):
            pool.submit(identity, 1)
        pool.shutdown(wait=False)
        assert pool._processes is None
    finally:
        try:
            process.kill()
        except (OSError, ValueError):
            pass
        pool.shutdown(wait=True)


def test_retired_workers_give_their_threads_and_descriptors_back():
    """Forty workers, each replaced after one task: once the run has ended,
    the parent's threads and open file descriptors are back where they
    started. `shutdown(wait=False)` leaves each executor's manager thread
    and pipes to end on their own, so a leak would grow with every worker.

    Killing mutation: a retired slot kept referenced, or its executor never
    shut down. No `recorded_pools` here: a held process object keeps its
    sentinel's descriptor open, which is the leak this test looks for.
    """
    import gc  # pylint: disable=import-outside-toplevel

    def counts():
        gc.collect()
        return threading.active_count(), len(os.listdir("/dev/fd"))

    before = counts()
    outcome = _run_bounded(lambda: parallel.run_parallel(
        identity, list(range(40)), max_workers=4, maxtasksperchild=1,
        show_progress=False), 240)
    assert not outcome["hung"], outcome
    assert sorted(outcome["value"]) == list(range(40))
    def grown():
        now = counts()
        return now[0] > before[0] or now[1] > before[1]

    deadline = time.monotonic() + 15
    while grown() and time.monotonic() < deadline:
        time.sleep(0.1)
    # Each number on its own: a tuple compares by its first difference.
    assert not grown(), (before, counts())


def test_the_slot_depth_is_the_owner_ruled_two():
    """`_SLOT_DEPTH` is 2 by owner ruling (Q1 on #885, 2026-11-21): T0
    measured the export at depth 2 against `multiprocessing.Pool`, and a
    deeper slot holds more chunks a death loses. Changing it is a ruling,
    not a tuning.

    Killing mutation: any other depth.
    """
    assert parallel._SLOT_DEPTH == 2  # pylint: disable=protected-access


def test_a_finished_worker_that_cannot_leave_is_ended_while_the_stream_runs(
        tmp_path, monkeypatch, caplog, recorded_pools):
    """A worker that ran its quota and cannot leave is SIGKILLed about
    `_BROKEN_POOL_GRACE_S` after it was told to leave, while the stream is
    still running, not when the stream ends (Q2 on #885).

    One worker at a time, one chunk each: the first returns at once and is
    told to leave; the second runs 8 s. The grace is 1 s, so the first is
    killed some 7 s before the stream ends.

    Killing mutation: the late-leaver check removed from the stream's loop,
    leaving only the one in the finished exit.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2)
    initializer = functools.partial(_cannot_leave, str(markers))
    monkeypatch.setattr(parallel, "resolve_worker_initializer",
                        lambda disable_gc=False: initializer)

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        outcome = _run_bounded(lambda: parallel.run_parallel(
            _sleep_then_die_at, [(0, 0.0, None), (1, 8.0, None)],
            max_workers=1, maxtasksperchild=1, show_progress=False), 90)
        ended = time.time()

    assert not outcome["hung"], outcome
    assert "error" not in outcome, outcome
    assert sorted(outcome["value"]) == [0, 1]
    records = _late_leaver_records(caplog)
    assert records, [r.getMessage() for r in caplog.records]
    first = min(records, key=lambda r: r.created)
    assert ended - first.created > 4, (
        "the worker that finished first was killed only "
        f"{ended - first.created:.1f} s before the stream ended")
    assert _gone(recorded_pools), "a worker of the pool is still running"


def test_a_death_does_not_name_a_chunk_a_healthy_worker_still_holds(
        recorded_pools):
    """`lost` names only what the dead worker held: a chunk another, healthy
    worker is still running is not lost, and is not run again by #668.

    Two workers: item 0 ends its worker after 0.5 s; item 1 runs 8 s in the
    other and is still running when the death is seen.

    Killing mutation: `lost` taken from every live slot's chunks.
    """
    items = [(0, 0.5, 0), (1, 8.0, None)]
    outcome = _run_bounded(lambda: parallel.run_parallel(
        _sleep_then_die_at, items, max_workers=2, maxtasksperchild=25,
        show_progress=False, yield_exceptions=True), 60)

    assert not outcome["hung"], outcome
    exc = outcome["value"][-1]
    _, _, code = _broken_message(exc)
    assert code == 13, exc
    assert exc.lost == ((0, [items[0]]),), exc.lost
    assert _gone(recorded_pools), "a worker of the pool is still running"


class _FakeProcess:
    """A worker process whose sentinel is a pipe: it has ended once the
    write end is closed."""

    def __init__(self, pid):
        self.pid = pid
        self.sentinel, self._write = os.pipe()
        self.exitcode = None

    def end(self, code):
        if self.exitcode is None:
            self.exitcode = code
            os.close(self._write)

    def terminate(self):
        self.end(-signal.SIGTERM)

    def kill(self):
        self.end(-signal.SIGKILL)


class _FakeExecutor:
    """A one-worker executor whose futures the test settles. `plan` maps an
    item to what its chunk's future is at `submit()`: `"broken"` (failed
    with `BrokenProcessPool`, the worker ended with code 13),
    `"broken, not yet ended"` (failed, its sentinel not ready yet),
    `"pending"`, or `"pending, ended"` (pending, with the worker ended)."""

    def __init__(self, plan, made):
        self._plan = plan
        self.process = _FakeProcess(90000 + len(made))
        self._processes = {self.process.pid: self.process}
        self.futures = {}
        made.append(self)

    def submit(self, _fn, _func, chunk):
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        what = self._plan[chunk[0]]
        if what == "broken":
            self.process.end(13)
            future.set_exception(BrokenProcessPool("gone"))
        elif what == "broken, not yet ended":
            future.set_exception(BrokenProcessPool("gone"))
        elif what == "pending, ended":
            self.process.end(13)
        self.futures[chunk[0]] = future
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        del wait, cancel_futures
        self._processes = None


@pytest.fixture
def fake_slots(monkeypatch):
    """`_SlotPool` over `_FakeExecutor`s: yields `(made, use)`, where
    `use(plan)` sets the plan every executor opened from then on reads."""
    made = []
    plan = {}
    monkeypatch.setattr(
        parallel, "new_spawn_process_pool",
        lambda width, disable_gc=False: _FakeExecutor(plan, made))
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.05)
    try:
        yield made, plan.update
    finally:
        for executor in made:
            executor.process.end(0)
            os.close(executor.process.sentinel)


def _drain(stream):
    """Every value the stream yields before it raises, and what it raised."""
    values = []
    try:
        for value in stream:
            values.append(value)
    except BrokenProcessPool as exc:
        return values, exc
    return values, None


def _fake_pool():
    return parallel._SlotPool(  # pylint: disable=protected-access
        2, recycle_after=None, disable_gc=False, depth=1)


def test_every_worker_that_died_in_the_same_wake_has_its_chunks_in_lost(
        fake_slots):
    """Two workers' futures fail together: `lost` is the union of what
    both held, not the first worker's alone (Q-B on #1199). The second
    worker's sentinel is not ready yet, so only its failed future says it
    died.

    Killing mutation: only the first dead slot read by `_settle`.
    """
    _, use = fake_slots
    use({10: "broken", 20: "broken, not yet ended"})

    values, exc = _drain(_fake_pool().run(identity, [10, 20], 1, False))

    assert values == []
    assert exc is not None and exc.lost == ((0, [10]), (1, [20])), exc


def test_a_worker_found_ended_beside_a_death_has_its_chunks_in_lost(
        fake_slots):
    """One worker's future fails; the other's worker has ended too, but its
    future is not failed yet (the executor's manager thread marks it). Both
    workers' chunks are lost, so both are named (Q-B on #1199).

    Killing mutation: the sentinel read of the other live slots removed
    from the death's handling.
    """
    _, use = fake_slots
    use({10: "broken", 20: "pending, ended"})

    values, exc = _drain(_fake_pool().run(identity, [10, 20], 1, False))

    assert values == []
    assert exc is not None and exc.lost == ((0, [10]), (1, [20])), exc


def test_a_result_returned_as_a_death_is_seen_is_delivered_before_it(
        fake_slots, monkeypatch):
    """A result another worker returned just after the wait that saw a
    death is delivered before `BrokenProcessPool`, and is not in `lost`.

    The wait is wrapped so worker B's future completes the moment worker
    A's failed one is handed back, as the executors' threads race in a real
    run.

    Killing mutation: the other workers' finished futures not settled
    before the death is raised.
    """
    made, use = fake_slots
    use({10: "broken", 20: "pending"})
    real_wait = concurrent.futures.wait

    def wait_then_b_returns(futures, timeout=None, return_when=None):
        done, not_done = real_wait(futures, timeout=timeout,
                                   return_when=return_when)
        late = made[1].futures[20]
        if not late.done():
            late.set_result([40])
        return done, not_done

    monkeypatch.setattr(concurrent.futures, "wait", wait_then_b_returns)

    values, exc = _drain(_fake_pool().run(identity, [10, 20], 1, False))

    assert values == [40]
    assert exc is not None and exc.lost == ((0, [10]),), exc


def test_a_result_returned_before_a_refused_submit_is_delivered_before_it(
        fake_slots, monkeypatch):
    """A worker that is found dead when it is handed its next chunk fails
    the stream; the results already returned are delivered before
    `BrokenProcessPool`.

    Both first chunks return; then the executor handed chunk 30 refuses it.

    Killing mutation: a death seen by `_fill` raised before the results
    already in hand are yielded.
    """
    made, use = fake_slots
    use({10: "pending", 20: "pending", 30: "pending"})
    real_submit = _FakeExecutor.submit

    def submit(self, fn, func, chunk):
        if chunk[0] == 30:
            raise BrokenProcessPool("refused")
        future = real_submit(self, fn, func, chunk)
        future.set_result([chunk[0] * 2])
        return future

    monkeypatch.setattr(_FakeExecutor, "submit", submit)

    values, exc = _drain(_fake_pool().run(identity, [10, 20, 30], 1, False))

    assert sorted(values) == [20, 40]
    assert exc is not None and exc.lost == ((2, [30]),), exc
    assert len(made) == 2


# The task and the initializer live in a module of their own, as a real
# script's would: a function defined in `__main__` holds `__main__`'s
# globals, which puts the stream in a cycle that is collected before the
# modules are cleared. With none, the stream's `finally` runs during module
# teardown, after `parallel`'s globals are None, which is where the
# "Exception ignored" of the review of #1199 (finding 3) was printed.
_LEFT_OPEN_TASKS = '''
import threading
import time


def stay():
    """A worker that cannot leave by its sentinel: a thread that is not a
    daemon holds it."""
    threading.Thread(target=time.sleep, args=(3600,), daemon=False).start()


def slow(seconds):
    time.sleep(seconds)
    return seconds
'''

_LEFT_OPEN_AT_EXIT = '''
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from isocenter import parallel
import left_open_tasks

if __name__ == "__main__":
    # A runner may start the test with SIGINT ignored, and a child inherits
    # that; the interrupt case needs Python's own handler.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    parallel._BROKEN_POOL_GRACE_S = 1.0
    parallel.resolve_worker_initializer = (
        lambda disable_gc=False: left_open_tasks.stay)
    STREAM = parallel.run_parallel(
        left_open_tasks.slow, [0.0] + [3600.0] * 7, max_workers=4,
        maxtasksperchild=25, show_progress=False, return_generator=True)
    print("first", next(STREAM), flush=True)
    if sys.argv[1] == "interrupt":
        # The parent's own work on a result: a Ctrl-C lands here, in the
        # consumer, not in the pool's wait, and is not caught.
        time.sleep(3600)
    sys.exit(0)
'''


@pytest.mark.parametrize("how", ["exit", "interrupt"])
def test_an_interpreter_exit_with_a_stream_left_open_ends_its_workers(
        tmp_path, how):
    """A script that leaves a recycling stream open (held in a global, or
    by the traceback of a Ctrl-C in its loop) and exits does exit, within
    a bound, with no worker left (Q-A on #1199).

    The workers hold hour-long tasks and cannot leave by their sentinel.
    `concurrent.futures`' own exit hook joins every executor's manager
    thread, which waits for those tasks; `parallel._end_open_slot_pools`
    must run first. This runs a real interpreter exit, so the order of the
    two hooks is measured on each interpreter the gate runs.

    The stream's own `finally` then runs again during module teardown and
    must find nothing left to end; it printed "Exception ignored ...
    'NoneType' object has no attribute 'monotonic'" (finding 3).

    Killing mutations: the hook not registered; registered with `atexit`
    (which runs after the joins); the pool never added to the set; the
    second unfinished exit not a no-op.
    """
    (tmp_path / "left_open_tasks.py").write_text(_LEFT_OPEN_TASKS,
                                                 encoding="utf-8")
    script = tmp_path / "left_open.py"
    script.write_text(_LEFT_OPEN_AT_EXIT, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("COVERAGE_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    child = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, str(script), how],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        start_new_session=True, text=True)
    try:
        if how == "interrupt":
            assert child.stdout.readline().startswith("first"), "no result"
            time.sleep(1.0)
            os.kill(child.pid, signal.SIGINT)
        try:
            out, err = child.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail("the interpreter did not exit within 60 s with a "
                        "recycling stream left open")
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        child.wait()
    elapsed = time.monotonic() - started

    assert elapsed < 60, elapsed
    if how == "exit":
        assert child.returncode == 0, (child.returncode, err)
        assert out.startswith("first 0.0"), (out, err)
    else:
        assert "KeyboardInterrupt" in err, err
    assert "Exception ignored" not in err, err


class _RemoveInterrupted(list):
    """A slot list whose `remove()` is where a Ctrl-C lands."""

    def remove(self, value):
        raise KeyboardInterrupt


def test_a_ctrl_c_as_a_worker_is_retired_leaves_it_watched_once(
        fake_slots):
    """A Ctrl-C between watching a retired worker and dropping it from the
    live slots leaves it in at least one list the unfinished exit reads,
    and that exit signals it once, not once per list.

    Killing mutations: the worker dropped from the live slots before it is
    watched (a Ctrl-C there leaves it in neither list, and no exit ends
    it); the exit's processes not made unique.
    """
    made, use = fake_slots
    use({10: "pending"})
    pool = _fake_pool()
    pool._func = identity  # pylint: disable=protected-access
    slot = pool._open()  # pylint: disable=protected-access
    assert pool._submit(slot, (0, 0, [10]))  # pylint: disable=protected-access
    process = made[0].process
    signals = []
    process.terminate = lambda: signals.append("TERM")
    pool._live = _RemoveInterrupted(pool._live)  # pylint: disable=protected-access

    with pytest.raises(KeyboardInterrupt):
        pool._retire(slot)  # pylint: disable=protected-access

    assert process in [p for p, _ in pool._retiring]  # pylint: disable=protected-access
    process.end(0)
    pool._unfinished_exit()  # pylint: disable=protected-access
    assert signals == ["TERM"], signals

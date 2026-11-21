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
    have run. Killing mutations: `lost` taken from every future in flight;
    the exit code read before the sentinel is ready.
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
    """T7: after the first result, at most W * depth + 1 items have been
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
        assert len(read) <= 2 * parallel._SLOT_DEPTH + 1, len(read)
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
    from isocenter import io_handlers  # pylint: disable=import-outside-toplevel
    tree = ast.parse(pathlib.Path(io_handlers.__file__).read_text(
        encoding="utf-8"))
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
    deadline = time.monotonic() + 15
    while counts() > before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert counts() <= before, (before, counts())

"""What `run_parallel` promises, across its three execution paths.

It dispatches to a shared executor, a recycling `multiprocessing.Pool`,
or a fresh executor, and each path had its own copy of the progress-bar
setup and its own reading of the environment. These tests state the
behaviour those copies were supposed to share, so the copies can be
removed.
"""
import logging
import os

import pytest

from isocenter import parallel


def identity(value):
    return value


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

    `imap_unordered`, so the contract here is membership, not order.
    """
    results = parallel.run_parallel(
        double_or_raise, [1, -2, 3], show_progress=False, max_workers=2,
        maxtasksperchild=1, yield_exceptions=True)

    exceptions = [r for r in results if isinstance(r, Exception)]
    assert sorted(r for r in results if not isinstance(r, Exception)) == [2, 6]
    assert len(exceptions) == 1 and isinstance(exceptions[0], ValueError)


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


# --- #250: the child-side watchdog ------------------------------------------
#
# pytest's faulthandler_timeout dumps the *parent's* threads; #250's
# occurrence-four dump showed all of them idle, because the 900-second
# lock holder was a pool child no parent-side instrumentation can see
# into. `ISOCENTER_WORKER_FAULTHANDLER=1` (set only in tests.yml) arms
# `faulthandler.dump_traceback_later(..., exit=False)` inside each
# worker process, so the next child-side stall of any mechanism delivers
# the child's own stack.


def sleep_past_the_watchdog(value):
    """Module scope: it has to pickle into a process-pool worker."""
    import time
    time.sleep(1.5)
    return value * 2


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
    """
    from isocenter import parallel

    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    monkeypatch.setenv("ISOCENTER_WORKER_FAULTHANDLER", "1")
    monkeypatch.setattr(parallel, "_WORKER_FAULTHANDLER_TIMEOUT_S", 0.2)

    results = parallel.run_parallel(
        sleep_past_the_watchdog, [1, 2], show_progress=False, max_workers=1)

    assert results == [2, 4], (
        "the watchdog changed the run's outcome; it must only ever dump "
        "(exit=False), never kill the worker (#250)")

    stderr = capfd.readouterr().err
    assert "Timeout" in stderr and "Thread" in stderr, (
        "no traceback dump reached stderr: the spawned child never armed "
        "its watchdog, so the next CI stall is again missing the child's "
        f"half of the picture (#250). stderr was: {stderr!r}")

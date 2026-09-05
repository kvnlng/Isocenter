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

    Worker recycling beats both, whichever way they are set: only
    `multiprocessing.Pool` implements `maxtasksperchild`. That is why
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

    assert parallel._use_threads(False, None) is True, (
        "ISOCENTER_FORCE_PROCESSES overrode ISOCENTER_FORCE_THREADS; "
        "docs/environment.md says the reverse")
    assert parallel._use_threads(False, 25) is False, (
        "worker recycling was asked for and threads were chosen anyway; "
        "only multiprocessing.Pool implements maxtasksperchild")


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
    assert parallel._use_threads(False, None) is False, (
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
# rules threads out however the rest of the environment is set -- only
# `multiprocessing.Pool` implements it. So the export path runs in
# processes on every interpreter, including a free-threaded build, and
# `ISOCENTER_FORCE_THREADS` cannot change that. That is a decision (the
# recycling reclaims memory leaked by the imaging C libraries, and a
# thread pool has no process to recycle), and until now it was a silent
# one: the request was dropped with nothing anywhere saying so.


def test_a_forced_thread_request_is_reported_when_worker_recycling_overrides_it(
        monkeypatch, caplog):
    """The override is announced, once, where the precedence lives (#185).

    Both levers reach the same arm and both are asserted: the
    environment variable, which is what `docs/environment.md` documents
    as the way to force threads, and the `force_threads=True` argument,
    which is reachable only from user code -- the one in-library
    `force_threads=True` (the discovery scan) passes no
    `maxtasksperchild` and so never contradicts anything.

    The message has to name **both** levers and quote the recycling
    value: a warning that says only "there is a conflict" cannot be
    matched against what the operator typed, which is the same argument
    `_env_int`'s and `ISOCENTER_MAX_WORKERS`' warnings already make.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    with caplog.at_level(logging.WARNING):
        assert parallel._use_threads(False, 25) is False, (
            "recycling must still win; this test is about the silence, "
            "not about the decision")

    messages = [record.message for record in caplog.records]
    assert any("ISOCENTER_FORCE_THREADS" in message for message in messages), (
        "the request was overridden without naming the lever that was "
        "ignored, so an operator cannot tell why threads did not happen")
    assert any("maxtasksperchild" in message for message in messages), (
        "the warning does not name what overrode the request")
    assert any("25" in message for message in messages), (
        "the warning does not quote the recycling value that won")

    caplog.clear()
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS")
    with caplog.at_level(logging.WARNING):
        assert parallel._use_threads(True, 25) is False

    argument_messages = [record.message for record in caplog.records]
    assert any("force_threads" in message for message in argument_messages), (
        "an explicit force_threads=True argument was overridden in "
        "silence; the caller can see their own literal, but not that it "
        "lost to a value passed somewhere else")


def test_no_warning_when_recycling_was_not_contradicted(caplog):
    """The silence half, and it is the ordinary export path.

    `session.export()` calls exactly this -- `force_threads=False`, no
    environment variable, `maxtasksperchild=25` -- on every export.
    Nobody asked for threads, so nothing was overridden, and a warning
    on every export would be noise that teaches readers to filter this
    logger.
    """
    with caplog.at_level(logging.WARNING):
        assert parallel._use_threads(False, 25) is False

    assert not [record for record in caplog.records
                if "maxtasksperchild" in record.message], (
        "the ordinary export path warns; nothing was contradicted")


def test_export_runs_in_processes_by_decision(monkeypatch):
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
    plus `tests/profile_memory.py` assume a subprocess boundary and must
    be revisited before that lands --
    `tests/test_private_tag_vr_roundtrip.py`,
    `tests/test_redaction_failure_is_reported.py`,
    `tests/test_float_pixel_data_export.py`,
    `tests/test_export_worker_graph_purity.py`,
    `tests/test_redaction_identity.py`,
    `tests/test_redaction_attestation.py`,
    `tests/test_redaction_multizone.py` and this file.
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
    assert any("0" in record.message for record in caplog.records), (
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

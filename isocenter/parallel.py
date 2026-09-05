"""Parallel execution helpers.

`run_parallel` is the single entry point every parallel pass in Isocenter
goes through -- scanning, exporting, verification. It picks between three
execution strategies and adapts to a set of `ISOCENTER_*` environment
variables, so that tuning a cohort run never means editing code.
"""
import concurrent.futures
import functools
import os
import sys
import multiprocessing
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Any, Optional, TypeVar

from tqdm import tqdm

from .logger import get_logger

T = TypeVar('T')
R = TypeVar('R')

_TRUTHY = ("1", "true", "on", "yes")
_FALSEY = ("0", "false", "off", "no")

#: How long a worker process may live before it dumps every one of its
#: threads' tracebacks to stderr (diagnostic, never fatal). Sits below
#: pytest's faulthandler_timeout (300s) so a stalled child's dump lands
#: inside the same log window as the parent's -- occurrence four of #250
#: dumped every parent thread idle and could not see into the child that
#: actually held the 900-second story. test_packaging_contract.py pins
#: the inequality. Only consulted when `ISOCENTER_WORKER_FAULTHANDLER`
#: is set, which only tests.yml does.
_WORKER_FAULTHANDLER_TIMEOUT_S = 240


def _worker_init(disable_gc=False, faulthandler_timeout=None):
    """Runs once inside each freshly spawned or recycled worker process.

    Module scope because it must pickle into the child; imports live
    inside because they execute there, and keeping them with the only
    code that uses them makes clear whose collector and whose
    faulthandler are being touched -- the worker's, not the parent's.

    `exit=False` is load-bearing: the dump is diagnosis, and a
    slow-but-healthy worker must go on to finish its task rather than be
    killed by its own instrumentation -- `exit=True` would turn every
    long task into a lost one, which under the recycling pool is a hang
    (`run_parallel`'s docstring). Each child arms its own timer, so
    worker recycling re-arms it with the fresh process.
    """
    # pylint: disable=import-outside-toplevel
    if disable_gc:
        import gc
        gc.disable()
    if faulthandler_timeout is not None:
        import faulthandler
        faulthandler.dump_traceback_later(faulthandler_timeout, exit=False)


def resolve_worker_initializer(disable_gc: bool = False):
    """The one initializer worker processes run, or None if none is needed.

    Resolved in the *parent*, at pool-construction time: the returned
    `functools.partial` carries its settings as pickled arguments, so
    the child obeys what the parent decided rather than re-reading
    environment or module state after a spawn -- which is also what
    makes `_WORKER_FAULTHANDLER_TIMEOUT_S` patchable in tests.

    `Session._executor` uses this directly; `run_parallel`'s strategies
    reach it through `_Strategy.worker_initializer`, which adds the
    threads-get-nothing rule. One resolver, so the two kinds of pool
    cannot drift apart on what a worker's first act is.
    """
    disable_gc = disable_gc or _env_is("ISOCENTER_DISABLE_GC", ("1",))
    faulthandler_timeout = (
        _WORKER_FAULTHANDLER_TIMEOUT_S
        if _env_is("ISOCENTER_WORKER_FAULTHANDLER", ("1",)) else None)
    if not disable_gc and faulthandler_timeout is None:
        return None
    return functools.partial(_worker_init, disable_gc=disable_gc,
                             faulthandler_timeout=faulthandler_timeout)


class _ExceptionAsResult:
    """Runs one task; the task's exception becomes its return value.

    This is the worker-side half of `yield_exceptions=True`. A class at
    module scope rather than a closure because it has to pickle into a
    process-pool worker alongside the `func` it wraps -- the same
    constraint that keeps `scan_worker` and friends at module scope.

    `except Exception` is the contract, not sloppiness: whatever one
    task raises must cost that task alone, never the pass. What it
    deliberately does not catch is `BaseException` -- a `KeyboardInterrupt`
    or a dying interpreter should still tear the run down.
    """

    def __init__(self, func):
        self.func = func

    def __call__(self, item):
        try:
            return self.func(item)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return exc


def _trailing_exception(iterator):
    """Yields from `iterator`; a raise becomes the final yielded value.

    The pool-side half of `yield_exceptions=True`. `_ExceptionAsResult`
    already turns an ordinary task exception into a value inside the
    worker, so anything that still raises *here* is the machinery itself
    failing: a worker killed outright (`BrokenProcessPool`), a result
    that would not unpickle. The tasks queued behind that failure are
    gone and cannot be named -- the pool does not say which they were --
    so the one fact that survives is yielded as the last value, and the
    results already produced are kept rather than discarded with the
    raise (#232).
    """
    try:
        yield from iterator
    except Exception as exc:  # pylint: disable=broad-exception-caught
        yield exc


@dataclass(frozen=True)
class _Strategy:
    """How one `run_parallel` call will actually be executed.

    Resolved once, before any work starts, so the three execution paths
    below read settings rather than each deriving their own.
    """
    max_workers: int
    chunksize: int
    maxtasksperchild: Optional[int]
    disable_gc: bool
    use_threads: bool
    show_progress: bool
    desc: str
    total: Optional[int]

    @property
    def worker_initializer(self):
        """The initializer new worker *processes* should run, if any.

        Threads share the parent's interpreter, so disabling GC or
        arming a process-lifetime faulthandler watchdog in a thread
        would apply to the whole program rather than to a worker.
        """
        if self.use_threads:
            return None
        return resolve_worker_initializer(self.disable_gc)


def _env_int(name: str) -> Optional[int]:
    """Reads an integer tuning variable, or None if unset or unusable.

    A malformed value is reported rather than dropped. It used to be
    swallowed by a bare `except ValueError: pass`, so a typo in
    `ISOCENTER_MAX_WORKERS` silently reverted to the default and the only
    symptom was a cohort running at the wrong width.
    """
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        get_logger().warning(
            "%s is set to %r, which is not a whole number. Ignoring it and "
            "using the default.", name, raw)
        return None


def _env_is(name: str, values) -> bool:
    """Whether an environment variable is set to one of `values`."""
    return os.environ.get(name, "").lower() in values


def _resolve_strategy(max_workers, chunksize, maxtasksperchild, disable_gc,
                      force_threads, show_progress, desc, total) -> _Strategy:
    """Settles every knob before any work starts.

    Explicit arguments win over the environment, except for
    `ISOCENTER_SHOW_PROGRESS`, which can only ever switch a progress bar
    *off* -- it exists so that logs and CI output stay clean without
    every caller having to be changed.
    """
    # One parameter per knob run_parallel exposes. Collapsing them into a
    # dict to satisfy the argument-count check would hide which settings
    # exist, which is the opposite of the point.
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    if max_workers is None:
        configured = _env_int("ISOCENTER_MAX_WORKERS")
        if configured is not None and configured < 1:
            # `0` and every negative in one arm, so there is not one
            # behaviour for `0` and another for `-1`: neither is a
            # worker count, and an operator who typed either made the
            # same mistake. The value is named as well as the variable,
            # the way `_env_int`'s own warning does -- a message that
            # does not quote what was rejected cannot be matched against
            # what was typed.
            get_logger().warning(
                "ISOCENTER_MAX_WORKERS is set to %d, which is not a usable "
                "worker count. Ignoring it and using the default. Set it to "
                "1 to run with a single worker.", configured)
            configured = None
        # One worker per CPU, not the 1.5x an earlier version used:
        # predictable beats marginally faster when a run is hours long.
        #
        # Not `configured or (...)`: `or` is what discarded a `0` here
        # in silence, because the one value that must be reported is the
        # one value that is falsey (#335). A negative was worse -- truthy,
        # so it travelled to the pool constructor and raised there, in a
        # message naming no environment variable.
        #
        # Deliberately inside `if max_workers is None`, so an explicit
        # `max_workers=0` argument still reaches the pool and still
        # raises. That is a programming error in a line the caller can
        # see, not a misconfigured deployment; rewriting it to the CPU
        # count would hide their bug on a log line nobody is reading.
        max_workers = (configured if configured is not None
                       else (os.cpu_count() or 1))

    if chunksize == 1:
        # Only consulted at the default. An explicit `chunksize=1` is
        # indistinguishable from no argument at all here, so the
        # environment overrides it too.
        chunksize = _env_int("ISOCENTER_CHUNKSIZE") or 1

    if maxtasksperchild is None:
        configured = _env_int("ISOCENTER_MAX_TASKS_PER_CHILD")
        if configured is not None and configured < 1:
            # The exact mirror of the `ISOCENTER_MAX_WORKERS` arm above
            # (#335), one bunch later (#185). `_env_int` returns `0` --
            # its `if not raw` guard sees the non-empty string `"0"` --
            # and `0 is not None`, so a zero turned threads off on every
            # call site except export and then reached
            # `multiprocessing.Pool`, which raises `ValueError:
            # maxtasksperchild must be a positive int or None` naming no
            # environment variable. `0` and every negative in one arm,
            # for the same reason that one gives: neither is a recycling
            # interval and an operator who typed either made the same
            # mistake.
            #
            # Fixed here rather than in `_env_int`, which is shared with
            # `ISOCENTER_MAX_WORKERS` and `ISOCENTER_CHUNKSIZE`: a
            # rejection inside it would take the warning above dead and
            # decide `ISOCENTER_CHUNKSIZE`'s answer as a side effect
            # (#341 is where that is decided).
            get_logger().warning(
                "ISOCENTER_MAX_TASKS_PER_CHILD is set to %d, which is not "
                "a usable number of tasks per worker. Ignoring it and "
                "recycling no workers. Set it to 1 or more to recycle.",
                configured)
            configured = None
        # Deliberately inside `if maxtasksperchild is None`, so an
        # explicit `maxtasksperchild=0` argument still reaches the pool
        # and still raises: that is a programming error in a line the
        # caller can see, not a misconfigured deployment.
        maxtasksperchild = configured

    disable_gc = disable_gc or _env_is("ISOCENTER_DISABLE_GC", ("1",))

    if show_progress and _env_is("ISOCENTER_SHOW_PROGRESS", _FALSEY):
        show_progress = False

    return _Strategy(
        max_workers=max_workers,
        chunksize=chunksize,
        maxtasksperchild=maxtasksperchild,
        disable_gc=disable_gc,
        use_threads=_use_threads(force_threads, maxtasksperchild),
        show_progress=show_progress,
        desc=desc,
        total=total)


def _use_threads(force_threads: bool, maxtasksperchild: Optional[int]) -> bool:
    """Whether to run in threads rather than processes.

    Worker recycling has the last word: only `multiprocessing.Pool`
    implements `maxtasksperchild`, so asking for it rules threads out
    however the rest of the environment is set.

    This is also where that override is **announced** (#185). It is the
    only place that knows both halves -- `_resolve_strategy` calls it
    once per `run_parallel`, in the parent process, where the caller's
    logger is reachable -- and it is the whole of the precedence, so a
    warning anywhere else would be a second copy of this rule.

    The warning fires when, and only when, threads were actually asked
    for. `session.export()` passes `maxtasksperchild=25` on every
    export and asks for nothing else, so the ordinary path is silent; a
    line on every export would be noise that teaches readers to filter
    this logger.
    """
    if maxtasksperchild is not None:
        forced_by_env = _env_is("ISOCENTER_FORCE_THREADS", ("1",))
        if force_threads or forced_by_env:
            # Name both levers and quote the value, the way `_env_int`'s
            # and `ISOCENTER_MAX_WORKERS`' warnings do: a message that
            # says only "these conflict" cannot be matched against what
            # was typed. It says which lever to unset, because with
            # `ISOCENTER_MAX_TASKS_PER_CHILD` and
            # `ISOCENTER_FORCE_THREADS` both set this fires on EVERY
            # `run_parallel` call -- ingest, the PHI scan, OCR
            # verification, zone discovery, redaction -- which is
            # correct and is a lot of output on a long run.
            get_logger().warning(
                "%s was set, but worker recycling (maxtasksperchild=%s) "
                "was also asked for and only multiprocessing.Pool "
                "implements it, so this run uses processes. "
                "session.export() always sets maxtasksperchild=25, so it "
                "runs in processes on every interpreter including "
                "free-threaded builds; elsewhere, unset "
                "ISOCENTER_MAX_TASKS_PER_CHILD to get threads.",
                "ISOCENTER_FORCE_THREADS" if forced_by_env
                else "force_threads=True", maxtasksperchild)
        return False
    if force_threads or _env_is("ISOCENTER_FORCE_THREADS", ("1",)):
        return True
    if _env_is("ISOCENTER_FORCE_PROCESSES", ("1",)):
        return False
    # On a free-threaded build there is no GIL to escape, so threads keep
    # the parallelism without paying to pickle every item across a pipe.
    # `sys._is_gil_enabled` is underscored but is the only way to ask, and
    # is absent on builds that have always had a GIL -- hence the hasattr.
    return (hasattr(sys, "_is_gil_enabled")
            and not sys._is_gil_enabled())  # pylint: disable=protected-access


def _progress_total(strategy, items) -> Optional[int]:
    """How many items the bar should expect.

    A generator cannot be measured without consuming it, so callers pass
    `total` themselves in that case. A bar with no total still renders --
    it just cannot show progress, only motion.
    """
    if strategy.total is not None:
        return strategy.total
    return len(items) if hasattr(items, '__len__') else None


def _tracked(iterator, items, strategy) -> Iterator:
    """Yields from `iterator`, drawing a progress bar if one was asked for."""
    if not strategy.show_progress:
        yield from iterator
        return
    yield from tqdm(iterator, total=_progress_total(strategy, items),
                    desc=strategy.desc)


def _run_on_shared_executor(executor, func, items, strategy):
    """Uses an executor the caller owns, and does not shut it down.

    `imap` is preferred where the object offers it: a `multiprocessing.Pool`
    passed in here streams results, where `map` would collect them all
    first and give up the memory ceiling that streaming exists to hold.
    """
    mapper = executor.imap if hasattr(executor, 'imap') else executor.map
    iterator = mapper(func, items, chunksize=strategy.chunksize)
    yield from _tracked(iterator, items, strategy)


def _run_on_recycling_pool(func, items, strategy):
    """Runs in a pool whose workers are replaced every N tasks.

    This exists for the imaging paths, where the C libraries behind
    decoding and compression leak steadily. `ProcessPoolExecutor` cannot
    recycle workers, so the older `multiprocessing.Pool` is the only way
    to get memory back during a long run rather than at the end of it.

    Spawn, not fork: a forked worker inherits the parent's open SQLite
    handles and its sidecar file position.
    """
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=strategy.max_workers,
                  maxtasksperchild=strategy.maxtasksperchild,
                  initializer=strategy.worker_initializer) as pool:
        # Unordered: results are yielded as workers finish, so one slow
        # item does not hold back everything queued behind it.
        iterator = pool.imap_unordered(func, items,
                                       chunksize=strategy.chunksize)
        yield from _tracked(iterator, items, strategy)


def _run_on_new_executor(func, items, strategy):
    """Runs in a pool created for this call and shut down with it."""
    executor_class = (concurrent.futures.ThreadPoolExecutor
                      if strategy.use_threads
                      else concurrent.futures.ProcessPoolExecutor)

    kwargs = {'max_workers': strategy.max_workers}
    if not strategy.use_threads:
        # Spawn, not fork, for the recycling pool's reason: a forked
        # worker inherits the parent's open SQLite handles and its
        # sidecar file position. This is the pool that pickles the
        # store (#220), and it took the platform default -- fork on
        # Linux 3.12 -- which is the exact population where CI stalled
        # 900 seconds in a forked worker's `persist_pixel_data` and
        # failed with `database is locked`, while no spawn platform
        # ever reproduced it (#250). macOS spawns by default, so
        # nothing local ever showed the divergence;
        # `test_parallel_contract.py` pins the argument itself.
        kwargs['mp_context'] = multiprocessing.get_context("spawn")
    initializer = strategy.worker_initializer
    if initializer:
        kwargs['initializer'] = initializer

    with executor_class(**kwargs) as executor:
        iterator = executor.map(func, items, chunksize=strategy.chunksize)
        yield from _tracked(iterator, items, strategy)


def run_parallel(
    func: Callable[[T], R],
    items: Iterable[T],
    desc: str = "Processing",
    max_workers: int = None,
    chunksize: int = 1,
    show_progress: bool = True,
    force_threads: bool = False,
    total: int = None,
    executor: Any = None,
    maxtasksperchild: int = None,
    progress: bool = None,  # Alias for show_progress
    disable_gc: bool = False,  # Disable GC in worker processes
    return_generator: bool = False,  # Implement streaming
    yield_exceptions: bool = False  # Exceptions come back as values
) -> Any:  # Union[List[R], Iterator[R]]
    """
    Executes `func(item)` in parallel using multiple processes or threads.

    Adapts strategy based on environment variables (`ISOCENTER_MAX_WORKERS`,
    `ISOCENTER_FORCE_THREADS`, `ISOCENTER_FORCE_PROCESSES`, `ISOCENTER_CHUNKSIZE`,
    `ISOCENTER_MAX_TASKS_PER_CHILD`, `ISOCENTER_DISABLE_GC`) and presence of GIL.
    Defaults to `ProcessPoolExecutor`. `docs/environment.md` carries the whole
    table, including the order the three threads-or-processes levers resolve in
    (`_use_threads` is where that order lives).

    Args:
        func (Callable[[T], R]): The worker function.
        items (Iterable[T]): The collection of items to process.
        desc (str): Description for the progress bar.
        max_workers (int, optional): Override the number of workers.
        chunksize (int): Batch size for IPC.
        show_progress (bool): If True, displays a tqdm progress bar.
        force_threads (bool): If True, forces ThreadPoolExecutor.
        total (int, optional): Total item count (required for generators to show progress bar).
        executor (optional): Shared executor instance.
        maxtasksperchild (int, optional): Process recycling count (multiprocessing only).
        progress (bool, optional): Alias for show_progress.
        disable_gc (bool, optional): If True, disables GC in worker processes for speed.
        return_generator (bool): If True, returns a generator (streaming) instead of a list.
        yield_exceptions (bool): If True, a task whose `func` raises yields
            its exception as that task's result value instead of raising at
            the point of iteration, and a failure of the pool itself -- a
            worker killed outright, a result that will not unpickle -- is
            yielded once, as the final value, after which the results the
            pool can no longer deliver are over. Only for callers that
            branch on `isinstance(result, Exception)`; by default a raise
            is a raise, because a caller with no such arm must never
            receive an exception as data (#232). One promise the recycling
            pool cannot keep: `multiprocessing.Pool` answers a *killed*
            worker by respawning it and waiting forever for the lost task,
            so under `maxtasksperchild` that case hangs rather than
            yielding -- ordinary task exceptions still come back as values
            there.

    Returns:
        Union[List[R], Iterator[R]]: The results of the parallel execution.
    """
    if progress is not None:
        show_progress = progress

    strategy = _resolve_strategy(
        max_workers, chunksize, maxtasksperchild, disable_gc, force_threads,
        show_progress, desc, total)

    if yield_exceptions:
        func = _ExceptionAsResult(func)

    if executor is not None:
        results = _run_on_shared_executor(executor, func, items, strategy)
    elif strategy.maxtasksperchild is not None:
        results = _run_on_recycling_pool(func, items, strategy)
    else:
        results = _run_on_new_executor(func, items, strategy)

    if yield_exceptions:
        results = _trailing_exception(results)

    # Each path is a generator, so nothing has run yet. Streaming callers
    # get it untouched; everyone else gets the work done here.
    return results if return_generator else list(results)

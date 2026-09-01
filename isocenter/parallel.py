"""Parallel execution helpers.

`run_parallel` is the single entry point every parallel pass in Isocenter
goes through -- scanning, exporting, verification. It picks between three
execution strategies and adapts to a set of `ISOCENTER_*` environment
variables, so that tuning a cohort run never means editing code.
"""
import concurrent.futures
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


def _gc_off():
    """Worker-process initializer: turn the cyclic collector off.

    Imported here rather than at module scope because this runs in a
    freshly spawned child, and keeping the import with the only code that
    uses it makes clear the collector being disabled is the worker's, not
    the parent's.
    """
    import gc  # pylint: disable=import-outside-toplevel
    gc.disable()


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

        Threads share the parent's interpreter, so disabling GC in a
        thread would disable it for the whole program rather than for a
        worker.
        """
        return _gc_off if self.disable_gc and not self.use_threads else None


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
        # One worker per CPU, not the 1.5x an earlier version used:
        # predictable beats marginally faster when a run is hours long.
        max_workers = _env_int("ISOCENTER_MAX_WORKERS") or (os.cpu_count() or 1)

    if chunksize == 1:
        # Only consulted at the default. An explicit `chunksize=1` is
        # indistinguishable from no argument at all here, so the
        # environment overrides it too.
        chunksize = _env_int("ISOCENTER_CHUNKSIZE") or 1

    if maxtasksperchild is None:
        maxtasksperchild = _env_int("ISOCENTER_MAX_TASKS_PER_CHILD")

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
    """
    if maxtasksperchild is not None:
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
    `ISOCENTER_FORCE_THREADS`, `ISOCENTER_CHUNKSIZE`, `ISOCENTER_MAX_TASKS_PER_CHILD`,
    `ISOCENTER_DISABLE_GC`) and presence of GIL. Defaults to `ProcessPoolExecutor`.

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

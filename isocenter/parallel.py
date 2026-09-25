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
from typing import (Callable, Iterable, Iterator, Any, NamedTuple,
                    Optional, TypeVar)

from tqdm import tqdm

from .logger import get_logger

T = TypeVar('T')
R = TypeVar('R')

_TRUTHY = ("1", "true", "on", "yes")
_FALSEY = ("0", "false", "off", "no")

#: How long a worker process may live before it dumps every one of its
#: threads' tracebacks to stderr (diagnostic, never fatal). Sits below
#: pytest's faulthandler_timeout (300s) so a stalled child's dump lands
#: inside the same log window as the parent's, which cannot see into the
#: child. test_packaging_contract.py pins the inequality. Only consulted
#: when `ISOCENTER_WORKER_FAULTHANDLER` is set, which only tests.yml does.
_WORKER_FAULTHANDLER_TIMEOUT_S = 240


def _worker_init(disable_gc=False, faulthandler_timeout=None):
    """Configure one freshly spawned or recycled worker process.

    Args:
        disable_gc (bool): Disable the worker's garbage collector.
        faulthandler_timeout (float, optional): Seconds after which the
            worker dumps every thread's traceback to stderr, without
            exiting. None arms nothing.
    """
    # Module scope because it must pickle into the child. The imports sit
    # inside because they act on the worker's collector and faulthandler,
    # not the parent's.
    # pylint: disable=import-outside-toplevel
    if disable_gc:
        import gc
        gc.disable()
    if faulthandler_timeout is not None:
        import faulthandler
        # `exit=False` must stay: the dump is diagnostic, and a slow but
        # healthy worker must finish its task. `exit=True` would lose every
        # long task, which under the recycling pool is a hang. Each child
        # arms its own timer, so worker recycling re-arms it.
        faulthandler.dump_traceback_later(faulthandler_timeout, exit=False)


def resolve_worker_initializer(disable_gc: bool = False):
    """The one initializer worker processes run, or None if none is needed.

    Call it in the parent, when the pool is built: the settings travel to
    the child as pickled arguments. `ISOCENTER_DISABLE_GC=1` also disables
    the collector, and `ISOCENTER_WORKER_FAULTHANDLER=1` arms a traceback
    dump after `_WORKER_FAULTHANDLER_TIMEOUT_S` seconds.

    Args:
        disable_gc (bool): Disable the garbage collector in each worker.

    Returns:
        Optional[functools.partial]: `_worker_init` bound to the resolved
        settings, or None when there is nothing for a worker to set up.
    """
    # Resolved in the parent so the child obeys what the parent decided
    # rather than re-reading environment or module state after a spawn;
    # that is also what makes `_WORKER_FAULTHANDLER_TIMEOUT_S` patchable in
    # tests. `Session._executor` and `_Strategy.worker_initializer` both go
    # through here, so the two kinds of pool agree on what a worker runs
    # first.
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

    The worker-side half of `yield_exceptions=True`. An `Exception` costs
    that task alone, never the pass; a `BaseException` such as
    `KeyboardInterrupt` still propagates.
    """
    # A class at module scope rather than a closure because it has to
    # pickle into a process-pool worker alongside the `func` it wraps.

    def __init__(self, func):
        self.func = func

    def __call__(self, item):
        try:
            return self.func(item)
        # `except Exception` is the contract; `BaseException` is left to
        # tear the run down.
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return exc


def _trailing_exception(iterator):
    """Yields from `iterator`; a raise becomes the final yielded value.

    The pool-side half of `yield_exceptions=True`: a failure of the pool
    itself (a worker killed outright, a result that would not unpickle)
    is yielded once, last, after every result already produced.
    """
    # `_ExceptionAsResult` already turns a task's exception into a value
    # inside the worker, so what raises here is the machinery. The tasks
    # queued behind it are gone and the pool does not say which they were,
    # so the exception is the one fact left to yield.
    try:
        yield from iterator
    except Exception as exc:  # pylint: disable=broad-exception-caught
        yield exc


class _Choice(NamedTuple):
    """What the threads-or-processes ranking decided, and who asked.

    Callers that report why a run uses threads or processes read these
    fields rather than ranking the levers again.

    Attributes:
        use_threads (bool): Run in threads rather than processes.
        processes_requested_by (Optional[str]): The lever that asked for
            processes, whether or not it got them. None when nobody asked,
            when the operator's `ISOCENTER_FORCE_THREADS` superseded their
            `ISOCENTER_FORCE_PROCESSES`, and for the GIL-build default of
            processes, which is not a request.
        threads_request_overridden_by (Optional[str]): The threads lever
            that lost to worker recycling, `"ISOCENTER_FORCE_THREADS"` or
            `"force_threads=True"`; None otherwise.
        threads_requested_by (Optional[str]): The threads lever that asked
            and won, the variable taking the name when both are set; None
            when nobody asked, when recycling beat the request, and for
            the free-threaded default.
    """
    # A default is not a request: that `None` is what keeps
    # `Session(":memory:")` working out of the box on a GIL build.
    # `threads_request_overridden_by` exists so the recycling warning is
    # emitted at dispatch, not at resolution (`_resolve_execution_choice`).
    # `DicomImporter.import_files` reads `threads_requested_by` to say that
    # `ISOCENTER_FORCE_THREADS` had no effect on an `ingest()`, which runs
    # on the session's process pool. It is last and defaulted so a
    # positional three-field construction still works.
    use_threads: bool
    processes_requested_by: Optional[str]
    threads_request_overridden_by: Optional[str]
    threads_requested_by: Optional[str] = None


@dataclass(frozen=True)
class _Strategy:  # pylint: disable=too-many-instance-attributes
    # Eight settings and three attribution fields, each read by name by a
    # caller that reports on the decision. Grouping them to satisfy the
    # count would hide which fields exist -- the reason `_resolve_strategy`
    # keeps one parameter per knob.
    """How one `run_parallel` call will actually be executed.

    Resolved once, before any work starts (`_resolve_strategy`). The three
    attribution fields carry `_Choice`'s answer out: `redact()` reads
    `use_threads` and `processes_requested_by`,
    `DicomImporter.import_files` reads `threads_requested_by`, and
    `run_parallel` reads `threads_request_overridden_by` for the
    recycling-override warning.
    """
    max_workers: int
    chunksize: int
    maxtasksperchild: Optional[int]
    disable_gc: bool
    use_threads: bool
    show_progress: bool
    desc: str
    total: Optional[int]
    processes_requested_by: Optional[str]
    threads_request_overridden_by: Optional[str]
    threads_requested_by: Optional[str] = None

    @property
    def worker_initializer(self):
        """The initializer new worker *processes* should run, if any.

        Returns:
            Optional[functools.partial]: `resolve_worker_initializer`'s
            answer, or None when the strategy uses threads.
        """
        # Threads share the parent's interpreter, so disabling GC or arming
        # a process-lifetime faulthandler watchdog in a thread would apply
        # to the whole program rather than to a worker.
        if self.use_threads:
            return None
        return resolve_worker_initializer(self.disable_gc)


def _env_int(name: str, *, minimum: Optional[int]) -> Optional[int]:
    """Reads an integer tuning variable, or None if unset or unusable.

    A malformed value, or one below `minimum`, is logged as a WARNING
    naming the variable and the value, and dropped.

    Args:
        name (str): The environment variable.
        minimum (Optional[int]): The lowest accepted value, or None for no
            floor. Keyword-only and required.

    Returns:
        Optional[int]: The value, or None when unset, malformed or below
        `minimum` -- never `0` on rejection, so test with `is not None`,
        never `_env_int(...) or default`.
    """
    # `minimum` has no default on purpose: every read states its floor, or
    # states `minimum=None` visibly. A default of 1 would silently reject a
    # future variable that accepts 0; a default of None would let a new
    # read site skip the floor by omission. The floor is checked here, not
    # at the call sites, so every read of a variable gets the same one.
    #
    # `_env_int(...) or default` is the spelling to avoid: 0 is falsey, so
    # `or` discards it unnoticed, and a negative -- truthy -- would reach a
    # pool constructor that raises naming no environment variable.
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        get_logger().warning(
            "%s is set to %r, which is not a whole number. Ignoring it and "
            "using the default.", name, raw)
        return None
    if minimum is not None and value < minimum:
        # `0` and every negative in one arm: neither is a usable value and
        # an operator who typed either made the same mistake. The value is
        # quoted as well as the variable, so the message can be matched
        # against what was typed. Per-variable advice ("set it to 1 for a
        # single worker") lives in the docs/environment.md rows, not here,
        # so one behaviour has one spelling.
        get_logger().warning(
            "%s is set to %d, which is below the minimum of %d. Ignoring "
            "it and using the default.", name, value, minimum)
        return None
    return value


def _env_is(name: str, values) -> bool:
    """Whether an environment variable is set to one of `values`.

    Args:
        name (str): The environment variable.
        values: Lowercase spellings to accept; the variable is lowercased
            before the comparison.

    Returns:
        bool: True when the variable's lowercased value is in `values`.
    """
    return os.environ.get(name, "").lower() in values


def progress_enabled(show: bool = True) -> bool:
    """Whether a progress bar is drawn: the caller's `show`, unless
    `ISOCENTER_SHOW_PROGRESS` switches it off.

    The variable is read on every call, so one set after import applies.

    Args:
        show (bool): Whether the caller wants a bar.

    Returns:
        bool: False when `show` is false or `ISOCENTER_SHOW_PROGRESS` is
        `0`, `false`, `off` or `no`; True otherwise.
    """
    # The one spelling of the rule: `_resolve_strategy` and `progress_bar`
    # both call it, so every bar in the package obeys the variable.
    return bool(show) and not _env_is("ISOCENTER_SHOW_PROGRESS", _FALSEY)


def progress_bar(iterable=None, *, show: bool = True, **kwargs):
    """`tqdm`, drawn only when `progress_enabled(show)`.

    Every progress bar outside `run_parallel` goes through here.

    Args:
        iterable (Iterable, optional): What to iterate, as for `tqdm`.
        show (bool): Whether the caller wants a bar.
        **kwargs: Passed to `tqdm` (`desc`, `total`, `unit`, ...).

    Returns:
        tqdm: The bar, disabled when `progress_enabled(show)` is False.
    """
    # No other module may import `tqdm`: a bar drawn by a second import is
    # one `ISOCENTER_SHOW_PROGRESS` does not reach
    # (`tests/test_progress_bars_honour_the_environment.py` enforces it).
    # This calls the module's `tqdm` global, so a test that patches
    # `isocenter.parallel.tqdm` sees these bars too.
    return tqdm(iterable, disable=not progress_enabled(show), **kwargs)


def resolve_max_workers() -> int:
    """The default worker count: `ISOCENTER_MAX_WORKERS`, else one per CPU.

    `_resolve_strategy` uses it when no `max_workers` argument is given,
    and `Session` uses it to size its shared pool and every restart of it.

    Returns:
        int: The worker count, never less than 1 (a value below 1 is
        logged by `_env_int` and dropped).
    """
    # A helper rather than a `_resolve_strategy` call: resolving a whole
    # strategy at `Session()` would read five other variables and emit
    # their warnings when the session opens.
    configured = _env_int("ISOCENTER_MAX_WORKERS", minimum=1)
    return configured if configured is not None else (os.cpu_count() or 1)


def _resolve_strategy(max_workers, chunksize, maxtasksperchild, disable_gc,
                      force_threads, show_progress, desc, total) -> _Strategy:
    """Settles every knob before any work starts.

    Explicit arguments win over the environment, except for
    `ISOCENTER_SHOW_PROGRESS`, which can only ever switch a progress bar
    *off*. The arguments are `run_parallel`'s, with the same meanings.

    Args:
        max_workers (Optional[int]): None for `resolve_max_workers()`.
        chunksize (int): 1 lets `ISOCENTER_CHUNKSIZE` override it.
        maxtasksperchild (Optional[int]): None lets
            `ISOCENTER_MAX_TASKS_PER_CHILD` supply it.
        disable_gc (bool): Or `ISOCENTER_DISABLE_GC=1`.
        force_threads (bool): Ask for threads.
        show_progress (bool): Draw a progress bar.
        desc (str): The bar's label.
        total (Optional[int]): The bar's total.

    Returns:
        _Strategy: The settled strategy.
    """
    # One parameter per knob run_parallel exposes. Collapsing them into a
    # dict to satisfy the argument-count check would hide which settings
    # exist, which is the opposite of the point.
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    # The integer variables below share one floor, stated at each read (the
    # worker count's is in `resolve_max_workers`): `_env_int(...,
    # minimum=1)` reports `0` and every negative and returns `None`, so the
    # `is not None` fallbacks here see a rejected value exactly as they see
    # an unset or malformed one. Never spell it `_env_int(...) or default`.
    if max_workers is None:
        # Deliberately inside `if max_workers is None`, so an explicit
        # `max_workers=0` argument still reaches the pool and still
        # raises. That is a programming error in a line the caller can
        # see, not a misconfigured deployment; rewriting it to the CPU
        # count would hide their bug on a log line nobody is reading.
        max_workers = resolve_max_workers()

    if chunksize == 1:
        # Only consulted at the default. An explicit `chunksize=1` is
        # indistinguishable from no argument at all here, so the
        # environment overrides it too.
        configured = _env_int("ISOCENTER_CHUNKSIZE", minimum=1)
        chunksize = configured if configured is not None else 1

    # Which spelling of the recycling lever supplied the value, for the
    # attribution below. This is the only place that knows, because it
    # is the only place that chooses between them -- the same choice the
    # recycling-override warning makes for the threads side.
    recycling_lever = ("the maxtasksperchild argument"
                       if maxtasksperchild is not None else None)
    if maxtasksperchild is None:
        # Deliberately inside `if maxtasksperchild is None`, so an
        # explicit `maxtasksperchild=0` argument still reaches the pool
        # and still raises: that is a programming error in a line the
        # caller can see, not a misconfigured deployment. A rejected
        # environment value falls back to the documented *Unlimited*,
        # which is `None` -- the same `None` `_env_int` returns for it.
        maxtasksperchild = _env_int("ISOCENTER_MAX_TASKS_PER_CHILD",
                                    minimum=1)
        if maxtasksperchild is not None:
            recycling_lever = "ISOCENTER_MAX_TASKS_PER_CHILD"

    disable_gc = disable_gc or _env_is("ISOCENTER_DISABLE_GC", ("1",))

    show_progress = progress_enabled(show_progress)

    choice = _resolve_execution_choice(force_threads, maxtasksperchild,
                                       recycling_lever)
    return _Strategy(
        max_workers=max_workers,
        chunksize=chunksize,
        maxtasksperchild=maxtasksperchild,
        disable_gc=disable_gc,
        use_threads=choice.use_threads,
        show_progress=show_progress,
        desc=desc,
        total=total,
        processes_requested_by=choice.processes_requested_by,
        threads_request_overridden_by=choice.threads_request_overridden_by,
        threads_requested_by=choice.threads_requested_by)


def _resolve_execution_choice(
        force_threads: bool, maxtasksperchild: Optional[int],
        recycling_lever: Optional[str]) -> _Choice:
    """Whether to run in threads rather than processes, and who asked.

    Precedence, highest first: worker recycling (`maxtasksperchild`)
    forces processes; then `force_threads` or `ISOCENTER_FORCE_THREADS`
    gives threads; then `ISOCENTER_FORCE_PROCESSES` gives processes;
    otherwise threads on a free-threaded build and processes on a GIL
    build. Logs nothing: `run_parallel` warns at dispatch.

    Args:
        force_threads (bool): The caller asked for threads.
        maxtasksperchild (Optional[int]): The resolved recycling count, or
            None for no recycling.
        recycling_lever (Optional[str]): Which spelling supplied
            `maxtasksperchild`: `"ISOCENTER_MAX_TASKS_PER_CHILD"` or
            `"the maxtasksperchild argument"`.

    Returns:
        _Choice: The decision and its attribution.
    """
    # Recycling beats threads because on 3.12 only `multiprocessing.Pool`
    # recycles workers: `ProcessPoolExecutor`'s `max_tasks_per_child`
    # deadlocks `map` there at the first replacement. The whole precedence
    # lives here; nothing else may read these variables to decide, or there
    # are two copies of the order.
    #
    # Emits nothing because `redact()` resolves a strategy before deciding
    # whether to run at all, and a warning here could precede the refusal
    # of a run that never starts. `recycling_lever` is passed in because
    # `_resolve_strategy` is the only caller that knows which spelling it
    # used; reading the environment again here would be a second answer.
    forced_by_env = _env_is("ISOCENTER_FORCE_THREADS", ("1",))
    processes_by_env = _env_is("ISOCENTER_FORCE_PROCESSES", ("1",))

    # Attribution is settled on the way *through* the ranks and before
    # any of them short-circuits. Computed after the fact -- from the
    # resolved `use_threads`, say -- it would be `None` on every `redact()`
    # call against a `:memory:` store, which passes `force_threads=True`
    # and so always ends in threads however the environment is set: the
    # very path whose ignored processes lever has to be reported.
    if maxtasksperchild is not None:
        processes_requested_by = recycling_lever
    elif forced_by_env:
        # The operator's own threads lever supersedes their processes
        # lever by the documented order, so their effective request is
        # threads and nothing has been denied.
        processes_requested_by = None
    elif processes_by_env:
        processes_requested_by = "ISOCENTER_FORCE_PROCESSES"
    else:
        # Rank 4 below is a default, and a default is not a request.
        processes_requested_by = None

    if maxtasksperchild is not None:
        overridden_by = None
        if forced_by_env:
            overridden_by = "ISOCENTER_FORCE_THREADS"
        elif force_threads:
            overridden_by = "force_threads=True"
        return _Choice(False, processes_requested_by, overridden_by)
    if force_threads or forced_by_env:
        # The one return that grants a request for threads, so the one
        # place that can name who asked. Not derived from
        # `use_threads` anywhere else: the free-threaded default below
        # also ends in threads, and nobody asked for it.
        return _Choice(True, processes_requested_by, None,
                       "ISOCENTER_FORCE_THREADS" if forced_by_env
                       else "force_threads=True")
    if processes_by_env:
        return _Choice(False, processes_requested_by, None)
    # On a free-threaded build there is no GIL to escape, so threads keep
    # the parallelism without paying to pickle every item across a pipe.
    # `sys._is_gil_enabled` is underscored but is the only way to ask, and
    # is absent on builds that have always had a GIL -- hence the hasattr.
    return _Choice(
        hasattr(sys, "_is_gil_enabled")
        and not sys._is_gil_enabled(),  # pylint: disable=protected-access
        processes_requested_by, None)


def _announce_recycling_override(strategy: _Strategy) -> None:
    """Logs a WARNING that worker recycling beat a request for threads.

    Logs only when threads were actually asked for
    (`strategy.threads_request_overridden_by` is set).

    Args:
        strategy (_Strategy): The strategy about to be dispatched.
    """
    # Called at dispatch, from `run_parallel`, rather than where the
    # ranking is resolved: a strategy resolved and then not run has nothing
    # to report, and `redact()` can resolve without running. Silent unless
    # threads were asked for, because `session.export()` passes
    # `maxtasksperchild=25` on every export; a line on every export would
    # teach readers to filter this logger.
    if strategy.threads_request_overridden_by is None:
        return
    # Name both levers and quote the value, the way `_env_int`'s and
    # `ISOCENTER_MAX_WORKERS`' warnings do: a message that says only
    # "these conflict" cannot be matched against what was typed. It says
    # which lever to unset, because with `ISOCENTER_MAX_TASKS_PER_CHILD`
    # and `ISOCENTER_FORCE_THREADS` both set this fires on EVERY
    # `run_parallel` call -- ingest, the PHI scan, OCR verification, zone
    # discovery, redaction -- which is correct and is a lot of output on
    # a long run.
    get_logger().warning(
        "%s was set, but worker recycling (maxtasksperchild=%s) "
        "was also asked for and only multiprocessing.Pool "
        "implements it, so this run uses processes. "
        "session.export() always sets maxtasksperchild=25, so it "
        "runs in processes on every interpreter including "
        "free-threaded builds; for audit(), scan_pixel_content() "
        "and redact(), unset ISOCENTER_MAX_TASKS_PER_CHILD to get "
        "threads; ingest() runs on the session's executor and "
        "takes no lever.",
        strategy.threads_request_overridden_by, strategy.maxtasksperchild)


def _progress_total(strategy, items) -> Optional[int]:
    """How many items the bar should expect.

    Args:
        strategy (_Strategy): Its `total`, when set, wins.
        items: The items being processed.

    Returns:
        Optional[int]: `strategy.total`, else `len(items)`, else None for
        an iterable with no length (a generator), whose bar shows motion
        but not progress.
    """
    if strategy.total is not None:
        return strategy.total
    return len(items) if hasattr(items, '__len__') else None


def _tracked(iterator, items, strategy) -> Iterator:
    """Yields from `iterator`, drawing a progress bar if one was asked for.

    Args:
        iterator (Iterator): The results to pass through.
        items: The items being processed, for the bar's total.
        strategy (_Strategy): Its `show_progress` and `desc` apply.

    Yields:
        Each result of `iterator`, unchanged.
    """
    if not strategy.show_progress:
        yield from iterator
        return
    yield from tqdm(iterator, total=_progress_total(strategy, items),
                    desc=strategy.desc)


def _run_on_shared_executor(executor, func, items, strategy):
    """Uses an executor the caller owns, and does not shut it down.

    Args:
        executor: A `concurrent.futures` executor, or a
            `multiprocessing.Pool` (whose `imap` is used).
        func: The worker function.
        items: The items to process.
        strategy (_Strategy): Its `chunksize` and progress settings apply.

    Yields:
        Each result, in submission order.
    """
    # `imap` where offered: a `multiprocessing.Pool`'s `map` would collect
    # every result first and give up the memory ceiling streaming holds.
    mapper = executor.imap if hasattr(executor, 'imap') else executor.map
    iterator = mapper(func, items, chunksize=strategy.chunksize)
    yield from _tracked(iterator, items, strategy)


def _run_on_recycling_pool(func, items, strategy, ordered=False):
    """Runs in a spawned pool whose workers are replaced every N tasks.

    Args:
        func: The worker function.
        items: The items to process.
        strategy (_Strategy): Its worker count, `maxtasksperchild`,
            initializer, `chunksize` and progress settings apply.
        ordered (bool): Yield in submission order rather than as workers
            finish.

    Yields:
        Each result, as workers finish unless `ordered`.
    """
    # For the imaging paths, where the C libraries behind decoding and
    # compression leak steadily. `ProcessPoolExecutor(max_tasks_per_child=)`
    # deadlocks `map` on 3.12 the first time a worker is replaced, so
    # `multiprocessing.Pool` is the only way to get memory back on every
    # supported interpreter. Do not switch to the executor keyword while
    # 3.12 is supported.
    #
    # Spawn, not fork: a forked worker inherits the parent's open SQLite
    # handles and its sidecar file position.
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=strategy.max_workers,
                  maxtasksperchild=strategy.maxtasksperchild,
                  initializer=strategy.worker_initializer) as pool:
        # Unordered unless asked: results are yielded as workers finish,
        # so one slow item does not hold back everything queued behind
        # it. `import_files` asks, because the order it links files in
        # decides which of two files sharing an SOP Instance UID is kept,
        # and arrival order would make that a matter of scheduling.
        # `export()` does not: its results are small and order-free, and
        # holding them behind a slow instance buys nothing.
        mapper = pool.imap if ordered else pool.imap_unordered
        iterator = mapper(func, items, chunksize=strategy.chunksize)
        yield from _tracked(iterator, items, strategy)


def _run_on_new_executor(func, items, strategy):
    """Runs in a pool created for this call and shut down with it.

    A `ThreadPoolExecutor` when the strategy uses threads, else a spawned
    `ProcessPoolExecutor`.

    Args:
        func: The worker function.
        items: The items to process.
        strategy (_Strategy): Its worker count, initializer, `chunksize`
            and progress settings apply.

    Yields:
        Each result, in submission order.
    """
    executor_class = (concurrent.futures.ThreadPoolExecutor
                      if strategy.use_threads
                      else concurrent.futures.ProcessPoolExecutor)

    kwargs = {'max_workers': strategy.max_workers}
    if not strategy.use_threads:
        # Spawn, not fork, for the recycling pool's reason: a forked
        # worker inherits the parent's open SQLite handles and its
        # sidecar file position. This is the pool that pickles the store,
        # and the platform default is fork on Linux 3.12, where a forked
        # worker's `persist_pixel_data` can stall on `database is locked`.
        # macOS spawns by default, so a local run does not show the
        # difference; `test_parallel_contract.py` pins the argument.
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
    yield_exceptions: bool = False,  # Exceptions come back as values
    strategy: Optional[_Strategy] = None,  # Already resolved by the caller
    ordered: bool = False  # Submission order on the recycling pool too
) -> Any:  # Union[List[R], Iterator[R]]
    """
    Executes `func(item)` in parallel using multiple processes or threads.

    Adapts strategy based on environment variables (`ISOCENTER_MAX_WORKERS`,
    `ISOCENTER_FORCE_THREADS`, `ISOCENTER_FORCE_PROCESSES`, `ISOCENTER_CHUNKSIZE`,
    `ISOCENTER_MAX_TASKS_PER_CHILD`, `ISOCENTER_DISABLE_GC`) and presence of GIL.
    Defaults to `ProcessPoolExecutor`. `docs/environment.md` carries the whole
    table, including the order the three threads-or-processes levers resolve in.
    Logs a WARNING when worker recycling overrides a request for threads.

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
            receive an exception as data. One promise the recycling
            pool cannot keep: `multiprocessing.Pool` answers a *killed*
            worker by respawning it and waiting forever for the lost task,
            so under `maxtasksperchild` that case hangs rather than
            yielding -- ordinary task exceptions still come back as values
            there.
        strategy (optional): A `_Strategy` the caller has already
            resolved with `_resolve_strategy`. When given it is used as
            it stands and **every resolution keyword above is ignored**
            -- `max_workers`, `chunksize`, `maxtasksperchild`,
            `disable_gc`, `force_threads`, `show_progress`, `progress`,
            `desc` and `total` -- because the caller has settled them
            already. `executor`, `return_generator`, `yield_exceptions` and
            `ordered` are not resolution settings and still apply.
        ordered (bool): If True, results come back in submission order
            on the recycling pool (`maxtasksperchild`), which otherwise
            yields them as workers finish. The shared-executor path
            (`executor.map`, or a Pool's `imap`) and the per-call
            executor path (`executor.map`) are ordered already, so it
            changes nothing there.

    Returns:
        Union[List[R], Iterator[R]]: The results of the parallel
        execution: a list, or with `return_generator` an iterator that
        does the work as it is consumed.
    """
    # `redact()` and `import_files` pass `strategy=` so the strategy they
    # report on is the one the dispatch uses, rather than a second
    # resolution that can disagree.
    if progress is not None:
        show_progress = progress

    if strategy is None:
        strategy = _resolve_strategy(
            max_workers, chunksize, maxtasksperchild, disable_gc,
            force_threads, show_progress, desc, total)

    if yield_exceptions:
        func = _ExceptionAsResult(func)

    # Before the three paths and after both ways of obtaining a
    # strategy, so a strategy handed in as `strategy=` announces its
    # recycling override exactly as one resolved here does -- and so a
    # strategy resolved by a caller that then declines to run says
    # nothing at all.
    _announce_recycling_override(strategy)

    if executor is not None:
        results = _run_on_shared_executor(executor, func, items, strategy)
    elif strategy.maxtasksperchild is not None:
        results = _run_on_recycling_pool(func, items, strategy, ordered)
    else:
        results = _run_on_new_executor(func, items, strategy)

    if yield_exceptions:
        results = _trailing_exception(results)

    # Each path is a generator, so nothing has run yet. Streaming callers
    # get it untouched; everyone else gets the work done here.
    return results if return_generator else list(results)

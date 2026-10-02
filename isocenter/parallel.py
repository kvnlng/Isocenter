"""Parallel execution helpers.

`run_parallel` is the single entry point every parallel pass in Isocenter
goes through -- scanning, exporting, verification. It picks between three
execution strategies and adapts to a set of `ISOCENTER_*` environment
variables, so that tuning a cohort run never means editing code.
"""
import collections
import concurrent.futures
import functools
import os
import signal
import sys
import multiprocessing
import multiprocessing.connection
import multiprocessing.pool
import threading
import time
from concurrent.futures.process import BrokenProcessPool
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

#: How long a worker process may take to end once its exit has begun,
#: before the kernel ends it with SIGALRM (#888, #844). No environment
#: variable: tests patch it. Resolved in the parent and handed to the
#: worker as an argument, so a patched value reaches a spawned child.
#:
#: Armed by a recycling-pool worker once it leaves its task loop, however
#: it left: at its quota, by the pool's sentinel on `close()`, or on
#: `terminate()`. So it must be at least `_BROKEN_POOL_GRACE_S +
#: _POOL_EXIT_AFTER_KILL_S`: inside #860's bounded exit the parent's own
#: kill then always acts first, and that exit's WARNING lines are
#: unchanged. Below conftest's `_STALL_S`, pytest's faulthandler window and
#: `_WORKER_FAULTHANDLER_TIMEOUT_S`. `test_packaging_contract.py` pins all
#: of it.
_WORKER_EXIT_GRACE_S = 15.0


def _arm_exit_bound(seconds):
    """End this worker process with SIGALRM if it is still running
    `seconds` from now. Does nothing in a process that is not a
    multiprocessing child.

    Args:
        seconds (float): The bound.
    """
    # Three traps, each a reason this is not something tidier:
    #
    # 1. SIGALRM with its default action, not a `threading.Timer` that calls
    #    `os._exit`. No Python thread runs once `Py_FinalizeEx` has marked
    #    the interpreter finalizing, and #844's worker was stuck past
    #    `atexit`, in `_PyFaulthandler_Fini`. The kernel's default action
    #    ends a process blocked anywhere (measured on 3.12 and 3.14t: a
    #    child stuck after `atexit` and one stuck in `threading._shutdown`
    #    both ended with -14 about 1.05 s after a 1 s bound). The handler
    #    is reset first, because the worker's own code may have taken it.
    # 2. Never in the parent: a SIGALRM in the caller's own process would
    #    end their program.
    # 3. No log call here or after it: the process being bounded may hold
    #    the logging lock (#434's lesson). The parent reports, from the
    #    exit code.
    if multiprocessing.parent_process() is None:
        return
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, seconds)


def _end_the_dump_within(seconds):
    """At a worker's exit: bound the exit, then cancel any pending
    traceback dump (#844). Registered with `atexit` by `_worker_init`.

    Args:
        seconds (float): The exit bound.
    """
    # The bound first. A dump already spinning makes the cancel itself wait
    # for good, which is how the 1.0.0rc4 worker's exit hung (in
    # `_PyFaulthandler_Fini`, which runs after `atexit`), so the alarm must
    # already be set when the cancel is called. Cancelling also stops a dump
    # from starting while the interpreter tears down. `faulthandler` is read
    # at call time, on purpose: a test replaces its cancel.
    _arm_exit_bound(seconds)
    import faulthandler  # pylint: disable=import-outside-toplevel
    faulthandler.cancel_dump_traceback_later()


def _worker_init(disable_gc=False, faulthandler_timeout=None,
                 exit_bound=None):
    """Configure one freshly spawned or recycled worker process.

    Args:
        disable_gc (bool): Disable the worker's garbage collector.
        faulthandler_timeout (float, optional): Seconds after which the
            worker dumps every thread's traceback to stderr, without
            exiting. None arms nothing.
        exit_bound (float, optional): With `faulthandler_timeout`, the
            seconds the worker's exit may take before SIGALRM ends it
            (#844). None bounds nothing.
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
        if exit_bound is not None:
            # An exit handler, so it runs in `Py_FinalizeEx` before
            # `_PyFaulthandler_Fini`, the frame the rc4 worker never left.
            # Under coverage's `concurrency = multiprocessing` a worker's
            # data is saved in its patched `_bootstrap`, before `sys.exit`,
            # so before this bound is armed.
            import atexit
            atexit.register(_end_the_dump_within, exit_bound)


def resolve_worker_initializer(disable_gc: bool = False):
    """The one initializer worker processes run, or None if none is needed.

    Call it in the parent, when the pool is built: the settings travel to
    the child as pickled arguments. `ISOCENTER_DISABLE_GC=1` also disables
    the collector, and `ISOCENTER_WORKER_FAULTHANDLER=1` arms a traceback
    dump after `_WORKER_FAULTHANDLER_TIMEOUT_S` seconds and bounds the
    worker's exit at `_WORKER_EXIT_GRACE_S` (#844).

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
    settings = {"disable_gc": disable_gc,
                "faulthandler_timeout": faulthandler_timeout}
    if faulthandler_timeout is not None:
        # Only beside the dump: the bound exists for a dump that can hold
        # the worker's exit (#844), and production never arms one.
        settings["exit_bound"] = _WORKER_EXIT_GRACE_S
    return functools.partial(_worker_init, **settings)


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


#: How long the workers of a broken process pool get to end, counted from
#: when `_end_broken_pool_stragglers` finds the pool broken, before it
#: sends SIGKILL to each one still running (#796). No environment
#: variable: tests patch it.
#:
#: Below conftest's `_STALL_S` and pytest's faulthandler window, so a pool
#: teardown that ends in a kill is never reported as a stall or dumped.
#: Unrelated to the family of waits on sqlite and the sidecar gate: it
#: holds no sqlite handle and no gate, and the only lock around it is the
#: shared pass-lock of `ingest()` and `redact()`, which `compact()` refuses
#: on rather than waits for. Not below a few seconds: a worker that saves
#: its state on SIGTERM must get to finish, and coverage's `sigterm = True`
#: handler takes up to 0.8 s under load to write the worker's data file.
#: `test_packaging_contract.py` pins both ends, the floor at 5 s.
_BROKEN_POOL_GRACE_S = 10.0


def _end_broken_pool_stragglers(executor) -> list[int]:
    """SIGKILL the workers a broken process pool's SIGTERM left running.

    When a worker of a `ProcessPoolExecutor` ends outright, the pool's
    manager thread sends every other worker SIGTERM, twice, and then waits
    for each one with no timeout while holding the pool's shutdown lock.
    `submit()` and `shutdown()`, whatever `wait` says, take that lock first,
    so a worker that outlives SIGTERM hangs whichever of them comes next,
    for good (#796; upstream, python/cpython#158413). A worker outlives
    SIGTERM when something in it handles the signal: a script's
    module-level handler, which spawn runs again in every worker; a handler
    that calls `sys.exit()`, which a worker running a task catches as that
    task's failure; coverage's `sigterm = True`.

    Does nothing unless `executor` is a broken process pool. Otherwise gives
    its workers `_BROKEN_POOL_GRACE_S` from now to end, SIGKILLs each one
    still running, and logs one WARNING naming them. An interrupt during
    that wait, such as Ctrl-C, sends the SIGKILL and logs the line at once,
    and then goes on. Takes no lock. A worker in uninterruptible I/O cannot
    be ended by any signal.

    Args:
        executor: Any executor or pool. Only a `ProcessPoolExecutor` that is
            broken is acted on.

    Returns:
        list[int]: The pids it sent SIGKILL, or `[]`.
    """
    # `_broken` is CPython's own verdict, set before the futures are failed,
    # and `_processes` its list of the workers, which nothing clears before
    # `shutdown()`. Both are private. There is no public way in: 3.14's
    # `kill_workers()` and `terminate_workers()` take the shutdown lock
    # first, and 3.12 has neither. The `getattr` defaults let every other
    # kind of pool through, so a rename would make this a silent no-op;
    # `test_parallel_contract.py` pins both on each interpreter instead.
    if not getattr(executor, "_broken", False):
        return []
    processes = getattr(executor, "_processes", None)
    if not processes:
        return []
    waiting = {}
    for process in list(processes.values()):
        try:
            waiting[process.sentinel] = process
        except ValueError:  # a closed process object; nothing to end
            continue
    # The sentinels, never `join()` or `is_alive()`: those reap the child
    # with `waitpid`, racing the manager thread's own `join()` of it. A
    # worker whose sentinel is ready has ended and is the manager's to reap.
    found_broken = time.monotonic()
    deadline = found_broken + _BROKEN_POOL_GRACE_S
    try:
        while waiting:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                ready = multiprocessing.connection.wait(list(waiting),
                                                        timeout=left)
            except (OSError, ValueError):
                break
            for sentinel in ready:
                del waiting[sentinel]
    finally:
        # On every way out of the wait, and Ctrl-C is one: the grace is
        # seconds in which the program looks stalled, which is when a user
        # presses it, and its `KeyboardInterrupt` is raised inside `wait()`.
        # Let out with the workers still running, it reached the caller's
        # `shutdown()`, which then waited on them for good (review of
        # #861). A `finally` and not an `except`, so nothing is swallowed,
        # and never a `return` in it, which would swallow the interrupt.
        #
        # Only the ones still running: an interrupt that lands before a
        # `wait()` has returned an ended worker's sentinel leaves that
        # worker in `waiting`, and it was named as still running (#862).
        # Never catch `BaseException` around this check: a second Ctrl-C
        # here must still reach the caller, and the fallback for a handle
        # that cannot be asked is to kill them all, never to skip the kill.
        killed = []
        for process in _still_running_or_all(waiting.values()):
            try:
                process.kill()
            except (OSError, ValueError):
                continue
            killed.append(process.pid)
        if killed:
            # Two `ingest()` calls on one broken pool can both get here and
            # both kill: `kill()` skips a worker already reaped and swallows
            # one already gone, so the cost is a second line naming the
            # same pids. The time is measured, not the grace: an interrupt
            # ends the wait early.
            get_logger().warning(
                "%d worker process(es) of a broken process pool were still "
                "running %.1f s after the pool was found broken, and were "
                "sent SIGKILL (pid %s). A worker outlives SIGTERM when "
                "something in it handles that signal; a handler a script "
                "installs at module level runs again in every spawned "
                "worker.",
                len(killed), time.monotonic() - found_broken,
                ", ".join(str(pid) for pid in killed))
    return killed


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
    # First, for a pool that broke while idle: the out-of-memory killer on a
    # worker between two `ingest()` calls. `submit()` would wait on the lock
    # its manager thread holds until every worker has ended, and nothing
    # would be raised to reap on (#796).
    _end_broken_pool_stragglers(executor)
    try:
        # `map` is inside the `try` with the rest, though its own raise
        # leaves nothing to reap: `submit()` raises `BrokenProcessPool`
        # only once it holds the shutdown lock, which the manager thread
        # gives up only after it has joined every worker. A death during
        # the submits was seen after them in every run measured, on 3.12
        # and 3.14t, with tasks of 5 ms, of 50 ms and of next to nothing,
        # up to a million items: the manager thread counts a pool broken
        # only when neither a submit's wakeup nor a result is waiting. Seen
        # between two submits, it would block the next one behind a worker
        # that outlived SIGTERM, which nothing on this thread could end.
        iterator = mapper(func, items, chunksize=strategy.chunksize)
        yield from _tracked(iterator, items, strategy)
    finally:
        # On every way out, before the owner hears of it: the owner shuts
        # the pool down or retires it, and `shutdown()` takes the lock the
        # manager thread holds while it joins the workers. A future's
        # `BrokenProcessPool` is one way out of a broken pool; a reader
        # that stops (`GeneratorExit`), Ctrl-C and a task's own exception
        # are others, and `ingest()` shuts its own retry pools down after
        # any of them. On a pool that is not broken this costs a `getattr`.
        _end_broken_pool_stragglers(executor)


#: How long the recycling pool's exit waits, once every worker has been
#: SIGKILLed, for the stdlib's own exit to finish on its helper thread
#: before it lets the caller go and leaves that thread behind (#860). No
#: environment variable: tests patch it.
#:
#: A killed worker ends at once, so this is only ever used up when the
#: helper is stuck in `Pool._help_stuff_finish`, acquiring the inqueue's
#: read lock, which a killed worker can hold for good: the kernel does not
#: release a POSIX semaphore. Together with the grace it bounds the exit at
#: `_BROKEN_POOL_GRACE_S + _POOL_EXIT_AFTER_KILL_S`, below conftest's
#: `_STALL_S` and pytest's faulthandler window, so a bounded exit is never
#: reported as a stall or dumped. `test_packaging_contract.py` pins that.
_POOL_EXIT_AFTER_KILL_S = 5.0


def _still_running(processes) -> list:
    """The processes of `processes` whose sentinel is not ready.

    The sentinels, never `join()` or `is_alive()`: those reap the child
    with `waitpid`, racing the helper thread's own `join()` of it (#861's
    rule). A process object already closed has nothing to end.
    """
    waiting = {}
    for process in processes:
        try:
            waiting[process.sentinel] = process
        except ValueError:
            continue
    if not waiting:
        return []
    ready = set(multiprocessing.connection.wait(list(waiting), timeout=0))
    return [process for sentinel, process in waiting.items()
            if sentinel not in ready]


def _still_running_or_all(processes) -> list:
    """`_still_running(processes)`, or every one of them when the sentinels
    cannot be asked.

    For a kill: a handle that cannot be waited on must never skip the kill
    of a worker that may still be running (#862).
    """
    processes = list(processes)
    try:
        return _still_running(processes)
    except (OSError, ValueError):
        return processes


def _end_recycling_pool(pool, finished: bool) -> list[int]:
    """End a recycling `multiprocessing.Pool`, holding the caller no longer
    than `_BROKEN_POOL_GRACE_S + _POOL_EXIT_AFTER_KILL_S`.

    The stdlib's exit can wait for good. `terminate()`, which is what a
    `with` block's exit is, first takes the inqueue's read lock and never
    gives it back, so a worker not yet waiting for work (one still booting,
    or a recycled replacement) can never read its sentinel; then it SIGTERMs
    each worker and joins every one with no timeout. A worker that also
    outlives SIGTERM (a handler a script installs at module level runs again
    in every spawned worker; coverage's `sigterm = True` can deadlock) hung
    `export()` for good after every file was written (#860).

    So the stdlib's whole exit runs on a daemon helper thread: on success,
    `close()` here and then `join()` and `terminate()` there, so every
    worker leaves by its sentinel and its own exit work, `atexit` included,
    runs; otherwise `terminate()` there. The caller waits for it at most
    `_BROKEN_POOL_GRACE_S`, SIGKILLs every worker still running, logs one
    WARNING naming them, and waits at most `_POOL_EXIT_AFTER_KILL_S` more.
    A helper still stuck then is left behind, with a second WARNING. An
    interrupt during the grace, such as Ctrl-C, kills at once and goes on.
    Takes no lock.

    Args:
        pool: The `multiprocessing.Pool` `_run_on_recycling_pool` built.
        finished (bool): Whether every result was read. False on any other
            way out: a reader that stops, a task's exception, Ctrl-C.

    Returns:
        list[int]: The pids it sent SIGKILL, or `[]`.
    """
    # Four rules here are traps someone would tidy away:
    #
    # 1. Never `terminate()` or `join()` on the caller's thread, and never
    #    let the pool be collected there before the helper has run its
    #    `terminate()`. Both can block where SIGKILL does not reach: a
    #    worker killed while it held the inqueue's read lock leaves it held
    #    for good, and `_help_stuff_finish` acquires it. A pool's finalizer
    #    is `_terminate_pool` too, so a pool only closed and joined would
    #    run `_help_stuff_finish` on whatever thread dropped it. The
    #    helper's trailing `terminate()` consumes the finalizer on the
    #    helper: `util.Finalize` deletes its registry entry before it calls
    #    back, so it runs once.
    # 2. Never kill while the pool is RUN: the worker handler replaces the
    #    dead, and the replacements starve behind the lock a dead worker
    #    held. On success `close()` is called here, before the helper
    #    starts (it only sets the state and posts a wakeup; it does not
    #    block), so a Ctrl-C at any later instant finds the pool closed. On
    #    the other ways out the state changes only when the helper's
    #    `terminate()` runs, which blocks and so cannot run here; a Ctrl-C
    #    in the instant before it can kill under RUN, and the after-kill
    #    bound covers what that can leave stuck.
    # 3. Wait on an `Event`, never `Thread.join()` and `is_alive()`. On
    #    3.12 a `KeyboardInterrupt` inside `join()` runs bpo-45274's
    #    handler, which marks a thread that is still running as stopped:
    #    `is_alive()` then says False, nothing is killed, and the
    #    interpreter's exit joins the worker for good (measured).
    # 4. The kill in a `finally`, never an `except`, and no `return` in it:
    #    Ctrl-C is raised inside the wait, and must kill and still reach
    #    the caller (#861, round 2). The log line comes after the kill
    #    loop, and nothing here takes a lock.
    #
    # A residual, named so nobody mistakes it for covered: a Ctrl-C that
    # lands before the `try` below (from the `finally` in
    # `_run_on_recycling_pool` through `close()` and building the helper)
    # escapes with no helper started and nothing killed. The pool is then
    # left to its finalizer, which is `_terminate_pool`, the unbounded
    # SIGTERM-and-join this function exists to avoid. The window is
    # microseconds against a 10 s grace, and moving those lines inside the
    # `try` would only move it, not close it (review of #884, finding 2).
    if finished:
        pool.close()
    started = time.monotonic()
    exited = threading.Event()
    failure = []

    def stdlib_exit():
        try:
            if finished:
                pool.join()
            pool.terminate()
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            # Kept to log, so a helper that raises does not read as a hang.
            failure.append(exc)
        finally:
            exited.set()

    helper = threading.Thread(target=stdlib_exit, name="isocenter-pool-exit",
                              daemon=True)
    killed = []
    try:
        # The start inside the `try`: `start()` itself waits for the thread
        # to run, and a Ctrl-C landing there, or between it and the wait,
        # must kill as one landing in the wait does.
        helper.start()
        exited.wait(_BROKEN_POOL_GRACE_S)
    finally:
        if not exited.is_set():
            # The live list, read only now: nothing grows it once the pool
            # is closed or terminating.
            for process in _still_running_or_all(list(pool._pool)):
                try:
                    process.kill()
                except (OSError, ValueError):
                    continue
                killed.append(process.pid)
            if killed:
                # The time measured, not the grace: an interrupt ends the
                # wait early. The cause by path: a finished pool is closed
                # and joined, and no signal is sent, so a worker still
                # running failed to leave by its sentinel; only the other
                # ways out go through `terminate()`'s SIGTERM (review of
                # #884, finding 1).
                cause = (
                    "A worker cannot leave by its sentinel when its exit "
                    "does not finish: a non-daemon thread still running, "
                    "or an exit handler that does not return."
                    if finished else
                    "A worker outlives SIGTERM when something in it "
                    "handles that signal; a handler a script installs at "
                    "module level runs again in every spawned worker.")
                get_logger().warning(
                    "%d worker process(es) of the recycling pool were still "
                    "running %.1f s after the pool was told to stop, and were "
                    "sent SIGKILL (pid %s). %s",
                    len(killed), time.monotonic() - started,
                    ", ".join(str(pid) for pid in killed), cause)
    # After the `finally`, so an interrupt skips it and goes on.
    if not exited.wait(_POOL_EXIT_AFTER_KILL_S):
        get_logger().warning(
            "The recycling pool's exit had not finished %.1f s after the "
            "pool was told to stop, and was left to a daemon thread "
            "(isocenter-pool-exit). A killed worker can leave a lock of the "
            "pool held, which its exit then waits on for good.",
            time.monotonic() - started)
    elif failure:
        get_logger().warning("The recycling pool's exit raised: %r",
                             failure[0])
    return killed


#: How often the recycling pool's stream checks its workers while it waits
#: for a result, and at most how often while results flow (#887). It is
#: the latency of seeing a dead worker; it costs nothing while results
#: flow, and a check is one zero-timeout `select()` over the sentinels of
#: the workers still running, at most about twice the pool's width. No
#: environment variable: tests patch it.
_POOL_WATCH_S = 1.0


def _recycling_worker(inqueue, outqueue, initializer, initargs, maxtasks,
                      wrap_exception, exit_grace):
    """`multiprocessing.pool.worker`, then a bound on this process's exit.

    The target of every worker `_RecyclingPool` starts. Module scope: it
    pickles into the spawned child by reference.

    Args:
        inqueue, outqueue, initializer, initargs, maxtasks, wrap_exception:
            `multiprocessing.pool.worker`'s own arguments, passed on.
        exit_grace (float): Seconds this process may take to end once the
            loop has returned (#888), resolved in the parent.
    """
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    multiprocessing.pool.worker(inqueue, outqueue, initializer, initargs,
                                maxtasks, wrap_exception)
    # A worker that ran its quota leaves the loop and exits, and the pool
    # replaces it only once it has ended; one whose exit does not finish (a
    # non-daemon thread, an `atexit` that never returns) was never replaced,
    # and with every worker so, the pool stalled with tasks queued (#888).
    #
    # Ending it here does not break #860's rule 2 (never kill while the
    # pool is RUN, because the replacements starve behind a lock the dead
    # held): the loop returned after its last `put` released the outqueue's
    # write lock and without calling `get` again, so this process holds
    # none of the pool's locks. A worker #860 kills, or one #887 sees die,
    # can die inside either lock, which is why those paths keep their rules.
    # Armed however the loop ended, since the worker cannot tell why: hence
    # `_WORKER_EXIT_GRACE_S` sits above #860's own exit bound.
    _arm_exit_bound(exit_grace)


class _RecyclingPool(multiprocessing.pool.Pool):  # pylint: disable=abstract-method
    """`multiprocessing.Pool` that records every worker it starts, and starts
    each as `_recycling_worker`, so its exit is bounded (#887, #888).

    Args:
        exit_grace (float): Keyword-only: each worker's exit bound.
        *args, **kwargs: `multiprocessing.pool.Pool`'s.
    """
    # The hook is `Pool.Process`: `__init__` starts the first workers
    # through `self.Process`, and hands `self.Process` to the worker-handler
    # thread, which starts every replacement. A bound method holds the pool,
    # so that thread keeps it alive until it exits; nothing here relies on
    # the pool being collected, because `_end_recycling_pool`'s helper runs
    # `terminate()` itself (#860's rule 1).

    def __init__(self, *args, exit_grace, **kwargs):
        # Before `super().__init__()`, which starts the first workers
        # through `self.Process`: set after it, these do not exist yet when
        # those workers start.
        # `deque` because the worker-handler thread appends and the stream's
        # thread pops; both are atomic.
        self._started = collections.deque()
        self._exit_grace = exit_grace
        super().__init__(*args, **kwargs)

    # An instance method where the base class has a staticmethod, so it sees
    # the pool: hence `arguments-differ`, which counts `self`.
    def Process(self, ctx, *args, **kwds):  # pylint: disable=invalid-name,arguments-differ
        """Start one worker, as `_recycling_worker`, and record it.

        A target other than `multiprocessing.pool.worker` passes through
        unbounded; `test_parallel_contract.py` pins that the stdlib's target
        is that one.

        Args:
            ctx: The pool's multiprocessing context.
            *args, **kwds: `ctx.Process`'s.

        Returns:
            multiprocessing.process.BaseProcess: The worker, not yet started.
        """
        if kwds.get("target") is multiprocessing.pool.worker:
            kwds["target"] = _recycling_worker
            kwds["args"] = tuple(kwds.get("args", ())) + (self._exit_grace,)
        process = ctx.Process(*args, **kwds)
        self._started.append(process)
        return process


class _RecyclingWatch:
    """Reads how each worker of a `_RecyclingPool` ended (#887, #888).

    `check()` raises `BrokenProcessPool` on a worker that ended abnormally,
    and both it and `sweep()` log the workers their exit bound ended.
    """

    def __init__(self, pool):
        self._pool = pool
        # The processes still running, or just ended and not yet read. Only
        # those: each process held keeps its sentinel's file descriptor
        # open, so holding every worker of a long export would hold
        # thousands. A process is dropped once its exit code is read.
        self._live = []
        self._next = time.monotonic() + _POOL_WATCH_S

    def due(self) -> bool:
        """Whether `_POOL_WATCH_S` has passed since the last check."""
        return time.monotonic() >= self._next

    def _read(self):
        """Read every worker that has ended since the last read.

        Returns:
            tuple: `(broken, late)`: the first abnormal `(pid, exitcode)` or
            None, and the pids the exit bound ended.
        """
        started = self._pool._started  # pylint: disable=protected-access
        while True:
            try:
                self._live.append(started.popleft())
            except IndexError:
                break
        keep = []
        waiting = {}
        for process in self._live:
            try:
                waiting[process.sentinel] = process
            except ValueError:
                # Recorded before `start()`: no sentinel yet. Kept, and read
                # at the next check; dropping it would leave it unwatched.
                keep.append(process)
        ready = (set(multiprocessing.connection.wait(list(waiting), timeout=0))
                 if waiting else set())
        broken, late = None, []
        for sentinel, process in waiting.items():
            if sentinel not in ready:
                keep.append(process)
                continue
            # `exitcode` and not `join()` or `is_alive()` (#861's rule): it is
            # read only once the sentinel is ready, so the process has ended,
            # and it is `Popen.poll(WNOHANG)`, which never blocks. When the
            # worker handler's `_join_exited_workers` reaped it first,
            # `poll` catches ECHILD and returns None; the `Popen` is the one
            # the handler holds, so its `returncode` is cached for both, and
            # the race costs one re-read at the next check, never a wrong
            # answer. `test_parallel_contract.py` pins the None.
            code = process.exitcode
            if code is None:
                keep.append(process)
            elif code == -signal.SIGALRM:
                late.append(process.pid)
            elif code != 0 and broken is None:
                # 0 is a worker that left at its quota or by its sentinel.
                broken = (process.pid, code)
        self._live = keep
        return broken, late

    def _report(self, late):
        if not late:
            return
        # The bound, not a measurement: the parent does not see when the
        # worker's loop ended, so nothing here can time it. Do not "fix" it
        # into a measured time. A log line and no audit row, as #860's
        # ruling Q3 has for a kill: the worker had sent every result. Its
        # words must not include "recycling pool" or "SIGKILL", by which
        # tests count #860's lines.
        get_logger().warning(
            "%d worker process(es) had finished their tasks but were still "
            "running %.1f s later, and were ended by their own exit bound "
            "(SIGALRM) (pid %s). A worker cannot end while a non-daemon "
            "thread is still running or an exit handler does not return; a "
            "pool that recycles its workers replaces it only once it has "
            "ended.",
            len(late), self._pool._exit_grace,  # pylint: disable=protected-access
            ", ".join(str(pid) for pid in late))

    def check(self):
        """Read the workers; raise if one ended abnormally.

        Raises:
            BrokenProcessPool: A worker ended with a nonzero exit code or a
                signal other than its exit bound's.
        """
        self._next = time.monotonic() + _POOL_WATCH_S
        broken, late = self._read()
        self._report(late)
        if broken is not None:
            # Any death, not only one mid-task (owner ruling Q1 on #887): an
            # idle worker always holds the inqueue's read lock while it
            # waits for work (measured, #860), and one killed while sending
            # can hold the outqueue's write lock, so no death can be judged
            # harmless. Its words must not include "recycling pool" or
            # "SIGKILL", by which tests count #860's lines.
            raise BrokenProcessPool(
                f"A worker process of a pool that recycles its workers ended "
                f"abruptly (pid {broken[0]}, exit code {broken[1]}) while the "
                f"pool was running; the task it held, if any, cannot finish, "
                f"and a worker that ends while it holds one of the pool's "
                f"locks stops every other worker, so the pool was stopped.")

    def sweep(self):
        """After the pool's exit, log the workers their bound ended that no
        check saw. Raises nothing: the stream is over."""
        # Required: a worker that reaches its quota just before the stream
        # ends has its bound fire inside #860's exit, where no check runs.
        # A -SIGKILL here is the parent's own kill and a -SIGTERM is
        # `terminate()`'s, so the abnormal half is not read.
        _, late = self._read()
        self._report(late)


def _watched(iterator, watch):
    """Yields from a pool's result iterator, checking its workers while it
    waits (#887).

    Args:
        iterator: `Pool.imap`'s or `imap_unordered`'s iterator.
        watch (_RecyclingWatch): The pool's watch.

    Yields:
        Each result. A task's own exception propagates as before.

    Raises:
        BrokenProcessPool: From `watch.check()`, once every result already
            delivered has been yielded.
    """
    # `next(timeout=)` wakes on a timer with no second thread. Without it
    # the read waits for good on the task a dead worker held: the worker
    # handler replaces the dead worker and records nothing about its task.
    while True:
        try:
            value = iterator.next(timeout=_POOL_WATCH_S)
        except multiprocessing.TimeoutError:
            yield from _checked(iterator, watch)
            continue
        except StopIteration:
            return
        yield value
        if watch.due():
            yield from _checked(iterator, watch)


def _checked(iterator, watch):
    """`watch.check()`; on a failure, yield the results already delivered
    first, then raise."""
    try:
        watch.check()
    except BrokenProcessPool:
        # Results the result handler has already taken off the pipe are not
        # lost. On the ordered path (`imap`), results held behind the lost
        # task's index are not delivered, and are lost.
        while True:
            try:
                value = iterator.next(timeout=0)
            except (multiprocessing.TimeoutError, StopIteration):
                break
            yield value
        raise


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
    #
    # `_RecyclingPool`, not `ctx.Pool`, so every worker it starts is watched
    # and bounds its own exit (#887, #888).
    ctx = multiprocessing.get_context("spawn")
    pool = _RecyclingPool(processes=strategy.max_workers,
                          maxtasksperchild=strategy.maxtasksperchild,
                          initializer=strategy.worker_initializer,
                          context=ctx, exit_grace=_WORKER_EXIT_GRACE_S)
    watch = _RecyclingWatch(pool)
    # No `with`: its exit is `terminate()`, on every way out, which can
    # wait for good (#860). `_end_recycling_pool` bounds it, and lets the
    # workers of a run that finished leave by their sentinel.
    finished = False
    try:
        # Unordered unless asked: results are yielded as workers finish,
        # so one slow item does not hold back everything queued behind
        # it. `import_files` asks, because the order it links files in
        # decides which of two files sharing an SOP Instance UID is kept,
        # and arrival order would make that a matter of scheduling.
        # `export()` does not: its results are small and order-free, and
        # holding them behind a slow instance buys nothing.
        mapper = pool.imap if ordered else pool.imap_unordered
        iterator = mapper(func, items, chunksize=strategy.chunksize)
        yield from _tracked(_watched(iterator, watch), items, strategy)
        # Only once every result has been read: any other way out (a
        # reader that stops, a task's exception, a dead worker, Ctrl-C)
        # leaves tasks queued or running, which `close()` and `join()`
        # would wait for.
        finished = True
    finally:
        _end_recycling_pool(pool, finished)
        # After the exit, for the bounds that fired inside it. An interrupt
        # propagating out of `_end_recycling_pool` skips it, which costs
        # only a log line; no `try` for it.
        watch.sweep()


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
        try:
            iterator = executor.map(func, items, chunksize=strategy.chunksize)
            yield from _tracked(iterator, items, strategy)
        finally:
            # Inside the `with`, and on every way out of it: its exit is
            # `shutdown(wait=True)`, which would wait for good on a worker
            # that outlived a broken pool's SIGTERM (#796). A future's
            # `BrokenProcessPool` is one way out of a broken pool, which
            # `_trailing_exception` sees only after the `with` exit; a
            # reader that stops (`GeneratorExit`: `redact()` streams),
            # Ctrl-C and a task's own exception are others. On a pool that
            # is not broken, or a thread pool, this costs a `getattr` or two.
            _end_broken_pool_stragglers(executor)


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
            receive an exception as data. Under `maxtasksperchild`, a
            worker that ends abnormally while the pool runs (killed, or
            its initializer raised) fails the pool with
            `BrokenProcessPool`, yielded last after the results already
            delivered, within about `_POOL_WATCH_S` (1 s) (#887); a
            worker whose exit does not finish after its last task is ended
            by SIGALRM `_WORKER_EXIT_GRACE_S` (15 s) later and named in a
            WARNING log line (#888). The recycling pool's exit is bounded: a worker still
            running `_BROKEN_POOL_GRACE_S` (10 s) after the pool is told
            to stop is sent SIGKILL and named in one WARNING log line, and
            the call waits at most `_POOL_EXIT_AFTER_KILL_S` (5 s) more
            for the pool to finish (#860).
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
